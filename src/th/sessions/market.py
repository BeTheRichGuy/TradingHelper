"""NYSE 캘린더 기반 레짐 판정.

서머타임과 공휴일·반휴장은 직접 계산하지 않고 `pandas_market_calendars` 에 맡긴다.
직접 계산하면 언젠가 반드시 틀린다.

프리장 시작(04:00 ET)과 애프터 종료(마감 +4시간)는 라이브러리 버전에 따라
제공 여부가 달라서 여기서 직접 파생시킨다. 반휴장일(13:00 ET 마감)에는
애프터도 17:00 ET 에 끝난다 — 마감 기준 상대 계산이라 자동으로 맞는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from ..logging_setup import get_logger
from .models import Regime

log = get_logger(__name__)
_WARNED_YEARS: set[int] = set()

NY = ZoneInfo("America/New_York")
PRE_START_ET = time(4, 0)
POST_DURATION = timedelta(hours=4)
REGULAR_CLOSE_ET = time(16, 0)


@dataclass(frozen=True, slots=True)
class DayWindows:
    """거래일 하루의 경계 (전부 UTC)."""

    day: date
    pre_start: datetime
    market_open: datetime
    market_close: datetime
    post_end: datetime
    early_close: bool

    def boundaries(self) -> list[datetime]:
        return [self.pre_start, self.market_open, self.market_close, self.post_end]

    def regime_at(self, at: datetime) -> Regime:
        if at < self.pre_start:
            return Regime.CLOSED
        if at < self.market_open:
            return Regime.PRE
        if at < self.market_close:
            return Regime.RTH
        if at < self.post_end:
            return Regime.POST
        return Regime.CLOSED


class SessionCalendar:
    """연 단위로 NYSE 일정을 캐싱한다 (연 250행 정도라 부담 없음)."""

    def __init__(self, exchange: str = "NYSE") -> None:
        self.exchange = exchange
        self._years: dict[int, dict[date, DayWindows]] = {}

    # ── 내부 ──────────────────────────────────────────────
    def _load_year(self, year: int) -> dict[date, DayWindows]:
        if year in self._years:
            return self._years[year]
        try:
            out = self._from_pandas_calendar(year)
            source = "pandas_market_calendars"
        except ImportError:
            out = self._from_builtin_rules(year)
            source = "내장 규칙"
            if year not in _WARNED_YEARS:
                _WARNED_YEARS.add(year)
                log.warning(
                    "pandas_market_calendars 가 없어 내장 NYSE 규칙을 씁니다 "
                    "(%d년). 임시 휴장(국장 등)은 반영되지 않습니다.", year
                )
        self._years[year] = out
        log.debug("%d년 거래일 %d개 로드 (%s)", year, len(out), source)
        return out

    def _windows_for(self, d: date, close_et_time: time) -> DayWindows:
        open_utc = datetime.combine(d, time(9, 30), tzinfo=NY).astimezone(UTC)
        close_utc = datetime.combine(d, close_et_time, tzinfo=NY).astimezone(UTC)
        pre_start = datetime.combine(d, PRE_START_ET, tzinfo=NY).astimezone(UTC)
        return DayWindows(
            day=d,
            pre_start=pre_start,
            market_open=open_utc,
            market_close=close_utc,
            post_end=close_utc + POST_DURATION,
            early_close=close_et_time < REGULAR_CLOSE_ET,
        )

    def _from_builtin_rules(self, year: int) -> dict[date, DayWindows]:
        """외부 라이브러리 없이 도는 경로. `us_calendar` 의 규칙을 쓴다."""
        from . import us_calendar as uc

        out: dict[date, DayWindows] = {}
        for d in uc.trading_days(date(year, 1, 1), date(year, 12, 31)):
            close_et = time(13, 0) if uc.is_early_close(d) else REGULAR_CLOSE_ET
            out[d] = self._windows_for(d, close_et)
        return out

    def _from_pandas_calendar(self, year: int) -> dict[date, DayWindows]:
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar(self.exchange)
        sched = cal.schedule(
            start_date=f"{year}-01-01", end_date=f"{year}-12-31", tz="UTC"
        )

        out: dict[date, DayWindows] = {}
        for idx, row in sched.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            open_utc = row["market_open"].to_pydatetime().astimezone(UTC)
            close_utc = row["market_close"].to_pydatetime().astimezone(UTC)

            close_et = close_utc.astimezone(NY)
            pre_start = datetime.combine(
                close_et.date(), PRE_START_ET, tzinfo=NY
            ).astimezone(UTC)

            out[d] = DayWindows(
                day=d,
                pre_start=pre_start,
                market_open=open_utc,
                market_close=close_utc,
                post_end=close_utc + POST_DURATION,
                early_close=close_et.timetz().replace(tzinfo=None) < REGULAR_CLOSE_ET,
            )
        return out

    # ── 공개 ──────────────────────────────────────────────
    def windows(self, day: date) -> DayWindows | None:
        """해당 날짜가 거래일이면 경계를, 아니면 None."""
        return self._load_year(day.year).get(day)

    def is_trading_day(self, day: date) -> bool:
        return self.windows(day) is not None

    def regime_at(self, at: datetime) -> tuple[Regime, DayWindows | None]:
        at = _as_utc(at)
        d = at.astimezone(NY).date()
        w = self.windows(d)
        if w is None:
            return Regime.CLOSED, None
        return w.regime_at(at), w

    def next_boundary(self, at: datetime, horizon_days: int = 10) -> datetime | None:
        """레짐이 바뀌는 다음 시각. 못 찾으면 None."""
        at = _as_utc(at)
        d = at.astimezone(NY).date()
        for i in range(horizon_days + 1):
            w = self.windows(d + timedelta(days=i))
            if w is None:
                continue
            for b in w.boundaries():
                if b > at:
                    return b
        return None

    def trading_days(self, start: date, end: date) -> list[date]:
        days: list[date] = []
        for year in range(start.year, end.year + 1):
            days += [d for d in self._load_year(year) if start <= d <= end]
        return sorted(days)

    def is_last_n_trading_days_of_month(self, day: date, n: int) -> bool:
        """월말 n거래일 이내인가."""
        w = self.windows(day)
        if w is None:
            return False
        year = self._load_year(day.year)
        same_month = sorted(d for d in year if d.year == day.year and d.month == day.month)
        return day in same_month[-n:]

    def previous_trading_day(self, day: date) -> date | None:
        for i in range(1, 12):
            cand = day - timedelta(days=i)
            if self.is_trading_day(cand):
                return cand
        return None


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime 은 받지 않는다. UTC 를 명시할 것")
    return dt.astimezone(UTC)


def et_time_on(day: date, hhmm: time) -> datetime:
    """해당 날짜의 ET 벽시계 시각을 UTC 로."""
    return datetime.combine(day, hhmm, tzinfo=NY).astimezone(UTC)


def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """weekday: 월=0 … 일=6. 예) 셋째 금요일 → nth_weekday_of_month(y, m, 4, 3)"""
    d = date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    return d + timedelta(days=shift + 7 * (n - 1))


@lru_cache(maxsize=1)
def get_calendar() -> SessionCalendar:
    return SessionCalendar()
