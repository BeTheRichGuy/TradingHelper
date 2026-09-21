"""Phase 1 세션 게이트 테스트.

docs/05-세션-이벤트-타임맵.md 8절 "테스트로 고정할 것" 을 그대로 옮긴 것.
여기서 깨지면 실전에서 조용히 틀린 시간대에 신호가 나간다.
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from th.sessions import Regime, get_calendar, get_context, get_rules
from th.sessions.market import NY, et_time_on, nth_weekday_of_month
from th.sessions.models import Adjustment, Block, SessionContext
from th.sessions.rules import RuleError, parse_rule

# 평범한 거래일 — 공휴일·FOMC·만기일이 아닌 2026년 9월 9일(수)
NORMAL_DAY = date(2026, 9, 9)
FOMC_DAY = date(2026, 9, 16)          # events.yaml 의 FOMC 성명일
SATURDAY = date(2026, 9, 12)
CHRISTMAS = date(2026, 12, 25)        # 2026년 크리스마스는 금요일


def at_et(day: date, hh: int, mm: int = 0) -> datetime:
    return et_time_on(day, time(hh, mm))


def ctx(day: date, hh: int, mm: int = 0, symbol: str = "SOXLUSDT") -> SessionContext:
    return get_context(symbol, at_et(day, hh, mm))


def block_ids(c: SessionContext) -> set[str]:
    return {b.rule_id for b in c.blocks}


# ─────────────────────────────────────────────────────────
# 1. 레짐 판정
# ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("hh", "mm", "expected"),
    [
        (2, 0, Regime.CLOSED),    # 04:00 이전
        (5, 0, Regime.PRE),
        (9, 29, Regime.PRE),
        (9, 30, Regime.RTH),      # 개장 정각은 본장
        (12, 0, Regime.RTH),
        (15, 59, Regime.RTH),
        (16, 0, Regime.POST),     # 마감 정각은 애프터
        (19, 59, Regime.POST),
        (20, 0, Regime.CLOSED),
        (23, 0, Regime.CLOSED),
    ],
)
def test_regime_boundaries(hh, mm, expected):
    assert ctx(NORMAL_DAY, hh, mm).regime is expected


def test_weekend_is_closed():
    for hh in (5, 12, 18, 23):
        c = ctx(SATURDAY, hh)
        assert c.regime is Regime.CLOSED
        assert c.is_trading_day is False


def test_us_holiday_is_closed():
    for hh in (5, 12, 18):
        assert ctx(CHRISTMAS, hh).regime is Regime.CLOSED


# ─────────────────────────────────────────────────────────
# 2. 서머타임 — 한국에서 보는 개장 시각이 1시간 밀린다
# ─────────────────────────────────────────────────────────

def test_dst_shifts_open_by_exactly_one_hour():
    """2026년 미국 서머타임 시작은 3/8. 그 전후 개장 시각(UTC)이 1시간 차이."""
    cal = get_calendar()
    before = cal.windows(date(2026, 3, 6))   # 금, EST
    after = cal.windows(date(2026, 3, 9))    # 월, EDT
    assert before is not None and after is not None

    assert before.market_open.hour == 14 and before.market_open.minute == 30  # 14:30 UTC
    assert after.market_open.hour == 13 and after.market_open.minute == 30    # 13:30 UTC

    # ET 벽시계로는 둘 다 09:30
    assert before.market_open.astimezone(NY).strftime("%H:%M") == "09:30"
    assert after.market_open.astimezone(NY).strftime("%H:%M") == "09:30"


def test_dst_end_shifts_back():
    """2026년 서머타임 종료는 11/1."""
    cal = get_calendar()
    before = cal.windows(date(2026, 10, 30))  # 금, EDT
    after = cal.windows(date(2026, 11, 2))    # 월, EST
    assert before is not None and after is not None
    assert before.market_open.hour == 13
    assert after.market_open.hour == 14


# ─────────────────────────────────────────────────────────
# 3. 반휴장일 — 마감 기준 창이 자동으로 따라와야 한다
# ─────────────────────────────────────────────────────────

def _day_after_thanksgiving(year: int) -> date:
    """추수감사절(11월 넷째 목) 다음 거래일 = 조기 마감일."""
    thanksgiving = nth_weekday_of_month(year, 11, 3, 4)   # 목=3
    cal = get_calendar()
    d = thanksgiving + timedelta(days=1)
    while not cal.is_trading_day(d):
        d += timedelta(days=1)
    return d


def test_day_after_thanksgiving_is_early_close():
    d = _day_after_thanksgiving(2026)
    w = get_calendar().windows(d)
    assert w is not None
    assert w.early_close is True
    assert w.market_close.astimezone(NY).strftime("%H:%M") == "13:00"
    # 애프터도 4시간 뒤인 17:00 에 끝난다
    assert w.post_end.astimezone(NY).strftime("%H:%M") == "17:00"


def test_early_close_day_is_closed_after_post():
    d = _day_after_thanksgiving(2026)
    assert ctx(d, 12, 0).regime is Regime.RTH
    assert ctx(d, 14, 0).regime is Regime.POST
    assert ctx(d, 18, 0).regime is Regime.CLOSED


def test_etf_rebalance_window_follows_early_close():
    """평소 15:45–16:00 인 3배 ETF 리밸런싱 창이 반휴장일엔 12:45–13:00 으로 이동."""
    normal = ctx(NORMAL_DAY, 15, 50)
    assert "etf_close_rebalance" in block_ids(normal)

    early_day = _day_after_thanksgiving(2026)
    early = ctx(early_day, 12, 50)
    assert "etf_close_rebalance" in block_ids(early)

    # 반휴장일의 15:50 은 이미 애프터라 리밸런싱 창이 아니다
    late = ctx(early_day, 15, 50)
    assert "etf_close_rebalance" not in block_ids(late)
    assert late.regime is Regime.POST


def test_rebalance_rule_only_applies_to_leveraged_etfs():
    assert "etf_close_rebalance" in block_ids(ctx(NORMAL_DAY, 15, 50, "SOXLUSDT"))
    assert "etf_close_rebalance" not in block_ids(ctx(NORMAL_DAY, 15, 50, "NVDAUSDT"))


# ─────────────────────────────────────────────────────────
# 4. ★ 가장 중요 — 차단 구간에서 절대 신호가 나가지 않는다
# ─────────────────────────────────────────────────────────

def test_closed_regime_always_blocks():
    """언더라잉 정지 구간은 예외 없이 차단."""
    cases = [
        (NORMAL_DAY, 2),      # 새벽
        (NORMAL_DAY, 22),     # 밤
        (SATURDAY, 12),       # 주말
        (CHRISTMAS, 12),      # 공휴일
    ]
    for day, hh in cases:
        c = ctx(day, hh)
        assert c.regime is Regime.CLOSED
        assert c.blocked is True, f"{day} {hh}시가 차단되지 않았습니다"
        assert "underlying_closed" in block_ids(c)


def test_opening_15_minutes_blocked():
    assert "opening_15m" in block_ids(ctx(NORMAL_DAY, 9, 35))
    assert "opening_15m" in block_ids(ctx(NORMAL_DAY, 9, 44))
    assert "opening_15m" not in block_ids(ctx(NORMAL_DAY, 9, 46))


def test_econ_release_windows_blocked():
    assert "econ_0830_window" in block_ids(ctx(NORMAL_DAY, 8, 30))
    assert "econ_1000_window" in block_ids(ctx(NORMAL_DAY, 10, 0))
    assert "econ_1000_window" not in block_ids(ctx(NORMAL_DAY, 10, 10))


def test_fomc_day_blocks_statement_and_presser():
    assert ctx(FOMC_DAY, 13, 45).blocked is True
    assert ctx(FOMC_DAY, 14, 45).blocked is True
    # 같은 시각이라도 FOMC 가 아닌 날은 차단되지 않는다
    assert "fomc_statement" not in block_ids(ctx(NORMAL_DAY, 13, 45))


def test_fomc_high_impact_event_window():
    """발표 ±10분은 이벤트 자체로도 차단된다."""
    c = ctx(FOMC_DAY, 14, 5)
    assert c.blocked is True
    assert any(b.rule_id.startswith("event:") for b in c.blocks)


def test_normal_midday_is_not_blocked():
    c = ctx(NORMAL_DAY, 11, 0)
    assert c.regime is Regime.RTH
    assert c.blocked is False, f"차단됨: {block_ids(c)}"


# ─────────────────────────────────────────────────────────
# 5. 신뢰도 가감
# ─────────────────────────────────────────────────────────

def test_lunch_penalty_hits_breakout_only():
    c = ctx(NORMAL_DAY, 12, 0)
    assert c.blocked is False
    assert c.adjustment_for("breakout") == -25
    assert c.adjustment_for("mean_reversion") == 10
    assert c.adjustment_for("trend") == 0


def test_opening_drive_boosts_trend():
    c = ctx(NORMAL_DAY, 9, 50)
    assert c.adjustment_for("trend") == 10
    assert c.adjustment_for("mean_reversion") == 0


def test_power_hour_boosts_trend():
    assert ctx(NORMAL_DAY, 15, 20).adjustment_for("trend") == 10


def test_premarket_and_afterhours_penalise_everything():
    assert ctx(NORMAL_DAY, 7, 0).adjustment_for("trend") == -10
    assert ctx(NORMAL_DAY, 17, 0).adjustment_for("trend") == -15


def test_adjustment_accepts_setup_id_or_category():
    from th.sessions import category_of

    assert category_of("S1_vwap_pullback") == "trend"
    assert category_of("breakout") == "breakout"
    assert category_of(None) is None
    assert category_of("없는셋업") is None


def test_adjustment_for_sums_matching_rules():
    c = SessionContext(
        symbol="X",
        at=datetime.now(UTC),
        regime=Regime.RTH,
        regime_ends_at=None,
        is_trading_day=True,
        early_close=False,
        blocked=False,
        penalties=(Adjustment("a", "PENALTY", -25, ("breakout",), ""),),
        boosts=(Adjustment("b", "BOOST", 10, (), ""),),
    )
    assert c.adjustment_for("breakout") == -15   # -25 + 10
    assert c.adjustment_for("trend") == 10       # 전체 적용 보너스만


# ─────────────────────────────────────────────────────────
# 6. 규칙 파일 자체의 무결성
# ─────────────────────────────────────────────────────────

def test_rules_yaml_loads_and_is_sane():
    rules = get_rules()
    assert len(rules) > 10
    for r in rules:
        assert r.reason_ko, f"{r.id} 에 reason_ko 가 없습니다"
        assert r.action in ("BLOCK", "PENALTY", "BOOST", "WARN")
        if r.action == "PENALTY":
            assert r.points < 0
        if r.action == "BOOST":
            assert r.points > 0


def test_penalty_with_positive_points_is_rejected():
    with pytest.raises(RuleError):
        parse_rule(
            {
                "id": "bad",
                "action": "PENALTY",
                "points": 25,
                "window": {"all_day": True},
                "reason_ko": "x",
            }
        )


def test_unknown_setup_category_is_rejected():
    with pytest.raises(RuleError):
        parse_rule(
            {
                "id": "bad2",
                "action": "BOOST",
                "points": 5,
                "applies_to": ["없는분류"],
                "window": {"all_day": True},
                "reason_ko": "x",
            }
        )


def test_missing_points_is_rejected():
    with pytest.raises(RuleError):
        parse_rule(
            {"id": "bad3", "action": "PENALTY", "window": {"all_day": True}, "reason_ko": "x"}
        )


# ─────────────────────────────────────────────────────────
# 7. 부가 정보
# ─────────────────────────────────────────────────────────

def test_next_event_is_in_the_future():
    c = ctx(NORMAL_DAY, 11, 0)
    assert c.next_event is not None
    assert c.next_event.ts_utc > c.at
    assert c.next_event_in_sec > 0


def test_regime_ends_at_is_next_boundary():
    c = ctx(NORMAL_DAY, 11, 0)
    assert c.regime_ends_at is not None
    # 본장 중이므로 다음 경계는 그날 마감
    w = get_calendar().windows(NORMAL_DAY)
    assert c.regime_ends_at == w.market_close


def test_early_close_warning_is_surfaced():
    d = _day_after_thanksgiving(2026)
    c = ctx(d, 11, 0)
    assert any("조기 마감" in w for w in c.warnings)


def test_blocked_context_still_explains_itself():
    """차단이어도 근거는 비어 있으면 안 된다 — UI가 '왜 지금은 아닌가'를 보여줘야 한다."""
    c = ctx(SATURDAY, 12)
    assert c.blocked
    assert c.reasons_ko()
    assert all(text.strip() for text in c.reasons_ko())


# ── 캘린더 자동 동기화 (Phase 1.5 에서 이월된 항목) ──────
# 손으로 `th calendar sync` 를 치는 방식은 언젠가 반드시 빠뜨리고,
# 빠뜨린 날이 하필 NVDA 실적일이다. 그래서 `th stream` 안에서 하루 1회 돈다.

def test_캘린더_자동동기화는_하루에_한_번만_돈다():
    import asyncio

    from th.sessions import sync

    calls = []

    async def fake():
        # 실제 SyncReport 를 돌려준다 — 가짜 속성(.events)으로 흉내 내면
        # 로그 한 줄이 TypeError 를 내는 것을 테스트가 가려 버린다
        calls.append(1)
        now = datetime.now(UTC)
        return sync.SyncReport(started_at=now, finished_at=now, total_events=3)

    import logging

    class Catch(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    catch = Catch()
    logger = logging.getLogger(sync.__name__)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(catch)
    real = sync.sync_calendar
    sync.sync_calendar = fake
    try:
        async def main():
            ev = asyncio.Event()
            t = asyncio.create_task(
                sync.auto_sync_loop(ev, hour_utc=0, check_every_s=0.02)
            )
            await asyncio.sleep(0.15)
            ev.set()
            await t

        asyncio.run(main())
    finally:
        sync.sync_calendar = real
        logger.removeHandler(catch)
        logger.setLevel(old_level)
    assert len(calls) == 1, f"하루 한 번만 돌아야 하는데 {len(calls)}번 돌았다"
    # 성공 경로에서 예외 로그(실패로 위장)가 남으면 안 된다
    errors = [r for r in catch.records if r.levelno >= logging.ERROR]
    assert not errors, f"동기화가 성공했는데 실패로 기록됐다: {errors[0].getMessage()}"
    assert any("이벤트 3건" in r.getMessage() for r in catch.records)


def test_캘린더_동기화가_실패해도_수집이_멈추지_않는다():
    """무료 소스는 예고 없이 죽는다. 그때 수집기까지 멈추면 안 된다."""
    import asyncio

    from th.sessions import sync

    calls = []

    async def boom():
        calls.append(1)
        raise RuntimeError("FRED 죽음")

    real = sync.sync_calendar
    sync.sync_calendar = boom
    try:
        async def main():
            ev = asyncio.Event()
            t = asyncio.create_task(
                sync.auto_sync_loop(ev, hour_utc=0, check_every_s=0.02)
            )
            await asyncio.sleep(0.15)
            ev.set()
            await t          # 예외가 밖으로 나오면 여기서 실패한다

        asyncio.run(main())
    finally:
        sync.sync_calendar = real
    assert len(calls) == 1, "실패한 날 무한 재시도하면 안 된다"


def test_stream_이_캘린더_동기화를_켠다():
    """배선을 빠뜨리면 자동화가 '있는데 안 도는' 상태가 된다."""
    import pathlib

    cli = (pathlib.Path(__file__).resolve().parent.parent
           / "src" / "th" / "cli.py").read_text(encoding="utf-8")
    body = cli.split("def stream(")[1].split("@app.command")[0]
    assert "auto_sync_loop" in body, "th stream 이 캘린더 자동 동기화를 켜지 않는다"


# ── events.yaml 파싱 — 따옴표 없는 time_et ────────────────
def test_unquoted_time_et_parses_as_sexagesimal_int():
    """YAML 은 `time_et: 14:00` 을 60진수 정수 840 으로 읽는다.
    그걸 문자열 split 하면 EventError 라 캘린더 **전체**가 사라졌다."""
    from th.sessions.events import _parse_event

    e = _parse_event({"name": "FOMC", "date": "2026-09-16", "time_et": 840,
                      "kind": "fomc", "importance": "high"})
    assert e.ts_utc.astimezone(NY).hour == 14 and e.ts_utc.astimezone(NY).minute == 0
    quoted = _parse_event({"name": "FOMC", "date": "2026-09-16", "time_et": "14:00",
                           "kind": "fomc", "importance": "high"})
    assert e.ts_utc == quoted.ts_utc


def test_load_manual_skips_a_bad_entry_instead_of_dropping_the_whole_file():
    """한 줄 오타로 나머지 FOMC·실적 일정까지 게이트에서 사라지면 안 된다."""
    import tempfile
    from pathlib import Path

    from th.sessions.events import load_manual

    text = (
        "events:\n"
        "  - {name: CPI, date: 2026-09-10, time_et: '08:30', kind: econ, importance: high}\n"
        "  - {name: 깨진것, date: 2026-09-11, kind: 없는종류}\n"
        "  - {name: FOMC, date: 2026-09-16, time_et: 14:00, kind: fomc, importance: high}\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "events.yaml"
        p.write_text(text, encoding="utf-8")
        evs = load_manual(p)
    assert [e.name for e in evs] == ["CPI", "FOMC"], evs
