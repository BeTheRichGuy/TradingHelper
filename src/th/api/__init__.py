"""웹 API (Phase 5).

    from th.api.schemas import NoTradeMessage      # 순수 스키마
    from th.api.messages import build_no_trade     # 순수 변환
    from th.api.app import create_app              # FastAPI (선택 의존성)

이 `__init__` 은 스키마와 순수 변환만 노출한다. `.app` 은 fastapi 를 끌어오므로
여기서 임포트하지 않는다 — Phase 1.5·2·3 에서 반복해서 겪은 문제다.
"""

from .schemas import (
    ALERT_CODES,
    FORBIDDEN_TYPES,
    NO_PERMISSION_NOTE,
    OUTBOUND_TYPES,
    AlertMessage,
    CoverageItem,
    CoverageMessage,
    NoTradeMessage,
    NoTradeReason,
    RiskMessage,
    SessionMessage,
    SnapshotMessage,
)

__all__ = [
    "ALERT_CODES",
    "FORBIDDEN_TYPES",
    "NO_PERMISSION_NOTE",
    "OUTBOUND_TYPES",
    "AlertMessage",
    "CoverageItem",
    "CoverageMessage",
    "NoTradeMessage",
    "NoTradeReason",
    "RiskMessage",
    "SessionMessage",
    "SnapshotMessage",
]
