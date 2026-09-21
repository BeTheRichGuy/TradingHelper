"""데이터 커버리지 점검 — 이번 프로젝트 최대 버그의 재발 방지.

'조용히 비어 있는 것'이 가장 위험하다. OI·펀딩·basis 가 백테스트 구간에 아예
없는데 조건은 그대로 채점돼, S1 의 선택 가중치 115점 중 35점이 자동 소멸했다.
`th doctor` 가 매번 이걸 보여주도록 했고, 여기서 그 판정을 고정한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from th.ingest.coverage import (
    SourceCoverage,
    backtest_span_days,
    warnings_for,
)

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def cov(name, rows, days_back, days_span, depends="어떤 조건") -> SourceCoverage:
    if rows == 0:
        return SourceCoverage(name, 0, None, None, depends)
    last = NOW - timedelta(days=days_back)
    return SourceCoverage(name, rows, last - timedelta(days=days_span), last, depends)


def realistic() -> list[SourceCoverage]:
    """2026-09-07 시점에 실제로 있던 모양."""
    return [
        cov("1분봉", 165_646, 0, 115),
        cov("미결제약정", 118, 0, 0, "ΔOI 조건"),
        cov("펀딩", 21, 0, 7, "펀딩 쏠림 조건"),
        cov("호가 스프레드", 4_413, 0, 0, "스프레드 필터"),
        cov("본주·지수 가격", 4_409, 3, 4, "basis 조건"),
    ]


def test_span_comes_from_the_bars():
    assert backtest_span_days(realistic()) == pytest.approx(115.0)


def test_span_is_zero_without_bars():
    assert backtest_span_days([]) == 0.0
    assert backtest_span_days([cov("1분봉", 0, 0, 0)]) == 0.0


def test_the_real_situation_produces_warnings_for_everything_but_bars():
    ws = warnings_for(realistic())
    assert len(ws) == 4
    assert all("1분봉" not in w for w in ws)


def test_completely_missing_source_says_always_unmet():
    ws = warnings_for([cov("1분봉", 100, 0, 100), cov("미결제약정", 0, 0, 0, "ΔOI 조건")])
    assert any("전혀 없습니다" in w and "ΔOI 조건" in w for w in ws)


def test_thin_source_says_which_period_dies():
    ws = warnings_for([cov("1분봉", 100, 0, 100), cov("펀딩", 50, 0, 7, "펀딩 조건")])
    assert any("그 이전 구간" in w for w in ws)


def test_a_full_history_produces_no_warning():
    full = [cov("1분봉", 100, 0, 100), cov("미결제약정", 9_000, 0, 98)]
    assert warnings_for(full) == []


def test_status_labels():
    span = 100.0
    assert cov("x", 0, 0, 0).status(span) == "없음"
    assert cov("x", 10, 0, 5).status(span) == "거의 없음"
    assert cov("x", 10, 0, 50).status(span) == "부분"
    assert cov("x", 10, 0, 95).status(span) == "충분"


def test_fraction_never_exceeds_one():
    """다른 소스가 봉보다 길 수도 있다 (일봉은 10년치)."""
    assert cov("x", 10, 0, 3650).fraction_of(100.0) == 1.0


def test_fraction_is_zero_without_a_span():
    assert cov("x", 10, 0, 5).fraction_of(0.0) == 0.0


def test_stale_minutes_detects_a_dead_collector():
    c = cov("1분봉", 100, 0, 100)
    assert c.stale_minutes(NOW) == pytest.approx(0.0, abs=1.0)
    old = cov("1분봉", 100, 1, 100)
    assert old.stale_minutes(NOW) == pytest.approx(1440.0, abs=1.0)
    assert cov("x", 0, 0, 0).stale_minutes(NOW) is None


def test_missing_source_is_not_silently_fine():
    """이 테스트가 이 파일의 존재 이유다 — 비어 있음이 조용히 지나가면 안 된다."""
    empty_everything = [
        cov("1분봉", 165_646, 0, 115),
        cov("미결제약정", 0, 0, 0, "ΔOI"),
        cov("펀딩", 0, 0, 0, "펀딩"),
        cov("호가 스프레드", 0, 0, 0, "스프레드"),
        cov("본주·지수 가격", 0, 0, 0, "basis"),
    ]
    ws = warnings_for(empty_everything)
    assert len(ws) == 4, "빈 소스가 하나라도 조용히 넘어갔습니다"
