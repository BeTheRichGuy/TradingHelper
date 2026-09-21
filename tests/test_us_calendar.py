"""내장 NYSE 달력 — 알려진 날짜로 고정한다.

`pandas_market_calendars` 없이도 세션 게이트가 돌아야 검증이 특정 기계에 묶이지
않는다. 다만 손으로 옮긴 규칙이므로 **틀리면 조용히 틀린다.** 그래서 공표된
휴장일·반휴장일을 그대로 박아 둔다.
"""

from __future__ import annotations

from datetime import date

import pytest

from th.sessions.us_calendar import (
    early_closes,
    easter,
    holidays,
    is_early_close,
    is_trading_day,
    nth_weekday,
    observed,
    trading_days,
)

# NYSE 가 공표한 날짜 그대로
KNOWN_HOLIDAYS = {
    2024: ["01-01", "01-15", "02-19", "03-29", "05-27", "06-19", "07-04",
           "09-02", "11-28", "12-25"],
    2025: ["01-01", "01-20", "02-17", "04-18", "05-26", "06-19", "07-04",
           "09-01", "11-27", "12-25"],
    2026: ["01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03",
           "09-07", "11-26", "12-25"],
}

KNOWN_EARLY = {
    2024: ["07-03", "11-29", "12-24"],
    2025: ["07-03", "11-28", "12-24"],
    2026: ["11-27", "12-24"],          # 2026-07-03 은 휴장이라 반휴장이 없다
}


@pytest.mark.parametrize("year", sorted(KNOWN_HOLIDAYS))
def test_holidays_match_the_published_list(year):
    expected = {date(year, *map(int, s.split("-"))) for s in KNOWN_HOLIDAYS[year]}
    assert holidays(year) == expected


@pytest.mark.parametrize("year", sorted(KNOWN_EARLY))
def test_early_closes_match_the_published_list(year):
    expected = {date(year, *map(int, s.split("-"))) for s in KNOWN_EARLY[year]}
    assert early_closes(year) == expected


def test_easter_known_dates():
    assert easter(2024) == date(2024, 3, 31)
    assert easter(2025) == date(2025, 4, 20)
    assert easter(2026) == date(2026, 4, 5)


def test_good_friday_is_two_days_before_easter():
    assert date(2026, 4, 3) in holidays(2026)      # 부활절 4/5 의 이틀 전


def test_nth_weekday_forwards_and_backwards():
    assert nth_weekday(2025, 1, 0, 3) == date(2025, 1, 20)      # 1월 셋째 월요일
    assert nth_weekday(2025, 5, 0, -1) == date(2025, 5, 26)     # 5월 마지막 월요일
    assert nth_weekday(2025, 11, 3, 4) == date(2025, 11, 27)    # 11월 넷째 목요일


def test_weekend_holidays_are_observed_on_a_weekday():
    assert observed(date(2027, 7, 4)) == date(2027, 7, 5)       # 일 → 월
    assert observed(date(2026, 7, 4)) == date(2026, 7, 3)       # 토 → 금


def test_weekends_are_never_trading_days():
    assert not is_trading_day(date(2026, 9, 5))    # 토
    assert not is_trading_day(date(2026, 9, 6))    # 일
    assert is_trading_day(date(2026, 9, 4))        # 금


def test_a_holiday_is_never_an_early_close():
    """휴장일을 반휴장으로 세면 그날 신호가 나온다."""
    for year in KNOWN_HOLIDAYS:
        assert not (holidays(year) & early_closes(year))
        for d in holidays(year):
            assert not is_early_close(d)


def test_trading_day_count_is_sane():
    """미국 주식시장은 연 250일 안팎이다."""
    for year in (2024, 2025, 2026):
        n = len(trading_days(date(year, 1, 1), date(year, 12, 31)))
        assert 248 <= n <= 254, f"{year}년 {n}일"


def test_juneteenth_only_from_2022():
    assert date(2021, 6, 18) not in holidays(2021)
    assert date(2021, 6, 21) not in holidays(2021)
    assert date(2022, 6, 20) in holidays(2022)     # 6/19 일 → 6/20 월


def test_calendar_falls_back_without_pandas():
    """실제 게이트가 내장 규칙으로도 도는지."""
    from th.sessions.market import SessionCalendar

    cal = SessionCalendar()
    w = cal.windows(date(2026, 9, 4))
    assert w is not None and not w.early_close
    assert cal.windows(date(2026, 9, 7)) is None    # 노동절

    half = cal.windows(date(2026, 11, 27))
    assert half is not None and half.early_close
    assert (half.market_close - half.market_open).seconds == 3.5 * 3600
