"""지표 계산 — 전부 순수 함수.

pandas 를 쓰지 않는다. 봉 수백 개 정도라 속도 차이가 없고, 순수 파이썬이면
네트워크·DB·라이브러리 버전과 무관하게 테스트로 고정할 수 있기 때문이다.

TradingView 와 숫자를 맞추기 위한 규칙 두 가지:
  · RSI·ATR 은 **Wilder 평활(RMA)** 을 쓴다. 단순이동평균이 아니다.
  · EMA 는 처음 period 개의 단순평균으로 시드한다.
이 둘이 어긋나면 같은 지표라도 값이 미묘하게 달라진다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime                  # 봉 시작 시각 (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy: float | None = None   # 매수 체결량 (CVD 계산용)

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def close_position(self) -> float | None:
        """봉 범위 안에서 종가가 어디에 있나. 1.0 = 고가 마감."""
        if self.range <= 0:
            return None
        return (self.close - self.low) / self.range


# ─────────────────────────────────────────────────────────
# 평활
# ─────────────────────────────────────────────────────────

def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    total = sum(values[:period])
    out[period - 1] = total / period
    for i in range(period, len(values)):
        total += values[i] - values[i - period]
        out[i] = total / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period      # SMA 시드 (TradingView 와 동일)
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rma(values: list[float], period: int) -> list[float | None]:
    """Wilder 평활. RSI·ATR 의 기본이다."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


# ─────────────────────────────────────────────────────────
# 지표
# ─────────────────────────────────────────────────────────

def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains[i] = max(diff, 0.0)
        losses[i] = max(-diff, 0.0)

    avg_g = rma(gains[1:], period)
    avg_l = rma(losses[1:], period)
    for i, (g, loss) in enumerate(zip(avg_g, avg_l, strict=True), start=1):
        if g is None or loss is None:
            continue
        if loss == 0:
            out[i] = 100.0
        else:
            rs = g / loss
            out[i] = 100 - 100 / (1 + rs)
    return out


def true_ranges(bars: list[Bar]) -> list[float]:
    if not bars:
        return []
    out = [bars[0].range]
    for i in range(1, len(bars)):
        b, prev = bars[i], bars[i - 1].close
        out.append(max(b.high - b.low, abs(b.high - prev), abs(b.low - prev)))
    return out


def atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    """Wilder ATR. 첫 봉의 TR 은 전일 종가를 모르므로 제외하고 시작한다."""
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    trs = true_ranges(bars)[1:]              # 첫 봉 제외
    smoothed = rma(trs, period)
    for i, v in enumerate(smoothed, start=1):
        out[i] = v
    return out


@dataclass(frozen=True, slots=True)
class VwapPoint:
    vwap: float
    sigma: float

    def band(self, mult: float) -> tuple[float, float]:
        return self.vwap - mult * self.sigma, self.vwap + mult * self.sigma

    def distance_pct(self, price: float) -> float:
        if self.vwap <= 0:
            return 0.0
        return (price - self.vwap) / self.vwap * 100


def session_vwap(bars: list[Bar], session_start: datetime) -> list[VwapPoint | None]:
    """세션 앵커 VWAP.

    **세션마다 리셋한다.** 프리장(04:00 ET)과 본장(09:30 ET)의 VWAP 은 별개다.
    하나로 이어 붙이면 본장 VWAP 이 밤새 거래에 오염되어 되돌림 기준선이 망가진다.
    """
    out: list[VwapPoint | None] = [None] * len(bars)
    pv = vol = pv2 = 0.0
    for i, b in enumerate(bars):
        if b.ts < session_start or b.volume <= 0:
            continue
        tp = b.typical
        pv += tp * b.volume
        pv2 += tp * tp * b.volume
        vol += b.volume
        if vol <= 0:
            continue
        v = pv / vol
        var = max(0.0, pv2 / vol - v * v)
        out[i] = VwapPoint(vwap=v, sigma=math.sqrt(var))
    return out


def cumulative_delta(bars: list[Bar]) -> list[float | None]:
    """CVD — 매수 체결에서 매도 체결을 뺀 누적값.

    taker_buy 는 '시장가 매수로 체결된 물량'이다. 나머지는 매도 체결이므로
    한 봉의 델타는 (매수 − 매도) = 2×taker_buy − volume 이 된다.
    가격은 오르는데 CVD 가 안 따라오면 실수요 없는 상승이다.
    """
    out: list[float | None] = []
    total = 0.0
    seen = False
    for b in bars:
        if b.taker_buy is None:
            out.append(total if seen else None)
            continue
        seen = True
        total += 2 * b.taker_buy - b.volume
        out.append(total)
    return out


def slope(values: list[float | None], lookback: int = 3) -> float | None:
    """최근 lookback 개 구간의 단순 기울기 (값/봉)."""
    pts = [v for v in values[-(lookback + 1):] if v is not None]
    if len(pts) < 2:
        return None
    return (pts[-1] - pts[0]) / (len(pts) - 1)


# ─────────────────────────────────────────────────────────
# 구조 — 스윙 고·저점
# ─────────────────────────────────────────────────────────

def swing_lows(bars: list[Bar], lookback: int = 5) -> list[int]:
    """좌우 lookback 개보다 낮은 저점의 인덱스."""
    out: list[int] = []
    for i in range(lookback, len(bars) - lookback):
        low = bars[i].low
        if all(low <= bars[j].low for j in range(i - lookback, i)) and all(
            low < bars[j].low for j in range(i + 1, i + lookback + 1)
        ):
            out.append(i)
    return out


def swing_highs(bars: list[Bar], lookback: int = 5) -> list[int]:
    out: list[int] = []
    for i in range(lookback, len(bars) - lookback):
        high = bars[i].high
        if all(high >= bars[j].high for j in range(i - lookback, i)) and all(
            high > bars[j].high for j in range(i + 1, i + lookback + 1)
        ):
            out.append(i)
    return out


# 최근 몇 봉 안에서만 구조를 찾을지.
#
# **셋업 자신의 정의 창과 같게 맞춘다.** S1 은 "최근 3개의 5분봉(=15분) 안에서
# VWAP 을 건드렸다가 복귀"로 정의된다. 그 셋업이 틀렸다고 판정되는 지점은 그
# 되돌림의 저점이지 한 시간 전 피벗이 아니다. 60 은 근거 없이 고른 숫자였고,
# 실측에서 손절 중앙값을 0.70% → 1.38% 로 벌리는 주범이었다.
SWING_WINDOW = 15


def last_swing_low(
    bars: list[Bar], lookback: int = 5, within: int | None = SWING_WINDOW
) -> float | None:
    """최근 `within` 봉 안의 마지막 스윙 저점. 없으면 None.

    `within` 을 두는 이유: 추세일에는 마지막 피벗이 몇 시간 전일 수 있고, 그걸
    '직전 스윙 저점'으로 쓰면 손절이 3% 넘게 벌어져 어떤 신호도 발행되지 않는다.
    구간 안에 구조가 없으면 None 을 돌려 ATR 손절만 쓰게 한다.
    """
    window = bars[-within:] if within else bars
    idx = swing_lows(window, lookback)
    return window[idx[-1]].low if idx else None


def last_swing_high(
    bars: list[Bar], lookback: int = 5, within: int | None = SWING_WINDOW
) -> float | None:
    window = bars[-within:] if within else bars
    idx = swing_highs(window, lookback)
    return window[idx[-1]].high if idx else None


# ─────────────────────────────────────────────────────────
# 집계
# ─────────────────────────────────────────────────────────

def resample(bars: list[Bar], minutes: int, drop_incomplete: bool = False) -> list[Bar]:
    """1분봉을 N분봉으로 묶는다. 경계는 시각 자체로 맞춘다(09:30, 09:35 …).

    `drop_incomplete=True` 면 **아직 안 닫힌 마지막 묶음을 버린다.**

    이게 없으면 10:47에 평가할 때 마지막 '5분봉'이 10:45~10:47 세 개짜리가 되고,
    그 거래량을 완성된 5분봉 중앙값과 비교하게 된다. 지표는 닫힌 봉으로만 계산한다.
    """
    if minutes <= 1 or not bars:
        return list(bars)

    out: list[Bar] = []
    bucket: list[Bar] = []
    current_key = None

    for b in bars:
        key = int(b.ts.timestamp()) // (minutes * 60)
        if current_key is None:
            current_key = key
        if key != current_key:
            out.append(_merge(bucket))
            bucket = []
            current_key = key
        bucket.append(b)
    if bucket and not (drop_incomplete and len(bucket) < minutes):
        out.append(_merge(bucket))
    return out


def _merge(group: list[Bar]) -> Bar:
    taker = [b.taker_buy for b in group if b.taker_buy is not None]
    return Bar(
        ts=group[0].ts,
        open=group[0].open,
        high=max(b.high for b in group),
        low=min(b.low for b in group),
        close=group[-1].close,
        volume=sum(b.volume for b in group),
        taker_buy=sum(taker) if taker else None,
    )


def percentile_rank(values: list[float], target: float) -> float | None:
    """target 이 values 안에서 몇 퍼센타일인가."""
    clean = [v for v in values if v is not None]
    if len(clean) < 5:
        return None
    below = sum(1 for v in clean if v <= target)
    return below / len(clean) * 100


# ─────────────────────────────────────────────────────────
# 거래량 가중 이동평균 (VWMA)
#
# 단순 이동평균과 달리 **거래가 실린 가격에 무게를 준다.** 얇은 호가에서 몇 계약이
# 만든 가격은 덜 반영되고, 물량이 실려 나간 가격이 더 반영된다. 프리장처럼 거래가
# 드문 구간에서 EMA 보다 덜 속는 이유가 이것이다.
#
# VWAP 과 헷갈리기 쉬운데 다르다. VWAP 은 세션 시작부터 **누적**이고 하루에 한 값이
# 쌓여간다. VWMA 는 최근 n봉만 보는 **이동** 평균이라 여러 개를 겹쳐 배열을 볼 수 있다.
# ─────────────────────────────────────────────────────────

def vwma(bars: list[Bar], period: int) -> list[float | None]:
    """Σ(종가 × 거래량) / Σ(거래량), 최근 period 봉."""
    out: list[float | None] = []
    pv = vol = 0.0
    for i, b in enumerate(bars):
        pv += b.close * b.volume
        vol += b.volume
        if i >= period:
            old = bars[i - period]
            pv -= old.close * old.volume
            vol -= old.volume
        out.append(pv / vol if i >= period - 1 and vol > 0 else None)
    return out


@dataclass(frozen=True, slots=True)
class VwmaStack:
    """빠름·중간·느림 세 줄. 정배열/역배열과 그 지속 기간을 판단한다."""

    fast: list[float | None]
    mid: list[float | None]
    slow: list[float | None]

    def order_at(self, i: int) -> int:
        """+1 정배열(빠름>중간>느림) · −1 역배열 · 0 뒤섞임/데이터 없음."""
        try:
            f, m, s = self.fast[i], self.mid[i], self.slow[i]
        except IndexError:
            return 0
        if f is None or m is None or s is None:
            return 0
        if f > m > s:
            return 1
        if f < m < s:
            return -1
        return 0

    @property
    def order(self) -> int:
        return self.order_at(-1)

    def alignment_age(self) -> int:
        """지금 배열이 몇 봉째 유지되고 있는가. 0이면 배열이 아니다.

        **갓 만들어진 배열은 신뢰하지 않는다.** 한 봉 만에 뒤집히는 자리가
        추세추종에서 가장 비싸게 물리는 곳이다.
        """
        now = self.order
        if now == 0:
            return 0
        n = 0
        for i in range(len(self.fast) - 1, -1, -1):
            if self.order_at(i) != now:
                break
            n += 1
        return n

    def values(self) -> tuple[float | None, float | None, float | None]:
        end = (
            self.fast[-1] if self.fast else None,
            self.mid[-1] if self.mid else None,
            self.slow[-1] if self.slow else None,
        )
        return end

    def zone(self) -> tuple[float, float] | None:
        """눌림을 기다리는 구간 = 빠른 선과 중간 선 사이."""
        f, m, _ = self.values()
        if f is None or m is None:
            return None
        return (min(f, m), max(f, m))

    def mid_slope(self, n: int = 5) -> float | None:
        return slope([v for v in self.mid if v is not None], n)


def build_vwma_stack(
    bars: list[Bar], fast: int = 9, mid: int = 21, slow: int = 50
) -> VwmaStack:
    return VwmaStack(vwma(bars, fast), vwma(bars, mid), vwma(bars, slow))
