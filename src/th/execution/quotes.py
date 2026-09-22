"""페이퍼 체결에 먹일 시세를 DB 에서 읽는다.

`th stream` 이 적재하는 두 가지를 합쳐 하나의 `Quote` 로 만든다.

  · `book_ticker`  최우선 호가 (bid/ask) — 지정가 체결 판정에 쓴다
  · `klines(1m)`   직전 1분봉 고저      — 봉 안에서 스쳐간 손절을 놓치지 않으려고

호가만 보면 **봉 안에서 잠깐 찍고 돌아온 가격**을 못 본다. 실전에서는 거기서
손절이 체결되므로, 반드시 봉 범위로도 판정해야 한다.

시세가 낡았으면 `None` 을 돌려준다. **낡은 값으로 체결을 만들어내면 안 된다** —
그건 페이퍼 성적을 실제보다 좋게 만드는 가장 흔한 방법이다.

v3.0 (docs/17 §5): **수집 종목(TH_SYMBOLS)이 아닌** 종목은 바이낸스 공개 `bookTicker` + 공개 1분봉으로
폴백한다. 수집 종목은 폴백하지 않는다 — 스트림이 끊겼을 때 "시세 끊김" 경고가 조용히 사라지면 안 된다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .. import db
from ..config import get_settings
from ..logging_setup import get_logger
from .paper import Quote

log = get_logger(__name__)

MAX_AGE_S = 90        # 이보다 낡은 호가는 쓰지 않는다
PUBLIC_BAR_CACHE_S = 30   # 공개 1분봉 고저는 이만큼 캐시한다 (호가는 매번 받는다)

_public_api: Any = None
_bar_cache: dict[str, tuple[datetime, float | None, float | None, datetime | None]] = {}


def set_public_api(api: Any) -> None:
    """폴백에 쓸 바이낸스 공개 클라이언트. 서버가 자기 것을 넣어 준다. 없으면 필요할 때 만든다."""
    global _public_api
    _public_api = api


def _api() -> Any:
    global _public_api
    if _public_api is None:
        from ..exchange.binance_rest import BinanceFutures
        _public_api = BinanceFutures()
    return _public_api


def public_allowed(symbol: str) -> bool:
    """수집 종목이 아닐 때만 공개 호가로 폴백한다."""
    try:
        return symbol.upper() not in get_settings().symbols
    except Exception:  # noqa: BLE001
        return False


async def _db_quote(symbol: str, *, max_age_s: int) -> Quote | None:
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


async def public_quote(symbol: str, api: Any = None) -> Quote | None:
    """바이낸스 공개 호가 + 최근 1분봉 고저. 실패하면 None (예외를 밖으로 내지 않는다)."""
    api = api or _api()
    now = datetime.now(UTC)
    try:
        bt = await api.book_ticker(symbol)
        bid, ask = float(bt.get("bidPrice") or 0), float(bt.get("askPrice") or 0)
    except Exception as e:  # noqa: BLE001
        log.debug("공개 호가 실패 %s: %s", symbol, e)
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    cached = _bar_cache.get(symbol)
    if cached is None or (now - cached[0]).total_seconds() > PUBLIC_BAR_CACHE_S:
        hi = lo = bar_start = None
        try:
            bars = await api.klines(symbol, "1m", limit=2)
            if bars:
                hi = max(b.high for b in bars)
                lo = min(b.low for b in bars)
                bar_start = min(b.ts for b in bars)
        except Exception as e:  # noqa: BLE001
            log.debug("공개 1분봉 실패 %s: %s", symbol, e)
        cached = (now, hi, lo, bar_start)
        _bar_cache[symbol] = cached
    _, hi, lo, bar_start = cached
    return Quote(symbol=symbol, bid=bid, ask=ask, at=now, high=hi, low=lo, bar_start=bar_start)


async def latest_quote(symbol: str, *, max_age_s: int = MAX_AGE_S,
                       allow_public: bool | None = None) -> Quote | None:
    q = await _db_quote(symbol, max_age_s=max_age_s)
    if q is not None:
        return q
    if allow_public is None:
        allow_public = public_allowed(symbol)
    if not allow_public:
        return None
    return await public_quote(symbol)
