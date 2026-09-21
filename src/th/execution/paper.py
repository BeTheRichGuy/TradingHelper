"""페이퍼 트레이딩 — 실제 시세로 체결을 흉내낸다. 돈은 나가지 않는다.

체결 규칙을 **일부러 불리하게** 잡았다. 페이퍼가 실전보다 좋게 나오면
그 숫자는 사람을 실전으로 밀어넣는 거짓말이 된다.

  · 시장가   : 반대편 호가 + 슬리피지
  · 지정가   : 반대편 호가가 내 가격을 **넘어야** 체결 (닿기만 해서는 안 됨)
  · 손절     : 트리거에 닿으면 시장가. 슬리피지를 더 크게 잡는다
  · 같은 순간에 손절·익절이 둘 다 닿으면 → **손절 우선**

마지막 규칙이 핵심이다. 1분봉 안에서 어느 쪽이 먼저였는지 알 수 없으므로
항상 나쁜 쪽을 택한다. 백테스터(`backtest/simulate.py`)와 같은 원칙이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import Fill, Order, OrderStatus, OrderType, Purpose, Side

# 실측 기반 기본값 (docs/11 참고)
#   SOXLUSDT 스프레드 중앙값 0.79bps → 편도 통과 0.4bps
#   테이커 0.05% / 메이커 0.02%
TAKER_FEE_PCT = 0.05
MAKER_FEE_PCT = 0.02
MARKET_SLIP_PCT = 0.01        # 시장가 추가 슬리피지
STOP_SLIP_PCT = 0.05          # 손절은 급한 구간에 나가므로 더 크게


@dataclass(frozen=True, slots=True)
class Quote:
    """한 시점의 시장 상태.

    `high`/`low` 는 직전 봉의 고저다. 호가만 보면 봉 안에서 스쳐간 손절을
    놓친다 — 실전에서는 그 스침에 체결되므로 반드시 봉 범위로도 판정한다.

    `bar_start` 는 그 고저를 만든 봉 중 **가장 오래된 봉의 시각**이다.
    주문보다 먼저 지나간 봉의 고저로 손절·익절을 판정하면 안 되므로,
    주문이 `bar_start` 이후에 생겼으면 호가로만 판정한다.
    """
    symbol: str
    bid: float
    ask: float
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    high: float | None = None
    low: float | None = None
    bar_start: datetime | None = None

    def covers(self, order_created_at: datetime) -> bool:
        """이 봉 범위가 주문 이후의 시장을 담고 있는가.

        `bar_start` 를 모르면(테스트·수동 투입) 예전처럼 봉 범위를 믿는다.
        """
        if self.bar_start is None:
            return True
        return order_created_at <= self.bar_start

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def bar_high(self) -> float:
        return self.high if self.high is not None else self.ask

    def bar_low(self) -> float:
        return self.low if self.low is not None else self.bid


class PaperClient:
    """`ExecutionClient` 의 페이퍼 구현.

    상태를 메모리에 들고 있고, `feed()` 로 시세를 먹여주면 체결을 판정한다.
    영속화는 `store` 가 따로 한다 — 여기는 순수하게 체결 논리만 담는다.
    """

    mode = "paper"

    def __init__(self, *, taker_fee_pct: float = TAKER_FEE_PCT,
                 maker_fee_pct: float = MAKER_FEE_PCT) -> None:
        self._orders: dict[str, Order] = {}
        self._pending_fills: list[Fill] = []
        self._last: dict[str, Quote] = {}
        self.taker_fee_pct = taker_fee_pct
        self.maker_fee_pct = maker_fee_pct

    # ── ExecutionClient ────────────────────────────────
    async def place(self, order: Order) -> Order:
        exist = self._orders.get(order.client_id)
        if exist is not None:
            return exist                      # 멱등 — 두 번 보내도 하나
        o = order.with_status(OrderStatus.OPEN, exchange_id=f"paper-{order.client_id}")
        self._orders[o.client_id] = o
        # 시장가는 다음 시세를 기다리지 않고 마지막 호가로 즉시 체결
        q = self._last.get(o.symbol)
        if q is None:
            return o
        if o.type is OrderType.MARKET:
            self._fill(o, self._market_price(o, q), q, maker=False)
            return self._orders[o.client_id]
        if o.type is OrderType.LIMIT and o.price is not None:
            # 낼 때 이미 호가를 넘은 지정가는 테이커다 — 시장가처럼 바로 체결되고
            # 수수료도 테이커로 낸다. 체결가는 내 지정가보다 나빠지지는 않는다.
            if o.side is Side.BUY and q.ask <= o.price:
                self._fill(o, min(o.price, self._market_price(o, q)), q, maker=False)
                return self._orders[o.client_id]
            if o.side is Side.SELL and q.bid >= o.price:
                self._fill(o, max(o.price, self._market_price(o, q)), q, maker=False)
                return self._orders[o.client_id]
        return o

    async def cancel(self, order: Order) -> Order:
        o = self._orders.get(order.client_id)
        if o is None or not o.is_open:
            return o or order.with_status(OrderStatus.CANCELED)
        o = o.with_status(OrderStatus.CANCELED)
        self._orders[o.client_id] = o
        return o

    async def fetch(self, order: Order) -> Order:
        return self._orders.get(order.client_id, order)

    async def poll_fills(self) -> list[Fill]:
        out, self._pending_fills = self._pending_fills, []
        return out

    async def close(self) -> None:
        return None

    # ── 시세 투입 ──────────────────────────────────────
    def feed(self, q: Quote) -> list[Fill]:
        """시세 한 틱을 먹인다. 이번에 발생한 체결을 돌려준다."""
        self._last[q.symbol] = q
        fills: list[Fill] = []

        live = [o for o in self._orders.values()
                if o.is_open and o.symbol == q.symbol]
        # 손절 우선 — 같은 순간에 둘 다 닿았을 때 나쁜 쪽을 택하기 위해
        live.sort(key=lambda o: 0 if o.purpose is Purpose.STOP else 1)

        stop_hit = False
        for o in live:
            if o.purpose in (Purpose.TP1, Purpose.TP2) and stop_hit:
                continue                       # 손절이 먼저 났다고 본다
            px = self._match(o, q)
            if px is None:
                continue
            maker = o.type is OrderType.LIMIT
            f = self._fill(o, px, q, maker=maker)
            fills.append(f)
            if o.purpose is Purpose.STOP:
                stop_hit = True
        return fills

    # ── 내부 ───────────────────────────────────────────
    def _market_price(self, o: Order, q: Quote) -> float:
        base = q.ask if o.side is Side.BUY else q.bid
        slip = MARKET_SLIP_PCT / 100
        return base * (1 + slip) if o.side is Side.BUY else base * (1 - slip)

    def _match(self, o: Order, q: Quote) -> float | None:
        """체결가를 돌려주거나, 아직이면 None."""
        if o.type is OrderType.MARKET:
            return self._market_price(o, q)

        if o.type is OrderType.LIMIT:
            if o.price is None:
                return None
            # 반대편 호가가 내 가격을 **넘어야** 체결. 닿기만 한 건 인정하지 않는다.
            # 체결가는 **내 지정가**다 — 걸어둔 지정가는 그 가격에 맞아 나가지,
            # 호가만큼 더 좋게 체결되지 않는다.
            if o.side is Side.BUY and q.ask < o.price:
                return o.price
            if o.side is Side.SELL and q.bid > o.price:
                return o.price
            return None

        if o.trigger is None:
            return None
        slip = STOP_SLIP_PCT / 100

        # 주문보다 먼저 지나간 봉의 고저로 판정하면 안 된다 — 호가만 본다
        if q.covers(o.created_at):
            hi, lo = q.bar_high(), q.bar_low()
        else:
            hi, lo = q.ask, q.bid

        if o.type is OrderType.STOP_MARKET:
            # 롱 손절 = SELL, 가격이 트리거 **이하**로 내려오면 발동
            hit = (lo <= o.trigger) if o.side is Side.SELL else (hi >= o.trigger)
            if not hit:
                return None
            return o.trigger * (1 - slip) if o.side is Side.SELL \
                else o.trigger * (1 + slip)

        if o.type is OrderType.TAKE_PROFIT_MARKET:
            # 롱 익절 = SELL, 가격이 트리거 **이상**으로 올라오면 발동
            hit = (hi >= o.trigger) if o.side is Side.SELL else (lo <= o.trigger)
            if not hit:
                return None
            slip = MARKET_SLIP_PCT / 100
            return o.trigger * (1 - slip) if o.side is Side.SELL \
                else o.trigger * (1 + slip)
        return None

    def _fill(self, o: Order, price: float, q: Quote, *, maker: bool) -> Fill:
        notional = o.qty * price
        fee = notional * ((self.maker_fee_pct if maker else self.taker_fee_pct) / 100)
        ref = o.price or o.trigger or q.mid
        slip_pct = ((price - ref) / ref * 100) if ref else 0.0
        filled = o.with_status(
            OrderStatus.FILLED, filled_qty=o.qty, avg_price=price
        )
        self._orders[o.client_id] = filled
        f = Fill(
            order_client_id=o.client_id, bracket_id=o.bracket_id, symbol=o.symbol,
            side=o.side, qty=o.qty, price=price, fee=fee, purpose=o.purpose,
            at=q.at, slippage_pct=slip_pct,
        )
        self._pending_fills.append(f)
        return f

    # ── 조회 ───────────────────────────────────────────
    @property
    def orders(self) -> dict[str, Order]:
        return dict(self._orders)

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._orders.values()
                if o.is_open and (symbol is None or o.symbol == symbol)]
