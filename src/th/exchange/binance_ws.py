"""바이낸스 선물 WebSocket 수집기.

설계 메모
---------
* 1분봉은 `k.x == true`(봉 마감)일 때만 확정 저장한다. 진행 중인 봉을 쓰면
  백테스트와 실전 결과가 달라진다.
* aggTrade 스트림은 Phase 0 에서 구독하지 않는다. CVD 는 1분봉의
  taker_buy_base 로 충분히 계산되고, aggTrade 는 초당 수백 건이라
  로컬 PC 에서 디스크만 잡아먹는다. 초 단위 델타가 필요해지면 그때 켠다.
* bookTicker 는 초당 여러 번 오므로 1초에 1건만 샘플링해 저장한다.
* 재연결 시 끊긴 구간은 자동으로 REST 백필한다. 이게 없으면 조용히
  구멍 뚫린 데이터로 지표를 계산하게 된다.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import websockets
from websockets.exceptions import ConnectionClosed

from .. import db
from ..config import get_settings
from ..logging_setup import get_logger
from .base import Kline

log = get_logger(__name__)

FLUSH_INTERVAL_SEC = 2.0
BOOK_SAMPLE_SEC = 1.0
FUNDING_SAMPLE_SEC = 60.0
# 저장 실패 시 버퍼에 되돌려 두는 행 수 상한 (버퍼 종류별). DB 가 오래 죽어 있으면
# 메모리가 무한히 자라므로 오래된 것부터 버린다 — 봉은 어차피 재시작 백필로 복구된다.
RETRY_BUFFER_MAX = 20_000
# 재연결 시 내부 구멍을 살펴볼 범위 (시간)
GAP_REPAIR_HOURS = 24
# REST 꼬리 폴링 주기(초). 실측: 주식 perp 은 웹소켓 kline/markPrice 스트림이 오지 않는다
# (bookTicker 만 온다). 그래서 닫힌 봉은 REST 로 주기적으로 받아 온다 — 웹소켓이
# 나중에 정상화돼도 UPSERT 라 충돌하지 않는다.
TAIL_POLL_SEC = 20.0


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def build_stream_url(ws_base: str, symbols: list[str]) -> str:
    streams: list[str] = []
    for sym in symbols:
        s = sym.lower()
        streams += [f"{s}@kline_1m", f"{s}@markPrice@1s", f"{s}@bookTicker", f"{s}@forceOrder"]
    return f"{ws_base.rstrip('/')}/stream?streams={'/'.join(streams)}"


class StreamCollector:
    def __init__(self, symbols: list[str] | None = None) -> None:
        s = get_settings()
        self.settings = s
        self.symbols = [x.upper() for x in (symbols or s.symbols)]
        self.url = build_stream_url(s.ws_base, self.symbols)

        self._klines: list[tuple] = []
        self._books: list[tuple] = []
        self._funding: list[tuple] = []
        self._liqs: list[tuple] = []

        self._last_book_at: dict[str, float] = {}
        self._last_funding_at: dict[str, float] = {}
        self._last_closed_bar: dict[str, datetime] = {}

        self._stop = asyncio.Event()
        self._msg_count = 0
        self._bar_count = 0
        # 진단용 — 1분마다 "무엇을 받았고 무엇을 저장했나"를 로그로 남긴다.
        # 봉이 안 쌓일 때 스트림 문제인지 저장 문제인지 이 한 줄로 갈린다.
        self._kline_open_seen = 0      # 마감 전 kline 메시지
        self._kline_closed_seen = 0    # 마감(x=true) kline 메시지
        self._saved_klines = 0
        self._saved_books = 0
        self._last_heartbeat = 0.0
        # 갭 백필 태스크 — 참조를 잡아 두지 않으면 GC 에 조용히 사라질 수 있고,
        # 종료 시 기다릴 수도 없다
        self._tasks: set[asyncio.Task] = set()

    # ── 진입점 ────────────────────────────────────────────
    async def run(self) -> None:
        """stop() 이 불릴 때까지 수집한다.

        종료 순서가 중요하다. 남은 데이터를 저장하기 **전에** DB 풀이 닫히면
        마지막 배치가 통째로 날아가므로, 여기서 연결 루프만 먼저 끊고
        플러시를 끝낸 뒤에 반환한다.
        """
        log.info("수집 시작: %s", ", ".join(self.symbols))
        flusher = asyncio.create_task(self._flush_loop(), name="flusher")
        tail = asyncio.create_task(self._tail_poll_loop(), name="tail-poll")
        conn = asyncio.create_task(self._connect_loop(), name="ws")
        stopper = asyncio.create_task(self._stop.wait(), name="stop")
        try:
            await asyncio.wait({conn, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._stop.set()
            # 웹소켓 수신은 블로킹이라 취소해야 풀린다. 플러시는 취소하지 않는다.
            conn.cancel()
            stopper.cancel()
            tail.cancel()
            await asyncio.gather(conn, stopper, flusher, tail, return_exceptions=True)
            # 진행 중인 갭 백필이 있으면 끝날 때까지 기다린다 — 풀이 닫힌 뒤에
            # 저장하려 들면 그 구간은 그대로 구멍으로 남는다
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            await self._flush()          # 마지막 배치 — 아직 풀이 살아 있을 때
            log.info("수집 종료 (봉 %d개 저장)", self._bar_count)

    def stop(self) -> None:
        self._stop.set()

    # ── 연결 ──────────────────────────────────────────────
    async def _connect_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=20, max_queue=4096
                ) as ws:
                    log.info("WebSocket 연결됨 (스트림 %d개)", len(self.symbols) * 4)
                    backoff = 1.0
                    await self._backfill_all("연결 직후")
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._handle(raw)
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                if self._stop.is_set():
                    break
                log.warning("연결 끊김 (%s) — %.0f초 후 재연결", type(e).__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception:
                log.exception("수집기 예외 — %.0f초 후 재연결", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── 메시지 처리 ────────────────────────────────────────
    def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return

        stream: str = msg.get("stream", "")
        data = msg.get("data") or {}
        if not stream:
            return
        self._msg_count += 1

        if "@kline" in stream:
            self._on_kline(data)
        elif "@markPrice" in stream:
            self._on_mark_price(data)
        elif "@bookTicker" in stream:
            self._on_book(data)
        elif "@forceOrder" in stream:
            self._on_force_order(data)

    def _on_kline(self, d: dict) -> None:
        k = d.get("k") or {}
        if not k.get("x"):          # 봉 마감 전이면 버린다
            self._kline_open_seen += 1
            return
        self._kline_closed_seen += 1
        symbol = k["s"]
        ts = datetime.fromtimestamp(int(k["t"]) / 1000, tz=UTC)
        kl = Kline(
            symbol=symbol, tf="1m", ts=ts,
            open=float(k["o"]), high=float(k["h"]), low=float(k["l"]),
            close=float(k["c"]), volume=float(k["v"]),
            quote_volume=float(k["q"]), trades=int(k["n"]),
            taker_buy_base=float(k["V"]), closed=True,
        )
        self._klines.append(kl.as_row())
        self._bar_count += 1

        prev = self._last_closed_bar.get(symbol)
        if prev is not None and ts - prev > timedelta(minutes=1):
            missing = int((ts - prev).total_seconds() // 60) - 1
            log.warning("%s 봉 누락 %d개 (%s ~ %s) — 백필 예약", symbol, missing, prev, ts)
            self._spawn(self._fill_gap(symbol, prev, ts, missing), name=f"gap-{symbol}")
        self._last_closed_bar[symbol] = ts

    def _spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        """배경 태스크를 만들고 참조를 잡아 둔다. 끝나면 목록에서 빠지고, 예외는 로그로 남긴다."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("배경 태스크 %s 예외: %r", task.get_name(), exc)

    def _on_mark_price(self, d: dict) -> None:
        symbol = d.get("s")
        if not symbol:
            return
        now = time.monotonic()
        if now - self._last_funding_at.get(symbol, 0.0) < FUNDING_SAMPLE_SEC:
            return
        self._last_funding_at[symbol] = now
        self._funding.append((
            symbol,
            datetime.fromtimestamp(int(d["E"]) / 1000, tz=UTC),
            float(d["r"]),                                     # 현재 펀딩률
            float(d["p"]),                                     # 마크가격
            float(d.get("i") or 0) or None,                    # 인덱스가격
            datetime.fromtimestamp(int(d["T"]) / 1000, tz=UTC),  # 다음 정산
        ))

    def _on_book(self, d: dict) -> None:
        symbol = d.get("s")
        if not symbol:
            return
        self._book_seen = getattr(self, "_book_seen", 0) + 1
        now = time.monotonic()
        if now - self._last_book_at.get(symbol, 0.0) < BOOK_SAMPLE_SEC:
            return
        self._last_book_at[symbol] = now
        bid, ask = float(d["b"]), float(d["a"])
        if bid <= 0 or ask <= 0:
            return
        mid = (bid + ask) / 2
        self._books.append((
            symbol,
            datetime.now(UTC),
            bid, ask,
            (ask - bid) / mid * 10_000,   # bps
        ))

    def _on_force_order(self, d: dict) -> None:
        o = d.get("o") or {}
        if not o:
            return
        # ap(평균체결가)는 미체결 시 "0" 문자열로 온다. "0" 은 truthy 라
        # `o["ap"] or o["p"]` 로는 걸러지지 않으므로 숫자로 비교한다.
        avg = float(o.get("ap") or 0.0)
        price = avg if avg > 0 else float(o["p"])
        qty = float(o["q"])
        if price <= 0 or qty <= 0:
            return
        self._liqs.append((
            o["s"],
            datetime.fromtimestamp(int(o["T"]) / 1000, tz=UTC),
            o["S"], price, qty, price * qty,
        ))

    # ── 저장 ──────────────────────────────────────────────
    async def _flush_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=FLUSH_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            await self._flush()
            self._heartbeat()

    def _heartbeat(self) -> None:
        """60초마다 수신·저장 건수를 남긴다. 봉이 안 쌓이면 여기서 바로 보인다."""
        now = time.monotonic()
        if now - self._last_heartbeat < 60:
            return
        self._last_heartbeat = now
        note = ""
        if self._kline_open_seen > 0 and self._kline_closed_seen == 0:
            note = " ⚠ 마감 봉(x=true)이 한 번도 안 옴 — 스트림 문제"
        elif self._kline_closed_seen > self._saved_klines:
            note = " ⚠ 받은 봉보다 저장이 적음 — DB 저장 실패 확인"
        elif self._msg_count == 0:
            note = " ⚠ 메시지 0건 — 연결은 됐지만 아무것도 안 옴"
        if self._kline_open_seen == 0 and self._kline_closed_seen == 0 and self._msg_count > 0:
            note = " · 웹소켓 kline 없음 → REST 폴링으로 봉 수집 중"
        log.info(
            "수신 kline 진행중 %d · 마감 %d · 호가 %d | 저장 봉 %d(웹소켓) + %d(REST) · 호가 %d | 메시지 %d%s",
            self._kline_open_seen, self._kline_closed_seen, self._books_seen(),
            self._saved_klines, getattr(self, "_tail_polled", 0), self._saved_books,
            self._msg_count, note,
        )

    def _books_seen(self) -> int:
        return getattr(self, "_book_seen", 0)

    async def _flush(self) -> None:
        klines, self._klines = self._klines, []
        books, self._books = self._books, []
        funding, self._funding = self._funding, []
        liqs, self._liqs = self._liqs, []
        try:
            if klines:
                await db.save_klines(klines)
                for row in klines:
                    await db.set_ingest_state("kline_1m", row[0], row[2])
                self._saved_klines += len(klines)
                klines = []                  # 저장된 것은 되돌리지 않는다
            if books:
                await db.save_book_ticker(books)
                self._saved_books += len(books)
                books = []
            if funding:
                await db.save_funding(funding)
                funding = []
            if liqs:
                await db.save_liquidations(liqs)
                liqs = []
        except Exception as e:
            # 실패한 배치는 버퍼 앞에 되돌려 다음 플러시가 다시 시도한다.
            # 그 사이 새로 들어온 행은 이미 self._* 에 있으므로 순서를 지켜 앞에 붙인다.
            self._klines = self._requeue(klines, self._klines, "봉")
            self._books = self._requeue(books, self._books, "호가")
            self._funding = self._requeue(funding, self._funding, "펀딩")
            self._liqs = self._requeue(liqs, self._liqs, "청산")
            # 종료 중 풀이 닫히는 것은 정상 흐름이라 스택트레이스까지 찍지 않는다
            if "pool is closing" in str(e) or "pool is closed" in str(e):
                log.info("종료 중 — 마지막 배치 일부를 저장하지 못했습니다 (재시작 시 백필됨)")
            else:
                log.exception("저장 실패 — 다음 플러시에서 다시 시도합니다")

    @staticmethod
    def _requeue(failed: list[tuple], fresh: list[tuple], label: str) -> list[tuple]:
        """실패분을 새 행 앞에 되돌린다. 상한을 넘으면 **오래된 것부터** 버린다."""
        if not failed:
            return fresh
        merged = failed + fresh
        if len(merged) > RETRY_BUFFER_MAX:
            dropped = len(merged) - RETRY_BUFFER_MAX
            log.warning(
                "%s 재시도 버퍼가 %d행을 넘어 오래된 %d행을 버립니다 (재시작 시 백필됨)",
                label, RETRY_BUFFER_MAX, dropped,
            )
            merged = merged[dropped:]
        return merged

    # ── 백필 ──────────────────────────────────────────────
    async def _tail_poll_loop(self) -> None:
        """20초마다 REST 로 '마지막 저장 봉 이후'를 채운다.

        웹소켓 kline 이 정상이면 받을 게 없어 비용이 거의 없고(요청 1회),
        웹소켓이 봉을 안 주면(주식 perp 실측) 이 루프가 봉을 책임진다.
        """
        from ..ingest.backfill import backfill_symbol

        await asyncio.sleep(TAIL_POLL_SEC)          # 연결 직후 백필과 겹치지 않게
        while not self._stop.is_set():
            for sym in self.symbols:
                try:
                    n = await backfill_symbol(sym, lookback_minutes=None)
                    if n:
                        self._tail_polled = getattr(self, "_tail_polled", 0) + n
                except Exception:               # noqa: BLE001
                    log.debug("꼬리 폴링 실패 %s", sym, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TAIL_POLL_SEC)
            except asyncio.TimeoutError:
                pass

    async def _backfill_all(self, reason: str) -> None:
        """마지막 저장 봉 이후를 채우고, 그 앞쪽의 **내부 구멍**도 메운다.

        꼬리만 채우면 안 된다. 저장 실패로 중간이 비어 있거나, 지난 연결에서
        갭 백필이 끝나기 전에 끊겼다면 마지막 봉 앞에 구멍이 남아 있다.
        """
        from ..ingest.backfill import backfill_symbol
        from ..ingest.gapcheck import repair_gaps

        for sym in self.symbols:
            try:
                n = await backfill_symbol(sym, lookback_minutes=None)
                if n:
                    log.info("[%s] %s 백필 %d봉", reason, sym, n)
                repaired = await repair_gaps(sym, hours=GAP_REPAIR_HOURS)
                if repaired:
                    log.info("[%s] %s 내부 구멍 %d봉 복구", reason, sym, repaired)
                last = await db.get_last_kline_ts(sym)
                if last:
                    self._last_closed_bar[sym] = last
            except Exception:
                log.exception("%s 백필 실패", sym)

    async def _fill_gap(self, symbol: str, start: datetime, end: datetime, missing: int) -> None:
        from ..ingest.backfill import fetch_range

        try:
            await db.record_gap(symbol, "1m", start, end, missing)
            rows = await fetch_range(symbol, _ms(start) + 60_000, _ms(end))
            if rows:
                await db.save_klines(rows)
                # 이 구간만 메운 것이다 — 다른 미완료 갭까지 채웠다고 표시하면 안 된다
                await db.mark_gaps_filled(symbol, "1m", start, end)
                log.info("%s 누락 %d봉 백필 완료", symbol, len(rows))
        except Exception:
            log.exception("%s 갭 백필 실패", symbol)

    # ── 상태 ──────────────────────────────────────────────
    @property
    def stats(self) -> dict:
        return {"messages": self._msg_count, "bars": self._bar_count}
