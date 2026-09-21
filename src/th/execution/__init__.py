"""자동매매 실행 계층.

**여기 있는 것이 주문을 낼 수 있는 코드의 전부다.** `th.account` 는 조회 전용이고
앞으로도 그렇다. 주문 경로를 한 곳에 몰아둬야 "어디선가 주문이 나갈 수도 있다"는
불안이 사라진다.

`__init__` 에서는 DB·HTTP 를 끌어오는 모듈을 임포트하지 않는다. (Phase 1.5·2 에서
같은 실수를 두 번 했다 — 세션 게이트가 asyncpg 를 딸고 오는 문제.)
`store` 와 `manager` 는 필요한 쪽에서 직접 임포트한다.
"""

from .guard import GuardInput, GuardResult, check, notional_leverage, size_from_risk
from .models import (
    LONG,
    SHORT,
    Bracket,
    BracketState,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Purpose,
    Side,
    new_id,
)

__all__ = [
    "LONG", "SHORT", "Bracket", "BracketState", "Fill", "Order", "OrderStatus",
    "OrderType", "Purpose", "Side", "new_id",
    "GuardInput", "GuardResult", "check", "size_from_risk", "notional_leverage",
]
