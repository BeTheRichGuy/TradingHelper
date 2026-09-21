"""백테스트 자료형 — 순수 데이터. DB도 네트워크도 모른다."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 04 문서: 수수료 0.08% (왕복) + 슬리피지 0.03%
TAKER_FEE_PCT = 0.04           # 한 방향당 %
SLIPPAGE_PCT = 0.03            # 한 방향당 %

ENTRY_TIMEOUT_BARS = 3         # 진입 트리거가 이 안에 안 닿으면 그 신호는 무효
MAX_HOLD_MINUTES = 240         # "몇 분에서 몇 시간" — 상한을 4시간으로 잡는다


@dataclass(frozen=True, slots=True)
class Costs:
    """가격 대비 %. 한 방향(진입 또는 청산) 기준이다."""

    taker_fee_pct: float = TAKER_FEE_PCT
    slippage_pct: float = SLIPPAGE_PCT

    @property
    def round_trip_pct(self) -> float:
        return 2 * (self.taker_fee_pct + self.slippage_pct)


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    wallet: float = 10_000.0
    risk_pct: float = 0.5
    leverage: int = 10
    costs: Costs = field(default_factory=Costs)
    entry_timeout_bars: int = ENTRY_TIMEOUT_BARS
    max_hold_minutes: int = MAX_HOLD_MINUTES
    step_minutes: int = 5
    regimes: tuple[str, ...] = ("RTH",)
    # 한 번에 한 포지션만. 겹치는 신호는 버린다 (실제로도 그렇게 매매한다)
    allow_overlap: bool = False


@dataclass(frozen=True, slots=True)
class Exit:
    at: datetime
    price: float
    portion: float          # 0.5 = 절반 청산
    reason: str             # stop | tp1 | tp2 | breakeven | time | deadline


@dataclass(frozen=True, slots=True)
class Trade:
    setup_id: str
    direction: str
    regime: str
    score: int

    signal_at: datetime
    entry_at: datetime
    entry_ref: float        # 계획상 기준 진입가 (R 의 기준)
    fill_price: float       # 슬리피지 포함 실제 체결가
    stop_loss: float
    tp1: float
    tp2: float
    r_per_unit: float       # |entry_ref − stop_loss|

    exits: tuple[Exit, ...]
    exit_at: datetime
    exit_reason: str

    gross_r: float
    cost_r: float
    net_r: float
    bars_to_tp1: int | None
    hold_minutes: int
    funding_paid_per_unit: float = 0.0

    @property
    def won(self) -> bool:
        return self.net_r > 0

    @property
    def hit_tp1(self) -> bool:
        return self.bars_to_tp1 is not None


@dataclass(frozen=True, slots=True)
class Skipped:
    """신호는 났지만 거래가 안 된 경우. 왜 안 됐는지도 결과의 일부다."""

    at: datetime
    setup_id: str
    reason: str             # no_fill | overlap | no_forward_data
