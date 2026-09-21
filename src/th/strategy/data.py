"""DB에서 분석 입력을 읽어온다.

전략 계산은 이 층 바깥에서 순수 함수로 이뤄진다. 여기가 유일하게 DB를 아는 곳이다.
그래야 "과거 시각을 지정해 그대로 재현"이 가능하다 — 스냅샷(OI·펀딩·호가)은 `ts <= at`,
**봉**은 `ts <= at − (봉 길이)` 로 읽어 미래 데이터도, 아직 형성 중인 봉도 새어들 수 없다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .. import db
from ..config import get_settings
from ..logging_setup import get_logger
from .features import PerpContext
from .indicators import Bar

log = get_logger(__name__)

# 전일 고·저를 잡으려면 며칠치가 필요하다 (주말·공휴일 여유 포함)
DEFAULT_LOOKBACK_DAYS = 5


async def load_bars(
    symbol: str, at: datetime, days: int = DEFAULT_LOOKBACK_DAYS
) -> list[Bar]:
    # ts 가 붙은 1분봉은 ts+1분에야 닫힌다. `ts <= at` 로 읽으면 아직 형성 중인 봉이
    # 섞여 최대 59초를 미리 보는 셈이 된다. 닫힌 봉만 쓴다.
    until = at - timedelta(minutes=1)
    since = until - timedelta(days=days)
    rows = await db.fetch(
        """SELECT ts, open, high, low, close, volume, taker_buy_base
           FROM klines
           WHERE symbol = $1 AND tf = '1m' AND ts >= $2 AND ts <= $3
           ORDER BY ts""",
        symbol.upper(), since, until,
    )
    return [
        Bar(
            ts=r["ts"],
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
            taker_buy=float(r["taker_buy_base"]) if r["taker_buy_base"] is not None else None,
        )
        for r in rows
    ]


async def _oi_change_pct(symbol: str, at: datetime, minutes: int) -> float | None:
    rows = await db.fetch(
        """SELECT oi FROM open_interest
           WHERE symbol = $1 AND ts <= $2 ORDER BY ts DESC LIMIT 1""",
        symbol, at,
    )
    past = await db.fetch(
        """SELECT oi FROM open_interest
           WHERE symbol = $1 AND ts <= $2 ORDER BY ts DESC LIMIT 1""",
        symbol, at - timedelta(minutes=minutes),
    )
    if not rows or not past:
        return None
    now_v, past_v = float(rows[0]["oi"]), float(past[0]["oi"])
    if past_v <= 0:
        return None
    return (now_v - past_v) / past_v * 100


async def load_perp_context(symbol: str, at: datetime) -> PerpContext:
    s = get_settings()
    symbol = symbol.upper()

    funding_rate = next_funding = None
    try:
        rows = await db.fetch(
            """SELECT rate, next_funding_ts FROM funding
               WHERE symbol = $1 AND ts <= $2 ORDER BY ts DESC LIMIT 1""",
            symbol, at,
        )
        if rows:
            funding_rate = float(rows[0]["rate"])
            next_funding = rows[0]["next_funding_ts"]
    except Exception as e:
        log.debug("펀딩 조회 실패: %s", e)

    spread = None
    try:
        rows = await db.fetch(
            """SELECT spread_bps FROM book_ticker
               WHERE symbol = $1 AND ts <= $2 ORDER BY ts DESC LIMIT 1""",
            symbol, at,
        )
        if rows:
            spread = float(rows[0]["spread_bps"])
    except Exception as e:
        log.debug("스프레드 조회 실패: %s", e)

    oi5 = oi15 = None
    try:
        oi5 = await _oi_change_pct(symbol, at, 5)
        oi15 = await _oi_change_pct(symbol, at, 15)
    except Exception as e:
        log.debug("OI 조회 실패: %s", e)

    # 본주·VIX 1분봉도 `load_bars` 와 같은 규칙 — ts 봉은 ts+1분에 닫히니 `at − 1분` 까지만.
    # `ts <= at` 로 읽으면 형성 중인 봉의 종가를 1분 미리 보게 되고, basis 가 그만큼 새었다.
    bar_until = at - timedelta(minutes=1)
    underlying_price = basis = None
    ticker = s.underlying_map.get(symbol)
    if ticker:
        try:
            rows = await db.fetch(
                """SELECT close FROM underlying_prices
                   WHERE ticker = $1 AND tf = '1m' AND ts <= $2
                   ORDER BY ts DESC LIMIT 1""",
                ticker, bar_until,
            )
            if rows:
                underlying_price = float(rows[0]["close"])
        except Exception as e:
            log.debug("본주 가격 조회 실패: %s", e)

    vix = None
    try:
        rows = await db.fetch(
            """SELECT close FROM underlying_prices
               WHERE ticker = '^VIX' AND ts <= $1 ORDER BY ts DESC LIMIT 1""",
            bar_until,
        )
        if rows:
            vix = float(rows[0]["close"])
    except Exception as e:
        log.debug("VIX 조회 실패: %s", e)

    return PerpContext(
        funding_rate=funding_rate,
        next_funding_at=next_funding,
        oi_change_5m=oi5,
        oi_change_15m=oi15,
        basis_pct=basis,
        spread_bps=spread,
        underlying_price=underlying_price,
        vix=vix,
    )


def compute_basis(perp_price: float, underlying_price: float | None) -> float | None:
    """(perp − 본주) / 본주 × 100.

    주의: 본주 가격은 미국 장이 열려 있을 때만 의미가 있다. 🔴 구간의 괴리는
    '본주가 멈춰 있어서' 벌어지는 것이라 과열 판단에 쓰면 안 된다.
    """
    if not underlying_price or underlying_price <= 0:
        return None
    return (perp_price - underlying_price) / underlying_price * 100


MEDIAN_VOLUME_BUCKET_MINUTES = 5


async def median_volume_5m(symbol: str, at: datetime, bars: int = 100) -> float | None:
    # 5분 버킷 ts 는 ts+5분에야 완성된다. `ts <= at` 는 형성 중인(거래량이 덜 찬) 버킷을
    # 섞는다 — CSV 경로(`csvdata.median_volume_5m`)와 같이 완성된 버킷만 센다.
    until = at - timedelta(minutes=MEDIAN_VOLUME_BUCKET_MINUTES)
    try:
        rows = await db.fetch(
            """SELECT volume FROM klines_5m
               WHERE symbol = $1 AND ts <= $2 ORDER BY ts DESC LIMIT $3""",
            symbol.upper(), until, bars,
        )
        vals = sorted(float(r["volume"]) for r in rows)
        if len(vals) < 20:
            return None
        return vals[len(vals) // 2]
    except Exception as e:
        log.debug("거래량 중앙값 조회 실패 (연속집계 미갱신일 수 있음): %s", e)
        return None


async def last_bar_time(symbol: str) -> datetime | None:
    rows = await db.fetch(
        "SELECT max(ts) AS ts FROM klines WHERE symbol = $1 AND tf = '1m'",
        symbol.upper(),
    )
    return rows[0]["ts"] if rows else None


def now_floor_minute() -> datetime:
    """'지금'을 분 단위로 내림. 닫힌 봉만 쓰는 처리는 `load_bars` 가 한다."""
    return datetime.now(UTC).replace(second=0, microsecond=0)
