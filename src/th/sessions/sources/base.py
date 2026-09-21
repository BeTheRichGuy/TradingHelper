"""이벤트 소스 공통 규약.

무료 소스는 예고 없이 바뀌거나 사라진다. 그래서 소스마다 이 프로토콜만 맞추고,
하나가 죽어도 나머지가 계속 돌게 만든다. 그리고 **손으로 적은 항목은
어떤 자동 소스보다 우선**한다 — 자동화는 편의이지 의존 대상이 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from ..models import Event

# 숫자가 클수록 우선. 병합 시 같은 이벤트면 높은 쪽만 남는다.
SOURCE_PRIORITY: dict[str, int] = {
    "manual": 100,     # data/events.yaml — 사람이 적은 것
    "fred": 50,        # 연준 공식 릴리스 캘린더
    "finnhub": 40,
    "yfinance": 30,
}


def priority_of(source: str) -> int:
    return SOURCE_PRIORITY.get(source, 0)


@dataclass(slots=True)
class FetchResult:
    """한 소스의 수집 결과. 실패해도 예외를 밖으로 던지지 않고 이걸로 알린다."""

    source: str
    ok: bool
    events: list[Event] = field(default_factory=list)
    detail: str = ""

    @property
    def count(self) -> int:
        return len(self.events)


@runtime_checkable
class EventSource(Protocol):
    name: str

    async def fetch(self, start: date, end: date) -> FetchResult: ...
