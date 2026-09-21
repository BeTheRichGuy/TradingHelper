"""체결 시뮬레이션 — 순수 함수.

지켜야 할 규칙 네 가지. 이걸 어기면 백테스트가 실전보다 좋게 나온다.

1. **미래를 보지 않는다.** 신호 시각 T 의 봉은 이미 신호 계산에 쓰였다.
   체결 판정은 `ts > T` 인 봉부터만 한다.
2. **같은 봉 안에서는 손절이 먼저다.** 1분봉의 고가와 저가 중 무엇이 먼저였는지
   알 수 없다. 둘 다 닿았으면 손절로 친다.
3. **진입은 공짜가 아니다.** 진입 트리거에 실제로 닿아야 체결이다. 3봉 안에
   안 닿으면 그 신호는 거래가 안 된 것으로 기록한다 (`no_fill`).
4. **비용은 양쪽에 붙는다.** 진입·청산 각각 수수료와 슬리피지, 그리고 보유
   구간에 펀딩 시각이 끼면 펀딩까지.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..strategy.indicators import Bar
from ..strategy.risk import TradePlan
from .models import BacktestConfig, Exit, Trade

TP1_PORTION = 0.5


def _slip(price: float, pct: float, adverse_up: bool) -> float:
    """불리한 쪽으로 밀린 가격. adverse_up 이면 위로(살 때), 아니면 아래로."""
    return price * (1 + pct / 100) if adverse_up else price * (1 - pct / 100)


def find_fill(
    plan: TradePlan,
    forward: list[Bar],
    timeout_bars: int,
) -> tuple[int, float] | None:
    """진입 트리거에 닿은 봉 인덱스와 체결가. 못 닿으면 None.

    롱은 `entry_ref` 이상으로 올라와야 체결이다 (스톱 매수). 갭으로 위에서 열리면
    시가로 체결된다 — 트리거보다 불리한 값이므로 그대로 쓴다.
    """
    long = plan.direction == "long"
    ref = plan.entry_ref
    for i, b in enumerate(forward[:timeout_bars]):
        if long and b.high >= ref:
            return i, max(b.open, ref) if b.open > ref else ref
        if not long and b.low <= ref:
            return i, min(b.open, ref) if b.open < ref else ref
    return None


def simulate_trade(
    plan: TradePlan,
    forward: list[Bar],
    *,
    signal_at: datetime,
    setup_id: str,
    regime: str,
    score: int,
    config: BacktestConfig,
    deadline: datetime | None = None,
    funding_rate: float | None = None,
    next_funding_at: datetime | None = None,
) -> Trade | str:
    """체결·청산을 끝까지 돌린다. 거래가 안 되면 사유 문자열을 돌려준다."""
    forward = [b for b in forward if b.ts > signal_at]     # 규칙 1
    if not forward:
        return "no_forward_data"

    hit = find_fill(plan, forward, config.entry_timeout_bars)
    if hit is None:
        return "no_fill"

    idx, trigger = hit
    long = plan.direction == "long"
    c = config.costs
    fill = _slip(trigger, c.slippage_pct, adverse_up=long)
    entry_bar = forward[idx]

    stop = plan.stop_loss
    tp1 = plan.tp1.level if plan.tp1 else None
    tp2 = plan.tp2.level if plan.tp2 else None
    if tp1 is None or tp2 is None:
        return "no_targets"

    r_unit = plan.risk_per_unit
    if r_unit <= 0:
        return "zero_risk"

    hard_stop = signal_at + timedelta(minutes=config.max_hold_minutes)
    if deadline is not None:
        hard_stop = min(hard_stop, deadline)

    exits: list[Exit] = []
    remaining = 1.0
    tp1_hit = False
    bars_to_tp1: int | None = None
    be_stop = fill                      # TP1 이후 손절은 체결가(본전)로 옮긴다
    exit_reason = ""
    last_bar = entry_bar

    for n, b in enumerate(forward[idx:]):
        last_bar = b

        if not tp1_hit:
            # 규칙 2 — 손절 먼저
            if (long and b.low <= stop) or (not long and b.high >= stop):
                px = _slip(stop, c.slippage_pct, adverse_up=not long)
                exits.append(Exit(b.ts, px, remaining, "stop"))
                remaining = 0.0
                exit_reason = "stop"
                break
            if (long and b.high >= tp1) or (not long and b.low <= tp1):
                px = _slip(tp1, c.slippage_pct, adverse_up=not long)
                exits.append(Exit(b.ts, px, TP1_PORTION, "tp1"))
                remaining -= TP1_PORTION
                tp1_hit = True
                bars_to_tp1 = n
                continue                # 같은 봉에서 TP2 까지 봤다고 치지 않는다
        else:
            if (long and b.low <= be_stop) or (not long and b.high >= be_stop):
                px = _slip(be_stop, c.slippage_pct, adverse_up=not long)
                exits.append(Exit(b.ts, px, remaining, "breakeven"))
                remaining = 0.0
                exit_reason = "breakeven"
                break
            if (long and b.high >= tp2) or (not long and b.low <= tp2):
                px = _slip(tp2, c.slippage_pct, adverse_up=not long)
                exits.append(Exit(b.ts, px, remaining, "tp2"))
                remaining = 0.0
                exit_reason = "tp2"
                break

        if b.ts >= hard_stop:
            px = _slip(b.close, c.slippage_pct, adverse_up=not long)
            exits.append(Exit(b.ts, px, remaining, "time"))
            remaining = 0.0
            exit_reason = "time"
            break

    if remaining > 0:                   # 데이터가 끝났다 — 마지막 종가로 정리
        px = _slip(last_bar.close, c.slippage_pct, adverse_up=not long)
        exits.append(Exit(last_bar.ts, px, remaining, "data_end"))
        exit_reason = exit_reason or "data_end"

    sign = 1.0 if long else -1.0
    gross = sum(e.portion * sign * (e.price - fill) for e in exits)

    fees = fill * c.taker_fee_pct / 100
    fees += sum(e.portion * e.price * c.taker_fee_pct / 100 for e in exits)

    funding = 0.0
    exit_at = exits[-1].at
    if (
        funding_rate is not None
        and next_funding_at is not None
        and entry_bar.ts <= next_funding_at <= exit_at
    ):
        funding = sign * funding_rate * fill      # 양의 펀딩이면 롱이 낸다

    net = gross - fees - funding

    return Trade(
        setup_id=setup_id,
        direction=plan.direction,
        regime=regime,
        score=score,
        signal_at=signal_at,
        entry_at=entry_bar.ts,
        entry_ref=plan.entry_ref,
        fill_price=fill,
        stop_loss=stop,
        tp1=tp1,
        tp2=tp2,
        r_per_unit=r_unit,
        exits=tuple(exits),
        exit_at=exit_at,
        exit_reason=exit_reason,
        gross_r=gross / r_unit,
        cost_r=(fees + funding) / r_unit,
        net_r=net / r_unit,
        bars_to_tp1=bars_to_tp1,
        hold_minutes=int((exit_at - entry_bar.ts).total_seconds() // 60),
        funding_paid_per_unit=funding,
    )


# ─────────────────────────────────────────────────────────
# MAE / MFE — **손절폭을 정하는 정직한 방법**
#
# 지금 손절은 ATR(1m)×1.5 다. 그런데 1분봉 변동성으로 잰 손절폭을, 목표에 닿는 데
# 30분 넘게 걸리는 거래에 쓰고 있다. 실측에서 청산의 67%가 손절이었고 보유시간
# 중앙값이 11분이었다 — 자리가 틀려서가 아니라 노이즈에 먼저 털린 모양이다.
#
# 그걸 확인하는 방법이 MAE(최대 역행)다. **손절을 걸지 않았다면** 이 거래가 목표에
# 닿기 전에 얼마나 반대로 갔는지를 잰다. 결국 이긴 거래들의 MAE 분포가 현재
# 손절폭보다 넓으면, 우리는 이길 거래를 손절로 잘라내고 있는 것이다.
#
# **손익을 최대화하는 값을 찾는 게 아니다.** "이길 거래의 몇 %를 살리려면 손절이
# 얼마여야 하는가"라는 질문에 답할 뿐이고, 그 답은 손익과 무관하게 성립한다.
# ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Excursion:
    """손절 없이 끝까지 봤을 때의 역행·순행 폭 (진입가 대비 %)."""

    mae_pct: float                 # 최대 역행
    mfe_pct: float                 # 최대 순행
    minutes_to_mfe: int
    reached_tp1: bool
    minutes_to_tp1: int | None
    mae_before_tp1_pct: float | None    # TP1 에 닿기 전까지의 역행 — 이게 핵심이다


def excursion(
    plan: TradePlan,
    forward: list[Bar],
    *,
    signal_at: datetime,
    config: BacktestConfig,
    deadline: datetime | None = None,
) -> Excursion | None:
    """체결됐다고 가정하고 손절 없이 관찰만 한다."""
    forward = [b for b in forward if b.ts > signal_at]
    if not forward:
        return None
    hit = find_fill(plan, forward, config.entry_timeout_bars)
    if hit is None:
        return None

    idx, trigger = hit
    long = plan.direction == "long"
    fill = _slip(trigger, config.costs.slippage_pct, adverse_up=long)
    tp1 = plan.tp1.level if plan.tp1 else None

    stop_at = forward[idx].ts + timedelta(minutes=config.max_hold_minutes)
    if deadline is not None:
        stop_at = min(stop_at, deadline)

    worst = best = 0.0
    worst_before_tp1 = 0.0
    mins_to_best = 0
    tp1_min: int | None = None

    for n, b in enumerate(forward[idx:]):
        if b.ts > stop_at:
            break
        adverse = (fill - b.low) if long else (b.high - fill)
        favour = (b.high - fill) if long else (fill - b.low)
        adverse_pct = adverse / fill * 100
        favour_pct = favour / fill * 100

        if adverse_pct > worst:
            worst = adverse_pct
        if favour_pct > best:
            best, mins_to_best = favour_pct, n
        if tp1_min is None:
            if adverse_pct > worst_before_tp1:
                worst_before_tp1 = adverse_pct
            if tp1 is not None and ((long and b.high >= tp1) or (not long and b.low <= tp1)):
                tp1_min = n

    return Excursion(
        mae_pct=worst,
        mfe_pct=best,
        minutes_to_mfe=mins_to_best,
        reached_tp1=tp1_min is not None,
        minutes_to_tp1=tp1_min,
        mae_before_tp1_pct=worst_before_tp1 if tp1_min is not None else None,
    )
