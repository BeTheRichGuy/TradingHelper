"""손절 · 익절 · 포지션 사이즈 — 02 문서 5절을 그대로 옮긴 것.

이 파일의 숫자가 곧 실제로 거는 돈이므로, 전부 순수 함수로 두고 테스트로 고정한다.

**손절폭 0.50%~1.00% 인 자리에서만 신호가 나온다.** 그보다 좁으면 스프레드에
털리고, 넓으면 TP1 이 본주 2% 목표를 넘어간다. 이 범위는 클램프와 발행 조건이
가리키는 범위가 정확히 같도록 한 숫자에서 파생시켰다 (아래 상수 주석 참고).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .setups.base import Direction, EntryZone

ATR_MULT = 1.5
STRUCT_ATR_MULT = 0.2

# ── 손절폭 범위는 **사용자 목표에서 역산한다** ───────────────
#
# 목표: 본주(SOXL) 기준 1~2%, 포지션 기준 10~20%. 그게 TP1 이다.
#
#     TP1 = 2R ∈ [1.0%, 2.0%]   →   R ∈ [0.50%, 1.00%]
#     TP2 = 4R ∈ [2.0%, 4.0%]
#
# 그래서 클램프 범위와 발행 범위가 **같은 숫자**여야 한다. 예전에는
# 클램프가 [0.30, 0.70] 인데 발행이 [0.40, 0.625] 라 양 끝이 죽은 구간이었다.
# 클램프를 통과한 손절이 발행에서 거부되는 자리가 생기고, 실측에서 그게
# 리스크 단계 탈락의 91% 였다. `test_publish_band_matches_the_stop_clamp` 로
# 다시 어긋나지 않게 고정해 둔다.
SL_MIN_PCT = 0.50
SL_MAX_PCT = 1.00

TP1_R = 2.0
TP2_R = 4.0
TP1_MIN_PCT = TP1_R * SL_MIN_PCT       # 1.0% — 이보다 가까우면 먹을 게 없다
TP2_MAX_PCT = TP2_R * SL_MAX_PCT       # 4.0% — 이보다 멀면 목표 밴드를 벗어난다
MIN_RR = 2.0

# 발행 범위의 양 끝은 클램프의 양 끝과 **정확히 같은 값**이다. 그래서 손절이
# 하한/상한에 딱 걸린 자리는 TP1·4R 이 기준선 위에 정확히 얹힌다. 이때 가격에서
# 되계산하는 과정의 부동소수점 오차(1.0 이 0.9999999999999978 이 되는 것)로
# 거부되면 안 된다. 실측에서 이것 하나로 573건 중 103건이 사라지고 있었다.
BAND_EPS = 1e-9

DEFAULT_RISK_PCT = 0.5


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def floor_to_step(qty: float, step: float) -> float:
    """수량을 스텝 단위로 **내림**한다. 거래소는 스텝에 안 맞는 수량을 거부한다.

    부동소수점으로 나누면 0.3/0.1 = 2.9999… 라 floor 가 2 가 되어 한 스텝을
    통째로 잃는다. Decimal 로 문자열 표현을 거쳐 정확히 계산한다.
    """
    q, s = Decimal(str(qty)), Decimal(str(step))
    if s <= 0:
        return qty
    return float((q / s).to_integral_value(rounding=ROUND_DOWN) * s)


@dataclass(frozen=True, slots=True)
class TakeProfit:
    level: float
    r_multiple: float
    pct: float
    close_pct: int
    source: str = "r_multiple"     # r_multiple | structure


@dataclass(frozen=True, slots=True)
class Position:
    qty: float
    notional: float
    risk_usdt: float
    margin_usdt: float
    leverage: int


@dataclass(frozen=True, slots=True)
class TradePlan:
    direction: Direction
    entry: EntryZone
    entry_ref: float
    stop_loss: float
    risk_pct: float                # 손절까지 거리 %
    risk_per_unit: float           # 1R (가격 단위)
    take_profit: tuple[TakeProfit, ...]
    risk_reward: float
    position: Position | None = None
    funding_cost_usdt: float | None = None
    publishable: bool = True
    reject_reason: str = ""

    @property
    def tp1(self) -> TakeProfit | None:
        return self.take_profit[0] if self.take_profit else None

    @property
    def tp2(self) -> TakeProfit | None:
        return self.take_profit[1] if len(self.take_profit) > 1 else None


def stop_loss_price(
    direction: Direction,
    entry_ref: float,
    atr: float,
    price: float,
    swing: float | None,
) -> tuple[float, float]:
    """(손절가, 손절거리%) — 변동성 손절과 구조적 손절 중 **더 넓은 쪽**."""
    atr_pct = atr / price * 100 if price > 0 else 0.0
    dist_pct = clamp(ATR_MULT * atr_pct, SL_MIN_PCT, SL_MAX_PCT)

    if direction == "long":
        vol_sl = entry_ref * (1 - dist_pct / 100)
        struct_sl = (swing - STRUCT_ATR_MULT * atr) if swing is not None else None
        sl = min(vol_sl, struct_sl) if struct_sl is not None else vol_sl
        return sl, (entry_ref - sl) / entry_ref * 100

    vol_sl = entry_ref * (1 + dist_pct / 100)
    struct_sl = (swing + STRUCT_ATR_MULT * atr) if swing is not None else None
    sl = max(vol_sl, struct_sl) if struct_sl is not None else vol_sl
    return sl, (sl - entry_ref) / entry_ref * 100


def position_size(
    wallet: float,
    risk_pct: float,
    sl_dist_pct: float,
    entry_ref: float,
    risk_per_unit: float,
    leverage: int = 10,
    qty_step: float | None = None,
) -> Position | None:
    """**레버리지로 계산하지 않는다.** 손절 거리에서 역산한다 (02 문서 5.4).

    레버리지 배수는 사이즈를 키우는 손잡이가 아니라 청산가를 정하는 손잡이다.
    """
    if wallet <= 0 or sl_dist_pct <= 0 or entry_ref <= 0:
        return None
    notional = wallet * (risk_pct / 100) / (sl_dist_pct / 100)
    qty = notional / entry_ref
    qty = floor_to_step(qty, qty_step if qty_step and qty_step > 0 else 0.001)
    if qty <= 0:
        return None
    actual_notional = qty * entry_ref
    return Position(
        qty=qty,
        notional=actual_notional,
        risk_usdt=qty * risk_per_unit,
        margin_usdt=actual_notional / leverage if leverage else actual_notional,
        leverage=leverage,
    )


def build_plan(
    direction: Direction,
    entry: EntryZone,
    atr: float,
    price: float,
    swing: float | None,
    *,
    resistance: float | None = None,
    wallet: float = 0.0,
    risk_pct: float = DEFAULT_RISK_PCT,
    leverage: int = 10,
    qty_step: float | None = None,
    funding_rate: float | None = None,
    funding_within_hold: bool = False,
) -> TradePlan:
    long = direction == "long"
    ref = entry.reference(direction)

    sl, sl_dist_pct = stop_loss_price(direction, ref, atr, price, swing)
    r = abs(ref - sl)
    if r <= 0:
        return TradePlan(
            direction, entry, ref, sl, sl_dist_pct, 0.0, (), 0.0,
            publishable=False, reject_reason="손절 거리가 0입니다",
        )

    sign = 1 if long else -1
    tp1_level = ref + sign * TP1_R * r
    tp2_target = ref + sign * TP2_R * r

    tp2_level, tp2_source = tp2_target, "r_multiple"
    if resistance is not None:
        between = (
            tp1_level < resistance < tp2_target
            if long
            else tp2_target < resistance < tp1_level
        )
        if between:
            tp2_level, tp2_source = resistance, "structure"

    tp1_pct = abs(tp1_level - ref) / ref * 100
    tp2_pct = abs(tp2_level - ref) / ref * 100
    tp2_target_pct = abs(tp2_target - ref) / ref * 100
    rr = abs(tp2_level - ref) / r

    tps = (
        TakeProfit(tp1_level, TP1_R, tp1_pct, 50),
        TakeProfit(tp2_level, rr, tp2_pct, 50, tp2_source),
    )

    reject = ""
    if tp1_pct < TP1_MIN_PCT - BAND_EPS:
        reject = f"1차 목표가 {tp1_pct:.2f}% 로 너무 가깝습니다 (기준 {TP1_MIN_PCT}%)"
    elif tp2_target_pct > TP2_MAX_PCT + BAND_EPS:
        reject = (
            f"목표가 {tp2_target_pct:.2f}% 로 너무 멉니다 (기준 {TP2_MAX_PCT}%) — "
            "변동성이 커 손절폭이 과합니다"
        )
    elif rr < MIN_RR:
        reject = f"손익비 {rr:.2f} 가 기준({MIN_RR}) 미만입니다"

    pos = position_size(wallet, risk_pct, sl_dist_pct, ref, r, leverage, qty_step)

    cost = None
    if funding_rate is not None and pos is not None and funding_within_hold:
        cost = abs(funding_rate) * pos.notional

    return TradePlan(
        direction=direction,
        entry=entry,
        entry_ref=ref,
        stop_loss=sl,
        risk_pct=sl_dist_pct,
        risk_per_unit=r,
        take_profit=tps,
        risk_reward=rr,
        position=pos,
        funding_cost_usdt=cost,
        publishable=not reject,
        reject_reason=reject,
    )
