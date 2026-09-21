"""봉과 perp 데이터를 셋업이 읽을 수 있는 한 덩어리로 정리한다.

셋업 코드가 직접 지표를 계산하지 않게 하는 게 목적이다. 그래야 같은 지표를
셋업마다 다르게 계산하는 사고가 안 난다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..sessions.market import SessionCalendar, get_calendar
from ..sessions.models import Regime, SessionContext
from .indicators import (
    VwmaStack,
    build_vwma_stack,
    Bar,
    VwapPoint,
    atr,
    cumulative_delta,
    ema,
    last_swing_high,
    last_swing_low,
    percentile_rank,
    resample,
    rsi,
    session_vwap,
    slope,
    sma,
)

OPENING_RANGE_MINUTES = 15
ATR_PERCENTILE_LOOKBACK = 200


@dataclass(frozen=True, slots=True)
class PerpContext:
    """일반 주식 차트에는 없는 값들 (02 문서 2.2)."""

    funding_rate: float | None = None
    next_funding_at: datetime | None = None
    oi_change_5m: float | None = None      # %
    oi_change_15m: float | None = None     # %
    basis_pct: float | None = None         # (perp − 본주) / 본주 × 100
    spread_bps: float | None = None
    underlying_price: float | None = None
    vix: float | None = None

    @property
    def oi_rising(self) -> bool | None:
        if self.oi_change_15m is None:
            return None
        return self.oi_change_15m > 0


@dataclass(frozen=True, slots=True)
class Levels:
    """구조적 가격대. 스윕·돌파·목표 여유 판단에 쓴다."""

    prev_day_high: float | None = None
    prev_day_low: float | None = None
    prev_day_close: float | None = None
    premarket_high: float | None = None
    premarket_low: float | None = None
    opening_range_high: float | None = None
    opening_range_low: float | None = None

    def resistances_above(self, price: float) -> list[float]:
        vals = [self.prev_day_high, self.premarket_high, self.opening_range_high]
        return sorted(v for v in vals if v is not None and v > price)

    def supports_below(self, price: float) -> list[float]:
        vals = [self.prev_day_low, self.premarket_low, self.opening_range_low]
        return sorted((v for v in vals if v is not None and v < price), reverse=True)

    def nearest_resistance_pct(self, price: float) -> float | None:
        above = self.resistances_above(price)
        if not above or price <= 0:
            return None
        return (above[0] - price) / price * 100

    def sweep_levels(self) -> list[tuple[str, float]]:
        """스윕(유동성 사냥)의 대상이 되는 하방 레벨들."""
        out = []
        if self.prev_day_low is not None:
            out.append(("전일 저가", self.prev_day_low))
        if self.premarket_low is not None:
            out.append(("프리장 저가", self.premarket_low))
        return out


@dataclass(slots=True)
class Features:
    at: datetime
    symbol: str
    price: float

    bars_1m: list[Bar] = field(default_factory=list)
    bars_5m: list[Bar] = field(default_factory=list)
    bars_15m: list[Bar] = field(default_factory=list)

    vwap: VwapPoint | None = None
    ema9_5m: float | None = None
    ema21_5m: float | None = None
    ema9_15m: float | None = None
    ema21_15m: float | None = None
    rsi14_5m: float | None = None
    rsi9_1m: float | None = None
    rsi9_1m_prev: float | None = None
    rsi14_5m_prev: float | None = None

    vwma_5m: VwmaStack | None = None
    vwma_15m: VwmaStack | None = None

    atr_1m: float | None = None
    atr_pct: float | None = None
    atr_percentile: float | None = None

    volume_5m: float | None = None
    volume_ma20_5m: float | None = None
    volume_1m: float | None = None
    volume_ma20_1m: float | None = None

    cvd: float | None = None
    cvd_slope: float | None = None

    # 다이버전스·스윕 판정에 과거 값이 필요한 것만 시리즈로 보관
    rsi9_1m_series: list[float | None] = field(default_factory=list)
    cvd_series: list[float | None] = field(default_factory=list)

    swing_low_1m: float | None = None
    swing_high_1m: float | None = None

    levels: Levels = field(default_factory=Levels)
    perp: PerpContext = field(default_factory=PerpContext)
    session: SessionContext | None = None

    # ── 파생 판정 ────────────────────────────────────────
    @property
    def above_vwap(self) -> bool | None:
        if self.vwap is None:
            return None
        return self.price > self.vwap.vwap

    @property
    def vwap_distance_pct(self) -> float | None:
        if self.vwap is None:
            return None
        return self.vwap.distance_pct(self.price)

    @property
    def trend_5m_up(self) -> bool | None:
        if self.ema9_5m is None or self.ema21_5m is None:
            return None
        return self.ema9_5m > self.ema21_5m

    @property
    def trend_15m_up(self) -> bool | None:
        if self.ema9_15m is None or self.ema21_15m is None:
            return None
        return self.ema9_15m > self.ema21_15m

    @property
    def relative_volume_5m(self) -> float | None:
        if not self.volume_5m or not self.volume_ma20_5m:
            return None
        return self.volume_5m / self.volume_ma20_5m


# ─────────────────────────────────────────────────────────
# 구축
# ─────────────────────────────────────────────────────────

def build_levels(
    bars_1m: list[Bar], at: datetime, cal: SessionCalendar | None = None
) -> Levels:
    cal = cal or get_calendar()
    day_et = at.astimezone(_ny()).date()
    today = cal.windows(day_et)
    if today is None:
        return Levels()

    pre = [b for b in bars_1m if today.pre_start <= b.ts < today.market_open]
    orb_end = today.market_open + timedelta(minutes=OPENING_RANGE_MINUTES)
    orb = [b for b in bars_1m if today.market_open <= b.ts < min(orb_end, at)]

    prev_day = cal.previous_trading_day(day_et)
    prev_high = prev_low = prev_close = None
    if prev_day is not None:
        pw = cal.windows(prev_day)
        if pw is not None:
            prev_bars = [b for b in bars_1m if pw.market_open <= b.ts < pw.market_close]
            if prev_bars:
                prev_high = max(b.high for b in prev_bars)
                prev_low = min(b.low for b in prev_bars)
                prev_close = prev_bars[-1].close

    return Levels(
        prev_day_high=prev_high,
        prev_day_low=prev_low,
        prev_day_close=prev_close,
        premarket_high=max((b.high for b in pre), default=None),
        premarket_low=min((b.low for b in pre), default=None),
        opening_range_high=max((b.high for b in orb), default=None),
        opening_range_low=min((b.low for b in orb), default=None),
    )


def _ny():
    from ..sessions.market import NY

    return NY


def session_anchor(at: datetime, session: SessionContext | None,
                   cal: SessionCalendar | None = None) -> datetime | None:
    """VWAP 을 어디서부터 계산할 것인가.

    본장이면 09:30 ET, 프리장이면 04:00 ET. 애프터는 본장 VWAP 을 이어 쓴다
    (같은 날 가격 발견의 연장이므로).
    """
    cal = cal or get_calendar()
    w = cal.windows(at.astimezone(_ny()).date())
    if w is None:
        return None
    regime = session.regime if session else w.regime_at(at)
    if regime is Regime.PRE:
        return w.pre_start
    if regime in (Regime.RTH, Regime.POST):
        return w.market_open
    return None


def build_features(
    symbol: str,
    bars_1m: list[Bar],
    at: datetime,
    session: SessionContext | None = None,
    perp: PerpContext | None = None,
    cal: SessionCalendar | None = None,
    levels: Levels | None = None,
) -> Features:
    """확정된 1분봉만 넘길 것. 진행 중인 봉을 섞으면 실전과 백테스트가 갈라진다.

    `levels` 를 넘기면 그걸 그대로 쓴다. 백테스트가 하루치 레벨을 한 번만 계산해
    재사용하기 위한 구멍이고, 넘기지 않으면 실전과 똑같이 여기서 계산한다.
    """
    cal = cal or get_calendar()
    bars_1m = [b for b in bars_1m if b.ts <= at]
    if not bars_1m:
        raise ValueError("봉이 비어 있습니다")

    # 아직 안 닫힌 마지막 묶음은 버린다. 그걸 남기면 1~4분짜리 '5분봉' 의 거래량을
    # 완성된 5분봉 중앙값과 비교하게 되어 유동성 필터가 상시 걸린다.
    bars_5m = resample(bars_1m, 5, drop_incomplete=True)
    bars_15m = resample(bars_1m, 15, drop_incomplete=True)

    closes_5m = [b.close for b in bars_5m]
    closes_1m = [b.close for b in bars_1m]

    e9_5 = ema(closes_5m, 9)
    e21_5 = ema(closes_5m, 21)
    closes_15m = [b.close for b in bars_15m]
    e9_15 = ema(closes_15m, 9)
    e21_15 = ema(closes_15m, 21)

    r14_5 = rsi(closes_5m, 14)
    r9_1 = rsi(closes_1m, 9)

    atr_1m_series = atr(bars_1m, 14)
    atr_now = atr_1m_series[-1]
    price = bars_1m[-1].close
    atr_pct = (atr_now / price * 100) if (atr_now and price) else None

    atr_pcts = [
        (v / bars_1m[i].close * 100)
        for i, v in enumerate(atr_1m_series)
        if v is not None and bars_1m[i].close > 0
    ]
    atr_pctl = (
        percentile_rank(atr_pcts[-ATR_PERCENTILE_LOOKBACK:], atr_pct)
        if atr_pct is not None
        else None
    )

    vol_ma_5 = sma([b.volume for b in bars_5m], 20)
    vol_ma_1 = sma([b.volume for b in bars_1m], 20)
    cvd = cumulative_delta(bars_1m)

    anchor = session_anchor(at, session, cal)
    vwap_series = session_vwap(bars_1m, anchor) if anchor else [None] * len(bars_1m)

    return Features(
        at=at,
        symbol=symbol.upper(),
        price=price,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_15m=bars_15m,
        vwap=vwap_series[-1],
        # 5분봉이 하나도 안 닫혔으면(1분봉 5개 미만) 5분 지표는 None — 셋업은
        # 없는 값을 '미충족'으로 다루므로 여기서 IndexError 를 내면 안 된다
        ema9_5m=e9_5[-1] if e9_5 else None,
        ema21_5m=e21_5[-1] if e21_5 else None,
        ema9_15m=e9_15[-1] if e9_15 else None,
        ema21_15m=e21_15[-1] if e21_15 else None,
        rsi14_5m=r14_5[-1] if r14_5 else None,
        rsi14_5m_prev=r14_5[-2] if len(r14_5) > 1 else None,
        rsi9_1m=r9_1[-1],
        rsi9_1m_prev=r9_1[-2] if len(r9_1) > 1 else None,
        atr_1m=atr_now,
        atr_pct=atr_pct,
        atr_percentile=atr_pctl,
        vwma_5m=build_vwma_stack(bars_5m) if bars_5m else None,
        vwma_15m=build_vwma_stack(bars_15m) if bars_15m else None,
        volume_5m=bars_5m[-1].volume if bars_5m else None,
        volume_ma20_5m=vol_ma_5[-1] if vol_ma_5 else None,
        volume_1m=bars_1m[-1].volume,
        volume_ma20_1m=vol_ma_1[-1] if vol_ma_1 else None,
        cvd=cvd[-1] if cvd else None,
        cvd_slope=slope(cvd, 5),
        rsi9_1m_series=r9_1,
        cvd_series=cvd,
        swing_low_1m=last_swing_low(bars_1m, 5),
        swing_high_1m=last_swing_high(bars_1m, 5),
        levels=levels if levels is not None else build_levels(bars_1m, at, cal),
        perp=perp or PerpContext(),
        session=session,
    )
