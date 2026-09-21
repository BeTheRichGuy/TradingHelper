"""S5 — VWMA 정배열/역배열 눌림 추세추종 + 커버리지 인식 점수.

사용자가 지목한 주 매매 방식이라, 조건 하나하나를 합성 데이터로 고정한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from th.strategy.features import Features, Levels, PerpContext
from th.strategy.indicators import Bar, build_vwma_stack, vwma
from th.strategy.setups.base import Condition, EntryZone, SetupResult, cond
from th.strategy.setups.s5_vwma_pullback import MIN_ALIGNMENT_BARS, S5VwmaPullback

T0 = datetime(2026, 9, 10, 13, 30, tzinfo=UTC)


def bars(prices, volumes=None, start=T0, spread=0.1, minutes=1):
    vols = volumes or [100.0] * len(prices)
    return [
        Bar(ts=start + timedelta(minutes=i * minutes), open=p, high=p + spread,
            low=p - spread, close=p, volume=v, taker_buy=v * 0.6)
        for i, (p, v) in enumerate(zip(prices, vols))
    ]


# ─────────────────────────────────────────────────────────
# VWMA 자체
# ─────────────────────────────────────────────────────────

def test_vwma_weights_by_volume_not_just_price():
    """거래가 실린 가격 쪽으로 끌린다. 단순평균이면 105 가 나올 자리."""
    bs = bars([100.0, 110.0], volumes=[1.0, 9.0])
    assert vwma(bs, 2)[-1] == pytest.approx(109.0)


def test_vwma_equals_sma_when_volume_is_flat():
    bs = bars([100.0, 102.0, 104.0, 106.0], volumes=[50.0] * 4)
    assert vwma(bs, 4)[-1] == pytest.approx(103.0)


def test_vwma_is_none_until_the_window_fills():
    v = vwma(bars([100.0] * 5), 4)
    assert v[:3] == [None, None, None]
    assert v[3] is not None


def test_vwma_window_actually_rolls_off():
    """오래된 봉이 빠져야 이동평균이다. 안 빠지면 누적평균이 된다."""
    bs = bars([100.0] * 5 + [200.0] * 5)
    assert vwma(bs, 5)[-1] == pytest.approx(200.0)


def test_vwma_survives_zero_volume_bars():
    bs = bars([100.0, 101.0, 102.0], volumes=[0.0, 0.0, 0.0])
    assert vwma(bs, 2)[-1] is None          # 나눌 거래량이 없다


# ─────────────────────────────────────────────────────────
# 배열 판정
# ─────────────────────────────────────────────────────────

# 실제 셋업은 VWMA 9/21/50 을 쓴다. 테스트도 같은 기간으로 본다 —
# 짧은 기간(3/5/9)으로 줄이면 3봉 눌림에 빠른 선이 뒤집혀 production 과 다르게 논다.
FAST, MID, SLOW = 9, 21, 50
BARS = 140


def rising(n=BARS, base=100.0, step=0.08):
    return bars([base + i * step for i in range(n)])


def falling(n=BARS, base=100.0, step=0.08):
    return bars([base - i * step for i in range(n)])


def choppy(n=BARS, base=100.0, seed=7):
    """추세 없는 무작위 보행. 교대로 오르내리는 톱니는 이동평균에겐 추세다."""
    import random

    rng = random.Random(seed)
    price, out = base, []
    for _ in range(n):
        price += rng.uniform(-1.0, 1.0)
        out.append(price)
    return bars(out)


def test_rising_market_is_bullish_order():
    st = build_vwma_stack(rising(), FAST, MID, SLOW)
    assert st.order == 1
    f, m, s = st.values()
    assert f > m > s


def test_falling_market_is_bearish_order():
    assert build_vwma_stack(falling(), FAST, MID, SLOW).order == -1


def test_choppy_market_is_no_order():
    orders = [
        build_vwma_stack(choppy(seed=s), FAST, MID, SLOW).order for s in range(12)
    ]
    assert 0 in orders, "무작위 보행 12개에 배열 없는 구간이 하나도 없습니다"


def test_alignment_age_counts_only_the_current_order():
    """뒤집힌 순간부터 다시 센다. 갓 만들어진 배열을 구분하려는 값이다."""
    down = falling(BARS)
    up = bars([down[-1].close + i * 0.30 for i in range(60)],
              start=T0 + timedelta(minutes=BARS))
    st = build_vwma_stack(down + up, FAST, MID, SLOW)
    assert st.order == 1
    assert 0 < st.alignment_age() < 60      # 뒤집힌 뒤부터만 센다


def test_alignment_age_is_zero_without_an_order():
    for seed in range(12):
        st = build_vwma_stack(choppy(seed=seed), FAST, MID, SLOW)
        if st.order == 0:
            assert st.alignment_age() == 0
            return
    raise AssertionError("배열 없는 표본을 못 만들었습니다")


def test_zone_is_between_fast_and_mid():
    st = build_vwma_stack(rising(), FAST, MID, SLOW)
    lo, hi = st.zone()
    f, m, _ = st.values()
    assert lo == min(f, m) and hi == max(f, m)


# ─────────────────────────────────────────────────────────
# S5 셋업
# ─────────────────────────────────────────────────────────

def s5_features(bars_5m, **over) -> Features:
    b1 = bars_5m
    kw = dict(
        at=T0, symbol="SOXLUSDT", price=b1[-1].close,
        bars_1m=b1, bars_5m=b1, bars_15m=b1,
        vwma_5m=build_vwma_stack(b1),
        vwma_15m=build_vwma_stack(b1),
        rsi14_5m=55.0, atr_1m=0.3,
        volume_5m=300.0, volume_ma20_5m=400.0,
        cvd_slope=15.0,
        levels=Levels(prev_day_high=b1[-1].close + 8),
        perp=PerpContext(funding_rate=0.0001, oi_change_15m=1.0, spread_bps=1.0),
    )
    kw.update(over)
    return Features(**kw)


def uptrend_with_pullback():
    """상승 → VWMA 띠까지 눌림 → 다시 빠른 선 위로 마감."""
    up = [100.0 + i * 0.12 for i in range(BARS)]
    top = up[-1]
    dip = [top - 0.5, top - 1.0, top - 0.8]
    resume = [top - 0.2]
    return bars(up + dip + resume)


def test_s5_passes_on_a_clean_pullback():
    r = S5VwmaPullback().evaluate(s5_features(uptrend_with_pullback()), "long")
    assert r.required_ok, [c.label for c in r.failed_required]
    assert r.entry is not None
    assert r.base_score > 50


def test_s5_rejects_when_there_is_no_order():
    for seed in range(12):
        c = choppy(seed=seed)
        if build_vwma_stack(c).order == 0:
            r = S5VwmaPullback().evaluate(s5_features(c), "long")
            assert not r.required_ok
            assert any(x.id == "S5-1" for x in r.failed_required)
            return
    raise AssertionError("배열 없는 표본을 못 만들었습니다")


def test_s5_rejects_a_brand_new_alignment():
    """한 봉 만에 뒤집히는 자리를 받지 않는다는 것이 이 셋업의 핵심이다."""
    f = s5_features(uptrend_with_pullback())
    stack = f.vwma_5m
    assert stack.alignment_age() >= MIN_ALIGNMENT_BARS

    down = falling(BARS)
    flipped = down + bars([down[-1].close + i * 0.5 for i in range(34)],
                          start=T0 + timedelta(minutes=BARS))
    for cut in range(1, 34):
        st = build_vwma_stack(flipped[: BARS + cut])
        if st.order == 1 and st.alignment_age() < MIN_ALIGNMENT_BARS:
            r = S5VwmaPullback().evaluate(s5_features(flipped[: BARS + cut]), "long")
            s5_2 = next(c for c in r.conditions if c.id == "S5-2")
            assert not s5_2.passed
            assert not r.required_ok
            return
    raise AssertionError("갓 만들어진 배열 표본을 못 만들었습니다")


def test_s5_rejects_when_price_never_pulled_back():
    """띠에서 멀리 떨어져 계속 오르기만 한 자리는 눌림이 아니다."""
    up = [100.0 + i * 0.12 for i in range(BARS)]
    runaway = bars(up + [up[-1] + 4.0, up[-1] + 5.5, up[-1] + 7.0])
    r = S5VwmaPullback().evaluate(s5_features(runaway), "long")
    s5_3 = next(c for c in r.conditions if c.id == "S5-3")
    assert not s5_3.passed


def test_s5_rejects_when_the_fast_line_is_not_recovered():
    """눌렸는데 아직 못 올라온 자리 — 기다려야 하는 곳이다."""
    up = [100.0 + i * 0.12 for i in range(BARS)]
    still_down = [up[-1] - 1.2, up[-1] - 1.8, up[-1] - 2.6]
    r = S5VwmaPullback().evaluate(s5_features(bars(up + still_down)), "long")
    s5_4 = next(c for c in r.conditions if c.id == "S5-4")
    assert not s5_4.passed


def test_s5_short_is_the_mirror():
    down = [100.0 - i * 0.12 for i in range(BARS)]
    bottom = down[-1]
    bounce = [bottom + 0.5, bottom + 1.0, bottom + 0.8]
    resume = [bottom + 0.2]
    f = s5_features(bars(down + bounce + resume), rsi14_5m=45.0, cvd_slope=-15.0)
    r = S5VwmaPullback().evaluate(f, "short")
    assert r.required_ok, [c.label for c in r.failed_required]
    assert S5VwmaPullback().evaluate(f, "long").required_ok is False


def test_s5_runs_in_premarket_too():
    """사용자 요청 — 프리장에서도 평가한다."""
    assert "PRE" in S5VwmaPullback().regimes
    assert "RTH" in S5VwmaPullback().regimes


# ─────────────────────────────────────────────────────────
# 커버리지 인식 점수
# ─────────────────────────────────────────────────────────

def result_with(passed_weights, missing_weights, failed_weights):
    cs = [Condition("R", "필수", True, "", required=True)]
    for i, w in enumerate(passed_weights):
        cs.append(cond(f"P{i}", "통과", True, "", weight=w))
    for i, w in enumerate(missing_weights):
        cs.append(cond(f"M{i}", "데이터 없음", None, "", weight=w))
    for i, w in enumerate(failed_weights):
        cs.append(cond(f"F{i}", "실패", False, "", weight=w))
    return SetupResult("S", "trend", "long", tuple(cs), EntryZone(99.5, 100.0))


def test_missing_data_is_excluded_from_the_denominator():
    """없는 데이터를 분모에 넣으면 '데이터 없음'이 '조건 미충족'으로 둔갑한다."""
    r = result_with([20], [15, 10], [5])
    assert r.raw_score == 20
    assert r.max_score == 25            # 20 + 5 (없는 25 는 빠짐)
    assert r.total_weight == 50
    assert r.coverage == pytest.approx(0.5)
    assert r.base_score == 80


def test_missing_data_is_not_counted_as_passed_either():
    """분모에서 빼는 것과 통과로 쳐주는 것은 다르다."""
    r = result_with([], [40], [10])
    assert r.raw_score == 0
    assert r.base_score == 0


def test_full_coverage_scores_exactly_as_before():
    r = result_with([20, 15], [], [15, 10])
    assert r.coverage == 1.0
    assert r.base_score == round(35 / 60 * 100)


def test_missing_required_condition_still_fails():
    """커버리지 처리는 선택 조건 이야기다. 필수는 여전히 탈락."""
    cs = (cond("R", "필수", None, "", required=True),)
    r = SetupResult("S", "trend", "long", cs, EntryZone(99.5, 100.0))
    assert not r.required_ok


def test_low_coverage_blocks_publishing():
    from th.strategy.scoring import MIN_COVERAGE, score_setup

    f = Features(at=T0, symbol="X", price=100.0)
    thin = result_with([10], [90], [])        # 커버리지 10%
    s = score_setup(thin, f)
    assert s.base == 100                       # 볼 수 있었던 건 다 맞췄지만
    assert s.coverage < MIN_COVERAGE
    assert not s.publishable                   # 발행은 안 된다

    thick = result_with([70], [20], [10])      # 커버리지 80%
    assert score_setup(thick, f).publishable


def test_coverage_is_carried_into_the_score():
    from th.strategy.scoring import score_setup

    f = Features(at=T0, symbol="X", price=100.0)
    assert score_setup(result_with([20], [15, 10], [5]), f).coverage == pytest.approx(0.5)
