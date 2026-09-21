"""Phase 2 — 리스크 계산과 경고 테스트.

계정 API 는 타지 않는다. 여기서 검증하는 건 **숫자가 맞는가**와
**위험할 때 반드시 경고가 나오는가** 둘이다.
"""

from datetime import UTC, date, datetime, timedelta

from th.account.alerts import MarketStats, build_alerts, worst_level
from th.account.models import Balance, DailyPnl, OpenOrder, Position
from th.account.risk import (
    account_leverage,
    build_snapshot,
    find_stop_orders,
    liquidation_distance_pct,
    multiplier_for,
    r_unit,
    stop_distance_pct,
    underlying_move_to_liq_pct,
)
from th.sessions.models import Event, Regime, SessionContext

NOW = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)


def pos(side="LONG", qty=35.0, entry=231.4, mark=233.1, liq=209.8,
        lev=10, notional=None) -> Position:
    return Position(
        symbol="SOXLUSDT",
        side=side,
        qty=qty,
        entry_price=entry,
        mark_price=mark,
        liquidation_price=liq,
        unrealized_pnl=(mark - entry) * qty * (1 if side == "LONG" else -1),
        leverage=lev,
        margin_type="isolated",
        isolated_margin=(notional or qty * mark) / lev,
        notional=notional if notional is not None else qty * mark,
        updated_at=NOW,
    )


def order(otype="STOP_MARKET", side="SELL", stop=229.6, reduce_only=True,
          symbol="SOXLUSDT") -> OpenOrder:
    return OpenOrder(
        symbol=symbol, order_id="1", side=side, order_type=otype,
        stop_price=stop, price=0.0, qty=35.0,
        reduce_only=reduce_only, close_position=False,
    )


# ─────────────────────────────────────────────────────────
# API 응답 파싱
# ─────────────────────────────────────────────────────────

def test_position_parses_long_short_flat():
    long_p = Position.from_api(
        {"symbol": "soxlusdt", "positionAmt": "35", "entryPrice": "231.4",
         "markPrice": "233.1", "liquidationPrice": "209.8",
         "unRealizedProfit": "59.5", "leverage": "10", "marginType": "isolated",
         "isolatedMargin": "811", "notional": "8158.5", "updateTime": "1789000000000"}
    )
    assert long_p.side == "LONG"
    assert long_p.qty == 35
    assert long_p.symbol == "SOXLUSDT"
    assert long_p.is_open

    short_p = Position.from_api({"symbol": "X", "positionAmt": "-12", "markPrice": "10"})
    assert short_p.side == "SHORT"
    assert short_p.qty == 12

    flat = Position.from_api({"symbol": "X", "positionAmt": "0"})
    assert flat.side == "FLAT"
    assert not flat.is_open


def test_liquidation_price_zero_means_unknown():
    p = Position.from_api({"symbol": "X", "positionAmt": "1", "liquidationPrice": "0"})
    assert p.liquidation_price is None


def test_notional_falls_back_to_qty_times_mark():
    p = Position.from_api({"symbol": "X", "positionAmt": "2", "markPrice": "100"})
    assert p.notional == 200


# ─────────────────────────────────────────────────────────
# 청산 거리 — 이 앱의 핵심 숫자
# ─────────────────────────────────────────────────────────

def test_liquidation_distance_long():
    d = liquidation_distance_pct("LONG", 100.0, 91.0)
    assert round(d, 4) == 9.0


def test_liquidation_distance_short():
    d = liquidation_distance_pct("SHORT", 100.0, 109.0)
    assert round(d, 4) == 9.0


def test_liquidation_distance_unknown():
    assert liquidation_distance_pct("LONG", 100.0, None) is None
    assert liquidation_distance_pct("LONG", 100.0, 0) is None
    assert liquidation_distance_pct("FLAT", 100.0, 90.0) is None


def test_underlying_move_is_divided_by_etf_multiplier():
    """SOXL이 9.5% 빠져야 청산 = SOXX로는 약 3.17%."""
    assert round(underlying_move_to_liq_pct(9.5, 3.0), 2) == 3.17
    # 역방향 ETF(-3배)도 절대값으로 본다
    assert round(underlying_move_to_liq_pct(9.5, -3.0), 2) == 3.17
    # 일반 종목이면 그대로
    assert underlying_move_to_liq_pct(9.5, 1.0) == 9.5
    assert underlying_move_to_liq_pct(None, 3.0) is None


def test_multiplier_lookup():
    assert multiplier_for("SOXL") == 3.0
    assert multiplier_for("soxs") == -3.0
    assert multiplier_for("NVDA") == 1.0
    assert multiplier_for(None) == 1.0


def test_account_leverage():
    assert account_leverage(8000.0, 10000.0) == 0.8
    assert account_leverage(100000.0, 10000.0) == 10.0
    assert account_leverage(1000.0, 0.0) == 0.0


# ─────────────────────────────────────────────────────────
# 손절 주문 판별 — 10배에서 가장 흔한 사고
# ─────────────────────────────────────────────────────────

def test_stop_order_found_for_long():
    stops = find_stop_orders([order()], pos())
    assert len(stops) == 1


def test_take_profit_only_is_not_a_stop():
    """익절만 걸어두고 손절이 없는 상태를 반드시 잡아내야 한다."""
    tp = order(otype="TAKE_PROFIT_MARKET", stop=240.0)
    assert find_stop_orders([tp], pos()) == []


def test_stop_on_wrong_side_is_ignored():
    assert find_stop_orders([order(side="BUY")], pos(side="LONG")) == []
    assert find_stop_orders([order(side="SELL")], pos(side="SHORT")) == []


def test_non_reduce_only_order_is_not_a_stop():
    assert find_stop_orders([order(reduce_only=False)], pos()) == []


def test_close_position_flag_counts_as_stop():
    o = OpenOrder("SOXLUSDT", "1", "SELL", "STOP_MARKET", 229.6, 0, 35, False, True)
    assert len(find_stop_orders([o], pos())) == 1


def test_other_symbol_order_ignored():
    assert find_stop_orders([order(symbol="NVDAUSDT")], pos()) == []


def test_no_stops_when_flat():
    assert find_stop_orders([order()], pos(side="FLAT", qty=0)) == []


def test_stop_distance_picks_nearest():
    p = pos(mark=233.1)
    stops = [order(stop=225.0), order(stop=229.6)]
    d = stop_distance_pct(p, stops)
    assert round(d, 3) == round((233.1 - 229.6) / 233.1 * 100, 3)


def test_stop_distance_for_short_picks_lowest():
    p = pos(side="SHORT", mark=100.0)
    stops = [order(side="BUY", stop=105.0), order(side="BUY", stop=102.0)]
    assert round(stop_distance_pct(p, stops), 4) == 2.0


# ─────────────────────────────────────────────────────────
# 일일 손익 · R
# ─────────────────────────────────────────────────────────

def test_r_unit_is_wallet_times_risk_pct():
    assert r_unit(10000.0, 0.5) == 50.0
    assert r_unit(10000.0, 1.0) == 100.0
    assert r_unit(0.0, 0.5) == 0.0


def test_daily_r_value_and_lock():
    d = DailyPnl(date(2026, 9, 10), realized_pnl=-160.0, commission=-8.0,
                 r_unit=50.0, trade_count=4)
    assert round(d.net, 2) == -168.0
    assert round(d.r_value, 3) == -3.36
    assert d.locked() is True


def test_daily_not_locked_above_limit():
    d = DailyPnl(date(2026, 9, 10), realized_pnl=-60.0, r_unit=50.0)
    assert d.locked() is False


def test_lock_persists_even_if_recovered():
    """한 번 한도에 닿으면 손실을 회복해도 그날은 잠긴다."""
    d = DailyPnl(date(2026, 9, 10), realized_pnl=+100.0, r_unit=50.0,
                 limit_hit_at=NOW)
    assert d.r_value > 0
    assert d.locked() is True


# ─────────────────────────────────────────────────────────
# 스냅샷 조립
# ─────────────────────────────────────────────────────────

def snap(position=None, orders=None, daily=None, wallet=10000.0, underlying="SOXL"):
    return build_snapshot(
        symbol="SOXLUSDT",
        position=position if position is not None else pos(),
        balance=Balance(wallet, wallet * 0.9, 0.0, 0.0),
        orders=orders or [],
        daily=daily,
        underlying=underlying,
        at=NOW,
    )


def test_snapshot_computes_everything():
    s = snap(orders=[order()])
    assert s.has_position
    assert s.has_stop
    assert s.liq_distance_pct is not None
    assert s.underlying_multiplier == 3.0
    assert s.underlying_leverage is not None
    # 계좌 대비 실효 레버리지 × ETF 3배
    assert round(s.underlying_leverage, 3) == round(s.account_leverage * 3, 3)


def test_snapshot_without_position_is_quiet():
    s = snap(position=pos(side="FLAT", qty=0))
    assert not s.has_position
    assert s.liq_distance_pct is None
    assert s.account_leverage == 0.0


# ─────────────────────────────────────────────────────────
# 경고
# ─────────────────────────────────────────────────────────

def codes(alerts) -> set[str]:
    return {a.code for a in alerts}


def ctx(regime=Regime.RTH, ends_in=7200, event=None, warnings=()) -> SessionContext:
    return SessionContext(
        symbol="SOXLUSDT",
        at=NOW,
        regime=regime,
        regime_ends_at=NOW + timedelta(seconds=ends_in),
        is_trading_day=True,
        early_close=False,
        blocked=False,
        next_event=event,
        warnings=tuple(warnings),
    )


def test_no_stop_order_alert_is_critical():
    alerts = build_alerts(snap(orders=[]))
    assert "NO_STOP_ORDER" in codes(alerts)
    assert alerts[0].level == "critical"


def test_no_stop_alert_disappears_when_stop_exists():
    assert "NO_STOP_ORDER" not in codes(build_alerts(snap(orders=[order()])))


def test_no_stop_alert_not_raised_without_position():
    s = snap(position=pos(side="FLAT", qty=0), orders=[])
    assert "NO_STOP_ORDER" not in codes(build_alerts(s))


def test_liquidation_proximity_levels():
    near = snap(position=pos(mark=100.0, liq=98.0), orders=[order(stop=97.0)])
    a = [x for x in build_alerts(near) if x.code == "LIQ_PROXIMITY"]
    assert a and a[0].level == "critical"

    mid = snap(position=pos(mark=100.0, liq=95.0), orders=[order(stop=94.0)])
    a = [x for x in build_alerts(mid) if x.code == "LIQ_PROXIMITY"]
    assert a and a[0].level == "warning"

    far = snap(position=pos(mark=100.0, liq=90.0), orders=[order(stop=89.0)])
    assert "LIQ_PROXIMITY" not in codes(build_alerts(far))


def test_liquidation_alert_mentions_underlying_move():
    near = snap(position=pos(mark=100.0, liq=98.0), orders=[order(stop=97.0)])
    a = [x for x in build_alerts(near) if x.code == "LIQ_PROXIMITY"][0]
    assert "기초지수" in a.detail_ko


def test_daily_loss_limit_alert():
    d = DailyPnl(date(2026, 9, 10), realized_pnl=-200.0, r_unit=50.0, trade_count=5)
    assert "DAILY_LOSS_LIMIT" in codes(build_alerts(snap(orders=[order()], daily=d)))


def test_event_imminent_only_with_position():
    e = Event(name="미국 CPI", ts_utc=NOW + timedelta(minutes=10),
              kind="econ", importance="high")
    with_pos = build_alerts(snap(orders=[order()]), ctx(event=e))
    assert "EVENT_IMMINENT" in codes(with_pos)

    flat = build_alerts(snap(position=pos(side="FLAT", qty=0)), ctx(event=e))
    assert "EVENT_IMMINENT" not in codes(flat)


def test_event_imminent_ignores_far_and_low_importance():
    far = Event("미국 CPI", NOW + timedelta(hours=3), "econ", "high")
    assert "EVENT_IMMINENT" not in codes(build_alerts(snap(orders=[order()]), ctx(event=far)))

    low = Event("산업생산", NOW + timedelta(minutes=5), "econ", "low")
    assert "EVENT_IMMINENT" not in codes(build_alerts(snap(orders=[order()]), ctx(event=low)))


def test_regime_closing_alert():
    c = ctx(regime=Regime.RTH, ends_in=600)
    assert "REGIME_CLOSING" in codes(build_alerts(snap(orders=[order()]), c))


def test_weekend_warning_becomes_alert():
    c = ctx(warnings=("주말 진입 60분 전 — 월요일까지 perp만 가격을 만듭니다",))
    assert "REGIME_CLOSING" in codes(build_alerts(snap(orders=[order()]), c))


def test_vol_spike_alert():
    m = MarketStats(atr_pct=0.62, atr_percentile=97.0)
    assert "VOL_SPIKE" in codes(build_alerts(snap(orders=[order()]), None, m))

    calm = MarketStats(atr_pct=0.28, atr_percentile=61.0)
    assert "VOL_SPIKE" not in codes(build_alerts(snap(orders=[order()]), None, calm))


def test_liquidation_cascade_alert():
    m = MarketStats(liquidation_burst_ratio=8.2)
    assert "LIQUIDATION_CASCADE" in codes(build_alerts(snap(orders=[order()]), None, m))

    quiet = MarketStats(liquidation_burst_ratio=1.4)
    assert "LIQUIDATION_CASCADE" not in codes(build_alerts(snap(orders=[order()]), None, quiet))


def test_missing_market_stats_are_skipped_not_crashed():
    alerts = build_alerts(snap(orders=[order()]), None, MarketStats())
    assert "VOL_SPIKE" not in codes(alerts)
    assert "LIQUIDATION_CASCADE" not in codes(alerts)


def test_alerts_sorted_critical_first():
    """청산 경고가 정보성 알림보다 위에 온다."""
    d = DailyPnl(date(2026, 9, 10), realized_pnl=-200.0, r_unit=50.0)
    alerts = build_alerts(
        snap(position=pos(mark=100.0, liq=98.0), orders=[], daily=d),
        ctx(ends_in=600),
        MarketStats(atr_pct=0.6, atr_percentile=99.0),
    )
    levels = [a.level for a in alerts]
    assert levels == sorted(levels, key=lambda x: {"critical": 0, "warning": 1, "info": 2}[x])
    assert alerts[0].level == "critical"
    assert worst_level(alerts) == "critical"


def test_no_alerts_is_empty():
    assert build_alerts(snap(orders=[order()])) == []
    assert worst_level([]) is None
