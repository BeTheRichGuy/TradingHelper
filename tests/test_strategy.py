"""Phase 3 — 전략 엔진 테스트.

DB도 네트워크도 타지 않는다. 지표·리스크·점수·셋업·파이프라인 모두 순수 함수라
합성 데이터로 전부 고정할 수 있다.
"""

from datetime import UTC, datetime, timedelta

import pytest

from th.sessions.models import Adjustment, Block, Regime, SessionContext
from th.strategy.features import Features, Levels, PerpContext
from th.strategy.indicators import (
    Bar,
    atr,
    cumulative_delta,
    ema,
    percentile_rank,
    resample,
    rma,
    rsi,
    session_vwap,
    sma,
    swing_lows,
)
from th.strategy.risk import (
    MIN_RR,
    SL_MAX_PCT,
    SL_MIN_PCT,
    build_plan,
    position_size,
    stop_loss_price,
)
from th.strategy.scoring import score_setup
from th.strategy.setups.base import Condition, EntryZone, SetupResult
from th.strategy.setups.s1_vwap_pullback import S1VwapPullback
from th.strategy.setups.s3_sweep_reversal import S3SweepReversal

T0 = datetime(2026, 9, 10, 13, 30, tzinfo=UTC)     # 09:30 ET


def bars(prices, start=T0, volume=100.0, taker=None, spread=0.5):
    return [
        Bar(
            ts=start + timedelta(minutes=i),
            open=p,
            high=p + spread,
            low=p - spread,
            close=p,
            volume=volume,
            taker_buy=taker,
        )
        for i, p in enumerate(prices)
    ]


# ─────────────────────────────────────────────────────────
# 지표
# ─────────────────────────────────────────────────────────

def test_sma_and_ema_seeds_like_tradingview():
    vals = [float(i) for i in range(1, 11)]
    assert sma(vals, 3)[2] == 2.0
    e = ema(vals, 3)
    assert e[0] is None and e[1] is None
    assert e[2] == 2.0            # SMA 시드
    assert e[3] == 3.0
    assert e[-1] == 9.0


def test_rma_is_wilder_not_sma():
    vals = [10.0] * 20
    r = rma(vals, 14)
    assert r[13] == 10.0
    assert r[-1] == pytest.approx(10.0)


def test_rsi_extremes():
    up = [float(i) for i in range(1, 40)]
    assert rsi(up, 14)[-1] == pytest.approx(100.0)
    down = [float(i) for i in range(40, 1, -1)]
    assert rsi(down, 14)[-1] == pytest.approx(0.0, abs=1e-9)


def test_rsi_needs_enough_bars():
    assert all(v is None for v in rsi([1.0, 2.0, 3.0], 14))


def test_atr_with_constant_range():
    bs = bars([100.0] * 30, spread=1.0)     # 매 봉 고저 폭 2.0, 종가 동일
    a = atr(bs, 14)
    assert a[0] is None
    assert a[-1] == pytest.approx(2.0)


def test_session_vwap_ignores_bars_before_anchor():
    pre = bars([50.0] * 10, start=T0 - timedelta(minutes=10))
    rth = bars([100.0] * 10, start=T0)
    v = session_vwap(pre + rth, T0)
    assert all(x is None for x in v[:10])       # 앵커 이전은 계산하지 않는다
    assert v[-1].vwap == pytest.approx(100.0)   # 밤새 가격에 오염되지 않음


def test_session_vwap_is_volume_weighted():
    bs = [
        Bar(T0, 100, 100, 100, 100, volume=1),
        Bar(T0 + timedelta(minutes=1), 110, 110, 110, 110, volume=9),
    ]
    assert session_vwap(bs, T0)[-1].vwap == pytest.approx(109.0)


def test_cumulative_delta_all_buys():
    bs = bars([100.0] * 5, volume=100.0, taker=100.0)
    cvd = cumulative_delta(bs)
    assert cvd[-1] == pytest.approx(500.0)      # 매 봉 +100


def test_cumulative_delta_balanced_is_flat():
    bs = bars([100.0] * 5, volume=100.0, taker=50.0)
    assert cumulative_delta(bs)[-1] == pytest.approx(0.0)


def test_resample_merges_ohlcv():
    bs = bars([100.0, 101.0, 99.0, 102.0, 100.5], volume=10.0, spread=0.5)
    five = resample(bs, 5)
    assert len(five) == 1
    b = five[0]
    assert b.open == 100.0 and b.close == 100.5
    assert b.high == pytest.approx(102.5) and b.low == pytest.approx(98.5)
    assert b.volume == 50.0


def test_resample_boundaries_follow_clock():
    # 09:32 부터 시작해도 09:30/09:35 경계로 묶인다
    bs = bars([100.0] * 9, start=T0 + timedelta(minutes=2))
    five = resample(bs, 5)
    assert len(five) == 3          # 09:32-34, 09:35-39, 09:40


def test_resample_drops_the_bar_that_is_still_forming():
    """미완성 묶음을 남기면 1분치 거래량을 완성된 5분봉과 비교하게 된다."""
    bs = bars([100.0] * 7)                       # 5개 + 2개
    assert len(resample(bs, 5)) == 2
    kept = resample(bs, 5, drop_incomplete=True)
    assert len(kept) == 1
    assert kept[0].volume == 500.0               # 완성된 봉만


def test_resample_keeps_a_full_last_bucket():
    bs = bars([100.0] * 10)
    assert len(resample(bs, 5, drop_incomplete=True)) == 2


def test_swing_search_is_bounded_to_recent_bars():
    """추세일에 몇 시간 전 피벗을 '직전 스윙 저점'으로 쓰면 손절이 3% 넘게 벌어진다."""
    from th.strategy.indicators import last_swing_low

    old_dip = [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0]
    climb = [11.0 + i * 0.05 for i in range(60)]
    bs = bars([*old_dip, *climb], spread=0.0)

    assert last_swing_low(bs, 5, within=None) == pytest.approx(5.0)   # 옛 피벗
    assert last_swing_low(bs, 5, within=30) is None                   # 최근엔 구조 없음


def test_swing_lows_found():
    prices = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 11, 12]
    bs = bars([float(p) for p in prices], spread=0.0)
    assert 5 in swing_lows(bs, 5)


def test_percentile_rank():
    assert percentile_rank([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5) == 50.0
    assert percentile_rank([1, 2], 1) is None       # 표본 부족


# ─────────────────────────────────────────────────────────
# 리스크 — 02 문서 5절
# ─────────────────────────────────────────────────────────

def test_stop_uses_atr_when_structure_is_close():
    sl, pct = stop_loss_price("long", 100.0, atr=0.4, price=100.0, swing=99.8)
    assert pct == pytest.approx(0.6)              # 1.5 × 0.4%
    assert sl == pytest.approx(99.4)


def test_stop_takes_wider_structural_level():
    """구조적 손절이 더 넓으면 그쪽을 쓴다."""
    sl, pct = stop_loss_price("long", 100.0, atr=0.4, price=100.0, swing=98.0)
    assert sl == pytest.approx(98.0 - 0.2 * 0.4)
    assert pct > 0.6


def test_stop_distance_is_clamped():
    _, tight = stop_loss_price("long", 100.0, atr=0.05, price=100.0, swing=None)
    assert tight == pytest.approx(SL_MIN_PCT)
    _, wide = stop_loss_price("long", 100.0, atr=2.0, price=100.0, swing=None)
    assert wide == pytest.approx(SL_MAX_PCT)


def test_short_stop_is_mirrored():
    sl, pct = stop_loss_price("short", 100.0, atr=0.4, price=100.0, swing=100.2)
    assert sl == pytest.approx(100.6)
    assert pct == pytest.approx(0.6)


def plan(atr_v=0.4, price=100.0, swing=99.8, wallet=10000.0, **kw):
    return build_plan(
        "long", EntryZone(price * 0.995, price), atr=atr_v, price=price, swing=swing,
        wallet=wallet, risk_pct=0.5, **kw
    )


def test_plan_targets_land_in_the_intended_band():
    """손절 0.6% → TP1 +1.2%, TP2 +2.4%. TP1 이 본주 1~2% 목표 안이다."""
    p = plan()
    assert p.publishable
    assert p.risk_pct == pytest.approx(0.6)
    assert p.tp1.pct == pytest.approx(1.2)
    assert p.tp2.pct == pytest.approx(2.4)
    assert p.risk_reward == pytest.approx(4.0)


def test_publish_band_matches_the_stop_clamp():
    """클램프가 통과시킨 손절은 발행에서 거부되면 안 된다.

    예전엔 클램프 [0.30, 0.70] 인데 발행이 [0.40, 0.625] 라 양 끝이 죽은 구간이었고,
    실측에서 리스크 단계 탈락의 91%가 거기서 나왔다. 다시 어긋나면 여기서 걸린다.
    """
    from th.strategy.risk import TP1_MIN_PCT, TP1_R, TP2_MAX_PCT, TP2_R

    assert TP1_MIN_PCT == pytest.approx(TP1_R * SL_MIN_PCT)
    assert TP2_MAX_PCT == pytest.approx(TP2_R * SL_MAX_PCT)


def test_any_atr_only_stop_is_publishable():
    """구조가 개입하지 않으면 어떤 변동성에서도 발행 가능해야 한다."""
    for atr_v in (0.01, 0.05, 0.2, 0.4, 0.7, 1.0, 3.0):
        p = plan(atr_v=atr_v, swing=None)
        assert p.publishable, f"ATR {atr_v}: {p.reject_reason}"
        assert SL_MIN_PCT - 1e-9 <= p.risk_pct <= SL_MAX_PCT + 1e-9


def test_plan_rejects_when_structure_is_too_far():
    """구조는 클램프를 넘어설 수 있고, 그때는 거부하는 게 맞다."""
    p = plan(swing=98.0)          # 구조적 손절이 2% 넘게 벌어짐
    assert not p.publishable
    assert "너무 멉" in p.reject_reason


def test_plan_rejects_when_structural_stop_is_huge():
    p = plan(swing=95.0)
    assert not p.publishable


def test_structural_resistance_becomes_tp2():
    p = plan(resistance=101.8)    # TP1 101.2 < 101.8 < TP2 102.4
    assert p.tp2.source == "structure"
    assert p.tp2.level == pytest.approx(101.8)
    assert p.risk_reward == pytest.approx(3.0)
    assert p.risk_reward >= MIN_RR


def test_position_size_is_derived_from_stop_not_leverage():
    """계좌 0.5% 를 손절 0.6% 자리에 걸면 명목가는 계좌의 0.83배가 된다."""
    pos = position_size(
        wallet=10000.0, risk_pct=0.5, sl_dist_pct=0.6,
        entry_ref=100.0, risk_per_unit=0.6, leverage=10,
    )
    assert pos.qty == pytest.approx(83.333, abs=0.001)
    assert pos.notional == pytest.approx(8333.3, abs=1.0)
    assert pos.risk_usdt == pytest.approx(50.0, abs=0.1)
    assert pos.margin_usdt == pytest.approx(833.3, abs=1.0)


def test_leverage_changes_margin_not_size():
    a = position_size(10000.0, 0.5, 0.6, 100.0, 0.6, leverage=10)
    b = position_size(10000.0, 0.5, 0.6, 100.0, 0.6, leverage=5)
    assert a.qty == b.qty and a.notional == b.notional
    assert b.margin_usdt == pytest.approx(a.margin_usdt * 2)


def test_position_size_needs_wallet():
    assert position_size(0.0, 0.5, 0.6, 100.0, 0.6) is None


def test_entry_reference_assumes_worst_fill():
    z = EntryZone(99.5, 100.0)
    assert z.reference("long") == 100.0        # 롱은 비싸게 샀다고 가정
    assert z.reference("short") == 99.5


# ─────────────────────────────────────────────────────────
# 점수
# ─────────────────────────────────────────────────────────

def make_result(n_passed, all_weights, direction="long", category="trend"):
    """앞에서부터 n_passed 개의 선택 조건만 통과한 결과를 만든다."""
    cs = [Condition("R1", "필수", True, "", required=True)]
    for i, w in enumerate(all_weights):
        cs.append(Condition(f"C{i}", f"조건{i}", i < n_passed, "", weight=w))
    return SetupResult("S1_vwap_pullback", category, direction, tuple(cs),
                       EntryZone(99.5, 100.0))


def feat(**kw) -> Features:
    base = dict(at=T0, symbol="SOXLUSDT", price=100.0)
    base.update(kw)
    return Features(**base)


def test_base_score_excludes_required_from_denominator():
    r = make_result(n_passed=2, all_weights=[20, 15, 15, 10])
    s = score_setup(r, feat())
    assert s.base == round(35 / 60 * 100)


def test_session_penalty_applies_by_category():
    r = make_result(4, [20, 15, 15, 10], category="breakout")
    ctx = SessionContext(
        symbol="X", at=T0, regime=Regime.RTH, regime_ends_at=None,
        is_trading_day=True, early_close=False, blocked=False,
        penalties=(Adjustment("lunch", "PENALTY", -25, ("breakout",), "런치"),),
    )
    assert score_setup(r, feat(), ctx).final == 75

    trend = make_result(4, [20, 15, 15, 10], category="trend")
    assert score_setup(trend, feat(), ctx).final == 100   # 다른 분류엔 적용 안 됨


def test_funding_penalty_only_on_crowded_side():
    r = make_result(4, [20, 15, 15, 10])
    crowded_long = feat(perp=PerpContext(funding_rate=0.0008))
    assert score_setup(r, crowded_long).final == 90       # 롱 쏠림에 롱 → −10

    short = make_result(4, [20, 15, 15, 10], direction="short")
    assert score_setup(short, crowded_long).final == 100  # 숏은 해당 없음


def test_score_bands():
    assert score_setup(make_result(1, [100]), feat()).band == "강함"
    assert score_setup(make_result(1, [70, 30]), feat()).band == "보통"
    assert score_setup(make_result(1, [60, 40]), feat()).band == "약함"
    weak = score_setup(make_result(1, [40, 60]), feat())
    assert weak.band == "미달" and not weak.publishable


# ─────────────────────────────────────────────────────────
# 셋업
# ─────────────────────────────────────────────────────────

def s1_features(**over) -> Features:
    from th.strategy.indicators import VwapPoint

    up = [99.0 + i * 0.02 for i in range(120)]
    b1 = bars(up, volume=100.0, taker=70.0, spread=0.1)
    b5 = resample(b1, 5)
    b15 = resample(b1, 15)
    kw = dict(
        at=T0, symbol="SOXLUSDT", price=b1[-1].close,
        bars_1m=b1, bars_5m=b5, bars_15m=b15,
        vwap=VwapPoint(vwap=b1[-1].close - 0.10, sigma=0.2),
        ema9_5m=101.0, ema21_5m=100.5,
        ema9_15m=101.0, ema21_15m=100.5,
        rsi14_5m=52.0, atr_1m=0.4, atr_pct=0.4, atr_percentile=60.0,
        volume_5m=400.0, volume_ma20_5m=500.0,
        cvd=100.0, cvd_slope=20.0,
        swing_low_1m=b1[-1].close - 0.2,   # 구조적 손절이 ATR 손절보다 좁은 자리
        levels=Levels(prev_day_high=b1[-1].close + 5),
        perp=PerpContext(funding_rate=0.0001, oi_change_15m=1.2,
                         basis_pct=0.05, spread_bps=0.8),
    )
    kw.update(over)
    return Features(**kw)


def test_s1_passes_when_everything_lines_up():
    r = S1VwapPullback().evaluate(s1_features(), "long")
    assert r.required_ok, [c.label for c in r.failed_required]
    assert r.base_score >= 80
    assert r.entry is not None


def test_s1_accepts_a_pullback_that_already_recovered():
    """건드린 봉과 복귀한 봉이 달라도 '되돌림 후 복귀'다."""
    from th.strategy.indicators import VwapPoint

    f = s1_features()
    v = f.vwap.vwap
    touched = Bar(                       # 2봉 전에 VWAP 을 찍고
        ts=f.bars_5m[-3].ts, open=v + 0.3, high=v + 0.35,
        low=v + 0.01, close=v + 0.2, volume=100.0, taker_buy=60.0,
    )
    away = [                             # 이후 두 봉은 VWAP 에서 떨어져 마감
        Bar(ts=b.ts, open=v + 0.4, high=v + 0.6, low=v + 0.35,
            close=v + 0.5, volume=100.0, taker_buy=70.0)
        for b in f.bars_5m[-2:]
    ]
    f = s1_features(bars_5m=[*f.bars_5m[:-3], touched, *away],
                    vwap=VwapPoint(vwap=v, sigma=0.2))
    r = S1VwapPullback().evaluate(f, "long")
    s1_3 = next(c for c in r.conditions if c.id == "S1-3")
    assert s1_3.passed, s1_3.detail


def test_s1_rejects_when_price_never_came_near_vwap():
    from th.strategy.indicators import VwapPoint

    f = s1_features(vwap=VwapPoint(vwap=95.0, sigma=0.2))   # 6% 위에서 놀고 있음
    s1_3 = next(
        c for c in S1VwapPullback().evaluate(f, "long").conditions if c.id == "S1-3"
    )
    assert not s1_3.passed


def test_s1_required_fails_when_15m_trend_opposes():
    f = s1_features(ema9_15m=99.0, ema21_15m=101.0)
    r = S1VwapPullback().evaluate(f, "long")
    assert not r.required_ok
    assert "S1-2" in [c.id for c in r.failed_required]


def test_s1_missing_data_counts_as_unmet_not_passed():
    """모르는 것을 통과로 처리하면 데이터가 빠진 날 신호가 과하게 나온다."""
    f = s1_features(perp=PerpContext())
    r = S1VwapPullback().evaluate(f, "long")
    ids = {c.id: c.passed for c in r.conditions}
    assert ids["S1-6"] is False and ids["S1-8"] is False and ids["S1-9"] is False


def test_s1_entry_zone_brackets_price():
    r = S1VwapPullback().evaluate(s1_features(), "long")
    assert r.entry.low <= r.entry.high
    assert r.entry.low <= s1_features().price <= r.entry.high + 0.1


def test_s3_detects_sweep_and_recovery():
    level = 100.0
    prices = [100.5] * 10 + [99.75] * 5 + [100.4] * 5     # 0.25% 이탈 후 복귀
    b1 = bars(prices, volume=100.0, taker=70.0, spread=0.05)
    b5 = resample(b1, 5)
    f = Features(
        at=T0, symbol="SOXLUSDT", price=b1[-1].close,
        bars_1m=b1, bars_5m=b5, bars_15m=resample(b1, 15),
        atr_1m=0.3, volume_5m=500.0, volume_ma20_5m=200.0,
        cvd_slope=15.0,
        levels=Levels(prev_day_low=level),
        rsi9_1m_series=[float(50 - i % 5) for i in range(60)],
        perp=PerpContext(oi_change_15m=-1.5),
    )
    r = S3SweepReversal().evaluate(f, "long")
    assert r.required_ok, [c.detail for c in r.failed_required]
    assert "전일 저가" in r.note


def test_s3_ignores_too_deep_break():
    """깊게 깨면 스윕이 아니라 추세 이탈이다."""
    prices = [100.5] * 10 + [98.5] * 5 + [100.4] * 5      # 1.5% 이탈
    b1 = bars(prices, spread=0.05)
    f = Features(
        at=T0, symbol="X", price=b1[-1].close, bars_1m=b1,
        bars_5m=resample(b1, 5), atr_1m=0.3,
        levels=Levels(prev_day_low=100.0),
    )
    assert not S3SweepReversal().evaluate(f, "long").required_ok


# ─────────────────────────────────────────────────────────
# 파이프라인
# ─────────────────────────────────────────────────────────

def rth_ctx(blocked=False, blocks=(), penalties=(), boosts=()) -> SessionContext:
    return SessionContext(
        symbol="SOXLUSDT", at=T0, regime=Regime.RTH,
        regime_ends_at=T0 + timedelta(hours=2),
        is_trading_day=True, early_close=False,
        blocked=blocked, blocks=tuple(blocks),
        penalties=tuple(penalties), boosts=tuple(boosts),
    )


def test_blocked_session_short_circuits_before_any_setup():
    from th.strategy.engine import analyze_features

    ctx = rth_ctx(blocked=True, blocks=[Block("opening_15m", "개장 직후 15분")])
    a = analyze_features(s1_features(), ctx)
    assert a.blocked and a.direction == "none"
    assert a.candidates == ()            # 셋업 평가를 시작조차 하지 않는다
    assert "개장 직후 15분" in a.why_not_ko()


def test_wide_spread_blocks_signal():
    from th.strategy.engine import analyze_features

    f = s1_features(perp=PerpContext(spread_bps=12.0))
    a = analyze_features(f, rth_ctx())
    assert a.signal is None
    assert any(x.name == "spread" for x in a.failed_filters)


def test_volatility_spike_blocks_signal():
    from th.strategy.engine import analyze_features

    a = analyze_features(s1_features(atr_percentile=98.0), rth_ctx())
    assert a.signal is None
    assert any(x.name == "volatility" for x in a.failed_filters)


def test_signal_is_produced_when_clean():
    from th.strategy.engine import analyze_features

    a = analyze_features(s1_features(), rth_ctx(), wallet=10000.0)
    assert a.signal is not None, a.why_not_ko()
    assert a.direction == "long"
    assert a.signal.plan.publishable
    assert a.signal.plan.position.risk_usdt == pytest.approx(50.0, abs=1.0)


def test_failed_required_condition_is_named():
    """필수 조건에서 걸렸을 때 '셋업이 없다'가 아니라 **어느 조건인지** 말해야 한다."""
    from th.strategy.engine import analyze_features

    f = s1_features(ema9_15m=99.0, ema21_15m=101.0)   # 15분 추세가 반대
    a = analyze_features(f, rth_ctx())
    assert a.signal is None
    reasons = a.why_not_ko()
    assert any("15분봉 추세 일치" in r for r in reasons), reasons
    assert not any("평가할 셋업이 없습니다" in r for r in reasons)


def test_analysis_always_explains_itself():
    from th.strategy.engine import analyze_features

    a = analyze_features(s1_features(perp=PerpContext(spread_bps=20.0)), rth_ctx())
    assert a.why_not_ko()
    assert all(r.strip() for r in a.why_not_ko())


def test_stop_exactly_at_the_clamp_edge_is_still_publishable():
    """경계에 딱 걸린 자리가 부동소수점 오차로 거부되면 안 된다.

    발행 범위의 양 끝 = 클램프의 양 끝이므로, 손절이 하한/상한에 정확히 걸리면
    TP1·4R 이 기준선 위에 얹힌다. 가격에서 되계산하는 과정에서 1.0 이
    0.9999999999999978 이 되어 거부되던 버그가 있었다 (573건 중 103건).
    """
    for price in (7.123, 28.41, 55.55, 100.0, 213.77, 1400.51):
        tight = plan(atr_v=price * 0.0001, price=price, swing=None)   # 하한에 걸림
        assert tight.publishable, f"하한 {price}: {tight.reject_reason}"
        assert tight.risk_pct == pytest.approx(SL_MIN_PCT)

        wide = plan(atr_v=price * 0.01, price=price, swing=None)      # 상한에 걸림
        assert wide.publishable, f"상한 {price}: {wide.reject_reason}"
        assert wide.risk_pct == pytest.approx(SL_MAX_PCT)


# ── 수량 스텝 내림 — 부동소수점 오차 ─────────────────────
def test_qty_step_rounding_does_not_lose_a_step_to_float_error():
    """0.3 / 0.1 은 부동소수점으로 2.9999… 라 floor 가 2 가 된다.
    한 스텝을 통째로 잃으면 리스크 대비 사이즈가 33% 줄어든다."""
    from th.strategy.risk import floor_to_step

    assert floor_to_step(0.3, 0.1) == 0.3
    assert floor_to_step(0.29, 0.1) == 0.2          # 내림은 내림이다
    assert floor_to_step(2.9999, 0.001) == 2.999
    assert floor_to_step(41.66667, 0.01) == 41.66
    assert floor_to_step(0.07, 0.1) == 0.0


def test_position_size_uses_exact_step_rounding():
    # 지갑 30 · 리스크 100% · 손절 1% → 명목 3000 · 진입 10000 → 수량 0.3
    pos = position_size(wallet=30.0, risk_pct=100.0, sl_dist_pct=1.0,
                        entry_ref=10_000.0, risk_per_unit=100.0, qty_step=0.1)
    assert pos is not None and pos.qty == 0.3, pos


# ── 봉이 5개 미만이면 5분 지표는 None ──────────────────────
def test_build_features_with_fewer_than_five_bars_returns_none_not_indexerror():
    """장 시작 직후 1분봉이 4개뿐이면 닫힌 5분봉이 없다. 그때 IndexError 로
    엔진 전체가 죽으면 안 된다 — 셋업은 없는 값을 '미충족'으로 다룬다."""
    from th.strategy.features import build_features

    for n in (1, 2, 4):
        b = bars([100 + i * 0.1 for i in range(n)])
        f = build_features("SOXLUSDT", b, b[-1].ts)
        assert f.ema9_5m is None and f.ema21_5m is None
        assert f.rsi14_5m is None and f.rsi14_5m_prev is None
        assert f.volume_5m is None
        assert f.price == b[-1].close
