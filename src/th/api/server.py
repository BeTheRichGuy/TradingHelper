"""웹 서버 — 터미널(`th watch`)로 보던 것을 브라우저·폰에서 본다.

설계 원칙 (`01` 문서 원칙 4): **계산은 전부 서버에서 한다.**
브라우저는 서버가 준 문장과 숫자를 그리기만 한다. 그래야 웹과 앱이 같은 답을 낸다.
사유 목록도 `th.ui.dashboard.no_trade_reasons()` 를 그대로 쓰므로 터미널과 갈라지지 않는다.

⚠️ **여기에 방향을 제시하는 엔드포인트는 없다.** Phase 4 판정(`docs/08`·`docs/11`)에
따라 신호 카드가 빠졌고, 그 자리는 `no_trade`(금지 사유 목록)가 대신한다.
`reasons` 가 비어 있는 것은 매수 신호가 아니며, 스키마의 `is_permission_to_trade`
가 항상 False 인 이유가 그것이다.

v2 (docs/15): 워치리스트(주식 perp 유니버스) · 포커스 종목 전환 · 시장(펀딩·본주) ·
규율 지표 · 페이퍼 브래킷 패널 + 청산 요청.

v2.1 (docs/15 6~9절): 주문 패널 · 실매매 포지션 · 유니버스 확장. 페이퍼 엔진
(`th.execution.runner.PaperRunner`)이 **이 프로세스 안에서** 돈다. 그래도 이 파일에는
주문 코드가 없다 — 주문은 `th.execution` 에만 있고, 서버는 러너에 **요청을 넘길 뿐**이다.
`mode:"live"` 주문은 무조건 403 이고 실거래 클라이언트는 어디서도 만들지 않는다.

v2.2 (docs/15 10~12절): 시그널 엔진(`th.execution.signals`)이 러너 옆에서 5분마다 영상 200/20
규칙을 보고 **같은 주문 경로**로 페이퍼 브래킷을 연다. `/backtests` 페이지(웹 백테스트 실행·결과)와
`/history` 페이지(페이퍼 누적 기록)가 헤더 탭으로 붙는다. 계산은 여전히 전부 서버다.

fastapi 는 선택 의존성이라 이 모듈은 **필요할 때만** 임포트한다 (`th serve`).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from ..config import get_settings
from ..logging_setup import get_logger
from ..universe import apply_exchange_info, is_stock_perp, underlying_ticker
from .messages import (
    build_brackets,
    build_coverage,
    build_market,
    build_positions,
    build_snapshot,
    build_watchlist,
)

log = get_logger(__name__)
WEB_DIR = Path(__file__).resolve().parent / "web"

# 갱신 주기 — **비용이 다른 것을 분리한다.**
#
#   가격/호가 : DB 만 읽으므로 1초가 공짜다 → 화면이 매초 살아 있다
#   계정      : 바이낸스 API. 1초마다 두드리면 레이트리밋에 걸린다 → 3초
#   브래킷    : DB 만 읽는다 → 3초 (계정과 같은 박자)
#   워치리스트: 공개 ticker/24hr 한 번에 전 종목 → 5초
#   시장 통계 : 무거운 집계 + premiumIndex → 20초
#   당일 손익 : income 조회는 가중치가 크다 → 30초
#
# 평가손익·카운트다운·경과시간은 **브라우저가 매초 스스로 계산한다.**
# 그래서 1초 갱신을 하면서도 API 호출은 늘지 않는다.
TICK_S = 1
ACCOUNT_EVERY = 3        # 3초
BRACKETS_EVERY = 3       # 3초
WATCHLIST_EVERY = 5      # 5초
MARKET_EVERY = 20        # 20초
INCOME_EVERY = 30        # 30초
SESSION_EVERY = 5        # 5초
POSITIONS_EVERY = 3      # 3초 — 계정 조회와 같은 턴에서 만든다 (추가 호출 없음)
PAPER_INTERVAL_S = 3     # 내장 페이퍼 러너의 시세 확인 간격

# 러너가 없을 때의 주문 거절 (HTTP 503)
NO_RUNNER_KO = "페이퍼 엔진이 떠 있지 않습니다 — TH_EXECUTION_MODE=paper 로 th serve 를 다시 띄우세요"

# 수집하지 않는 종목의 가격은 워치리스트 티커로 대신한다. 이보다 낡으면 fresh=False.
WATCHLIST_FRESH_S = 15

CANDLE_TFS = (1, 5, 15, 60)
CANDLE_MAX_LIMIT = 1200
_INTERVAL = {1: "1m", 5: "5m", 15: "15m", 60: "1h"}


def _json(obj: Any) -> Any:
    """pydantic 모델·datetime 을 JSON 으로 만든다."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _json(v) for k, v in obj.items()}
    return obj


async def _session_ctx(symbol: str):
    from ..sessions import get_context

    return get_context(symbol)


def bucket_key(ts: datetime, tf: int) -> int:
    """N분봉 묶음 번호 — `indicators.resample` 과 같은 경계(시각 자체)를 쓴다."""
    return int(ts.timestamp()) // (max(tf, 1) * 60)


def vwap_by_bucket(bars1: list, pts: list, tf: int) -> dict[int, float]:
    """N분봉마다 그 묶음 **마지막** 1분봉의 VWAP 을 고른다.

    묶음 첫 1분봉의 값을 쓰면 5분봉 차트의 VWAP 이 실제보다 4분 뒤처진다 —
    봉의 종가는 마지막 1분에서 오는데 VWAP 만 첫 1분 것을 그리는 셈이다.
    """
    out: dict[int, float] = {}
    for b, p in zip(bars1, pts, strict=False):
        if p is not None:
            out[bucket_key(b.ts, tf)] = p.vwap     # 뒤에 오는 봉이 덮어쓴다
    return out


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_exposure_warned = False


def _warn_if_exposed(scope: dict | None) -> bool:
    """루프백이 아닌 경로로 접속이 들어오면 **한 번만** 경고한다.

    `th serve` 의 기본값이 0.0.0.0 이다 — 폰에서 보라고 그렇게 뒀지만, 이 화면에는
    계정 잔고·포지션이 그대로 뜬다. 인증이 없으므로 같은 네트워크의 누구나 볼 수 있다.
    create_app() 은 바인드 주소를 모르므로 ASGI scope 로 확인한다. 우리 쪽 주소
    (server)나 상대 주소(client)가 루프백이 아니면 바깥에 열려 있는 것이다.
    """
    global _exposure_warned
    if _exposure_warned or not scope:
        return False
    outside = [
        str(addr[0])
        for addr in (scope.get("server"), scope.get("client"))
        if addr and str(addr[0]) not in _LOOPBACK_HOSTS
    ]
    if not outside:
        return False
    _exposure_warned = True
    log.warning(
        "웹 서버가 루프백 밖(%s)에서 접속을 받고 있습니다 — 인증 없이 계정 정보가 "
        "노출됩니다. 같은 네트워크에서 볼 일이 없으면 `th serve --host 127.0.0.1` 로 띄우세요",
        ", ".join(outside),
    )
    return True


# ─────────────────────────────────────────────────────────
# 페이퍼 브래킷 패널 (WS `brackets` 와 GET /api/brackets 가 같은 본문을 쓴다)
# ─────────────────────────────────────────────────────────

async def brackets_payload(mode: str, at: datetime | None = None) -> dict:
    """docs/15 1-4. DB 를 못 읽으면 `available:false` — 빈 목록으로 그리지 않는다."""
    from ..account.risk import kst_today
    from ..execution import store

    when = at or datetime.now(UTC)
    try:
        opens = await store.load_open_brackets("paper")
        hist = await store.history(limit=50, mode="paper")
        today_r = await store.daily_realized_r("paper", kst_today(when))
        requested = set(await store.pending_close_requests("paper"))
    except Exception as e:              # noqa: BLE001
        log.debug("브래킷 조회 실패", exc_info=True)
        return {
            "type": "brackets", "ts": when.isoformat(), "mode": mode,
            "available": False,
            "message_ko": f"브래킷 기록을 못 읽었습니다 ({type(e).__name__})",
            "open": [], "today_realized_r": None, "today_count": 0,
            "expectancy_r": None,
        }
    msg = build_brackets(opens, hist, today_r, mode, requested, at=when)
    return _json(msg)


# ─────────────────────────────────────────────────────────
class Hub:
    """접속자들에게 같은 스냅샷을 뿌린다.

    브라우저를 두 개 열어도 조회는 한 번만 한다 — 바이낸스 레이트리밋을
    접속자 수만큼 곱하지 않기 위해서다.
    """

    def __init__(self, symbol: str, collected: list[str] | None = None,
                 mode: str = "none") -> None:
        self.symbol = symbol.upper()
        self.collected = [c.upper() for c in (collected or [symbol])]
        self.mode = mode
        self.clients: set[WebSocket] = set()
        # 새 접속자에게 처음 보낼 마지막 메시지들 (type → payload)
        self.latest: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._api = None
        # 워치리스트 티커의 마지막 가격 — 수집하지 않는 포커스 종목의 tick 대체
        self._watch: dict[str, tuple[float, datetime]] = {}
        self._last_discipline = None
        self._focus_dirty = False
        # v2.1 — 내장 페이퍼 러너 (paper 모드에서 앱 시작 때 붙는다). None 이면 주문은 503.
        self.runner = None
        # 실매매 포지션의 마지막 항목 — 계정 조회가 실패한 턴에 그대로 다시 보낸다
        self._last_positions: list = []
        # v2.2 — 시그널 엔진. 러너가 있을 때만 붙는다.
        self.signals = None

    # ── 공개 API 클라이언트 (라우트와 루프가 함께 쓴다) ────
    def public_api(self):
        if self._api is None:
            from ..exchange.binance_rest import BinanceFutures

            self._api = BinanceFutures()
        return self._api

    @property
    def is_collected(self) -> bool:
        return self.symbol in self.collected

    @property
    def runner_alive(self) -> bool:
        return self.runner is not None and bool(getattr(self.runner, "alive", False))

    @property
    def signals_alive(self) -> bool:
        return self.signals is not None and bool(getattr(self.signals, "alive", False))

    # ── v2.1 시작·종료 ──────────────────────────────────
    async def load_universe(self) -> None:
        """docs/15 9절 — 바이낸스 exchange_info 를 한 번 받아 유니버스를 정한다.
        실패하면 정적 후보 목록 그대로. 이 호출이 서버 시작을 막으면 안 된다."""
        try:
            info = await self.public_api().exchange_info()
        except Exception as e:              # noqa: BLE001
            log.warning("exchange_info 조회 실패 (%s) — 정적 유니버스를 씁니다", type(e).__name__)
            return
        st = apply_exchange_info(info)
        log.info("유니버스 %d개 (후보 %d · 발견 %d · 제외 %d)",
                 len(st.active) + len(st.discovered), len(st.active),
                 len(st.discovered), len(st.dropped))

    async def start_runner(self) -> None:
        """paper 모드면 내장 페이퍼 러너를 띄운다. 러너 코드는 전부 `th.execution.runner` 에 있다."""
        if self.mode != "paper":
            return
        from ..execution.runner import LeaseHeld, build_paper_runner

        try:
            runner = await build_paper_runner(
                self.collected, focus=self.symbol, interval=PAPER_INTERVAL_S,
                holder="serve", execution_mode=self.mode,
            )
            await runner.start()
        except LeaseHeld as e:
            log.warning("%s — 웹 주문 패널은 그 러너가 끝난 뒤 th serve 를 다시 띄워야 열립니다", e)
            return
        except Exception:                   # noqa: BLE001
            log.exception("페이퍼 러너 시작 실패 — 주문 패널은 503 을 돌려줍니다")
            return
        self.runner = runner
        await self.start_signals()

    async def start_signals(self) -> None:
        """v2.2 — 시그널 엔진. 러너 없이는 뜨지 않는다 (주문 경로가 러너에만 있다)."""
        if self.runner is None:
            return
        from ..execution.signals import build_signal_engine

        try:
            engine = build_signal_engine(self.runner, self.public_api(), get_settings())
            await engine.start()
        except Exception:                   # noqa: BLE001
            log.exception("시그널 엔진 시작 실패 — 손주문은 그대로 됩니다")
            return
        self.signals = engine

    async def stop_runner(self) -> None:
        if self.signals is not None:
            with contextlib.suppress(Exception):
                await self.signals.stop()
            self.signals = None
        if self.runner is not None:
            with contextlib.suppress(Exception):
                await self.runner.stop()
            self.runner = None

    # ── 포커스 ──────────────────────────────────────────
    async def set_focus(self, symbol: str) -> bool:
        """포커스 종목 변경. 바뀌었으면 True 이고 `focus` 메시지를 즉시 뿌린다."""
        symbol = symbol.upper()
        if not is_stock_perp(symbol):
            raise ValueError(f"유니버스 밖 종목: {symbol}")
        changed = symbol != self.symbol
        self.symbol = symbol
        if self.runner is not None:
            self.runner.focus = symbol      # 러너가 포커스 종목 시세도 먹인다
        if changed:
            # 옛 종목의 스냅샷·시장 값을 새 접속자에게 주면 안 된다
            for k in ("snapshot", "market", "tick"):
                self.latest.pop(k, None)
            self._focus_dirty = True
        await self._send({"type": "focus", "symbol": symbol,
                          "collected": self.is_collected}, keep=False)
        return changed

    # ── 접속 ────────────────────────────────────────────
    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        _warn_if_exposed(getattr(ws, "scope", None))
        self.clients.add(ws)
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "focus", "symbol": self.symbol,
                                "collected": self.is_collected})
            for k in ("watchlist", "market", "snapshot", "brackets", "positions"):
                if k in self.latest:
                    await ws.send_json(self.latest[k])
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def leave(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def _send(self, payload: dict, *, keep: bool) -> None:
        """`keep` 이면 새 접속자에게 처음 보낼 메시지로 기억해 둔다.
        가격 틱은 기억하지 않는다 — 접속하자마자 다음 초에 새로 온다."""
        if keep:
            self.latest[str(payload.get("type"))] = payload
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:               # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.leave(ws)

    # ── 매초: 가격 ──────────────────────────────────────
    def _watch_tick(self, at: datetime) -> dict:
        """수집하지 않는 포커스 종목 — 워치리스트 티커(5초)의 마지막 가격으로 대신한다."""
        hit = self._watch.get(self.symbol)
        if hit is None:
            return {"type": "tick", "symbol": self.symbol, "fresh": False,
                    "ts": at.isoformat(), "source": "ticker"}
        price, seen = hit
        return {
            "type": "tick", "symbol": self.symbol,
            "fresh": (at - seen).total_seconds() < WATCHLIST_FRESH_S,
            "ts": seen.isoformat(), "price": price, "bid": None, "ask": None,
            "spread_bps": None, "source": "ticker",
        }

    async def _tick(self) -> dict | None:
        """매초 — DB 에서 호가만 읽는다. 바이낸스 API 를 부르지 않는다."""
        from ..execution.quotes import latest_quote

        now = datetime.now(UTC)
        if not self.is_collected:
            return self._watch_tick(now)
        try:
            q = await latest_quote(self.symbol, max_age_s=120)
        except Exception:                   # noqa: BLE001
            log.debug("호가 조회 실패", exc_info=True)
            return None
        if q is None:
            return {"type": "tick", "symbol": self.symbol, "fresh": False,
                    "ts": now.isoformat()}
        spread = (q.ask - q.bid) / q.mid * 10000 if q.mid else 0.0
        return {
            "type": "tick", "symbol": self.symbol, "fresh": True,
            "ts": q.at.isoformat(), "bid": q.bid, "ask": q.ask,
            "price": q.mid, "spread_bps": spread,
        }

    # ── 3초: 실매매 포지션 ──────────────────────────────
    def _positions(self, view) -> dict:
        """계정 조회가 실패한 턴은 `ok:false` + 마지막 항목 — 빈 목록으로 그리지 않는다."""
        s = get_settings()
        ok = bool(view.risk.account_ok)
        msg = build_positions(
            view.positions, view.risk.open_orders, view.risk.balance.wallet, ok,
            last_items=self._last_positions, underlying_map=s.underlying_map,
            at=view.risk.at,
        )
        if ok:
            self._last_positions = list(msg.items)
        return _json(msg)

    # ── 5초: 워치리스트 ─────────────────────────────────
    async def _watchlist(self) -> dict | None:
        try:
            tickers = await self.public_api().ticker_24hr()
        except Exception:                   # noqa: BLE001
            log.debug("ticker/24hr 실패", exc_info=True)
            return None
        now = datetime.now(UTC)
        for t in tickers:
            if is_stock_perp(t["symbol"]) and t["last_price"] > 0:
                self._watch[t["symbol"]] = (t["last_price"], now)
        d = self._last_discipline
        basket = d.total_exposure_x if (d is not None and d.ok) else None
        return _json(build_watchlist(tickers, self.symbol, self.collected, basket, now))

    # ── 20초: 시장 (펀딩 · 마크가 · 본주 · 베이시스) ──────
    async def _market(self) -> dict | None:
        from .. import db

        s = get_settings()
        symbol = self.symbol
        try:
            premium = await self.public_api().premium_index(symbol)
        except Exception:                   # noqa: BLE001
            log.debug("premiumIndex 실패", exc_info=True)
            premium = None
        ticker = s.underlying_map.get(symbol) or underlying_ticker(symbol)
        und_price = None
        try:
            rows = await db.fetch(
                """SELECT close FROM underlying_prices
                   WHERE ticker = $1 AND tf = '1m' ORDER BY ts DESC LIMIT 1""",
                ticker,
            )
            if rows:
                und_price = float(rows[0]["close"])
        except Exception:                   # noqa: BLE001
            log.debug("본주 가격 조회 실패", exc_info=True)
        if premium is None and und_price is None:
            return None
        return _json(build_market(symbol, premium, und_price, ticker))

    # ── 루프 ────────────────────────────────────────────
    async def _loop(self) -> None:
        from ..account.alerts import build_alerts as build_account_alerts
        from ..account.client import AccountClient
        from ..account.service import (
            build_discipline,
            carry_forward,
            get_risk_view,
            underlying_close_at,
        )

        client = AccountClient(api=self.public_api())
        prev = None
        ctx = None
        tick = 0
        try:
            while self.clients:
                if self._focus_dirty:
                    # 종목이 바뀌었다 — 직전 값을 끌어오면 다른 종목의 포지션이 남는다
                    self._focus_dirty = False
                    prev = None
                    ctx = None
                    tick = 0                # 모든 주기가 즉시 한 번씩 돈다

                # ① 매초 — 가격 (DB 만 읽거나 워치리스트 캐시)
                t = await self._tick()
                if t is not None:
                    await self._send(t, keep=False)

                # ② 주기별 갱신. 각 항목은 **자기 주기로만** 판정한다 — 계정 주기(3초)
                # 안에 넣으면 5초·20초가 3의 배수인 15초·60초에만 맞아떨어진다.
                due_session = tick % SESSION_EVERY == 0 or ctx is None
                due_income = tick % INCOME_EVERY == 0
                due_market = tick % MARKET_EVERY == 0
                if tick % ACCOUNT_EVERY == 0 or due_session or due_income or due_market:
                    try:
                        if due_session:
                            ctx = await _session_ctx(self.symbol)

                        view = await get_risk_view(
                            self.symbol, ctx, client=client,
                            with_income=due_income,
                            with_market=due_market,
                        )
                        # 못 가져온 값은 직전 것을 유지한다. 특히 계정 조회가
                        # 실패했을 때 '포지션 없음'으로 그리지 않는다.
                        view = carry_forward(view, prev)
                        view.alerts = build_account_alerts(view.risk, ctx, view.market)
                        # 규율 지표는 보정 **뒤에** 계산한다 (당일 손익은 30초에 한 번만 온다).
                        # 계정 조회가 실패한 턴은 carry_forward 가 붙여 둔 마지막 값을 그대로 둔다.
                        if view.risk.account_ok or view.discipline is None:
                            view.discipline = build_discipline(
                                view, ctx, at=view.risk.at,
                                closes_at=underlying_close_at(view.risk.at),
                            )
                        self._last_discipline = view.discipline

                        payload = _json(build_snapshot(view))
                        payload["ok"] = True
                        if payload.get("risk") is not None:
                            payload["risk"]["stale"] = bool(view.risk.errors)
                        await self._send(payload, keep=True)
                        # 실매매 포지션 (docs/15 8절) — 같은 계정 조회에서 만든다. 추가 호출 없음.
                        if tick % POSITIONS_EVERY == 0 or tick == 0:
                            await self._send(self._positions(view), keep=True)
                        prev = view
                    except Exception as e:  # noqa: BLE001
                        log.exception("스냅샷 실패")
                        # 화면을 비우지 않는다 — 마지막 값을 남기고 '오래됨'만 표시
                        await self._send({
                            "type": "error", "ok": False,
                            "ts": datetime.now(UTC).isoformat(),
                            "symbol": self.symbol,
                            "message_ko": f"데이터를 가져오지 못했습니다: {type(e).__name__}",
                        }, keep=False)

                if tick % WATCHLIST_EVERY == 0:
                    w = await self._watchlist()
                    if w is not None:
                        await self._send(w, keep=True)
                if due_market:
                    m = await self._market()
                    if m is not None:
                        await self._send(m, keep=True)
                if tick % BRACKETS_EVERY == 0:
                    await self._send(await brackets_payload(self.mode), keep=True)

                tick += 1
                await asyncio.sleep(TICK_S)
        finally:
            await client.close()            # 공개 API 는 앱이 갖고 있으므로 닫지 않는다
            log.info("브로드캐스트 루프 종료 (접속자 0)")


# ─────────────────────────────────────────────────────────
# 차트용 봉
# ─────────────────────────────────────────────────────────

def _kline_bars(klines: list) -> list:
    """거래소 `Kline` → 지표 계산용 `Bar`."""
    from ..strategy.indicators import Bar

    return [
        Bar(ts=k.ts, open=k.open, high=k.high, low=k.low, close=k.close,
            volume=k.volume, taker_buy=k.taker_buy_base)
        for k in klines
    ]


async def public_bars(api, symbol: str, tf: int, limit: int) -> tuple[list, Any]:
    """수집하지 않는 종목 — 바이낸스 공개 klines 로 (닫힌 봉들, 살아있는 봉).

    tf 간격을 그대로 요청한다 (1분봉으로 받아 묶으면 60분봉 240개에 1분봉 14,400개가
    필요해 한 번에 못 받는다). EMA99 워밍업으로 200개를 더 받는다.
    """
    ks = await api.klines(symbol, _INTERVAL[tf], limit=min(limit + 200, 1500),
                          include_open=True)
    closed = _kline_bars([k for k in ks if k.closed])
    live = None
    if ks and not ks[-1].closed:
        live = _kline_bars([ks[-1]])[0]
    return closed, live


def candle_rows(bars: list, live, vmap: dict[int, float], tf: int, limit: int) -> list[dict]:
    """닫힌 봉 + 지표, 마지막에 살아있는 봉(`live:true`). 지표는 닫힌 봉으로만 계산한다."""
    from ..strategy.indicators import ema

    closes = [b.close for b in bars]
    e9, e21, e99 = ema(closes, 9), ema(closes, 21), ema(closes, 99)
    start = max(0, len(bars) - limit)
    out = []
    for j in range(start, len(bars)):
        b = bars[j]
        out.append({
            "t": b.ts.isoformat(), "o": b.open, "h": b.high,
            "l": b.low, "c": b.close, "v": b.volume,
            "ema9": e9[j], "ema21": e21[j], "ema99": e99[j],
            "vwap": vmap.get(bucket_key(b.ts, tf)),
        })
    if live is not None:
        out.append({
            "t": live.ts.isoformat(), "o": live.open, "h": live.high,
            "l": live.low, "c": live.close, "v": live.volume,
            "ema9": None, "ema21": None, "ema99": None, "vwap": None,
            "live": True,
        })
    return out


# ─────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    s = get_settings()
    symbol = s.symbols[0]
    hub = Hub(symbol, s.symbols, s.execution_mode)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # 유니버스는 바이낸스가 정한다 (docs/15 9절). 페이퍼 러너는 접속자와 무관하게 돈다 (7절).
        await hub.load_universe()
        await hub.start_runner()
        try:
            yield
        finally:
            await hub.stop_runner()

    app = FastAPI(title="Trading Helper", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/backtests")
    async def backtests_page():
        return FileResponse(WEB_DIR / "backtests.html")

    @app.get("/history")
    async def history_page():
        return FileResponse(WEB_DIR / "history.html")

    @app.get("/api/health")
    async def health():
        # ui: 화면이 기대하는 서버 세대. 옛 서버 프로세스에 새 화면이 붙으면 화면이 알려준다.
        return {"ok": True, "symbol": hub.symbol, "mode": s.execution_mode,
                "collected": hub.collected, "ui": 3, "runner": hub.runner_alive,
                "signals": hub.signals_alive, "risk_pct": float(s.risk_pct),
                "signal_symbols": s.signal_symbols}

    @app.get("/api/candles")
    async def candles(symbol: str | None = None, tf: int = 5, limit: int = 240):
        """차트용 봉 + 세션 VWAP. 계산은 전부 여기서 한다.

        수집 종목이면 DB 1분봉을 묶고, 아니면 바이낸스 공개 klines 로 폴백한다.
        어느 쪽이든 EMA·VWAP 은 **여기서** 계산하고 브라우저는 그리기만 한다.
        """
        from ..sessions import get_context
        from ..sessions.market import get_calendar
        from ..strategy.data import load_bars, now_floor_minute
        from ..strategy.features import session_anchor
        from ..strategy.indicators import resample, session_vwap

        sym = (symbol or hub.symbol).upper()
        if tf not in CANDLE_TFS:
            return JSONResponse({"error_ko": f"tf 는 {CANDLE_TFS} 중 하나여야 합니다"},
                                status_code=400)
        if not is_stock_perp(sym) and sym not in hub.collected:
            return JSONResponse({"error_ko": f"유니버스 밖 종목: {sym}"}, status_code=400)
        limit = max(20, min(int(limit), CANDLE_MAX_LIMIT))
        at = now_floor_minute()
        source = "db"

        if sym in hub.collected:
            # 봉 며칠치가 필요한가 — EMA99 를 채우려면 넉넉히 본다
            days = max(2, int((limit + 200) * tf / 1440) + 2)
            bars1 = await load_bars(sym, at, days)
            if not bars1:
                return {"bars": [], "tf": tf, "symbol": sym, "source": source,
                        "note_ko": "봉 데이터가 없습니다. th backfill 을 먼저 돌리세요."}
            # 지표는 닫힌 봉으로만 계산한다. 아직 안 닫힌 마지막 묶음은 따로 붙여서
            # 브라우저가 매초 호가로 갱신하는 "살아있는 봉"의 출발점으로 쓴다.
            bars = resample(bars1, tf, drop_incomplete=True) if tf > 1 else bars1
            live = None
            if tf > 1:
                full = resample(bars1, tf, drop_incomplete=False)
                if full and (not bars or full[-1].ts != bars[-1].ts):
                    live = full[-1]
            vwap_src = bars1
        else:
            source = "binance"
            try:
                bars, live = await public_bars(hub.public_api(), sym, tf, limit)
            except Exception as e:          # noqa: BLE001
                log.debug("공개 klines 실패", exc_info=True)
                return {"bars": [], "tf": tf, "symbol": sym, "source": source,
                        "note_ko": f"바이낸스에서 봉을 못 받았습니다 ({type(e).__name__})"}
            if not bars:
                return {"bars": [], "tf": tf, "symbol": sym, "source": source,
                        "note_ko": "바이낸스에 이 종목의 봉이 없습니다."}
            vwap_src = bars

        # 세션 앵커 VWAP — 계산은 서버에서. 브라우저는 그리기만 한다.
        vmap: dict[int, float] = {}
        try:
            ctx = get_context(sym)
            anchor = session_anchor(at, ctx, get_calendar())
            if anchor:
                vmap = vwap_by_bucket(vwap_src, session_vwap(vwap_src, anchor), tf)
        except Exception:                   # noqa: BLE001
            log.debug("VWAP 계산 실패 — 차트는 VWAP 없이 그린다", exc_info=True)

        return {"bars": candle_rows(bars, live, vmap, tf, limit),
                "tf": tf, "symbol": sym, "source": source}

    @app.post("/api/focus")
    async def focus(body: dict):
        """포커스 종목 변경. 유니버스 밖이면 400. 바뀌면 `focus` 를 즉시 뿌린다."""
        sym = str((body or {}).get("symbol") or "").upper()
        if not is_stock_perp(sym):
            return JSONResponse({"error_ko": f"유니버스 밖 종목: {sym or '(없음)'}"},
                                status_code=400)
        changed = await hub.set_focus(sym)
        return {"ok": True, "symbol": hub.symbol, "collected": hub.is_collected,
                "changed": changed}

    @app.get("/api/coverage")
    async def coverage():
        from ..ingest.coverage import backtest_span_days, collect_coverage, warnings_for

        cov = await collect_coverage(hub.symbol)
        return _json(build_coverage(cov, backtest_span_days(cov),
                                    warnings_for(cov), hub.symbol))

    @app.get("/api/events")
    async def events(days: int = 3):
        from ..sessions import get_events, upcoming

        now = datetime.now(UTC)
        evs = upcoming(now, get_events(), days=days)
        return {
            "events": [
                {"name": e.name, "at": e.ts_utc.isoformat(),
                 "kind": e.kind, "importance": e.importance,
                 "in_sec": int(e.seconds_from(now))}
                for e in evs
            ],
        }

    @app.get("/api/brackets")
    async def brackets():
        """docs/15 1-4 와 같은 본문 (초기 로드용)."""
        return await brackets_payload(s.execution_mode)

    @app.post("/api/brackets/{id}/close")
    async def close_bracket(id: str):
        """**페이퍼 전용** 시장가 청산 요청 (docs/15 3절).

        요청은 언제나 DB 에 남긴다 — CLI 러너가 관리하는 브래킷이면 그쪽이 처리한다.
        내장 러너가 살아 있으면 **바로 그 자리에서** 처리한다 (v2.1).
        실거래 모드에서는 이 경로가 아예 없다 — 403. 이 서버에는 주문 코드가 없다.
        """
        from ..execution import store

        if s.execution_mode != "paper":
            return JSONResponse(
                {"error_ko": "페이퍼 모드에서만 청산 요청을 받습니다 (TH_EXECUTION_MODE=paper)"},
                status_code=403,
            )
        try:
            opens = await store.load_open_brackets("paper")
            hit = [st for st in opens
                   if st.bracket.bracket_id == id or st.bracket.bracket_id.endswith(id)]
            if len(hit) != 1:
                return JSONResponse({"error_ko": "열린 페이퍼 브래킷이 아닙니다"}, status_code=404)
            full_id = hit[0].bracket.bracket_id
            if not await store.request_close(full_id, "paper"):
                return JSONResponse({"error_ko": "열린 페이퍼 브래킷이 아닙니다"}, status_code=404)
        except Exception as e:              # noqa: BLE001
            log.debug("청산 요청 저장 실패", exc_info=True)
            return JSONResponse({"error_ko": f"기록을 못 남겼습니다 ({type(e).__name__})"},
                                status_code=503)
        if hub.runner_alive:
            try:
                done = await hub.runner.serve_close_requests()
            except Exception:               # noqa: BLE001
                log.exception("내장 러너 청산 실패 — 요청은 남아 있어 다음 반복에 다시 시도")
                done = []
            if full_id in done:
                log.info("웹 청산 요청 %s — 내장 러너가 시장가로 정리했습니다", full_id[-8:])
                await hub._send(await brackets_payload(s.execution_mode), keep=True)
                return {"ok": True, "id": full_id[-8:], "status_ko": "청산됨"}
        log.info("웹 청산 요청 %s — 러너가 처리합니다", full_id[-8:])
        return {"ok": True, "id": full_id[-8:], "status_ko": "청산 요청됨 · 러너 대기"}

    # ── v2.1 주문 패널 (docs/15 7절) ─────────────────────
    # 주문 코드는 여기 없다. 요청을 `th.execution.runner` 에 넘기고 결과를 그대로 돌려준다.
    def _order_request(body: dict):
        from ..execution.runner import OpenRequest

        return OpenRequest.from_dict(body)

    def _refuse_mode(req):
        """`live` 는 무조건 403. 러너가 없으면 503. 통과면 None."""
        from ..execution.live import LIVE_ORDER_REFUSED_KO

        if req.mode == "live":
            return JSONResponse({"ok": False, "reasons": [LIVE_ORDER_REFUSED_KO]}, status_code=403)
        if req.mode != "paper":
            return JSONResponse(
                {"ok": False, "reasons": [f"페이퍼 주문만 받습니다 (mode={req.mode or '(없음)'})"]},
                status_code=403,
            )
        if not hub.runner_alive:
            return JSONResponse({"ok": False, "reasons": [NO_RUNNER_KO]}, status_code=503)
        return None

    @app.post("/api/orders/preview")
    async def order_preview(body: dict):
        """수량·노셔널·1R·손절폭·비용/R + 가드 사유. 주문은 내지 않는다."""
        req = _order_request(body)
        bad = _refuse_mode(req)
        if bad is not None:
            return bad
        p = await hub.runner.preview(req)
        return p.as_dict()

    @app.post("/api/orders")
    async def order_open(body: dict):
        """페이퍼 브래킷을 연다 — CLI `th paper run` 과 **같은 경로**. 성공하면 `brackets` 를 즉시 뿌린다."""
        req = _order_request(body)
        bad = _refuse_mode(req)
        if bad is not None:
            return bad
        out = await hub.runner.open_from_request(req)
        if out.ok:
            log.info("웹 페이퍼 주문 %s %s %s", out.bracket_id[-8:], req.symbol, req.direction)
            await hub._send(await brackets_payload(s.execution_mode), keep=True)
        return out.as_dict()

    @app.get("/api/orders/gate")
    async def orders_gate():
        """docs/07 게이트 체크리스트. `live_allowed` 는 항상 False."""
        from ..execution.live import gate_status

        return gate_status(s.execution_mode)

    @app.get("/api/paper")
    async def paper():
        """페이퍼 트레이딩 상태 + 손으로 적은 매매 기록."""
        from ..execution import store

        try:
            opens = await store.load_open_brackets("paper")
            hist = await store.history(limit=20, mode="paper")
            journal = await store.journal_entries(20)
        except Exception as e:              # noqa: BLE001
            return {"available": False, "message_ko": f"기록을 못 읽었습니다 ({type(e).__name__})"}
        done = [h for h in hist if h.get("realized_r") is not None]
        wins = [h for h in done if h["realized_r"] > 0]
        jdone = [j for j in journal if j.get("outcome_r") is not None]
        return {
            "available": True,
            "mode": s.execution_mode,
            "open": [
                {"id": st.bracket.bracket_id[-8:], "symbol": st.bracket.symbol,
                 "direction": st.bracket.direction, "qty": st.position_qty,
                 "entry": st.avg_entry, "stop": st.bracket.stop_price,
                 "has_stop": st.live_stop is not None}
                for st in opens
            ],
            "closed": len(done),
            "win_rate": (len(wins) / len(done)) if done else None,
            "expectancy_r": (sum(h["realized_r"] for h in done) / len(done)) if done else None,
            "journal_total": len(journal),
            "journal_closed": len(jdone),
            "journal_expectancy_r": (
                sum(j["outcome_r"] for j in jdone) / len(jdone) if jdone else None
            ),
            "enough_sample": len(jdone) >= 30,
        }

    # ── v2.2 시그널 엔진 (docs/15 10절) ───────────────────
    # 경로 이름이 `engine` 인 이유: 이건 "지금 사라"는 조언이 아니라 **페이퍼 자동매매 엔진의 상태**다.
    # docs/08 판정(방향 제시 엔드포인트 금지)은 그대로 살아 있다 — test_web 이 지킨다.
    @app.get("/api/engine")
    async def engine_status():
        """엔진 상태 + 종목별 국면 + 최근 판단. 엔진이 없으면 available:false."""
        if hub.signals is None:
            return {"available": False, "mode": s.execution_mode,
                    "message_ko": ("시그널 엔진이 떠 있지 않습니다 — TH_EXECUTION_MODE=paper 로 th serve 를 띄우세요"
                                   if s.execution_mode != "paper" else NO_RUNNER_KO),
                    "symbols": s.signal_symbols}
        out = hub.signals.status()
        out["available"] = True
        out["mode"] = s.execution_mode
        out["collected"] = hub.collected
        return out

    @app.post("/api/engine/toggle")
    async def engine_toggle(body: dict):
        """엔진 켜기/끄기 (관찰은 계속한다). 페이퍼 전용이라 켠다고 실거래가 되지 않는다."""
        if hub.signals is None:
            return JSONResponse({"ok": False, "error_ko": "시그널 엔진이 떠 있지 않습니다"}, status_code=503)
        want = (body or {}).get("enabled")
        hub.signals.enabled = bool(want) if want is not None else not hub.signals.enabled
        log.info("시그널 엔진 %s (웹)", "켜짐" if hub.signals.enabled else "꺼짐")
        return {"ok": True, "enabled": hub.signals.enabled}

    @app.get("/api/engine/log")
    async def engine_log(limit: int = 200, symbol: str | None = None):
        from ..execution import store

        try:
            rows = await store.signal_log(limit=max(1, min(int(limit), 1000)), symbol=symbol)
        except Exception as e:              # noqa: BLE001
            return {"available": False, "message_ko": f"기록을 못 읽었습니다 ({type(e).__name__})", "items": []}
        return {"available": True, "items": _json(rows)}

    # ── v2.2 웹 백테스트 (docs/15 11절) ───────────────────
    @app.get("/api/backtests")
    async def backtests_list(limit: int = 50):
        from ..execution import store

        try:
            rows = await store.backtest_list(limit=max(1, min(int(limit), 200)))
        except Exception as e:              # noqa: BLE001
            return {"available": False, "message_ko": f"기록을 못 읽었습니다 ({type(e).__name__}) — th migrate 를 돌렸나요?",
                    "items": []}
        return {"available": True, "items": _json(rows)}

    @app.get("/api/backtests/data")
    async def backtests_data():
        from ..backtest.ma5m_runs import inventory

        return _json(await inventory(list(dict.fromkeys(s.signal_symbols + hub.collected))))

    @app.post("/api/backtests")
    async def backtests_submit(body: dict):
        from ..backtest.ma5m_runs import submit

        b = body or {}
        try:
            run_id, err = await submit(symbol=str(b.get("symbol") or ""), source=str(b.get("source") or "csv"),
                                       since=str(b.get("since") or ""), params_raw=b.get("params") or {},
                                       api=hub.public_api())
        except Exception as e:              # noqa: BLE001
            log.debug("백테스트 접수 실패", exc_info=True)
            return JSONResponse({"ok": False, "error_ko": f"접수 실패 ({type(e).__name__}) — th migrate 를 돌렸나요?"},
                                status_code=503)
        if err:
            return JSONResponse({"ok": False, "error_ko": err}, status_code=400)
        return {"ok": True, "id": run_id}

    @app.get("/api/backtests/{run_id}")
    async def backtests_get(run_id: int):
        from ..execution import store

        try:
            row = await store.backtest_get(int(run_id))
        except Exception as e:              # noqa: BLE001
            return JSONResponse({"error_ko": f"기록을 못 읽었습니다 ({type(e).__name__})"}, status_code=503)
        if row is None:
            return JSONResponse({"error_ko": "그런 실행이 없습니다"}, status_code=404)
        return _json(row)

    @app.delete("/api/backtests/{run_id}")
    async def backtests_delete(run_id: int):
        from ..execution import store

        try:
            ok = await store.backtest_delete(int(run_id))
        except Exception as e:              # noqa: BLE001
            return JSONResponse({"error_ko": f"삭제 실패 ({type(e).__name__})"}, status_code=503)
        return {"ok": ok}

    # ── v2.2 페이퍼 누적 기록 (docs/15 12절) ──────────────
    @app.get("/api/history")
    async def history_api(limit: int = 1000, setup: str | None = None):
        """닫힌 페이퍼 브래킷 전부 + 누적 지표. `setup=auto` 면 시그널 엔진 것만, `manual` 이면 손주문만."""
        from ..execution import store
        from .history import build_history

        setup_id = None
        if setup == "auto":
            from ..backtest.ma5m import SETUP_ID
            setup_id = SETUP_ID
        try:
            rows = await store.history_with_fills(limit=max(1, min(int(limit), 5000)), mode="paper",
                                                  setup_id=setup_id)
            if setup == "manual":
                rows = [r for r in rows if not r.get("setup_id")]
            opens = await store.load_open_brackets("paper")
        except Exception as e:              # noqa: BLE001
            return {"available": False, "message_ko": f"기록을 못 읽었습니다 ({type(e).__name__})", "items": []}
        return _json(build_history(rows, opens, mode=s.execution_mode))

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await hub.join(sock)
        try:
            while True:
                await sock.receive_text()   # 클라이언트는 ping 만 보낸다
        except WebSocketDisconnect:
            pass
        except Exception:                   # noqa: BLE001
            pass
        finally:
            hub.leave(sock)

    # 테스트·운영 진단에서 허브에 닿을 수 있게 둔다 (Starlette 는 app.state 에 둔다)
    getattr(app, "state", app).hub = hub
    return app
