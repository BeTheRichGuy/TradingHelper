"""주문·체결 자료형.

설계에서 가장 중요한 결정 하나: **진입과 손절을 따로 만들 수 없다.**
`Bracket` 이 두 개를 한 덩어리로 묶고, 손절은 필수 필드다. 그래서 "손절 거는 걸
깜빡했다"가 타입 수준에서 불가능하다. 10배 레버리지에서 이건 편의가 아니라 안전이다.

Phase 2 에서 `NO_STOP_ORDER` 경고를 만들었지만, 그건 이미 벌어진 일을 알려주는 것이다.
여기서는 아예 벌어지지 않게 한다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum

LONG = "LONG"
SHORT = "SHORT"


def round_qty(qty: float, step: float | None) -> float:
    """수량을 거래소 스텝(stepSize)에 맞춰 **내림**한다.

    float 로 `floor(qty / step) * step` 을 하면 0.1 스텝에서 0.3 이 0.29999… 가 된다.
    Decimal 로 계산해야 손절 수량과 TP1+TP2 가 정확히 같아진다.
    `step` 이 None/0 이면 그대로 돌려준다.
    """
    if not step or step <= 0 or qty <= 0:
        return qty
    d_step = Decimal(str(step))
    # 0.7 - 0.3 = 0.39999… 같은 float 찌꺼기를 먼저 털어낸 뒤 내린다
    q = (Decimal(str(round(qty, 12))) / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
    return float(q)


def round_price(px: float | None, tick: float | None) -> float | None:
    """가격을 틱사이즈에 맞춰 반올림한다. `tick` 이 None/0 이면 그대로."""
    if px is None or not tick or tick <= 0:
        return px
    d_tick = Decimal(str(tick))
    p = (Decimal(str(px)) / d_tick).to_integral_value(rounding=ROUND_HALF_UP) * d_tick
    return float(p)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"          # 손절 — 트리거 닿으면 시장가
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderStatus(str, Enum):
    PENDING = "PENDING"        # 아직 안 보냄
    OPEN = "OPEN"              # 거래소에 걸려 있음
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"        # 유효시간 초과

    @property
    def is_final(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED,
                        OrderStatus.REJECTED, OrderStatus.EXPIRED)


class Purpose(str, Enum):
    ENTRY = "ENTRY"
    STOP = "STOP"
    TP1 = "TP1"
    TP2 = "TP2"
    EXIT = "EXIT"              # 수동/강제 청산


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Order:
    """주문 하나. 불변이며, 상태가 바뀌면 새 객체를 만든다.

    `client_id` 는 우리가 만드는 멱등 키다. 재시작 후 같은 주문을 두 번 보내는
    사고를 막는 유일한 장치이므로, 거래소로 보낼 때 반드시 같이 보낸다.
    """
    client_id: str
    bracket_id: str
    symbol: str
    side: Side
    type: OrderType
    purpose: Purpose
    qty: float
    price: float | None = None          # LIMIT 지정가
    trigger: float | None = None        # STOP/TP 트리거
    reduce_only: bool = False
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_price: float | None = None
    exchange_id: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)
    note: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.OPEN)

    def with_status(self, status: OrderStatus, **kw) -> Order:
        return replace(self, status=status, updated_at=now(), **kw)


@dataclass(frozen=True, slots=True)
class Fill:
    order_client_id: str
    bracket_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    purpose: Purpose
    at: datetime = field(default_factory=now)
    slippage_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class Bracket:
    """진입 + 손절 (+ 익절) 한 덩어리.

    **손절(`stop_price`)은 필수다.** 이게 이 클래스의 존재 이유다.
    """
    bracket_id: str
    symbol: str
    direction: str                  # LONG | SHORT
    qty: float
    stop_price: float               # ← 기본값 없음. 반드시 준다
    entry_type: OrderType = OrderType.LIMIT
    entry_price: float | None = None
    tp1_price: float | None = None
    tp2_price: float | None = None
    tp1_ratio: float = 0.5          # TP1 에서 청산할 비율
    reason: str = ""                # 왜 잡았나 — 나중에 분석할 때 이게 전부다
    setup_id: str = ""
    created_at: datetime = field(default_factory=now)
    expire_after_s: int = 300       # 진입 지정가가 이 시간 안에 안 붙으면 취소
    qty_step: float | None = None   # 거래소 stepSize — 있으면 TP 분할 수량을 여기에 맞춘다
    tick_size: float | None = None  # 거래소 tickSize

    def __post_init__(self) -> None:
        # 방향 오타("LNOG")가 롱 의도로 숏을 잡는 사고를 타입 수준에서 막는다
        if self.direction not in (LONG, SHORT):
            raise ValueError(f"방향은 LONG 또는 SHORT 여야 합니다: {self.direction!r}")
        if not (0.0 <= self.tp1_ratio <= 1.0):
            raise ValueError(f"tp1_ratio 는 0~1 사이여야 합니다: {self.tp1_ratio!r}")

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self.direction == LONG else Side.SELL

    @property
    def exit_side(self) -> Side:
        return self.entry_side.opposite

    def risk_per_unit(self, entry: float) -> float:
        return abs(entry - self.stop_price)

    def stop_pct(self, entry: float) -> float:
        return abs(entry - self.stop_price) / entry * 100 if entry > 0 else 0.0


@dataclass(slots=True)
class BracketState:
    """브래킷 하나의 살아있는 상태. 재시작하면 DB 에서 이대로 복구한다."""
    bracket: Bracket
    orders: dict[str, Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    position_qty: float = 0.0        # 부호 있음 (+롱 / −숏)
    avg_entry: float | None = None
    realized_pnl: float = 0.0
    realized_r: float = 0.0
    closed_at: datetime | None = None
    close_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def has_position(self) -> bool:
        return abs(self.position_qty) > 1e-12

    def order_by_purpose(self, p: Purpose) -> Order | None:
        for o in self.orders.values():
            if o.purpose is p and o.is_open:
                return o
        return None

    @property
    def live_stop(self) -> Order | None:
        return self.order_by_purpose(Purpose.STOP)
