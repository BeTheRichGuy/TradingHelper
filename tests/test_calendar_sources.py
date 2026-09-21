"""Phase 1.5 — 이벤트 자동 수집 테스트.

네트워크를 타지 않는 부분만 검증한다. 실제 FRED·yfinance 호출은
`th calendar sync` 로 확인한다.

여기서 가장 중요한 것: **손으로 적은 항목이 자동 수집에 절대 덮이지 않는다.**
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from th.sessions.events import (
    EventStore,
    SourceStatus,
    event_to_dict,
    freshness_warnings,
    load_generated,
    parse_note,
    write_generated,
)
from th.sessions.market import NY, et_time_on
from th.sessions.models import Event
from th.sessions.sources.earnings import _normalize, _to_datetimes
from th.sessions.sources.fred import FredSource, ReleaseRule, load_whitelist
from th.sessions.sources.merge import (
    dedup_key,
    merge_events,
    normalize_name,
    same_event,
    stable_id,
)

D = date(2026, 9, 10)


def ev(name, source, kind="econ", hh=8, mm=30, symbols=(), note="") -> Event:
    return Event(
        name=name,
        ts_utc=et_time_on(D, time(hh, mm)),
        kind=kind,
        importance="high",
        symbols=tuple(symbols),
        note=note,
        source=source,
    )


# ─────────────────────────────────────────────────────────
# 병합 — 우선순위
# ─────────────────────────────────────────────────────────

def test_manual_beats_automatic_sources():
    """★ 손으로 적은 것은 자동 수집에 덮이지 않는다."""
    merged = merge_events(
        [
            ev("미국 CPI (소비자물가)", "fred"),
            ev("미국 CPI", "manual"),
        ]
    )
    assert len(merged) == 1
    assert merged[0].source == "manual"
    assert merged[0].name == "미국 CPI"


def test_manual_beats_automatic_regardless_of_order():
    for order in ([("manual", "fred")], [("fred", "manual")]):
        a, b = order[0]
        merged = merge_events([ev("미국 CPI", a), ev("미국 CPI (소비자물가)", b)])
        assert len(merged) == 1
        assert merged[0].source == "manual"


def test_fred_beats_yfinance():
    merged = merge_events([ev("X 지표", "yfinance"), ev("X 지표", "fred")])
    assert len(merged) == 1
    assert merged[0].source == "fred"


def test_different_events_same_day_are_kept():
    merged = merge_events([ev("미국 CPI", "fred"), ev("미국 PPI", "fred")])
    assert len(merged) == 2


def test_earnings_dedup_by_symbol_not_name():
    a = ev("NVDA 실적", "manual", kind="earnings", hh=16, mm=20, symbols=("NVDA",))
    b = ev("NVIDIA Q3 Earnings", "yfinance", kind="earnings", hh=16, mm=0, symbols=("NVDA",))
    merged = merge_events([a, b])
    assert len(merged) == 1
    assert merged[0].source == "manual"


def test_different_symbols_are_separate():
    a = ev("NVDA 실적", "yfinance", kind="earnings", symbols=("NVDA",))
    b = ev("AMD 실적", "yfinance", kind="earnings", symbols=("AMD",))
    assert len(merge_events([a, b])) == 2


def test_events_on_different_days_are_separate():
    a = ev("미국 CPI", "fred")
    b = Event(
        name="미국 CPI",
        ts_utc=et_time_on(date(2026, 10, 13), time(8, 30)),
        kind="econ",
        source="fred",
    )
    assert len(merge_events([a, b])) == 2


def test_merged_output_is_time_sorted():
    merged = merge_events(
        [ev("늦은 것", "fred", hh=10), ev("이른 것", "fred", hh=8, mm=30)]
    )
    assert [e.name for e in merged] == ["이른 것", "늦은 것"]


def test_normalize_and_same_event():
    assert normalize_name("미국 CPI (소비자물가)") == "미국cpi소비자물가"
    assert same_event(ev("미국 CPI", "manual"), ev("미국 CPI (소비자물가)", "fred"))
    assert not same_event(ev("미국 CPI", "manual"), ev("미국 PPI", "fred"))


def test_stable_id_is_deterministic_and_distinct():
    a = ev("미국 CPI", "manual")
    b = ev("미국 CPI (소비자물가)", "fred")
    assert stable_id(a) == stable_id(a)
    assert stable_id(a) != stable_id(ev("미국 PPI", "fred"))
    # 이름이 달라도 같은 사건이면 dedup_key 로 걸러진 뒤 하나만 남는다
    assert dedup_key(a) != dedup_key(b)          # 1차 키는 다르지만
    assert len(merge_events([a, b])) == 1        # 2차 판정에서 합쳐진다


# ─────────────────────────────────────────────────────────
# FRED 소스 (네트워크 없이)
# ─────────────────────────────────────────────────────────

def test_whitelist_loads():
    rules = load_whitelist()
    assert len(rules) >= 8
    labels = [r.label for r in rules]
    assert any("CPI" in x for x in labels)
    assert any("고용" in x for x in labels)
    for r in rules:
        assert r.match == r.match.lower()
        assert r.importance in ("high", "medium", "low")


def test_fred_matches_release_names_case_insensitively():
    src = FredSource(api_key="", whitelist=load_whitelist())
    assert src._match("Consumer Price Index") is not None
    assert src._match("CONSUMER PRICE INDEX") is not None
    assert src._match("Employment Situation") is not None
    assert src._match("Beige Book") is None


def test_fred_converts_rows_to_events_with_mapped_times():
    """FRED 는 시각을 주지 않는다 — 매핑표의 time_et 가 적용되어야 한다."""
    src = FredSource(
        api_key="",
        whitelist=[
            ReleaseRule("consumer price index", "미국 CPI", time(8, 30), "high"),
            ReleaseRule("job openings", "JOLTS", time(10, 0), "medium"),
        ],
    )
    rows = [
        {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-09-10"},
        {"release_id": 192, "release_name": "Job Openings and Labor Turnover Survey",
         "date": "2026-09-09"},
        {"release_id": 999, "release_name": "Beige Book", "date": "2026-09-10"},
    ]
    events = src._to_events(rows, date(2026, 9, 1), date(2026, 9, 30))

    assert len(events) == 2                       # Beige Book 은 화이트리스트 밖
    by_name = {e.name: e for e in events}
    assert f"{by_name['미국 CPI'].ts_utc.astimezone(NY):%H:%M}" == "08:30"
    assert f"{by_name['JOLTS'].ts_utc.astimezone(NY):%H:%M}" == "10:00"
    assert by_name["미국 CPI"].importance == "high"
    assert all(e.source == "fred" for e in events)
    assert all(e.time_estimated for e in events)  # 시각은 추정값임을 표시


def test_fred_filters_out_of_range_dates():
    src = FredSource(
        api_key="",
        whitelist=[ReleaseRule("consumer price index", "CPI", time(8, 30), "high")],
    )
    rows = [
        {"release_name": "Consumer Price Index", "date": "2026-01-13"},
        {"release_name": "Consumer Price Index", "date": "2026-09-10"},
    ]
    events = src._to_events(rows, date(2026, 9, 1), date(2026, 9, 30))
    assert len(events) == 1


@pytest.mark.asyncio
async def test_fred_without_key_fails_gracefully():
    """키가 없어도 예외를 던지지 않고 결과로 알린다 — 게이트가 죽으면 안 된다."""
    res = await FredSource(api_key="").fetch(date(2026, 9, 1), date(2026, 9, 30))
    assert res.ok is False
    assert res.events == []
    assert "TH_FRED_API_KEY" in res.detail


# ─────────────────────────────────────────────────────────
# 실적 소스 (네트워크 없이)
# ─────────────────────────────────────────────────────────

def test_midnight_timestamp_becomes_after_close_estimate():
    """소스가 날짜만 주면 장 마감 후(16:20 ET)로 가정하고 추정 표시."""
    naive_midnight = datetime(2026, 11, 18, 0, 0)
    ts, estimated = _normalize(naive_midnight)
    assert estimated is True
    assert f"{ts.astimezone(NY):%H:%M}" == "16:20"


def test_real_timestamp_is_kept():
    aware = datetime(2026, 11, 18, 16, 5, tzinfo=NY)
    ts, estimated = _normalize(aware)
    assert estimated is False
    assert f"{ts.astimezone(NY):%H:%M}" == "16:05"


def test_to_datetimes_handles_dict_and_list():
    assert len(_to_datetimes({"Earnings Date": [date(2026, 11, 18)]})) == 1
    assert len(_to_datetimes([date(2026, 11, 18), date(2027, 2, 20)])) == 2
    assert _to_datetimes(None) == []
    assert _to_datetimes({}) == []


# ─────────────────────────────────────────────────────────
# 스냅샷 저장/로드
# ─────────────────────────────────────────────────────────

def test_generated_snapshot_roundtrip(tmp_path):
    events = [
        ev("미국 CPI", "fred"),
        ev("NVDA 실적", "yfinance", kind="earnings", hh=16, mm=20,
           symbols=("NVDA",), note="warn_days_before=3,time_estimated"),
    ]
    statuses = [
        SourceStatus("fred", True, 1, "릴리스 300건 조회"),
        SourceStatus("yfinance", True, 1, "10/10 종목 조회"),
    ]
    p = tmp_path / "events.generated.yaml"
    write_generated(events, statuses, path=p)

    store = load_generated(p)
    assert store.has_generated is True
    assert store.synced_at is not None
    assert len(store.events) == 2
    assert {s.name for s in store.sources} == {"fred", "yfinance"}

    nvda = [e for e in store.events if e.kind == "earnings"][0]
    assert nvda.symbols == ("NVDA",)
    assert nvda.source == "yfinance"
    assert nvda.time_estimated is True
    assert parse_note(nvda.note)["warn_days_before"] == "3"


def test_event_to_dict_uses_eastern_time():
    d = event_to_dict(ev("미국 CPI", "fred", hh=8, mm=30))
    assert d["date"] == "2026-09-10"
    assert d["time_et"] == "08:30"


def test_parse_note():
    assert parse_note("warn_days_before=3,time_estimated") == {
        "warn_days_before": "3",
        "time_estimated": "",
    }
    assert parse_note("") == {}


# ─────────────────────────────────────────────────────────
# 신선도 경고 — 낡은 일정으로 조용히 도는 것을 막는다
# ─────────────────────────────────────────────────────────

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_warns_when_never_synced():
    store = EventStore(events=(), synced_at=None, has_generated=False)
    w = freshness_warnings(NOW, store)
    assert any("아직 실행하지 않았습니다" in x for x in w)


def test_warns_when_stale():
    store = EventStore(
        events=(), synced_at=NOW - timedelta(hours=30), has_generated=True
    )
    w = freshness_warnings(NOW, store)
    assert any("30시간 전" in x for x in w)


def test_no_warning_when_fresh():
    store = EventStore(
        events=(),
        synced_at=NOW - timedelta(hours=2),
        has_generated=True,
        sources=(SourceStatus("fred", True, 5, ""),),
    )
    assert freshness_warnings(NOW, store) == []


def test_warns_when_a_source_failed():
    store = EventStore(
        events=(),
        synced_at=NOW - timedelta(minutes=5),
        has_generated=True,
        sources=(SourceStatus("fred", False, 0, "HTTP 403"),),
    )
    w = freshness_warnings(NOW, store)
    assert any("수집 실패: fred" in x for x in w)
