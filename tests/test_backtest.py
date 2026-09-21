"""Phase 4 — 백테스트 테스트.

지금까지 이 프로젝트에서 실제로 터진 버그의 종류를 그대로 겨냥한다.

  1. 미래 누수      — 신호 봉을 체결 판정에 다시 쓰는 것
  2. 미완성 봉      — 아직 안 닫힌 것을 완성된 것처럼 쓰는 것
  3. 낙관적 가정    — 진입은 무조건 되고, 같은 봉이면 익절이 먼저라고 치는 것
  4. 비용 누락      — 수수료·슬리피지·펀딩을 빼먹는 것
  5. 임포트 오염    — 순수 계산 모듈이 DB·네트워크를 끌고 오는 것
  6. 표본 착시      — 20건 승률 60%를 성과라고 부르는 것
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from th.backtest.metrics import (
    equity_curve,
    evaluate_gates,
    group_by,
    histogram,
    max_drawdown_r,
    max_loss_streak,
    quartiles,
    summarize,
    wilson_ci,
)
from th.backtest.models import BacktestConfig, Costs, Trade
from th.backtest.simulate import find_fill, simulate_trade
from th.backtest.walkforward import split_in_out, walk_forward
from th.strategy.indicators import Bar
from th.strategy.risk import build_plan
from th.strategy.setups.base import EntryZone

T0 = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
CFG = BacktestConfig(wallet=10_000.0, risk_pct=0.5)
FREE = BacktestConfig(costs=Costs(taker_fee_pct=0.0, slippage_pct=0.0))


def bar(minute: int, o: float, h: float, low: float, c: float, volume: float = 100.0) -> Bar:
    return Bar(
        ts=T0 + timedelta(minutes=minute),
        open=o, high=h, low=low, close=c, volume=volume, taker_buy=volume / 2,
    )


def flat(minute: int, price: float) -> Bar:
    return bar(minute, price, price, price, price)


def long_plan(entry_high=100.0, atr=0.4, swing=99.8, price=100.0):
    """기준 진입가 100, 손절 99.4(0.6%), TP1 101.2, TP2 102.4."""
    return build_plan(
        "long", EntryZone(99.5, entry_high), atr=atr, price=price, swing=swing,
        wallet=10_000.0, risk_pct=0.5,
    )


def sim(plan, forward, cfg=CFG, **kw):
    return simulate_trade(
        plan, forward, signal_at=T0, setup_id="S1_vwap_pullback",
        regime="RTH", score=70, config=cfg, **kw,
    )


# ─────────────────────────────────────────────────────────
# 1. 미래 누수
# ─────────────────────────────────────────────────────────

def test_signal_bar_itself_is_never_used_for_fills():
    """신호 시각의 봉은 이미 신호 계산에 쓰였다. 체결 판정에 또 쓰면 누수다."""
    plan = long_plan()
    signal_bar = Bar(T0, 100.0, 105.0, 100.0, 104.0, volume=100.0)   # ts == signal_at
    out = sim(plan, [signal_bar])
    assert out == "no_forward_data"


def test_fill_requires_the_trigger_to_be_touched():
    plan = long_plan()
    never = [flat(i, 99.0) for i in range(1, 10)]     # 트리거 100 에 안 닿음
    assert sim(plan, never) == "no_fill"


def test_fill_window_is_bounded():
    plan = long_plan()
    late = [flat(i, 99.0) for i in range(1, 6)] + [bar(6, 99.0, 101.0, 99.0, 101.0)]
    assert sim(plan, late) == "no_fill"          # 3봉 지나서 닿았다
    hit = find_fill(plan, late, timeout_bars=10)
    assert hit is not None and hit[0] == 5


def test_gap_through_the_trigger_fills_at_the_worse_price():
    plan = long_plan()
    gapped = [bar(1, 100.9, 101.0, 100.8, 100.9)]     # 트리거 위에서 갭 시가
    idx, price = find_fill(plan, gapped, 3)
    assert idx == 0
    assert price == pytest.approx(100.9)             # 100.0 이 아니라 시가


# ─────────────────────────────────────────────────────────
# 2·3. 같은 봉 안의 순서 — 손절이 먼저
# ─────────────────────────────────────────────────────────

def test_stop_wins_when_one_bar_touches_both():
    """1분봉 안에서 고가와 저가 중 뭐가 먼저였는지 알 수 없다. 손절로 친다."""
    plan = long_plan()
    both = [bar(1, 100.0, 101.5, 99.0, 100.0)]       # TP1 101.2 · SL 99.4 둘 다 닿음
    t = sim(plan, both, cfg=FREE)
    assert isinstance(t, Trade)
    assert t.exit_reason == "stop"
    assert t.net_r == pytest.approx(-1.0, abs=0.01)


def test_tp2_is_not_taken_in_the_same_bar_as_tp1():
    plan = long_plan()
    huge = [bar(1, 100.0, 103.0, 100.0, 103.0), flat(2, 103.0)]
    t = sim(plan, huge, cfg=FREE)
    assert isinstance(t, Trade)
    reasons = [e.reason for e in t.exits]
    assert reasons[0] == "tp1"
    assert t.bars_to_tp1 == 0
    assert reasons[1] != "tp2" or t.exits[1].at > t.exits[0].at


def test_full_winner_pays_two_r():
    """TP1 절반(+2R×0.5) + TP2 절반(+4R×0.5) = +3R."""
    plan = long_plan()
    path = [
        bar(1, 100.0, 100.1, 100.0, 100.05),
        bar(2, 100.05, 101.3, 100.0, 101.3),      # TP1
        bar(3, 101.3, 102.5, 101.2, 102.5),       # TP2
    ]
    t = sim(plan, path, cfg=FREE)
    assert isinstance(t, Trade)
    assert t.exit_reason == "tp2"
    assert t.net_r == pytest.approx(3.0, abs=0.02)


def test_breakeven_stop_after_tp1_gives_one_r():
    plan = long_plan()
    path = [
        bar(1, 100.0, 101.3, 100.0, 101.3),       # TP1 → 손절을 본전으로
        bar(2, 101.3, 101.3, 99.0, 99.5),         # 본전 이탈
    ]
    t = sim(plan, path, cfg=FREE)
    assert isinstance(t, Trade)
    assert t.exit_reason == "breakeven"
    assert t.net_r == pytest.approx(1.0, abs=0.02)


def test_deadline_closes_the_position():
    """세션이 끝나면 (본주 정지) 들고 가지 않는다."""
    plan = long_plan()
    path = [flat(i, 100.5) for i in range(1, 40)]
    t = sim(plan, path, cfg=FREE, deadline=T0 + timedelta(minutes=10))
    assert isinstance(t, Trade)
    assert t.exit_reason == "time"
    assert t.exit_at <= T0 + timedelta(minutes=11)


def test_max_hold_is_enforced_without_a_deadline():
    plan = long_plan()
    path = [flat(i, 100.5) for i in range(1, 400)]
    t = sim(plan, path, cfg=FREE)
    assert isinstance(t, Trade)
    assert t.hold_minutes <= CFG.max_hold_minutes


def test_short_is_mirrored():
    plan = build_plan(
        "short", EntryZone(100.0, 100.5), atr=0.4, price=100.0, swing=100.2,
        wallet=10_000.0, risk_pct=0.5,
    )
    # 숏 기준 진입가 = entry.low = 100.0, 손절 위, 목표 아래
    path = [
        bar(1, 100.0, 100.0, 99.9, 99.95),
        bar(2, 99.9, 99.9, 98.7, 98.7),           # TP1 아래로
        bar(3, 98.7, 98.7, 97.5, 97.5),           # TP2
    ]
    t = sim(plan, path, cfg=FREE)
    assert isinstance(t, Trade)
    assert t.direction == "short"
    assert t.net_r > 0


# ─────────────────────────────────────────────────────────
# 4. 비용
# ─────────────────────────────────────────────────────────

def test_costs_make_the_same_trade_worse():
    plan = long_plan()
    path = [
        bar(1, 100.0, 101.3, 100.0, 101.3),
        bar(2, 101.3, 102.5, 101.2, 102.5),
    ]
    free = sim(plan, path, cfg=FREE)
    paid = sim(plan, path, cfg=CFG)
    assert paid.net_r < free.net_r
    assert paid.cost_r > 0
    assert paid.gross_r < free.gross_r          # 슬리피지가 체결가까지 밀었다


def test_costs_are_material_at_this_stop_width():
    """손절 0.6% 자리에서 왕복 0.14% 는 0.23R 이다. 못 본 척하면 안 되는 크기다."""
    plan = long_plan()
    loser = [bar(1, 100.0, 100.1, 99.0, 99.0)]
    t = sim(plan, loser, cfg=CFG)
    assert t.net_r < -1.0
    assert t.cost_r == pytest.approx(0.14, abs=0.06)


def test_funding_is_charged_only_when_the_window_is_crossed():
    plan = long_plan()
    path = [bar(1, 100.0, 101.3, 100.0, 101.3), bar(2, 101.3, 102.5, 101.2, 102.5)]
    inside = sim(plan, path, cfg=FREE, funding_rate=0.0005,
                 next_funding_at=T0 + timedelta(minutes=1))
    outside = sim(plan, path, cfg=FREE, funding_rate=0.0005,
                  next_funding_at=T0 + timedelta(hours=5))
    assert inside.funding_paid_per_unit > 0
    assert outside.funding_paid_per_unit == 0
    assert inside.net_r < outside.net_r


def test_short_receives_positive_funding():
    plan = build_plan(
        "short", EntryZone(100.0, 100.5), atr=0.4, price=100.0, swing=100.2,
        wallet=10_000.0, risk_pct=0.5,
    )
    path = [bar(1, 100.0, 100.0, 99.9, 99.95), flat(2, 99.9)]
    t = sim(plan, path, cfg=FREE, funding_rate=0.0005,
            next_funding_at=T0 + timedelta(minutes=1))
    assert t.funding_paid_per_unit < 0          # 받는다


# ─────────────────────────────────────────────────────────
# 6. 성과 지표
# ─────────────────────────────────────────────────────────

def fake_trade(net_r: float, when: datetime, regime="RTH", setup="S1_vwap_pullback",
               hold=30) -> Trade:
    return Trade(
        setup_id=setup, direction="long", regime=regime, score=70,
        signal_at=when, entry_at=when, entry_ref=100.0, fill_price=100.0,
        stop_loss=99.4, tp1=101.2, tp2=102.4, r_per_unit=0.6,
        exits=(), exit_at=when + timedelta(minutes=hold),
        exit_reason="tp2" if net_r > 0 else "stop",
        gross_r=net_r, cost_r=0.1, net_r=net_r,
        bars_to_tp1=5 if net_r > 0 else None, hold_minutes=hold,
    )


def series(rs: list[float], spacing_minutes: int = 600) -> list[Trade]:
    return [fake_trade(r, T0 + timedelta(minutes=i * spacing_minutes))
            for i, r in enumerate(rs)]


def test_wilson_interval_is_wide_when_the_sample_is_small():
    """20건 승률 60% 로는 아무 말도 할 수 없다는 걸 숫자로 보여줘야 한다."""
    lo, hi = wilson_ci(12, 20)
    assert lo < 0.40 and hi > 0.78
    lo2, hi2 = wilson_ci(600, 1000)
    assert hi2 - lo2 < 0.07                      # 표본이 크면 좁아진다


def test_win_rate_alone_can_be_misleading():
    """승률 70% 인데 기대값은 음수인 경우 — 지표를 하나만 보면 안 된다."""
    s = summarize(series([0.5] * 7 + [-2.0] * 3))
    assert s.win_rate == pytest.approx(0.7)
    assert s.expectancy_r < 0


def test_expectancy_and_drawdown():
    s = summarize(series([1.0, -1.0, 1.0, -1.0, 2.0]))
    assert s.n == 5
    assert s.expectancy_r == pytest.approx(0.4)
    assert s.total_r == pytest.approx(2.0)
    assert s.max_drawdown_r == pytest.approx(1.0)


def test_drawdown_and_streak_edges():
    assert max_drawdown_r([]) == 0.0
    assert max_drawdown_r([1.0, 2.0, 3.0]) == 0.0
    assert max_loss_streak(series([-1, -1, -1, 1, -1, -1])) == 3


def test_equity_curve_is_cumulative():
    assert equity_curve(series([1.0, -0.5, 0.5])) == pytest.approx([1.0, 0.5, 1.0])


def test_profit_factor_is_none_without_losses():
    assert summarize(series([1.0, 2.0])).profit_factor is None


def test_dd_ratio_is_none_when_there_is_no_profit():
    s = summarize(series([-1.0, -1.0]))
    assert s.total_r < 0
    assert s.dd_ratio is None


def test_empty_summary_does_not_explode():
    s = summarize([])
    assert s.n == 0 and s.expectancy_r == 0.0 and s.dd_ratio is None


def test_quartiles_and_histogram():
    assert quartiles([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx((2.0, 3.0, 4.0))
    bins = histogram([-1.0, 0.5, 3.0, 99.0], -1.5, 3.5, 5)
    assert sum(c for _, _, c in bins) == 4        # 범위 밖도 버리지 않는다


def test_group_by_splits_regimes():
    trades = series([1.0, 1.0]) + [fake_trade(-1.0, T0 + timedelta(days=5), regime="PRE")]
    g = group_by(trades, "regime")
    assert set(g) == {"RTH", "PRE"}
    assert g["PRE"].n == 1


def test_gates_reject_a_small_sample_even_with_a_great_number():
    """기대값이 아무리 좋아도 표본 20건이면 통과가 아니다."""
    s = summarize(series([1.0] * 20))
    gates = evaluate_gates(s, {"RTH": s})
    by_name = {g.name: g for g in gates}
    assert not by_name["표본 ≥ 300"].passed
    assert not all(g.passed for g in gates)


def test_gates_reject_when_rth_alone_is_negative():
    rth = summarize(series([-1.0] * 10))
    good = summarize(series([1.0] * 400))
    gates = evaluate_gates(good, {"RTH": rth})
    assert not next(g for g in gates if "RTH" in g.name).passed


# ─────────────────────────────────────────────────────────
# 워크포워드 — 경계 누수
# ─────────────────────────────────────────────────────────

def test_split_purges_trades_that_straddle_the_boundary():
    trades = series([1.0] * 10, spacing_minutes=600)
    is_, oos, purged = split_in_out(trades, oos_fraction=0.4)
    assert is_ and oos
    assert max(t.exit_at for t in is_) <= min(t.signal_at for t in oos)
    assert purged + len(is_) + len(oos) == len(trades)


def test_embargo_leaves_a_gap_after_the_boundary():
    trades = series([1.0] * 20, spacing_minutes=60)
    is_, oos, _ = split_in_out(trades, oos_fraction=0.5, embargo_minutes=240)
    gap = min(t.signal_at for t in oos) - max(t.exit_at for t in is_)
    assert gap >= timedelta(minutes=200)


def test_walk_forward_never_tests_on_training_data():
    trades = series([1.0] * 40, spacing_minutes=120)
    for f in walk_forward(trades, folds=4):
        train_ids = {id(t) for t in f.train}
        assert not any(id(t) in train_ids for t in f.test)
        assert max(t.exit_at for t in f.train) <= min(t.signal_at for t in f.test)


def test_walk_forward_handles_tiny_input():
    assert walk_forward([], folds=4) == []
    assert walk_forward(series([1.0]), folds=4) == []


# ─────────────────────────────────────────────────────────
# 5. 임포트 오염
# ─────────────────────────────────────────────────────────

def test_backtest_package_stays_pure():
    """`th.backtest` 를 임포트하는 것만으로 DB·HTTP 가 딸려오면 안 된다."""
    import subprocess
    import sys

    code = (
        "import sys, th.backtest;"
        "bad=[m for m in ('asyncpg','httpx','tenacity') if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", f"딸려온 모듈: {out.stdout.strip()}"


def test_report_builds_without_any_dependency():
    from th.backtest.report import build_report

    trades = series([1.0, -1.0, 2.0])
    s = summarize(trades)
    page = build_report(
        symbol="SOXLUSDT", start=T0, end=T0 + timedelta(days=30),
        generated_at=T0, overall=s, in_sample=s, out_sample=s,
        gates=evaluate_gates(s, {"RTH": s}),
        by_regime=group_by(trades, "regime"), by_setup=group_by(trades, "setup_id"),
        folds=[(1, s, s)], trades=trades, purged=0,
        counters={"평가": 100}, basis_coverage=0.5, skip_reasons={"no_fill": 3},
    )
    assert "<title>" in page and "http://" not in page and "https://" not in page
    assert "SOXLUSDT" in page
