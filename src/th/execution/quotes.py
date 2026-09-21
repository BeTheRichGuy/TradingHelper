"""페이퍼 체결에 먹일 시세를 DB 에서 읽는다.

`th stream` 이 적재하는 두 가지를 합쳐 하나의 `Quote` 로 만든다.

  · `book_ticker`  최우선 호가 (bid/ask) — 지정가 체결 판정에 쓴다
  · `klines(1m)`   직전 1분봉 고저      — 봉 안에서 스쳐간 손절을 놓치지 않으려고

호가만 보면 **봉 안에서 잠깐 찍고 돌아온 가격**을 못 본다. 실전에서는 거기서
손절이 체결되므로, 반드시 봉 범위로도 판정해야 한다.

시세가 낡았으면 `None` 을 돌려준다. **낡은 값으로 체결을 만들어내면 안 된다** —
그건 페이퍼 성적을 실제보다 좋게 만드는 가장 흔한 방법이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .. import db
from ..logging_setup import get_logger
from .paper import Quote

log = get_logger(__name__)

MAX_AGE_S = 90        # 이보다 낡은 호가는 쓰지 않는다


async def latest_quote(symbol: str, *, max_age_s: int = MAX_AGE_S) -> Quote | None:
    now = datetime.now(UTC)

    rows = await db.fetch(
        "SELECT ts, bid, ask FROM book_ticker WHERE symbol = $1 "
        "ORDER BY ts DESC LIMIT 1", symbol
    )
    if not rows:
        return None
    r = rows[0]
    age = (now - r["ts"]).total_seconds()
    if age > max_age_s:
        log.debug("호가가 %.0f초 낡음 — 건너뜀", age)
        return None
    bid, ask = float(r["bid"]), float(r["ask"])
    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    hi = lo = bar_start = None
    bars = await db.fetch(
        "SELECT ts, high, low FROM klines WHERE symbol = $1 AND tf = '1m' "
        "AND ts >= $2 ORDER BY ts DESC LIMIT 2",
        symbol, now - timedelta(minutes=3),
    )
    if bars:
        hi = max(float(b["high"]) for b in bars)
        lo = min(float(b["low"]) for b in bars)
        # 가장 오래된 봉의 시각 — 이보다 나중에 낸 주문은 이 고저로 판정하지 않는다
        bar_start = min(b["ts"] for b in bars)

    return Quote(symbol=symbol, bid=bid, ask=ask, at=r["ts"], high=hi, low=lo,
                 bar_start=bar_start)
