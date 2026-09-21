"""NYSE 거래일·반휴장 규칙 — 순수 파이썬. 외부 라이브러리를 쓰지 않는다.

`pandas_market_calendars` 가 없는 환경(클라우드, CI, 최소 설치)에서도 세션 게이트가
돌아야 한다. 그게 없다고 백테스트를 못 돌리면, 검증이 특정 기계에 묶인다.

규칙은 NYSE 공식 휴장일 정의를 그대로 옮겼다.

  신정 · 마틴루터킹데이(1월 셋째 월) · 워싱턴탄생일(2월 셋째 월) · 성금요일
  메모리얼데이(5월 마지막 월) · 준틴스(6/19, 2022~) · 독립기념일(7/4)
  노동절(9월 첫째 월) · 추수감사절(11월 넷째 목) · 성탄절(12/25)

  주말에 걸리면: 토요일 → 직전 금요일, 일요일 → 다음 월요일 (준틴스·독립기념일 등)

반휴장(13:00 ET 마감): 독립기념일 전날(평일일 때) · 추수감사절 다음날 ·
성탄절 전날(평일이고 성탄절이 화~금일 때)

**정확도가 중요한 값이므로 테스트로 알려진 날짜를 고정해 둔다.**
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

JUNETEENTH_FROM = 2022


def easter(year: int) -> date:
    """그레고리력 부활절 (Anonymous Gregorian algorithm). 성금요일 계산용."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """그 달의 n번째 요일 (weekday: 월=0). n 이 음수면 뒤에서부터."""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset + 7 * (-n - 1))


def observed(day: date) -> date:
    """주말에 걸린 고정일 휴일을 실제 휴장일로 옮긴다."""
    if day.weekday() == 5:            # 토 → 금
        return day - timedelta(days=1)
    if day.weekday() == 6:            # 일 → 월
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    days = {
        observed(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),                    # MLK
        nth_weekday(year, 2, 0, 3),                    # 워싱턴 탄생일
        easter(year) - timedelta(days=2),              # 성금요일
        nth_weekday(year, 5, 0, -1),                   # 메모리얼데이
        observed(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),                    # 노동절
        nth_weekday(year, 11, 3, 4),                   # 추수감사절
        observed(date(year, 12, 25)),
    }
    if year >= JUNETEENTH_FROM:
        days.add(observed(date(year, 6, 19)))
    return frozenset(days)


@lru_cache(maxsize=64)
def early_closes(year: int) -> frozenset[date]:
    """13:00 ET 마감."""
    out: set[date] = set()

    july3 = date(year, 7, 3)
    if july3.weekday() < 5 and july3 not in holidays(year):
        out.add(july3)

    out.add(nth_weekday(year, 11, 3, 4) + timedelta(days=1))   # 추수감사절 다음날

    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in holidays(year):
        out.add(dec24)

    return frozenset(d for d in out if d.weekday() < 5 and d not in holidays(year))


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def is_early_close(day: date) -> bool:
    return is_trading_day(day) and day in early_closes(day.year)


def trading_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out
