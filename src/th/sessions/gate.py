"""세션·이벤트 게이트 — 전략 엔진이 셋업을 평가하기 **전에** 호출하는 유일한 입구.

    ctx = get_context("SOXLUSDT")
    if ctx.blocked:
        return no_signal(ctx)          # 여기서 끝. 셋업 계산을 시작하지 않는다.

계산해놓고 나중에 가리는 구조로 만들면 언젠가 UI 어딘가로 새어 나간다.
그래서 이 함수는 '평가해도 되는가'를 먼저 답하도록 설계되어 있다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from ..logging_setup import get_logger
from . import events as ev
from .market import NY, SessionCalendar, get_calendar
from .models import Adjustment, Block, Regime, SessionContext
from .rules import Rule, get_rules

log = get_logger(__name__)


def get_context(
    symbol: str = "SOXLUSDT",
    at: datetime | None = None,
    *,
    calendar: SessionCalendar | None = None,
    rules: tuple[Rule, ...] | None = None,
    events: tuple[ev.Event, ...] | None = None,
) -> SessionContext:
    """지금(또는 지정 시각) 이 심볼의 세션 상태."""
    at = (at or datetime.now(UTC)).astimezone(UTC)
    cal = calendar or get_calendar()
    rules = rules if rules is not None else get_rules()

    # 이벤트를 명시적으로 넘기면(테스트 등) 신선도 경고는 붙이지 않는다.
    store = None
    if events is None:
        store = ev.get_store()
        events = store.events

    regime, day = cal.regime_at(at)
    day_et = at.astimezone(NY).date()
    is_trading_day = cal.is_trading_day(day_et)
    early_close = bool(day and day.early_close)

    blocks: list[Block] = []
    penalties: list[Adjustment] = []
    boosts: list[Adjustment] = []
    warnings: list[str] = []

    # ── 1. 언더라잉 정지 — 다른 어떤 규칙보다 먼저 ────────────
    if regime is Regime.CLOSED:
        blocks.append(
            Block(
                rule_id="underlying_closed",
                reason_ko=(
                    "미국 본주가 거래되지 않는 시간입니다. perp 단독 가격이라 "
                    "호가가 얇고 지지·저항이 지켜지지 않습니다 — 분석 신뢰 불가"
                ),
            )
        )

    # ── 2. rules.yaml ────────────────────────────────────────
    if day is not None:
        kinds_today = ev.kinds_on(day_et, events)
        for r in rules:
            if not r.symbol_matches(symbol):
                continue
            if not r.session_matches(regime):
                continue
            if not r.day_matches(day_et, cal, kinds_today):
                continue
            if not r.time_matches(at, day):
                continue

            if r.action == "BLOCK":
                blocks.append(Block(rule_id=r.id, reason_ko=r.reason_ko))
            elif r.action == "PENALTY":
                penalties.append(
                    Adjustment(r.id, "PENALTY", r.points, r.applies_to, r.reason_ko)
                )
            elif r.action == "BOOST":
                boosts.append(
                    Adjustment(r.id, "BOOST", r.points, r.applies_to, r.reason_ko)
                )
            else:  # WARN
                warnings.append(r.reason_ko)

    # ── 3. 고중요도 이벤트 전후 ───────────────────────────────
    blocks.extend(ev.high_impact_blocks(at, events))

    # ── 4. 배너 경고 ─────────────────────────────────────────
    warnings.extend(ev.upcoming_warnings(at, events))
    if early_close and regime in (Regime.PRE, Regime.RTH):
        warnings.append("오늘은 조기 마감일입니다 (13:00 ET) — 애프터도 일찍 끝납니다")
    if store is not None:
        # 낡은 일정으로 조용히 도는 것이 이벤트가 없는 것보다 위험하다
        warnings.extend(ev.freshness_warnings(at, store))

    return SessionContext(
        symbol=symbol.upper(),
        at=at,
        regime=regime,
        regime_ends_at=cal.next_boundary(at),
        is_trading_day=is_trading_day,
        early_close=early_close,
        blocked=bool(blocks),
        blocks=tuple(blocks),
        penalties=tuple(penalties),
        boosts=tuple(boosts),
        next_event=ev.next_event_after(at, events),
        warnings=tuple(dict.fromkeys(warnings)),   # 순서 유지 중복 제거
    )


def day_timeline(
    day_et: date,
    symbol: str = "SOXLUSDT",
    step_min: int = 5,
) -> list[tuple[datetime, SessionContext]]:
    """하루를 step_min 간격으로 훑어 레짐·차단 구간을 만든다. CLI 표시용."""
    from datetime import time as _time
    from datetime import timedelta

    from .market import et_time_on

    cal = get_calendar()
    rules = get_rules()
    events = ev.get_events()

    start = et_time_on(day_et, _time(0, 0))
    out: list[tuple[datetime, SessionContext]] = []
    for i in range(0, 24 * 60, step_min):
        t = start + timedelta(minutes=i)
        out.append(
            (t, get_context(symbol, t, calendar=cal, rules=rules, events=events))
        )
    return out


def compress_timeline(
    tl: list[tuple[datetime, SessionContext]],
) -> list[tuple[datetime, datetime, SessionContext]]:
    """같은 상태가 이어지는 구간을 하나로 묶는다."""
    if not tl:
        return []

    def key(c: SessionContext):
        return (
            c.regime,
            c.blocked,
            tuple(b.rule_id for b in c.blocks),
            tuple(a.rule_id for a in c.penalties),
            tuple(a.rule_id for a in c.boosts),
        )

    out: list[tuple[datetime, datetime, SessionContext]] = []
    seg_start, seg_ctx = tl[0]
    for t, c in tl[1:]:
        if key(c) != key(seg_ctx):
            out.append((seg_start, t, seg_ctx))
            seg_start, seg_ctx = t, c
    out.append((seg_start, tl[-1][0], seg_ctx))
    return out
