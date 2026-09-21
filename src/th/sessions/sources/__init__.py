"""이벤트 소스들 (Phase 1.5).

    manual (events.yaml)  ─┐
    fred   (경제지표)      ├─▶ merge_events() ─▶ events.generated.yaml + DB
    yfinance (실적일)      ─┘                     └▶ 게이트가 읽는다

우선순위는 manual > fred > finnhub > yfinance. 손으로 적은 것은 절대 덮이지 않는다.
"""

from .base import SOURCE_PRIORITY, EventSource, FetchResult, priority_of
from .earnings import DEFAULT_SYMBOLS, EarningsSource
from .fred import FredSource, ReleaseRule, load_whitelist
from .merge import dedup_key, merge_events, normalize_name, stable_id

__all__ = [
    "DEFAULT_SYMBOLS",
    "SOURCE_PRIORITY",
    "EarningsSource",
    "EventSource",
    "FetchResult",
    "FredSource",
    "ReleaseRule",
    "dedup_key",
    "load_whitelist",
    "merge_events",
    "normalize_name",
    "priority_of",
    "stable_id",
]
