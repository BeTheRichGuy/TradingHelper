"""거래소로 주문을 내보내는 유일한 통로.

`th.account` 에는 주문 코드가 없고 앞으로도 없다. 조회 전용이다.
주문은 **오직 이 프로토콜을 구현한 객체를 통해서만** 나간다. 이 규칙 하나로
"어디선가 주문이 나갈 수도 있다"는 불안이 사라진다 — 구현체를 찾으면 전부다.

현재 구현체:
  · `paper.PaperClient` — 실제 시세로 체결을 흉내낸다. 돈이 나가지 않는다
  · `live.LiveClient`   — **NotImplementedError.** Phase 4 통과 전까지 열지 않는다
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Fill, Order


@runtime_checkable
class ExecutionClient(Protocol):
    """주문 하나 단위의 최소 인터페이스.

    브래킷 조립·안전장치·기록은 `manager` 가 한다. 여기는 '보내고 취소하고
    상태를 읽는' 것만 담당한다. 그래야 페이퍼와 실거래가 같은 코드로 굴러간다.
    """

    mode: str            # "paper" | "live"

    async def place(self, order: Order) -> Order:
        """주문을 낸다. `client_id` 로 멱등해야 한다 — 같은 id 를 두 번 보내면
        두 번째는 기존 주문을 그대로 돌려줘야 한다."""
        ...

    async def cancel(self, order: Order) -> Order:
        ...

    async def fetch(self, order: Order) -> Order:
        """거래소 기준 최신 상태."""
        ...

    async def poll_fills(self) -> list[Fill]:
        """지난 호출 이후 새로 생긴 체결."""
        ...

    async def close(self) -> None:
        ...
