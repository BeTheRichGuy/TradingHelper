"""계정 연동 · 리스크 엔진 (Phase 2).

읽기 전용이다. 이 패키지에는 주문을 내는 코드가 없고, 앞으로도 없다.

    from th.account.service import get_risk_view

    view = await get_risk_view("SOXLUSDT", session_ctx)
    for a in view.alerts:
        print(a.icon, a.message_ko)

주의: 이 패키지 `__init__` 은 **순수 계산(models·risk·alerts)만** 노출한다.
`.client` 와 `.service` 는 httpx·asyncpg 를 끌어오므로 여기서 임포트하지 않는다.
리스크 계산은 네트워크도 DB도 없이 임포트되고 테스트되어야 하기 때문이다.

    from th.account.client import AccountClient    ← 계정 조회가 필요할 때만
    from th.account.service import get_risk_view

설계 근거: docs/02-전략엔진-명세.md 5절, docs/03-아키텍처-API스펙.md 5.3
"""

from .alerts import Alert, MarketStats, build_alerts, worst_level
from .models import (
    FLAT,
    LONG,
    SHORT,
    Balance,
    DailyPnl,
    Discipline,
    OpenOrder,
    Position,
    RiskSnapshot,
)
from .risk import (
    DAILY_LOSS_LIMIT_R,
    DRAWDOWN_LIMIT_PCT,
    EXPOSURE_LIMIT_X,
    LEVERAGED_ETF_MULTIPLIER,
    LIQ_CRITICAL_PCT,
    LIQ_WARNING_PCT,
    SYMBOL_LIMIT_X,
    account_leverage,
    build_snapshot,
    find_stop_orders,
    kst_today,
    liquidation_distance_pct,
    multiplier_for,
    r_unit,
    stop_distance_pct,
    underlying_move_to_liq_pct,
)

__all__ = [
    "DAILY_LOSS_LIMIT_R",
    "DRAWDOWN_LIMIT_PCT",
    "EXPOSURE_LIMIT_X",
    "SYMBOL_LIMIT_X",
    "Discipline",
    "FLAT",
    "LEVERAGED_ETF_MULTIPLIER",
    "LIQ_CRITICAL_PCT",
    "LIQ_WARNING_PCT",
    "LONG",
    "SHORT",
    "Alert",
    "Balance",
    "DailyPnl",
    "MarketStats",
    "OpenOrder",
    "Position",
    "RiskSnapshot",
    "account_leverage",
    "build_alerts",
    "build_snapshot",
    "find_stop_orders",
    "kst_today",
    "liquidation_distance_pct",
    "multiplier_for",
    "r_unit",
    "stop_distance_pct",
    "underlying_move_to_liq_pct",
    "worst_level",
]
