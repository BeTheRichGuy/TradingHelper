"""봉 누락 검사.

Phase 0 완료 기준이 "24시간 무중단 수집 후 누락 0건"이므로,
그걸 실제로 확인할 수 있는 도구가 필요하다.

주의: 거래가 아예 없어 봉이 생성되지 않는 경우와, 우리가 놓친 경우는
다르다. 바이낸스는 거래가 없으면 이전 종가로 봉을 채워 보내주므로
정상 운영 중이라면 1분 간격이 끊기지 않는다. 즉 여기서 잡히는 구멍은
대부분 우리 쪽 문제다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .. import db
from ..config import get_settings
from ..logging_setup import get_logger
from .backfill import fetch_range

log = get_logger(__name__)


async def find_gaps(symbol: str, hours: int = 24, tf: str = "1m") -> list[tuple[datetime, datetime, int]]:
    """(구간시작, 구간끝, 누락개수) 리스트."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await db.fetch(
        """
        SELECT ts,
               LEAD(ts) OVER (ORDER BY ts) AS next_ts
        FROM klines
        WHERE symbol = $1 AND tf = $2 AND ts >= $3
        ORDER BY ts
        """,
        symbol, tf, since,
    )
    gaps: list[tuple[datetime, datetime, int]] = []
    for r in rows:
        nxt = r["next_ts"]
        if nxt is None:
            continue
        delta_min = int((nxt - r["ts"]).total_seconds() // 60)
        if delta_min > 1:
            gaps.append((r["ts"], nxt, delta_min - 1))
    return gaps


async def repair_gaps(symbol: str, hours: int = 24, tf: str = "1m") -> int:
    gaps = await find_gaps(symbol, hours, tf)
    if not gaps:
        return 0
    filled = 0
    for start, end, missing in gaps:
        rows = await fetch_range(
            symbol,
            int(start.timestamp() * 1000) + 60_000,
            int(end.timestamp() * 1000),
        )
        if rows:
            await db.save_klines(rows)
            filled += len(rows)
        log.info("%s %s ~ %s  누락 %d개 중 %d개 복구", symbol, start, end, missing, len(rows))
    await db.mark_gaps_filled(symbol, tf)
    return filled


async def report(hours: int = 24) -> dict[str, dict]:
    s = get_settings()
    out: dict[str, dict] = {}
    for sym in s.symbols:
        gaps = await find_gaps(sym, hours)
        first = await db.fetchval(
            "SELECT min(ts) FROM klines WHERE symbol=$1 AND tf='1m'", sym
        )
        last = await db.get_last_kline_ts(sym)
        count = await db.fetchval(
            "SELECT count(*) FROM klines WHERE symbol=$1 AND tf='1m'", sym
        )
        out[sym] = {
            "bars_total": count or 0,
            "first_bar": first,
            "last_bar": last,
            "gaps_in_window": len(gaps),
            "missing_bars": sum(g[2] for g in gaps),
            "gaps": gaps[:10],
        }
    return out
