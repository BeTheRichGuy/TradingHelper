"""계정·리스크 자료형.

바이낸스가 돌려주는 값은 전부 문자열이라, 여기서 한 번만 숫자로 바꾸고
이후 코드는 이 타입만 다룬다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"

# 손절로 인정하는 주문 종류. 진입 주문이나 익절만 있는 건 손절이 아니다.
STOP_ORDER_TYPES = {"STOP_MARKET", "STOP", "TRAILING_STOP_MARKET"}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ms(v: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    side: str                       # LONG | SHORT | FLAT
    qty: float                      # 절대값
    entry_price: float
    mark_price: float
    liquidation_price: float | None
    unrealized_pnl: float
    leverage: int
    margin_type: str                # isolated | cross
    isolated_margin: float
    notional: float                 # 절대값
    updated_at: datetime

    @property
    def is_open(self) -> bool:
        return self.side != FLAT and self.qty > 0

    @property
    def unrealized_pct_on_margin(self) -> float | None:
        """증거금 대비 손익률 — 사용자가 체감하는 숫자."""
        margin = self.isolated_margin or (
            self.notional / self.leverage if self.leverage else 0.0
        )
        if margin <= 0:
            return None
        return self.unrealized_pnl / margin * 100

    @property
    def price_move_pct(self) -> float | None:
        """진입가 대비 가격이 몇 % 움직였나 (본주 기준)."""
        if self.entry_price <= 0:
            return None
        raw = (self.mark_price - self.entry_price) / self.entry_price * 100
        return raw if self.side == LONG else -raw

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Position":
        amt = _f(raw.get("positionAmt"))
        side = LONG if amt > 0 else (SHORT if amt < 0 else FLAT)
        liq = _f(raw.get("liquidationPrice"))
        notional = abs(_f(raw.get("notional")))
        mark = _f(raw.get("markPrice"))
        if notional == 0:
            notional = abs(amt) * mark
        return cls(
            symbol=str(raw.get("symbol", "")).upper(),
            side=side,
            qty=abs(amt),
            entry_price=_f(raw.get("entryPrice")),
            mark_price=mark,
            liquidation_price=liq if liq > 0 else None,
            unrealized_pnl=_f(raw.get("unRealizedProfit") or raw.get("unrealizedProfit")),
            leverage=int(_f(raw.get("leverage"), 1)),
            margin_type=str(raw.get("marginType", "cross")).lower(),
            isolated_margin=_f(raw.get("isolatedMargin") or raw.get("isolatedWallet")),
            notional=notional,
            updated_at=_ms(raw.get("updateTime")),
        )


@dataclass(frozen=True, slots=True)
class OpenOrder:
    symbol: str
    order_id: str
    side: str                       # BUY | SELL
    order_type: str
    stop_price: float
    price: float
    qty: float
    reduce_only: bool
    close_position: bool

    @property
    def is_stop(self) -> bool:
        return self.order_type in STOP_ORDER_TYPES

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "OpenOrder":
        return cls(
            symbol=str(raw.get("symbol", "")).upper(),
            order_id=str(raw.get("orderId", "")),
            side=str(raw.get("side", "")).upper(),
            order_type=str(raw.get("type") or raw.get("origType") or "").upper(),
            stop_price=_f(raw.get("stopPrice")),
            price=_f(raw.get("price")),
            qty=_f(raw.get("origQty")),
            reduce_only=bool(raw.get("reduceOnly")),
            close_position=bool(raw.get("closePosition")),
        )


@dataclass(frozen=True, slots=True)
class Balance:
    wallet: float                   # 지갑 잔고
    available: float                # 사용 가능 증거금
    unrealized_pnl: float
    margin_used: float

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Balance":
        return cls(
            wallet=_f(raw.get("totalWalletBalance")),
            available=_f(raw.get("availableBalance")),
            unrealized_pnl=_f(raw.get("totalUnrealizedProfit")),
            margin_used=_f(raw.get("totalPositionInitialMargin")),
        )


@dataclass(frozen=True, slots=True)
class DailyPnl:
    """한국 날짜 기준 당일 실현손익.

    R 은 '한 번 매매에 걸기로 한 금액'이다 (지갑잔고 × 리스크비율).
    −3R 은 그 금액의 3배를 잃었다는 뜻이지, 계좌의 3%가 아니다.
    """

    date_kst: date
    realized_pnl: float = 0.0
    commission: float = 0.0
    funding: float = 0.0
    trade_count: int = 0
    r_unit: float = 0.0
    limit_hit_at: datetime | None = None

    @property
    def net(self) -> float:
        return self.realized_pnl + self.commission + self.funding

    @property
    def r_value(self) -> float:
        if self.r_unit <= 0:
            return 0.0
        return self.net / self.r_unit

    def locked(self, limit_r: float = -3.0) -> bool:
        # r_unit 을 못 정한 상태(지갑 0 이하)에서 손실이 있으면 '0.0R' 로 읽히면 안 된다
        if self.r_unit <= 0 and self.net < 0:
            return True
        return self.limit_hit_at is not None or self.r_value <= limit_r


@dataclass(frozen=True, slots=True)
class Discipline:
    """규율 지표 (docs/15 1-3) — 전 종목 노출·당일 손익·남은 R·이벤트·휴장까지.

    `ok=False` 면 계정 조회가 실패한 턴이다. 그때 계정에서 나온 숫자는 **None** 이지
    0 이 아니다 — 0 은 '노출 없음'으로 읽히고, 그건 조회 실패와 정반대 뜻이다.
    """

    ok: bool
    total_exposure_x: float | None = None
    total_limit_x: float = 3.0
    max_symbol: str | None = None
    max_symbol_exposure_x: float | None = None
    symbol_limit_x: float = 1.0
    daily_pnl_pct: float | None = None
    drawdown_limit_pct: float = 15.0
    remaining_r: float | None = None
    r_limit: float = 3.0
    event_blocked: bool = False
    event_name: str | None = None
    event_in_sec: int | None = None
    closes_in_sec: int | None = None


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """화면에 뿌리는 최종 상태."""

    at: datetime
    symbol: str
    position: Position | None
    balance: Balance
    open_orders: tuple[OpenOrder, ...] = ()
    daily: DailyPnl | None = None

    # 파생값 (risk.py 가 채운다)
    liq_distance_pct: float | None = None
    stop_orders: tuple[OpenOrder, ...] = ()
    stop_distance_pct: float | None = None
    account_leverage: float = 0.0
    underlying_multiplier: float = 1.0
    underlying_leverage: float | None = None
    underlying_move_to_liq_pct: float | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)
    account_ok: bool = True
    """포지션·잔고 조회 자체가 성공했는가.

    `errors` 에는 당일 손익(income)·DB 조회 실패도 섞여 들어온다. 그것까지
    '포지션을 모른다'로 읽으면 이미 닫힌 포지션을 화면에 계속 붙들어 둔다.
    """

    @property
    def has_position(self) -> bool:
        return self.position is not None and self.position.is_open

    @property
    def has_stop(self) -> bool:
        return len(self.stop_orders) > 0
