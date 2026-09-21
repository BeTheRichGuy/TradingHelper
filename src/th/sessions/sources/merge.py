"""여러 소스에서 온 이벤트를 하나로 합친다.

같은 사건이 소스마다 다른 이름으로 오는 게 문제다. 예를 들어 손으로 적은
"미국 CPI" 와 FRED 의 "미국 CPI (소비자물가)" 는 같은 발표다. 이걸 못 합치면
화면에 같은 게 두 번 뜨고, 사용자가 자동 수집을 못 믿게 된다.

우선순위는 각 이벤트의 `source` 로 정한다(manual > fred > finnhub > yfinance).
**손으로 적은 것은 어떤 자동 소스에도 덮이지 않는다.**
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime

from ..market import NY
from ..models import Event
from .base import priority_of

_NON_WORD = re.compile(r"[^0-9a-z가-힣]+")


def normalize_name(name: str) -> str:
    return _NON_WORD.sub("", name.lower())


def event_date(e: Event) -> date:
    return e.ts_utc.astimezone(NY).date()


def dedup_key(e: Event) -> tuple:
    """1차 키 — 완전히 같다고 볼 수 있는 최소 조건."""
    if e.kind == "earnings" and e.symbols:
        return ("earnings", event_date(e), e.symbols[0])
    return (e.kind, event_date(e), normalize_name(e.name))


def same_event(a: Event, b: Event) -> bool:
    """2차 판정 — 같은 날 같은 종류이고 이름 한쪽이 다른 쪽을 포함하면 같은 사건."""
    if a.kind != b.kind or event_date(a) != event_date(b):
        return False
    if a.kind == "earnings":
        return bool(a.symbols) and a.symbols == b.symbols
    na, nb = normalize_name(a.name), normalize_name(b.name)
    if not na or not nb:
        return False
    return na in nb or nb in na


def merge_events(events: Iterable[Event]) -> list[Event]:
    """중복을 걷어내고 시각 오름차순으로 정렬해 돌려준다.

    같은 사건이면 우선순위가 높은 소스의 항목만 남는다.
    같은 우선순위면 먼저 온 쪽을 남긴다.
    """
    items = list(events)

    # 1차: 정확히 같은 키
    picked: dict[tuple, Event] = {}
    for e in items:
        k = dedup_key(e)
        cur = picked.get(k)
        if cur is None or priority_of(e.source) > priority_of(cur.source):
            picked[k] = e

    # 2차: 같은 날·같은 종류에서 이름이 포함관계인 것끼리 접기
    by_day: dict[tuple[str, date], list[Event]] = {}
    for e in picked.values():
        by_day.setdefault((e.kind, event_date(e)), []).append(e)

    out: list[Event] = []
    for bucket in by_day.values():
        bucket.sort(key=lambda e: (-priority_of(e.source), e.ts_utc, e.name))
        kept: list[Event] = []
        for e in bucket:
            if any(same_event(e, k) for k in kept):
                continue          # 이미 더 높은 우선순위로 남아 있다
            kept.append(e)
        out.extend(kept)

    out.sort(key=lambda e: (e.ts_utc, e.name))
    return out


def stable_id(e: Event) -> str:
    """DB 기본키로 쓸 안정적인 문자열. 같은 사건이면 매번 같은 값이 나와야 한다."""
    d = event_date(e).isoformat()
    tail = e.symbols[0] if (e.kind == "earnings" and e.symbols) else normalize_name(e.name)
    return f"{e.kind}|{d}|{tail}"[:200]


def in_range(events: Iterable[Event], start: datetime, end: datetime) -> list[Event]:
    return [e for e in events if start <= e.ts_utc <= end]
