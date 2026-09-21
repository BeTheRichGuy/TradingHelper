"""리스크 계산 — 전부 순수 함수라 테스트로 고정할 수 있다.

여기서 가장 중요한 값은 수익이 아니라 **"본주가 몇 % 움직이면 청산되는가"** 다.
SOXL 은 반도체 지수(SOXX)의 3배 ETF라, 10배 레버리지를 얹으면 SOXX 대비
실효 30배가 된다. 사용자가 화면에서 늘 봐야 하는 숫자가 그것이다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from .models import LONG, SHORT, Balance, DailyPnl, OpenOrder, Position, RiskSnapshot

KST = ZoneInfo("Asia/Seoul")

# 레버리지 ETF 배수 (본주 티커 기준). 여기 없으면 1배로 본다.
LEVERAGED_ETF_MULTIPLIER: dict[str, float] = {
    "SOXL": 3.0, "SOXS": -3.0,
    "TQQQ": 3.0, "SQQQ": -3.0,
    "TNA": 3.0, "TZA": -3.0,
    "LABU": 3.0, "LABD": -3.0,
    "FNGU": 3.0, "FNGD": -3.0,
    "UPRO": 3.0, "SPXU": -3.0,
    "NVDL": 2.0, "TSLL": 2.0,
}

# 청산가까지 이 % 이내면 위험 경보
LIQ_CRITICAL_PCT = 3.0
LIQ_WARNING_PCT = 6.0

DAILY_LOSS_LIMIT_R = -3.0

# 규율 지표 한도 (docs/15 1-3). 화면의 게이지 눈금이자 넘으면 붉게 칠하는 기준.
#   총 노출     Σ|노셔널| ÷ 지갑          ≤ 3배
#   단일 종목   최대 종목 노셔널 ÷ 지갑    ≤ 1배
#   낙폭        당일 (실현+미실현) ÷ 지갑   ≥ −15%
EXPOSURE_LIMIT_X = 3.0
SYMBOL_LIMIT_X = 1.0
DRAWDOWN_LIMIT_PCT = 15.0


def multiplier_for(underlying: str | None) -> float:
    if not underlying:
        return 1.0
    return LEVERAGED_ETF_MULTIPLIER.get(underlying.upper(), 1.0)


def liquidation_distance_pct(
    side: str, mark_price: float, liquidation_price: float | None
) -> float | None:
    """현재가에서 청산가까지 몇 % 남았나 (양수 = 여유).

    음수가 나오면 이미 청산가를 지난 것이므로 데이터가 이상한 상태다.
    """
    if not liquidation_price or liquidation_price <= 0 or mark_price <= 0:
        return None
    if side == LONG:
        return (mark_price - liquidation_price) / mark_price * 100
    if side == SHORT:
        return (liquidation_price - mark_price) / mark_price * 100
    return None


def underlying_move_to_liq_pct(
    liq_pct: float | None, multiplier: float
) -> float | None:
    """본주(SOXL)가 아니라 **기초지수(SOXX)** 가 몇 % 움직이면 청산되는가.

    SOXL 이 9.5% 빠지면 청산 → SOXX 로는 3.2%. 체감이 완전히 다르다.
    """
    if liq_pct is None:
        return None
    m = abs(multiplier) or 1.0
    return liq_pct / m


def account_leverage(notional: float, wallet: float) -> float:
    """계좌 대비 실효 레버리지. 설정한 배수가 아니라 실제로 얼마나 걸었는가."""
    if wallet <= 0:
        return 0.0
    return notional / wallet


def find_stop_orders(orders: list[OpenOrder] | tuple[OpenOrder, ...], position: Position) -> list[OpenOrder]:
    """이 포지션을 실제로 닫아 줄 손절 주문만 골라낸다.

    조건 세 가지를 모두 만족해야 손절로 인정한다.
      1. STOP 계열 주문일 것
      2. 포지션과 반대 방향일 것 (롱이면 SELL)
      3. 포지션을 줄이는 주문일 것 (reduceOnly 또는 closePosition)

    익절 주문(TAKE_PROFIT)만 걸어두고 손절이 없는 상태가 가장 흔한 사고다.
    """
    if not position.is_open:
        return []
    want_side = "SELL" if position.side == LONG else "BUY"
    out: list[OpenOrder] = []
    for o in orders:
        if o.symbol != position.symbol:
            continue
        if not o.is_stop:
            continue
        if o.side != want_side:
            continue
        if not (o.reduce_only or o.close_position):
            continue
        out.append(o)
    return out


def stop_distance_pct(position: Position, stops: list[OpenOrder]) -> float | None:
    """가장 가까운 손절가까지 몇 % 남았나."""
    if not stops or position.mark_price <= 0:
        return None
    prices = [o.stop_price for o in stops if o.stop_price > 0]
    if not prices:
        return None
    if position.side == LONG:
        best = max(prices)                       # 롱은 가장 높은 손절이 가장 가깝다
        return (position.mark_price - best) / position.mark_price * 100
    best = min(prices)
    return (best - position.mark_price) / position.mark_price * 100


def r_unit(wallet: float, risk_pct: float) -> float:
    """1R = 한 번 매매에 걸기로 한 금액."""
    return max(0.0, wallet * risk_pct / 100)


def kst_today(at: datetime | None = None) -> date:
    return (at or datetime.now(UTC)).astimezone(KST).date()


def build_snapshot(
    symbol: str,
    position: Position | None,
    balance: Balance,
    orders: list[OpenOrder],
    daily: DailyPnl | None,
    underlying: str | None,
    at: datetime | None = None,
    errors: tuple[str, ...] = (),
    account_ok: bool = True,
) -> RiskSnapshot:
    at = at or datetime.now(UTC)
    mult = multiplier_for(underlying)

    stops: list[OpenOrder] = []
    liq_pct = None
    stop_pct = None
    acct_lev = 0.0

    if position is not None and position.is_open:
        stops = find_stop_orders(orders, position)
        liq_pct = liquidation_distance_pct(
            position.side, position.mark_price, position.liquidation_price
        )
        stop_pct = stop_distance_pct(position, stops)
        acct_lev = account_leverage(position.notional, balance.wallet)

    return RiskSnapshot(
        at=at,
        symbol=symbol.upper(),
        position=position,
        balance=balance,
        open_orders=tuple(orders),
        daily=daily,
        liq_distance_pct=liq_pct,
        stop_orders=tuple(stops),
        stop_distance_pct=stop_pct,
        account_leverage=acct_lev,
        underlying_multiplier=mult,
        underlying_leverage=(acct_lev * abs(mult)) if acct_lev else None,
        underlying_move_to_liq_pct=underlying_move_to_liq_pct(liq_pct, mult),
        errors=errors,
        account_ok=account_ok,
    )
