"""세션·이벤트 게이트 (Phase 1).

전략 엔진은 셋업을 평가하기 전에 반드시 여기를 먼저 거친다.

    from th.sessions import get_context

    ctx = get_context("SOXLUSDT")
    if ctx.blocked:
        return no_signal(reasons=ctx.reasons_ko())
    score += ctx.adjustment_for("trend")

설계 근거: docs/05-세션-이벤트-타임맵.md
"""

from .events import (
    EventStore,
    SourceStatus,
    clear_cache,
    freshness_warnings,
    get_events,
    get_store,
    load_events,
    upcoming,
)
from .gate import compress_timeline, day_timeline, get_context
from .market import NY, SessionCalendar, get_calendar
from .models import Adjustment, Block, Event, Regime, SessionContext
from .rules import SETUP_CATEGORIES, category_of, get_rules, load_rules

# 주의: `.sync` 는 여기서 임포트하지 않는다. 그쪽은 DB 드라이버(asyncpg)를 끌어오는데,
# 세션 게이트는 DB 없이도 임포트되어야 하기 때문이다.
#     from th.sessions.sync import sync_calendar   ← 필요할 때만

__all__ = [
    "NY",
    "Adjustment",
    "Block",
    "Event",
    "EventStore",
    "Regime",
    "SessionCalendar",
    "SessionContext",
    "SETUP_CATEGORIES",
    "SourceStatus",
    "category_of",
    "clear_cache",
    "compress_timeline",
    "day_timeline",
    "freshness_warnings",
    "get_calendar",
    "get_context",
    "get_events",
    "get_rules",
    "get_store",
    "load_events",
    "load_rules",
    "upcoming",
]
