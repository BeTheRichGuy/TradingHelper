"""백테스트 (Phase 4).

    from th.backtest.runner import run_backtest     # DB 를 읽는 진입점
    from th.backtest.simulate import simulate_trade # 순수 함수

주의: 이 `__init__` 은 순수 계산만 노출한다. `.runner` 는 DB·세션 캘린더를
끌어오므로 여기서 임포트하지 않는다 (Phase 1.5·2·3 에서 반복해서 겪은 문제다).
체결·지표·성과 계산은 네트워크도 DB도 없이 임포트·테스트되어야 한다.
"""

from .metrics import (
    Gate,
    Stats,
    equity_curve,
    evaluate_gates,
    group_by,
    histogram,
    max_drawdown_r,
    max_loss_streak,
    quartiles,
    summarize,
    wilson_ci,
)
from .models import BacktestConfig, Costs, Exit, Skipped, Trade
from .simulate import find_fill, simulate_trade
from .walkforward import Fold, split_in_out, walk_forward

__all__ = [
    "BacktestConfig",
    "Costs",
    "Exit",
    "Fold",
    "Gate",
    "Skipped",
    "Stats",
    "Trade",
    "equity_curve",
    "evaluate_gates",
    "find_fill",
    "group_by",
    "histogram",
    "max_drawdown_r",
    "max_loss_streak",
    "quartiles",
    "simulate_trade",
    "split_in_out",
    "summarize",
    "walk_forward",
    "wilson_ci",
]
