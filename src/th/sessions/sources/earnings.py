"""SOXX 상위 종목 실적 발표일 (yfinance).

SOXL 변동의 대부분은 몇 개 종목이 만든다. 특히 NVDA 실적일은 SOXL이 하루
10~20% 움직일 수 있어, 10배 포지션이 그날 밤 청산될 수 있다. 앱이 그 날짜를
모르면 아무 경고도 못 한다 — 이 소스가 있는 이유다.

yfinance 는 버전마다 접근 방법이 달라져서 여러 경로를 순서대로 시도한다.
전부 실패해도 예외를 밖으로 던지지 않고 FetchResult(ok=False) 로 알린다.
게이트가 외부 소스 때문에 죽으면 안 되기 때문이다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time
from typing import Any

from ...logging_setup import get_logger
from ..market import NY, et_time_on
from ..models import Event
from .base import FetchResult

log = get_logger(__name__)

# SOXX 상위 구성종목 (비중 순, 수시로 바뀌므로 .env 로 덮어쓸 수 있음)
DEFAULT_SYMBOLS = [
    "NVDA", "AVGO", "AMD", "TSM", "INTC",
    "QCOM", "TXN", "MU", "LRCX", "AMAT",
]

# 시각을 모를 때 쓰는 기본값 — 반도체는 대부분 장 마감 후 발표한다
ASSUMED_TIME_ET = time(16, 20)


def _to_datetimes(obj: Any) -> list[datetime]:
    """yfinance 가 돌려주는 여러 모양을 datetime 리스트로 정규화."""
    out: list[datetime] = []

    def push(v: Any) -> None:
        if v is None:
            return
        # pandas Timestamp
        if hasattr(v, "to_pydatetime"):
            try:
                out.append(v.to_pydatetime())
                return
            except Exception:
                return
        if isinstance(v, datetime):
            out.append(v)
        elif isinstance(v, date):
            out.append(datetime.combine(v, ASSUMED_TIME_ET))

    if obj is None:
        return out

    # DataFrame (get_earnings_dates) — 인덱스가 발표일
    if hasattr(obj, "index") and hasattr(obj, "columns"):
        try:
            for idx in list(obj.index):
                push(idx)
            return out
        except Exception:
            return out

    # dict (calendar)
    if isinstance(obj, dict):
        for key in ("Earnings Date", "earningsDate", "Earnings Dates"):
            v = obj.get(key)
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                for item in v:
                    push(item)
            else:
                push(v)
        return out

    if isinstance(obj, (list, tuple)):
        for item in obj:
            push(item)
    return out


def _fetch_symbol_sync(symbol: str) -> list[datetime]:
    """yfinance 호출 (블로킹). 여러 접근 경로를 순서대로 시도한다."""
    import yfinance as yf

    ticker = yf.Ticker(symbol)

    # 1) get_earnings_dates(limit=) — 과거+미래를 함께 준다
    try:
        fn = getattr(ticker, "get_earnings_dates", None)
        if callable(fn):
            got = _to_datetimes(fn(limit=24))
            if got:
                return got
    except Exception as e:
        log.debug("%s get_earnings_dates 실패: %s", symbol, e)

    # 2) earnings_dates 속성
    try:
        got = _to_datetimes(getattr(ticker, "earnings_dates", None))
        if got:
            return got
    except Exception as e:
        log.debug("%s earnings_dates 실패: %s", symbol, e)

    # 3) calendar — 다음 발표일만 주는 경우가 많다
    try:
        got = _to_datetimes(getattr(ticker, "calendar", None))
        if got:
            return got
    except Exception as e:
        log.debug("%s calendar 실패: %s", symbol, e)

    return []


def _normalize(dt: datetime) -> tuple[datetime, bool]:
    """(UTC 시각, 시각이_추정값인가)"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    et = dt.astimezone(NY)
    # 자정이면 소스가 날짜만 준 것 → 장 마감 후로 가정
    if et.hour == 0 and et.minute == 0:
        return et_time_on(et.date(), ASSUMED_TIME_ET), True
    return et.astimezone(UTC), False


class EarningsSource:
    """yfinance 기반 실적 캘린더."""

    name = "yfinance"

    def __init__(self, symbols: list[str] | None = None, warn_days_before: int = 3) -> None:
        self.symbols = [s.upper() for s in (symbols or DEFAULT_SYMBOLS)]
        self.warn_days_before = warn_days_before

    async def fetch(self, start: date, end: date) -> FetchResult:
        loop = asyncio.get_running_loop()
        events: list[Event] = []
        failed: list[str] = []

        for symbol in self.symbols:
            try:
                raw = await loop.run_in_executor(None, _fetch_symbol_sync, symbol)
            except Exception as e:
                log.debug("%s 실적일 조회 실패: %s", symbol, e)
                failed.append(symbol)
                continue

            if not raw:
                failed.append(symbol)
                continue

            for dt in raw:
                ts, estimated = _normalize(dt)
                d = ts.astimezone(NY).date()
                if not (start <= d <= end):
                    continue
                note = f"warn_days_before={self.warn_days_before}"
                if estimated:
                    note += ",time_estimated"
                events.append(
                    Event(
                        name=f"{symbol} 실적",
                        ts_utc=ts,
                        kind="earnings",
                        importance="high",
                        symbols=(symbol,),
                        note=note,
                        source=self.name,
                    )
                )
            # 야후 쪽 부하를 줄이기 위해 살짝 쉬어 간다
            await asyncio.sleep(0.3)

        ok = len(failed) < len(self.symbols)
        detail = f"{len(self.symbols) - len(failed)}/{len(self.symbols)} 종목 조회"
        if failed:
            detail += f" · 실패: {', '.join(failed)}"
        return FetchResult(self.name, ok=ok, events=events, detail=detail)
