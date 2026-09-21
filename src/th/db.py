"""asyncpg 커넥션 풀과 저장 헬퍼.

모든 쓰기는 UPSERT다. 재연결 후 백필이 겹쳐 들어와도 중복이 생기지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg

from .config import get_settings
from .logging_setup import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        s = get_settings()
        _pool = await asyncpg.create_pool(
            dsn=s.dsn, min_size=1, max_size=8, command_timeout=60
        )
        log.info("DB 연결됨: %s:%s/%s", s.db_host, s.db_port, s.db_name)
    return _pool


async def open_connection(command_timeout: float | None = None) -> asyncpg.Connection:
    """풀과 별개의 단일 연결.

    풀에는 60초 타임아웃이 걸려 있다. 집계 재계산처럼 수 분씩 걸리는 작업은
    거기에 걸려 죽으므로, command_timeout=None 인 별도 연결로 돌린다.
    다 쓰면 반드시 close() 할 것.
    """
    s = get_settings()
    return await asyncpg.connect(dsn=s.dsn, command_timeout=command_timeout)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire():
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


# ── 저장 ───────────────────────────────────────────────────

KLINE_UPSERT = """
INSERT INTO klines (symbol, tf, ts, open, high, low, close,
                    volume, quote_volume, trades, taker_buy_base, source)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
ON CONFLICT (symbol, tf, ts) DO UPDATE SET
    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, volume = EXCLUDED.volume,
    quote_volume = EXCLUDED.quote_volume, trades = EXCLUDED.trades,
    taker_buy_base = EXCLUDED.taker_buy_base
"""


async def save_klines(rows: Sequence[tuple]) -> int:
    """rows: (symbol, tf, ts, o, h, l, c, vol, qvol, trades, taker_buy, source)"""
    if not rows:
        return 0
    async with acquire() as conn:
        await conn.executemany(KLINE_UPSERT, rows)
    return len(rows)


async def save_open_interest(rows: Sequence[tuple]) -> int:
    if not rows:
        return 0
    async with acquire() as conn:
        await conn.executemany(
            """INSERT INTO open_interest (symbol, ts, oi, oi_value)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (symbol, ts) DO UPDATE
               SET oi = EXCLUDED.oi, oi_value = EXCLUDED.oi_value""",
            rows,
        )
    return len(rows)


async def save_funding(rows: Sequence[tuple]) -> int:
    if not rows:
        return 0
    async with acquire() as conn:
        await conn.executemany(
            """INSERT INTO funding (symbol, ts, rate, mark_price, index_price, next_funding_ts)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (symbol, ts) DO UPDATE
               SET rate = EXCLUDED.rate,
                   mark_price = EXCLUDED.mark_price,
                   index_price = EXCLUDED.index_price,
                   next_funding_ts = EXCLUDED.next_funding_ts""",
            rows,
        )
    return len(rows)


async def save_book_ticker(rows: Sequence[tuple]) -> int:
    if not rows:
        return 0
    async with acquire() as conn:
        await conn.executemany(
            """INSERT INTO book_ticker (symbol, ts, bid, ask, spread_bps)
               VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (symbol, ts) DO NOTHING""",
            rows,
        )
    return len(rows)


async def save_liquidations(rows: Sequence[tuple]) -> int:
    if not rows:
        return 0
    async with acquire() as conn:
        await conn.executemany(
            """INSERT INTO liquidations (symbol, ts, side, price, qty, notional)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            rows,
        )
    return len(rows)


async def save_underlying(rows: Sequence[tuple]) -> int:
    """rows: (ticker, tf, ts, o, h, l, c, vol, source)"""
    if not rows:
        return 0
    async with acquire() as conn:
        await conn.executemany(
            """INSERT INTO underlying_prices (ticker, tf, ts, open, high, low, close, volume, source)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (ticker, tf, ts) DO UPDATE
               SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                   close=EXCLUDED.close, volume=EXCLUDED.volume""",
            rows,
        )
    return len(rows)


async def upsert_symbol(
    symbol: str,
    kind: str,
    underlying: str | None,
    tick_size: float | None,
    qty_step: float | None,
    meta: dict[str, Any] | None = None,
) -> None:
    import json

    async with acquire() as conn:
        await conn.execute(
            """INSERT INTO symbols (symbol, kind, underlying, tick_size, qty_step, meta, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6, now())
               ON CONFLICT (symbol) DO UPDATE
               SET kind=EXCLUDED.kind, underlying=EXCLUDED.underlying,
                   tick_size=EXCLUDED.tick_size, qty_step=EXCLUDED.qty_step,
                   meta=EXCLUDED.meta, updated_at=now()""",
            symbol, kind, underlying, tick_size, qty_step, json.dumps(meta or {}),
        )


# ── 상태 조회 ──────────────────────────────────────────────

async def get_last_kline_ts(symbol: str, tf: str = "1m") -> datetime | None:
    async with acquire() as conn:
        return await conn.fetchval(
            "SELECT max(ts) FROM klines WHERE symbol=$1 AND tf=$2", symbol, tf
        )


async def set_ingest_state(stream: str, symbol: str, last_ts: datetime | None) -> None:
    async with acquire() as conn:
        await conn.execute(
            """INSERT INTO ingest_state (stream, symbol, last_ts, updated_at)
               VALUES ($1,$2,$3, now())
               ON CONFLICT (stream, symbol) DO UPDATE
               SET last_ts = EXCLUDED.last_ts, updated_at = now()""",
            stream, symbol, last_ts,
        )


async def record_gap(symbol: str, tf: str, start: datetime, end: datetime, missing: int) -> None:
    async with acquire() as conn:
        await conn.execute(
            """INSERT INTO ingest_gaps (symbol, tf, gap_start, gap_end, missing)
               VALUES ($1,$2,$3,$4,$5)""",
            symbol, tf, start, end, missing,
        )


async def mark_gaps_filled(
    symbol: str, tf: str, start: datetime | None = None, end: datetime | None = None,
) -> int:
    """갭을 채웠다고 표시한다.

    start/end 를 주면 **그 구간 하나만** 표시한다. 갭 백필은 구간마다 따로 돌기 때문에
    하나를 메웠다고 심볼의 미완료 갭 전부를 뒤집으면, 아직 못 메운 구간이 '해결됨'
    으로 둔갑한다. 둘 다 생략하면 (전수 복구 후) 전부 표시한다.
    """
    async with acquire() as conn:
        if start is not None and end is not None:
            res = await conn.execute(
                "UPDATE ingest_gaps SET filled=true "
                "WHERE symbol=$1 AND tf=$2 AND filled=false "
                "AND gap_start=$3 AND gap_end=$4",
                symbol, tf, start, end,
            )
        else:
            res = await conn.execute(
                "UPDATE ingest_gaps SET filled=true WHERE symbol=$1 AND tf=$2 AND filled=false",
                symbol, tf,
            )
    return int(res.split()[-1]) if res else 0


async def fetch(query: str, *args) -> list[asyncpg.Record]:
    async with acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchval(query: str, *args):
    async with acquire() as conn:
        return await conn.fetchval(query, *args)
