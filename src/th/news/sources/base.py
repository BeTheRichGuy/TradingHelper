"""뉴스 소스 공통 규약 (`sessions/sources/base.py` 와 같은 사상).

무료 소스는 예고 없이 바뀌거나 사라진다. 소스마다 이 프로토콜만 맞추고,
**하나가 죽어도 나머지가 계속 돌게** 만든다. 실패는 예외가 아니라 `FetchResult(ok=False)` 다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Protocol, runtime_checkable

from ..models import NewsItem

Sink = Callable[[NewsItem], Awaitable[bool]]
"""수집기가 소스에 넘겨주는 콜백. 새 항목이면 True."""


@dataclass(slots=True)
class FetchResult:
    """한 번의 폴링 결과."""

    source: str
    ok: bool
    items: list[NewsItem] = field(default_factory=list)
    detail: str = ""

    @property
    def count(self) -> int:
        return len(self.items)


@runtime_checkable
class NewsSource(Protocol):
    """폴링 소스. `interval_s` 마다 `fetch(since)` 를 부른다."""

    name: str
    interval_s: int

    async def fetch(self, since: datetime) -> FetchResult: ...


@runtime_checkable
class PushSource(Protocol):
    """푸시 소스 (텔레그램). 스스로 연결을 유지하고 새 항목을 `sink` 로 넘긴다.

    `stop_evt` 가 set 되면 정리하고 돌아온다. 예외를 밖으로 던지지 않는다.
    """

    name: str

    async def run(self, stop_evt, sink: Sink) -> None: ...
