"""전략 엔진 (Phase 3).

    from th.strategy.engine import analyze          # DB 를 읽는 진입점
    from th.strategy.engine import analyze_features # 순수 함수 (백테스트·테스트)

평가 순서는 02 문서 그대로다.
    세션 게이트 → 시장 필터 → 셋업 → 리스크 → 점수 → 발행 판정

주의: 이 `__init__` 은 순수 계산만 노출한다. `.engine` 과 `.data` 는 DB·계정을
끌어오므로 여기서 임포트하지 않는다 (Phase 1.5·2 에서 겪은 문제와 같다).
지표·리스크 계산은 네트워크도 DB도 없이 임포트·테스트되어야 한다.
"""

from .features import Features, Levels, PerpContext, build_features, build_levels
from .indicators import (
    Bar,
    VwapPoint,
    VwmaStack,
    atr,
    cumulative_delta,
    ema,
    last_swing_high,
    last_swing_low,
    percentile_rank,
    resample,
    rma,
    rsi,
    session_vwap,
    sma,
    vwma,
    swing_highs,
    swing_lows,
)
from .risk import TakeProfit, TradePlan, build_plan, position_size, stop_loss_price
from .scoring import Score, oi_reading, score_setup
from .setups import (
    Condition,
    EntryZone,
    S1VwapPullback,
    S3SweepReversal,
    S5VwmaPullback,
    SetupResult,
)

__all__ = [
    "Bar",
    "Condition",
    "EntryZone",
    "Features",
    "Levels",
    "PerpContext",
    "S1VwapPullback",
    "S3SweepReversal",
    "S5VwmaPullback",
    "Score",
    "SetupResult",
    "TakeProfit",
    "TradePlan",
    "VwapPoint",
    "VwmaStack",
    "atr",
    "build_features",
    "build_levels",
    "build_plan",
    "cumulative_delta",
    "ema",
    "last_swing_high",
    "last_swing_low",
    "oi_reading",
    "percentile_rank",
    "position_size",
    "resample",
    "rma",
    "rsi",
    "score_setup",
    "session_vwap",
    "sma",
    "stop_loss_price",
    "swing_highs",
    "swing_lows",
    "vwma",
]
