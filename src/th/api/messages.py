"""RiskView → WebSocket 메시지. **순수 변환이다.**

터미널(`th watch`)과 웹이 다른 답을 내면 안 되므로, 사유 목록은
`th.ui.dashboard.no_trade_reasons()` 를 그대로 쓴다. 여기서 규칙을 다시 적으면
언젠가 둘이 갈라진다 — 이 프로젝트에서 이미 여러 번 겪었다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..ui.dashboard import no_trade_reasons
from ..universe import active_symbols, group_of, leverage_tag, underlying_ticker
from .schemas import (
    AlertMessage,
    BracketItem,
    BracketsMessage,
    CoverageItem,
    CoverageMessage,
    DisciplineMessage,
    MarketMessage,
    NoTradeMessage,
    NoTradeReason,
    PositionItem,
    PositionsMessage,
    RiskMessage,
    SessionMessage,
    SnapshotMessage,
    WatchlistItem,
    WatchlistMessage,
)

if TYPE_CHECKING:                       # 서버 코드가 httpx·asyncpg 를 끌고 오면 안 된다
    from ..account.models import Discipline, OpenOrder, Position
    from ..account.service import RiskView
    from ..execution.models import BracketState

# 사유 문장을 어느 계층에서 왔는지로 나눈다. 화면이 묶어서 보여줄 때 쓴다.
_SOURCE_HINTS: tuple[tuple[str, str, str], ...] = (
    ("손실 한도", "account", "daily_loss_limit"),
    ("까지", "event", "event_imminent"),
    ("스프레드", "market", "spread"),
    ("변동성", "market", "volatility"),
)


def classify_reason(text: str) -> tuple[str, str]:
    for needle, source, code in _SOURCE_HINTS:
        if needle in text:
            return source, code
    return "session", "session_block"


def build_no_trade(view: RiskView, at: datetime | None = None) -> NoTradeMessage:
    reasons = [
        NoTradeReason(source=src, code=code, text_ko=text)
        for text in no_trade_reasons(view)
        for src, code in [classify_reason(text)]
    ]
    return NoTradeMessage(
        symbol=view.risk.symbol,
        ts=at or datetime.now(UTC),
        reasons=reasons,
    )


def build_risk(view: RiskView, at: datetime | None = None) -> RiskMessage:
    r = view.risk
    d = r.daily
    return RiskMessage(
        symbol=r.symbol,
        ts=at or datetime.now(UTC),
        errors=list(r.errors),
        has_position=r.has_position,
        side=r.position.side if r.position else None,
        entry_price=r.position.entry_price if r.position else None,
        qty=r.position.qty if r.position else 0.0,
        stop_price=(r.stop_orders[0].stop_price if r.stop_orders else None),
        liquidation_price=r.position.liquidation_price if r.position else None,
        unrealized_pnl=r.position.unrealized_pnl if r.position else None,
        liq_distance_pct=r.liq_distance_pct,
        underlying_move_to_liq_pct=r.underlying_move_to_liq_pct,
        underlying_multiplier=r.underlying_multiplier,
        has_stop=r.has_stop,
        stop_distance_pct=r.stop_distance_pct,
        account_leverage=r.account_leverage,
        daily_r=d.r_value if d else None,
        daily_locked=bool(d and d.locked()),
    )


def build_session(view: RiskView, at: datetime | None = None) -> SessionMessage | None:
    s = view.session
    if s is None:
        return None
    ev = s.next_event
    return SessionMessage(
        symbol=view.risk.symbol,
        ts=at or datetime.now(UTC),
        regime=s.regime.value,
        regime_label_ko=s.regime.label_ko,
        regime_ends_in_sec=s.regime_ends_in_sec,
        blocked=s.blocked,
        next_event_name=ev.name if ev else None,
        next_event_in_sec=s.next_event_in_sec,
        warnings=list(s.warnings),
    )


def build_alerts(view: RiskView, at: datetime | None = None) -> list[AlertMessage]:
    when = at or datetime.now(UTC)
    return [
        AlertMessage(
            symbol=view.risk.symbol, ts=when, level=a.level, code=a.code,
            message_ko=a.message_ko, detail_ko=a.detail_ko or "",
        )
        for a in view.alerts
    ]


def build_coverage(coverage, span_days: float, warnings: list[str],
                   symbol: str, at: datetime | None = None) -> CoverageMessage:
    return CoverageMessage(
        symbol=symbol,
        ts=at or datetime.now(UTC),
        items=[
            CoverageItem(
                name=c.name, rows=c.rows, first=c.first, last=c.last,
                status=c.status(span_days), depends=c.depends,
            )
            for c in coverage
        ],
        warnings=list(warnings),
    )


def build_discipline_message(d: Discipline | None) -> DisciplineMessage | None:
    """규율 지표 → 메시지. `ok=False` 면 계정 숫자는 **반드시 None** 으로 나간다.

    `carry_forward` 가 직전 값을 붙여 뒀더라도 그건 '지금 값'이 아니다. 브라우저는
    자기가 마지막으로 받은 값을 흐리게 유지한다 (docs/15 1-3).
    """
    if d is None:
        return None
    if not d.ok:
        return DisciplineMessage(
            ok=False,
            total_limit_x=d.total_limit_x, symbol_limit_x=d.symbol_limit_x,
            drawdown_limit_pct=d.drawdown_limit_pct, r_limit=d.r_limit,
            event_blocked=d.event_blocked, event_name=d.event_name,
            event_in_sec=d.event_in_sec, closes_in_sec=d.closes_in_sec,
        )
    return DisciplineMessage(
        ok=True,
        total_exposure_x=d.total_exposure_x, total_limit_x=d.total_limit_x,
        max_symbol=d.max_symbol, max_symbol_exposure_x=d.max_symbol_exposure_x,
        symbol_limit_x=d.symbol_limit_x,
        daily_pnl_pct=d.daily_pnl_pct, drawdown_limit_pct=d.drawdown_limit_pct,
        remaining_r=d.remaining_r, r_limit=d.r_limit,
        event_blocked=d.event_blocked, event_name=d.event_name,
        event_in_sec=d.event_in_sec, closes_in_sec=d.closes_in_sec,
    )


def build_snapshot(view: RiskView, at: datetime | None = None) -> SnapshotMessage:
    when = at or datetime.now(UTC)
    return SnapshotMessage(
        symbol=view.risk.symbol,
        ts=when,
        risk=build_risk(view, when),
        session=build_session(view, when),
        alerts=build_alerts(view, when),
        no_trade=build_no_trade(view, when),
        discipline=build_discipline_message(getattr(view, "discipline", None)),
    )


# ─────────────────────────────────────────────────────────
# 워치리스트 · 시장 · 브래킷 (docs/15 1-1 / 1-2 / 1-4)
# ─────────────────────────────────────────────────────────

def build_watchlist(
    tickers: list[dict[str, Any]], focus: str, collected: list[str] | set[str],
    basket_exposure_x: float | None, at: datetime | None = None,
) -> WatchlistMessage:
    """`ticker_24hr()` 결과 → 워치리스트. 유니버스(`active_symbols()`) 순서, 없는 심볼은 뺀다."""
    by_sym = {str(t.get("symbol", "")).upper(): t for t in tickers}
    coll = {c.upper() for c in collected}
    items = []
    for sym in active_symbols():
        t = by_sym.get(sym)
        if t is None:
            continue
        items.append(WatchlistItem(
            symbol=sym, price=t.get("last_price"), change_pct=t.get("change_pct"),
            quote_volume=t.get("quote_volume"), lev_tag=leverage_tag(sym),
            collected=sym in coll, group=group_of(sym),
        ))
    return WatchlistMessage(
        ts=at or datetime.now(UTC), focus=focus.upper(), items=items,
        basket_exposure_x=basket_exposure_x,
    )


def build_market(
    symbol: str, premium: dict[str, Any] | None, underlying_price: float | None,
    underlying_ticker: str | None, at: datetime | None = None,
) -> MarketMessage:
    """`premium_index()` + 본주 가격 → 시장 메시지. 베이시스는 마크가 기준."""
    mark = premium.get("mark_price") if premium else None
    basis = None
    if mark and underlying_price and underlying_price > 0:
        basis = (mark - underlying_price) / underlying_price * 100
    return MarketMessage(
        symbol=symbol.upper(), ts=at or datetime.now(UTC),
        funding_rate=premium.get("last_funding_rate") if premium else None,
        next_funding_at=premium.get("next_funding_ts") if premium else None,
        mark_price=mark, underlying_price=underlying_price, basis_pct=basis,
        underlying_ticker=underlying_ticker,
    )


def bracket_status_ko(st: BracketState, close_requested: bool,
                      at: datetime | None = None) -> str:
    """브래킷 한 줄 상태 — **서버가 만든다** (docs/15 1-4). 브라우저는 그대로 보여준다.

    "TP1 체결 → 손절 본절 이동" 은 손절이 실제로 진입가까지 올라와 있을 때만 쓴다.
    매니저가 손절을 옮기지 않았는데 그렇게 쓰면 화면이 거짓 안심을 준다.
    """
    from ..execution.models import LONG, OrderType, Purpose

    if close_requested:
        return "청산 요청됨 · 러너 대기"
    b = st.bracket
    if not st.has_position:
        e = st.order_by_purpose(Purpose.ENTRY)
        if e is None:
            return "진입 대기"
        if e.type is OrderType.LIMIT:
            now = at or datetime.now(UTC)
            left = int(b.expire_after_s - (now - e.created_at).total_seconds())
            left = min(b.expire_after_s, max(0, left))
            return f"진입 대기 · 지정가 · {left // 60}:{left % 60:02d} 후 만료"
        return "진입 대기 · 시장가"

    remain_pct = round(abs(st.position_qty) / b.qty * 100) if b.qty > 0 else 0
    tp1_done = any(f.purpose is Purpose.TP1 for f in st.fills)
    stop = st.live_stop
    if stop is None:
        return f"⚠ 손절 없음 · 잔여 {remain_pct}%"
    if tp1_done:
        entry = st.avg_entry or 0.0
        trig = stop.trigger or b.stop_price
        at_breakeven = (trig >= entry) if b.direction == LONG else (trig <= entry)
        if entry > 0 and at_breakeven:
            return "TP1 체결 → 손절 본절 이동"
        return f"TP1 체결 · 손절 대기 · 잔여 {remain_pct}%"
    return f"손절 대기 · 잔여 {remain_pct}%"


def build_brackets(
    opens: list[BracketState], history: list[dict], today_realized_r: float | None,
    mode: str, close_requested: set[str] | dict[str, Any], today: Any = None,
    at: datetime | None = None,
) -> BracketsMessage:
    """열린 브래킷 + 오늘 성적 → 브래킷 메시지. `history` 는 닫힌 브래킷 dict 목록."""
    from ..account.risk import KST

    when = at or datetime.now(UTC)
    day = today or when.astimezone(KST).date()
    items = []
    for st in opens:
        b = st.bracket
        requested = b.bracket_id in close_requested
        entry = st.avg_entry if st.avg_entry else b.entry_price
        items.append(BracketItem(
            id=b.bracket_id[-8:], symbol=b.symbol, direction=b.direction,
            entry=entry, stop=b.stop_price, tp1=b.tp1_price, tp2=b.tp2_price,
            qty=b.qty, position_qty=st.position_qty, has_stop=st.live_stop is not None,
            status_ko=bracket_status_ko(st, requested, when), realized_r=st.realized_r,
            risk_per_unit=b.risk_per_unit(entry) if entry else None,
            close_requested=requested, setup_id=b.setup_id or "",
        ))
    done = [h for h in history if h.get("realized_r") is not None]
    today_done = [
        h for h in done
        if h.get("closed_at") is not None and h["closed_at"].astimezone(KST).date() == day
    ]
    return BracketsMessage(
        ts=when, mode=mode, available=True, open=items,
        today_realized_r=today_realized_r, today_count=len(today_done),
        expectancy_r=(sum(h["realized_r"] for h in done) / len(done)) if done else None,
    )


# ─────────────────────────────────────────────────────────
# 실매매 포지션 (docs/15 8절)
# ─────────────────────────────────────────────────────────

def leverage_multiplier(symbol: str, underlying: str | None = None) -> float:
    """레버리지 ETF 배수 — `th.account.risk` 표(본주 티커 기준) 우선, 없으면 `LEVERAGE_TAG`("3x")."""
    from ..account.risk import multiplier_for

    m = multiplier_for(underlying or underlying_ticker(symbol))
    if m != 1.0:
        return m
    tag = leverage_tag(symbol)
    if tag and tag.lower().endswith("x"):
        try:
            return float(tag[:-1])
        except ValueError:
            return 1.0
    return 1.0


def position_item(p: Position, orders: list[OpenOrder] | tuple[OpenOrder, ...],
                  wallet: float, underlying: str | None = None) -> PositionItem:
    """포지션 하나 + 전 종목 미체결 주문 → 한 줄. 손절은 같은 종목의 STOP 계열 reduce-only 만 인정한다."""
    from ..account.risk import (
        find_stop_orders,
        liquidation_distance_pct,
        stop_distance_pct,
        underlying_move_to_liq_pct,
    )

    stops = find_stop_orders(orders, p)
    prices = [o.stop_price for o in stops if o.stop_price > 0]
    stop_price = None
    if prices:
        stop_price = max(prices) if p.side == "LONG" else min(prices)
    liq_pct = liquidation_distance_pct(p.side, p.mark_price, p.liquidation_price)
    return PositionItem(
        symbol=p.symbol, side=p.side, qty=p.qty, entry_price=p.entry_price,
        mark_price=p.mark_price, notional_usdt=p.notional,
        unrealized_pnl=p.unrealized_pnl, unrealized_pct=p.price_move_pct,
        liquidation_price=p.liquidation_price, liq_distance_pct=liq_pct,
        underlying_move_to_liq_pct=underlying_move_to_liq_pct(
            liq_pct, leverage_multiplier(p.symbol, underlying)),
        leverage_eff=(p.notional / wallet) if wallet > 0 else None,
        has_stop=bool(stops), stop_price=stop_price,
        stop_distance_pct=stop_distance_pct(p, stops),
    )


def build_positions(
    positions: list[Position] | tuple[Position, ...], orders: list[OpenOrder] | tuple[OpenOrder, ...],
    wallet: float, ok: bool, last_items: list[PositionItem] | None = None,
    underlying_map: dict[str, str] | None = None, at: datetime | None = None,
) -> PositionsMessage:
    """전 종목 포지션 → 메시지. `ok=False` 면 **마지막 항목을 그대로** 싣고 지갑은 None."""
    when = at or datetime.now(UTC)
    if not ok:
        return PositionsMessage(ts=when, ok=False, wallet_usdt=None, items=list(last_items or []))
    umap = underlying_map or {}
    items = [
        position_item(p, orders, wallet, umap.get(p.symbol))
        for p in positions if p.is_open
    ]
    return PositionsMessage(ts=when, ok=True, wallet_usdt=wallet, items=items)
