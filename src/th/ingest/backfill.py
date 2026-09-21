"""과거 1분봉 백필.

바이낸스 klines 는 한 번에 1500봉까지만 준다. 여기서 페이지네이션과
'마지막 저장 봉 이후만 가져오기'를 처리한다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from .. import db
from ..config import get_settings
from ..exchange.binance_rest import MAX_KLINE_LIMIT, BinanceFutures
from ..logging_setup import get_logger

log = get_logger(__name__)

MINUTE_MS = 60_000
# 연속 요청 사이 간격 — 레이트리밋 여유를 두고 보수적으로
REQUEST_GAP_SEC = 0.25


def closed_bar_end(now: datetime | None = None) -> datetime:
    """백필 상한(반열림) — **지금 만들어지고 있는 봉은 제외한다.**

    현재 분의 시작으로 내린 시각. `fetch_range` 는 이 시각 **앞까지만** 받으므로
    이 분에 열린(아직 안 닫힌) 봉은 들어오지 않는다. 진행 중인 봉이 확정봉인
    척 DB 에 들어가면 백테스트와 실전이 갈라진다.
    """
    now = now or datetime.now(UTC)
    return now.replace(second=0, microsecond=0)


async def fetch_range(
    symbol: str, start_ms: int, end_ms: int, interval: str = "1m",
    client: BinanceFutures | None = None,
) -> list[tuple]:
    """[start_ms, end_ms) 구간의 봉을 전부 가져와 DB row 튜플 리스트로 반환.

    바이낸스의 endTime 은 **포함** 경계다. 그대로 넘기면 end_ms 에 열린 봉 —
    보통 지금 진행 중인 봉 — 까지 딸려 온다. 1ms 를 빼서 반열림 구간을 지킨다
    (1분봉이라면 endTime 이 직전 분 안에 떨어지므로 직전 분 봉까지만 온다).
    """
    own = client is None
    api = client or BinanceFutures()
    rows: list[tuple] = []
    cursor = start_ms
    api_end = end_ms - 1
    try:
        while cursor < end_ms:
            batch = await api.klines(
                symbol, interval, start_ms=cursor, end_ms=api_end, limit=MAX_KLINE_LIMIT
            )
            if not batch:
                break
            rows.extend(k.as_row() for k in batch)
            last_ts_ms = int(batch[-1].ts.timestamp() * 1000)
            if last_ts_ms <= cursor:      # 진전 없음 → 무한루프 방지
                break
            cursor = last_ts_ms + MINUTE_MS
            if len(batch) < MAX_KLINE_LIMIT:
                break
            await asyncio.sleep(REQUEST_GAP_SEC)
    finally:
        if own:
            await api.close()
    return rows


async def backfill_symbol(
    symbol: str,
    lookback_minutes: int | None = None,
    client: BinanceFutures | None = None,
) -> int:
    """마지막 저장 봉 이후를 채운다.

    lookback_minutes 를 주면 DB 상태를 무시하고 그만큼 과거부터 다시 받는다.
    """
    now = closed_bar_end()
    end_ms = int(now.timestamp() * 1000)

    if lookback_minutes is not None:
        start = now - timedelta(minutes=lookback_minutes)
    else:
        last = await db.get_last_kline_ts(symbol, "1m")
        if last is None:
            # 처음이면 최근 3일치부터 (전체 히스토리는 backfill_full 로)
            start = now - timedelta(days=3)
            log.info("%s 저장된 봉이 없어 최근 3일부터 시작합니다", symbol)
        else:
            start = last + timedelta(minutes=1)

    start_ms = int(start.timestamp() * 1000)
    if start_ms >= end_ms:
        return 0

    rows = await fetch_range(symbol, start_ms, end_ms, client=client)
    if rows:
        await db.save_klines(rows)
        await db.set_ingest_state("kline_1m", symbol, rows[-1][2])
    return len(rows)


async def backfill_full(symbol: str, client: BinanceFutures | None = None) -> int:
    """상장 시점부터 전부. 최초 1회만 돌린다."""
    own = client is None
    api = client or BinanceFutures()
    try:
        filters = await api.symbol_filters(symbol)
        onboard = filters.get("onboardDate")
        if onboard:
            start = datetime.fromtimestamp(int(onboard) / 1000, tz=UTC)
            log.info("%s 상장일 %s 부터 백필합니다", symbol, start.date())
        else:
            start = datetime.now(UTC) - timedelta(days=365)
            log.warning("%s 상장일을 알 수 없어 1년치만 받습니다", symbol)

        now = closed_bar_end()
        total = 0
        cursor = start
        # 하루씩 끊어서 진행 상황을 볼 수 있게
        while cursor < now:
            chunk_end = min(cursor + timedelta(days=1), now)
            rows = await fetch_range(
                symbol, int(cursor.timestamp() * 1000),
                int(chunk_end.timestamp() * 1000), client=api,
            )
            if rows:
                await db.save_klines(rows)
                total += len(rows)
                log.info("%s %s  누적 %d봉", symbol, cursor.date(), total)
            cursor = chunk_end
        if total:
            await db.set_ingest_state("kline_1m", symbol, now)
        return total
    finally:
        if own:
            await api.close()


async def sync_symbol_meta(symbols: list[str] | None = None) -> None:
    """틱사이즈·수량스텝·상장일을 symbols 테이블에 저장."""
    s = get_settings()
    symbols = symbols or s.symbols
    umap = s.underlying_map
    async with BinanceFutures() as api:
        for sym in symbols:
            f = await api.symbol_filters(sym)
            if not f:
                log.warning("%s 을(를) 바이낸스에서 찾지 못했습니다 — 심볼명을 확인하세요", sym)
                continue
            await db.upsert_symbol(
                symbol=sym,
                kind="perp",
                underlying=umap.get(sym),
                tick_size=f.get("tickSize"),
                qty_step=f.get("stepSize"),
                meta=f,
            )
            log.info("%s 메타 저장 (tick=%s step=%s status=%s)",
                     sym, f.get("tickSize"), f.get("stepSize"), f.get("status"))
