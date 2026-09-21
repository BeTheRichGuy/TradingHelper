"""미결제약정(OI)과 펀딩 히스토리 폴링.

OI 는 WebSocket 으로 오지 않으므로 REST 로 5분마다 긁는다.
펀딩은 markPrice 스트림으로도 들어오지만, 재시작 시 빈 구간을 메우려면
히스토리 API 가 따로 필요하다.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from .. import db
from ..config import get_settings
from ..exchange.binance_rest import BinanceFutures
from ..logging_setup import get_logger

log = get_logger(__name__)

OI_INTERVAL_SEC = 300      # 5분
FUNDING_INTERVAL_SEC = 3600  # 1시간


async def poll_open_interest(symbols: list[str], api: BinanceFutures) -> int:
    total = 0
    for sym in symbols:
        try:
            rows = await api.open_interest_hist(sym, period="5m", limit=100)
            total += await db.save_open_interest(rows)
            if rows:
                await db.set_ingest_state("open_interest", sym, rows[-1][1])
        except Exception:
            log.exception("%s OI 수집 실패", sym)
        await asyncio.sleep(0.2)
    return total


async def poll_funding(symbols: list[str], api: BinanceFutures) -> int:
    total = 0
    for sym in symbols:
        try:
            start = datetime.now(UTC) - timedelta(days=7)
            rows = await api.funding_rate_hist(
                sym, limit=200, start_ms=int(start.timestamp() * 1000)
            )
            total += await db.save_funding(rows)
            if rows:
                await db.set_ingest_state("funding", sym, rows[-1][1])
        except Exception:
            log.exception("%s 펀딩 수집 실패", sym)
        await asyncio.sleep(0.2)
    return total


async def run_poller(symbols: list[str] | None = None) -> None:
    s = get_settings()
    symbols = [x.upper() for x in (symbols or s.symbols)]
    log.info("OI/펀딩 폴러 시작: %s", ", ".join(symbols))

    async with BinanceFutures() as api:
        # 기동 시 1회
        await poll_open_interest(symbols, api)
        await poll_funding(symbols, api)

        last_funding = time.monotonic()
        while True:
            await asyncio.sleep(OI_INTERVAL_SEC)
            n = await poll_open_interest(symbols, api)
            log.debug("OI %d행 저장", n)

            now = time.monotonic()
            if now - last_funding >= FUNDING_INTERVAL_SEC:
                m = await poll_funding(symbols, api)
                log.debug("펀딩 %d행 저장", m)
                last_funding = now
