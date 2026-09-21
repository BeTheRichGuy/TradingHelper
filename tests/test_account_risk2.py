"""Phase 2 안전장치 — 신호 기능을 빼도 남는 부분이라 두껍게 검증한다.

Phase 4 판정으로 신호 카드는 빠졌다. 그러면 이 앱이 실제로 하는 일은
**10배 레버리지에서 계좌가 죽는 경로를 막는 것** 하나로 좁혀진다.
그 하나는 조용히 틀리면 안 된다.

특히 노리는 것: "손절 주문이 있다"는 표시가 **거짓 안심**이 되는 상태들.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from th.account.alerts import build_alerts, worst_level
from th.account.models import Balance, DailyPnl, OpenOrder, Position, RiskSnapshot
from th.account.risk import (
    account_leverage,
    build_snapshot,
    find_stop_orders,
    kst_today,
    liquidation_distance_pct,
    multiplier_for,
    r_unit,
    stop_distance_pct,
    underlying_move_to_liq_pct,
)

T0 = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)


def _pos(side, mark, liq, qty=50.0, symbol="SOXLUSDT") -> Position:
    return Position(
        symbol=symbol, side=side, qty=qty, entry_price=mark, mark_price=mark,
        liquidation_price=liq, unrealized_pnl=0.0, leverage=10,
        margin_type="isolated", isolated_margin=qty * mark / 10,
        notional=qty * mark, updated_at=T0,
    )


def long_pos(mark=100.0, liq=91.0, qty=50.0, symbol="SOXLUSDT") -> Position:
    return _pos("LONG", mark, liq, qty, symbol)


def short_pos(mark=100.0, liq=109.0, qty=50.0) -> Position:
    return _pos("SHORT", mark, liq, qty)


def stop(price, side="SELL", symbol="SOXLUSDT", reduce_only=True,
        kind="STOP_MARKET", close_position=False) -> OpenOrder:
    return OpenOrder(symbol=symbol, order_id="1", side=side, order_type=kind,
                     stop_price=price, price=0.0, qty=50.0,
                     reduce_only=reduce_only, close_position=close_position)


def balance(wallet=10_000.0) -> Balance:
    return Balance(wallet=wallet, available=wallet * 0.9,
                   unrealized_pnl=0.0, margin_used=wallet * 0.1)


def daily_pnl(net, worst_hit=None, count=5, unit=50.0) -> DailyPnl:
    return DailyPnl(date_kst=kst_today(T0), realized_pnl=net, trade_count=count,
                    r_unit=unit, limit_hit_at=worst_hit)


def snap(position, orders=(), daily=None, underlying="SOXL") -> RiskSnapshot:
    return build_snapshot(
        symbol="SOXLUSDT", position=position, balance=balance(),
        orders=list(orders), daily=daily, underlying=underlying, at=T0,
    )


# ─────────────────────────────────────────────────────────
# 거짓 안심 — 손절이 있는데 소용없는 상태
# ─────────────────────────────────────────────────────────

def test_stop_beyond_liquidation_is_critical():
    """청산이 9% 앞인데 손절이 12% 뒤면, 손절은 절대 체결되지 않는다."""
    s = snap(long_pos(mark=100.0, liq=91.0), [stop(88.0)])
    assert s.has_stop                       # 화면엔 '손절 있음'으로 보인다
    codes = [a.code for a in build_alerts(s)]
    assert "STOP_BEYOND_LIQUIDATION" in codes
    assert worst_level(build_alerts(s)) == "critical"


def test_stop_inside_liquidation_is_fine():
    s = snap(long_pos(mark=100.0, liq=91.0), [stop(99.0)])
    codes = [a.code for a in build_alerts(s)]
    assert "STOP_BEYOND_LIQUIDATION" not in codes
    assert "NO_STOP_ORDER" not in codes


def test_stop_beyond_liquidation_for_short():
    s = snap(short_pos(mark=100.0, liq=109.0), [stop(112.0, side="BUY")])
    assert "STOP_BEYOND_LIQUIDATION" in [a.code for a in build_alerts(s)]


def test_stop_already_passed_is_critical():
    """롱인데 손절가가 현재가보다 위 — 주문은 남아 있어도 보호가 아니다."""
    s = snap(long_pos(mark=100.0, liq=91.0), [stop(103.0)])
    assert s.stop_distance_pct is not None and s.stop_distance_pct < 0
    assert "STOP_ALREADY_PASSED" in [a.code for a in build_alerts(s)]


def test_short_stop_already_passed():
    s = snap(short_pos(mark=100.0, liq=109.0), [stop(97.0, side="BUY")])
    assert "STOP_ALREADY_PASSED" in [a.code for a in build_alerts(s)]


def test_the_two_stop_alerts_never_fire_together():
    """둘 다 뜨면 사용자가 뭘 고쳐야 할지 모른다. 이미 지난 쪽이 먼저다."""
    s = snap(long_pos(mark=100.0, liq=91.0), [stop(103.0)])
    codes = [a.code for a in build_alerts(s)]
    assert not ("STOP_ALREADY_PASSED" in codes and "STOP_BEYOND_LIQUIDATION" in codes)


def test_no_stop_alerts_without_a_position():
    s = snap(None, [stop(88.0)])
    codes = [a.code for a in build_alerts(s)]
    assert "STOP_BEYOND_LIQUIDATION" not in codes
    assert "STOP_ALREADY_PASSED" not in codes
    assert "NO_STOP_ORDER" not in codes


def test_unknown_liquidation_does_not_produce_a_false_alert():
    """청산가를 모를 때 '손절이 더 멀다'고 단정하면 안 된다."""
    s = snap(long_pos(liq=0.0), [stop(88.0)])
    assert "STOP_BEYOND_LIQUIDATION" not in [a.code for a in build_alerts(s)]


# ─────────────────────────────────────────────────────────
# 손절 주문 판별 — 안 세어야 할 것을 세면 거짓 안심이다
# ─────────────────────────────────────────────────────────

def test_a_limit_order_is_not_a_stop():
    s = snap(long_pos(), [stop(95.0, kind="LIMIT")])
    assert not s.has_stop
    assert "NO_STOP_ORDER" in [a.code for a in build_alerts(s)]


def test_a_stop_without_reduce_only_is_not_protection():
    """reduceOnly 가 아니면 포지션을 뒤집는 주문일 수 있다."""
    s = snap(long_pos(), [stop(95.0, reduce_only=False)])
    assert not s.has_stop


def test_close_position_flag_is_accepted_even_without_reduce_only():
    s = snap(long_pos(), [stop(95.0, reduce_only=False, close_position=True)])
    assert s.has_stop


def test_stop_for_another_symbol_is_ignored():
    s = snap(long_pos(), [stop(95.0, symbol="TQQQUSDT")])
    assert not s.has_stop


def test_stop_on_the_same_side_is_ignored():
    """롱인데 BUY 스톱은 손절이 아니라 추가 진입이다."""
    s = snap(long_pos(), [stop(95.0, side="BUY")])
    assert not s.has_stop


def test_zero_stop_price_is_excluded_from_distance():
    """closePosition 시장가 스톱은 stopPrice 가 0일 수 있다."""
    orders = [stop(0.0, close_position=True), stop(97.0)]
    s = snap(long_pos(), orders)
    assert s.has_stop
    assert s.stop_distance_pct == pytest.approx(3.0)


def test_nearest_stop_wins_for_long_and_short():
    assert stop_distance_pct(long_pos(mark=100.0), [stop(95.0), stop(98.0)]) == pytest.approx(2.0)
    assert stop_distance_pct(
        short_pos(mark=100.0), [stop(105.0, side="BUY"), stop(102.0, side="BUY")]
    ) == pytest.approx(2.0)


def test_stop_distance_is_none_when_mark_is_zero():
    p = _pos("LONG", mark=0.0, liq=0.0, qty=1.0)
    assert stop_distance_pct(p, [stop(1.0)]) is None


# ─────────────────────────────────────────────────────────
# 청산 거리와 기초지수 환산 — 이 앱의 대표 숫자
# ─────────────────────────────────────────────────────────

def test_soxl_9_5_percent_is_soxx_3_2_percent():
    """SOXL 은 SOXX 3배다. 체감이 완전히 다르다는 것이 이 앱의 요지."""
    liq = liquidation_distance_pct("LONG", 100.0, 90.5)
    assert liq == pytest.approx(9.5)
    assert underlying_move_to_liq_pct(liq, multiplier_for("SOXL")) == pytest.approx(3.1667, abs=1e-3)


def test_inverse_etf_uses_absolute_multiplier():
    """SOXS 는 −3배지만 '몇 % 움직이면'은 부호가 없다."""
    assert underlying_move_to_liq_pct(9.0, multiplier_for("SOXS")) == pytest.approx(3.0)


def test_unknown_underlying_falls_back_to_one():
    assert multiplier_for(None) == 1.0
    assert multiplier_for("정체불명") == 1.0
    assert underlying_move_to_liq_pct(9.0, 1.0) == pytest.approx(9.0)


def test_liquidation_distance_is_none_when_unknown():
    assert liquidation_distance_pct("LONG", 100.0, 0.0) is None
    assert liquidation_distance_pct("LONG", 100.0, None) is None
    assert underlying_move_to_liq_pct(None, 3.0) is None


def test_account_leverage_survives_zero_wallet():
    assert account_leverage(10_000.0, 0.0) == 0.0
    assert account_leverage(8_000.0, 1_000.0) == pytest.approx(8.0)


# ─────────────────────────────────────────────────────────
# 일일 한도 — 한 번 잠기면 회복해도 안 풀린다
# ─────────────────────────────────────────────────────────

def test_daily_lock_does_not_release_after_recovery():
    """−3R 을 찍고 회복해도 그날은 잠긴 채로 둔다. 그게 규칙이다."""
    hit = daily_pnl(-160.0)
    assert hit.locked(-3.0)
    recovered = daily_pnl(+20.0, worst_hit=T0)      # 한 번 닿은 기록이 남는다
    assert recovered.r_value > 0
    assert recovered.locked(-3.0), "회복했다고 잠금이 풀리면 한도의 의미가 없다"


def test_daily_not_locked_before_the_limit():
    assert not daily_pnl(-100.0).locked(-3.0)


def test_r_unit_never_negative():
    assert r_unit(10_000.0, 0.5) == pytest.approx(50.0)
    assert r_unit(-100.0, 0.5) == 0.0


def test_kst_day_rolls_at_korean_midnight():
    """한국 자정 기준으로 하루를 센다 — 미국장 한복판이다."""
    before = datetime(2026, 9, 10, 14, 59, tzinfo=UTC)   # 23:59 KST
    after = datetime(2026, 9, 10, 15, 1, tzinfo=UTC)     # 00:01 KST 다음날
    assert kst_today(before) != kst_today(after)


# ─────────────────────────────────────────────────────────
# 스냅샷이 조용히 죽지 않는가
# ─────────────────────────────────────────────────────────

def test_snapshot_with_no_position_is_quiet():
    s = snap(None)
    assert not s.has_position and not s.has_stop
    assert build_alerts(s) == []


def test_snapshot_survives_missing_everything():
    s = build_snapshot(
        symbol="SOXLUSDT", position=None, balance=balance(0.0),
        orders=[], daily=None, underlying=None, at=T0,
        errors=("계정 조회 실패",),
    )
    assert s.errors
    assert build_alerts(s) == []


def test_alerts_are_sorted_with_critical_first():
    s = snap(long_pos(mark=100.0, liq=99.5))     # 손절 없음 + 청산 임박
    alerts = build_alerts(s)
    assert len(alerts) >= 2
    assert alerts[0].level == "critical"
    assert worst_level(alerts) == "critical"


def test_find_stop_orders_returns_empty_for_flat_position():
    flat = _pos("FLAT", mark=100.0, liq=0.0, qty=0.0)
    assert find_stop_orders([stop(95.0)], flat) == []


# ─────────────────────────────────────────────────────────
# 리뷰에서 확인된 버그들의 회귀 테스트
# ─────────────────────────────────────────────────────────

def test_wallet_zero_with_loss_is_locked_not_zero_r():
    """지갑이 0 이면 R 단위를 못 정한다. 그때 손실이 있으면 '0.0R' 로 풀리면 안 된다."""
    d = DailyPnl(date_kst=kst_today(T0), realized_pnl=-100.0, r_unit=0.0)
    assert d.r_value == 0.0
    assert d.locked(-3.0)
    # 손실이 없으면 잠글 이유도 없다
    assert not DailyPnl(date_kst=kst_today(T0), realized_pnl=0.0, r_unit=0.0).locked(-3.0)


def test_wallet_zero_makes_daily_unknown_in_service():
    """서비스 층: 지갑 0 이하는 income 블록 안에서 예외를 올려 daily 를 '모름'으로 남긴다."""
    import asyncio

    from th.account import service
    from th.account.service import get_risk_view

    class FakeClient:
        has_key = True

        async def positions(self, symbol=None):
            return []

        async def balance(self):
            return Balance(0.0, 0.0, 0.0, 0.0)

        async def open_orders(self, symbol=None):
            return []

        async def daily_income(self, symbol, at):
            raise AssertionError("지갑이 0 이면 income 까지 가면 안 된다")

        async def close(self):
            return None

    view = asyncio.run(get_risk_view("SOXLUSDT", client=FakeClient(), with_market=False))
    assert view.risk.daily is None
    assert view.risk.account_ok, "계정 조회 자체는 성공했다"
    assert any("당일 손익" in e for e in view.risk.errors)
    assert service.DAILY_LOSS_LIMIT_R == -3.0


def test_account_error_message_does_not_leak_request_details():
    """httpx 예외 문자열에는 서명된 URL(API 키)이 들어간다 — 타입·상태코드만 남긴다."""
    import asyncio

    from th.account.service import get_risk_view

    class Resp:
        status_code = 418

    class Boom(Exception):
        def __init__(self):
            super().__init__("GET https://fapi.binance.com/x?signature=SECRET123")
            self.response = Resp()

    class FakeClient:
        has_key = True

        async def positions(self, symbol=None):
            raise Boom()

        async def close(self):
            return None

    view = asyncio.run(get_risk_view("SOXLUSDT", client=FakeClient(), with_market=False))
    assert not view.risk.account_ok
    err = " ".join(view.risk.errors)
    assert "SECRET123" not in err and "signature" not in err
    assert "Boom" in err and "418" in err


def test_carry_forward_keeps_position_only_when_account_query_failed():
    """income/DB 만 실패한 턴은 포지션을 제대로 봤으므로 직전 포지션을 끌어오면 안 된다."""
    from dataclasses import replace

    from th.account.alerts import MarketStats
    from th.account.service import RiskView, carry_forward

    prev = RiskView(risk=snap(long_pos()), alerts=[], market=MarketStats())
    assert prev.risk.has_position

    # 1) 계정 조회 자체가 실패 → 직전 포지션 유지
    failed = build_snapshot(
        symbol="SOXLUSDT", position=None, balance=balance(0.0), orders=[], daily=None,
        underlying="SOXL", at=T0 + timedelta(seconds=5),
        errors=("계정 조회 실패: TimeoutException",), account_ok=False,
    )
    v = carry_forward(RiskView(risk=failed, alerts=[], market=MarketStats()), prev)
    assert v.risk.has_position, "계정 조회가 실패했으면 마지막으로 알던 포지션을 지켜야 한다"
    assert v.risk.errors

    # 2) 포지션은 잘 봤는데(닫혔음) income 만 실패 → 그대로 '포지션 없음'
    closed = build_snapshot(
        symbol="SOXLUSDT", position=None, balance=balance(), orders=[], daily=None,
        underlying="SOXL", at=T0 + timedelta(seconds=5),
        errors=("당일 손익 조회 실패: ReadTimeout",), account_ok=True,
    )
    v = carry_forward(RiskView(risk=closed, alerts=[], market=MarketStats()), prev)
    assert not v.risk.has_position, "income 만 실패한 턴에 닫힌 포지션을 되살리면 안 된다"

    # 기본값은 True — 웹 허브·th watch 의 기존 호출이 그대로 동작한다
    assert replace(closed).account_ok is True


# ═════════════════════════════════════════════════════════
# 규율 지표 (docs/15 1-3) — 전 종목 노출 · 당일 손익 · 남은 R
# ═════════════════════════════════════════════════════════

def _view(positions=(), wallet=10_000.0, daily=None, account_ok=True, errors=()):
    from th.account.alerts import MarketStats
    from th.account.service import RiskView

    mine = next((p for p in positions if p.symbol == "SOXLUSDT"), None)
    snap = build_snapshot(
        symbol="SOXLUSDT", position=mine, balance=balance(wallet), orders=[], daily=daily,
        underlying="SOXL", at=T0, errors=tuple(errors), account_ok=account_ok,
    )
    return RiskView(risk=snap, alerts=[], market=MarketStats(), positions=tuple(positions))


def _daily(net_r: float, locked_at=None, r_unit=100.0) -> DailyPnl:
    return DailyPnl(date_kst=kst_today(T0), realized_pnl=net_r * r_unit, r_unit=r_unit,
                    limit_hit_at=locked_at)


def test_discipline_limits_are_declared_in_risk():
    from th.account import risk

    assert risk.EXPOSURE_LIMIT_X == 3.0
    assert risk.SYMBOL_LIMIT_X == 1.0
    assert risk.DRAWDOWN_LIMIT_PCT == 15.0
    assert risk.DAILY_LOSS_LIMIT_R == -3.0


def test_discipline_exposure_sums_every_symbol_and_finds_the_biggest():
    from th.account.service import build_discipline

    ps = [long_pos(mark=100.0, qty=90.0),                     # 9,000 노셔널 (0.9x)
          long_pos(mark=50.0, qty=240.0, symbol="NVDAUSDT")]  # 12,000 노셔널 (1.2x)
    d = build_discipline(_view(ps, daily=_daily(-0.4)), None, at=T0)
    assert d.ok
    assert abs(d.total_exposure_x - 2.1) < 1e-9
    assert d.max_symbol == "NVDAUSDT" and abs(d.max_symbol_exposure_x - 1.2) < 1e-9
    assert d.total_limit_x == 3.0 and d.symbol_limit_x == 1.0 and d.drawdown_limit_pct == 15.0
    assert abs(d.remaining_r - 2.6) < 1e-9 and d.r_limit == 3.0


def test_discipline_daily_pnl_includes_unrealized_of_every_symbol():
    from dataclasses import replace

    from th.account.service import build_discipline

    ps = [replace(long_pos(), unrealized_pnl=-120.0),
          replace(long_pos(symbol="NVDAUSDT"), unrealized_pnl=-60.0)]
    d = build_discipline(_view(ps, daily=_daily(-0.5)), None, at=T0)     # 실현 -50
    assert abs(d.daily_pnl_pct - (-50.0 - 180.0) / 10_000 * 100) < 1e-9   # -2.3 %


def test_discipline_remaining_r_is_zero_when_locked_and_never_negative():
    from th.account.service import build_discipline

    d = build_discipline(_view(daily=_daily(-1.0, locked_at=T0)), None, at=T0)
    assert d.remaining_r == 0.0, "오늘 잠겼으면 남은 R 은 0"
    d = build_discipline(_view(daily=_daily(-3.5)), None, at=T0)
    assert d.remaining_r == 0.0, "한도 밑으로 내려가도 음수가 아니라 0"
    d = build_discipline(_view(daily=_daily(+1.5)), None, at=T0)
    assert d.remaining_r == 3.0, "수익 중이면 한도 전부가 남아 있다"
    d = build_discipline(_view(daily=None), None, at=T0)
    assert d.remaining_r is None and d.daily_pnl_pct is None, "손익을 모르면 0 이 아니라 None"


def test_discipline_without_positions_is_zero_not_none():
    from th.account.service import build_discipline

    d = build_discipline(_view(), None, at=T0)
    assert d.ok and d.total_exposure_x == 0.0 and d.max_symbol_exposure_x == 0.0
    assert d.max_symbol is None


def test_discipline_account_failure_is_ok_false_with_nulls():
    """조회 실패는 '노출 없음'이 아니다. 숫자는 None, 한도 상수와 세션 값은 남는다."""
    from th.account.service import build_discipline

    d = build_discipline(_view(account_ok=False, errors=("계정 조회 실패: Timeout",)), None, at=T0)
    assert d.ok is False
    for k in ("total_exposure_x", "max_symbol_exposure_x", "daily_pnl_pct", "remaining_r"):
        assert getattr(d, k) is None, k
    assert d.total_limit_x == 3.0


def test_discipline_reads_event_and_close_from_the_session():
    from datetime import timedelta

    from th.account.service import build_discipline
    from th.sessions.models import Block, Event, Regime, SessionContext

    ev = Event(name="FOMC 금리 결정", ts_utc=T0 + timedelta(seconds=2480), kind="fomc",
               importance="high")
    ctx = SessionContext(
        symbol="SOXLUSDT", at=T0, regime=Regime.RTH, regime_ends_at=T0 + timedelta(hours=2),
        is_trading_day=True, early_close=False, blocked=True,
        blocks=(Block(rule_id="event:fomc", reason_ko="FOMC 41분 후"),), next_event=ev,
    )
    d = build_discipline(_view(), ctx, at=T0, closes_at=T0 + timedelta(seconds=15160))
    assert d.event_blocked is True
    assert d.event_name == "FOMC 금리 결정" and d.event_in_sec == 2480
    assert d.closes_in_sec == 15160
    # 세션 차단이 이벤트 사유가 아니면 event_blocked 는 False
    ctx2 = SessionContext(
        symbol="SOXLUSDT", at=T0, regime=Regime.RTH, regime_ends_at=None,
        is_trading_day=True, early_close=False, blocked=True,
        blocks=(Block(rule_id="opening_15m", reason_ko="개장 15분"),),
    )
    d2 = build_discipline(_view(), ctx2, at=T0, closes_at=None)
    assert d2.event_blocked is False and d2.event_name is None and d2.closes_in_sec is None


def test_carry_forward_keeps_positions_and_discipline_on_account_failure():
    from th.account.service import build_discipline, carry_forward

    prev = _view([long_pos(qty=90.0)], daily=_daily(-0.4))
    prev.discipline = build_discipline(prev, None, at=T0)
    assert prev.discipline.ok and abs(prev.discipline.total_exposure_x - 0.9) < 1e-9

    failed = _view(account_ok=False, errors=("계정 조회 실패: Timeout",))
    v = carry_forward(failed, prev)
    assert v.positions == prev.positions, "전 종목 포지션도 마지막 값을 지킨다"
    assert v.discipline is not None and v.discipline.ok is False
    assert abs(v.discipline.total_exposure_x - 0.9) < 1e-9, "값은 남기되 ok=False 로 표시"

    # 성공한 턴은 끌어오지 않는다
    fine = _view([], daily=_daily(0.0))
    v2 = carry_forward(fine, prev)
    assert v2.positions == () and v2.discipline is None


def test_discipline_message_nulls_numbers_when_not_ok():
    """carry_forward 가 붙여 둔 값이 있어도 와이어에는 null — 브라우저가 마지막 값을 흐리게 유지한다."""
    import json

    from th.account.models import Discipline
    from th.api.messages import build_discipline_message

    stale = Discipline(ok=False, total_exposure_x=0.9, max_symbol="SOXLUSDT",
                       max_symbol_exposure_x=0.9, daily_pnl_pct=-1.8, remaining_r=2.6,
                       event_blocked=True, event_name="FOMC 금리 결정", event_in_sec=2480,
                       closes_in_sec=15160)
    m = json.loads(build_discipline_message(stale).model_dump_json())
    assert m["ok"] is False
    for k in ("total_exposure_x", "max_symbol", "max_symbol_exposure_x", "daily_pnl_pct", "remaining_r"):
        assert m[k] is None, k
    assert m["event_blocked"] is True and m["event_in_sec"] == 2480 and m["closes_in_sec"] == 15160
    assert build_discipline_message(None) is None


def test_get_risk_view_queries_positions_once_for_every_symbol():
    """positionRisk 를 심볼 없이 한 번 불러 이 심볼도 고르고 총 노출도 센다."""
    import asyncio

    from th.account.service import get_risk_view

    calls = []

    class FakeClient:
        has_key = True

        async def positions(self, symbol=None):
            calls.append(symbol)
            return [long_pos(qty=90.0), long_pos(symbol="NVDAUSDT", qty=10.0),
                    _pos("FLAT", 100.0, None, qty=0.0, symbol="AMDUSDT")]

        async def balance(self):
            return balance()

        async def open_orders(self, symbol=None):
            calls.append(("orders", symbol))
            return []

        async def close(self):
            return None

    view = asyncio.run(get_risk_view("SOXLUSDT", client=FakeClient(),
                                     with_income=False, with_market=False))
    assert calls == [None, ("orders", None)], "포지션도 미체결 주문도 심볼 없이 한 번만 불러야 한다"
    assert view.risk.has_position and view.risk.position.qty == 90.0
    assert [p.symbol for p in view.positions] == ["SOXLUSDT", "NVDAUSDT"], "FLAT 은 뺀다"
