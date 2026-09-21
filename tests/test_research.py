"""우위 검정 도구 — 이 도구가 틀리면 '우위가 없다'는 결론 자체가 틀린다.

그래서 **답을 아는 합성 데이터**로 고정한다. 우위가 있는 경로, 없는 경로,
정반대인 경로를 만들어 각 함수가 그걸 구분하는지 본다.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from th.backtest.research import (
    Path,
    autocorrelation,
    continuation_rate,
    direction_control,
    edge_signature,
    efficiency_ratio,
    evaluate,
    outcome_grid,
    outcome_r,
    random_walk_efficiency,
    series_character,
)
from th.strategy.indicators import Bar

T0 = datetime(2026, 9, 10, 13, 30, tzinfo=UTC)


def bars_from(prices):
    return [
        Bar(ts=T0 + timedelta(minutes=i), open=p, high=p, low=p, close=p, volume=100.0)
        for i, p in enumerate(prices)
    ]


def winner(target=1.0):
    """바로 목표까지 간 뒤 본전 스톱에 걸리는 경로."""
    return Path(adverse=(0.0, 0.05, 0.1, 0.2), favour=(0.2, 0.6, target + 0.2, 0.3))


def random_walk_bars(rng, n=240, sigma=0.0008, start=100.0):
    """추세 없는 무작위 보행. 고·저 = 이전 종가와 종가 — 봉 안의 흔들림은 없다."""
    p, out = start, []
    for i in range(n):
        q = p * (1 + rng.gauss(0, sigma))
        out.append(Bar(ts=T0 + timedelta(minutes=i), open=p, high=max(p, q),
                       low=min(p, q), close=q, volume=1.0))
        p = q
    return out


def loser(stop=1.0):
    return Path(adverse=(0.2, 0.6, stop + 0.2), favour=(0.1, 0.1, 0.0))


# ── 결과 계산 ────────────────────────────────────────────

def test_a_clean_winner_pays_about_half_of_r1():
    """TP1 절반 청산 뒤 본전 스톱에 걸리면 0.5R 에서 비용을 뺀 값."""
    r = outcome_r(winner(), target_pct=1.0, stop_pct=1.0, round_trip_pct=0.0)
    assert r == pytest.approx(0.5)


def test_a_loser_is_minus_one_r_plus_cost():
    r = outcome_r(loser(), 1.0, 1.0, round_trip_pct=0.14)
    assert r == pytest.approx(-1.14)


def test_stop_wins_when_both_are_touched_in_the_same_step():
    """같은 시점에 둘 다 닿으면 손절이다. 반대로 하면 백테스트가 좋아진다."""
    both = Path(adverse=(1.5,), favour=(1.5,))
    assert outcome_r(both, 1.0, 1.0, round_trip_pct=0.0) == pytest.approx(-1.0)


def test_no_touch_costs_only_the_fees():
    flat = Path(adverse=(0.1, 0.1), favour=(0.1, 0.1))
    assert outcome_r(flat, 1.0, 1.0, round_trip_pct=0.14) == pytest.approx(-0.14)


def test_time_exit_marks_the_remaining_position_to_market():
    """손절·목표 어느 쪽에도 안 닿고 끝나면 마지막 종가로 평가한다 — 0 이 아니다."""
    up = Path(adverse=(0.1, 0.1), favour=(0.5, 0.6), last_pct=0.4)
    down = Path(adverse=(0.1, 0.5), favour=(0.5, 0.1), last_pct=-0.4)
    assert outcome_r(up, 1.0, 1.0, round_trip_pct=0.0) == pytest.approx(0.4)
    assert outcome_r(down, 1.0, 1.0, round_trip_pct=0.0) == pytest.approx(-0.4)
    assert outcome_r(up, 1.0, 0.5, round_trip_pct=0.0) == pytest.approx(0.8)


def test_time_exit_after_tp1_marks_the_other_half():
    """TP1 뒤 본전도 TP2 도 안 닿고 끝나면 남은 절반은 종가로 평가한다."""
    p = Path(adverse=(0.0, -0.2, -0.3), favour=(1.1, 1.4, 1.5), last_pct=1.2)
    assert outcome_r(p, 1.0, 1.0, round_trip_pct=0.0) == pytest.approx(0.5 + 0.5 * 1.2)


def test_path_from_bars_carries_the_last_close_and_unslipped_trigger():
    bars = bars_from([100.0, 101.0, 99.0])
    p = Path.from_bars(bars, trigger=100.0, long=True, slippage_pct=0.03)
    assert p.favour == pytest.approx((0.0, 1.0, -1.0))
    assert p.adverse == pytest.approx((0.0, -1.0, 1.0))
    assert p.last_pct == pytest.approx(-1.0)
    assert p.slippage_pct == 0.03
    q = Path.from_bars(bars, trigger=100.0, long=False)
    assert q.last_pct == pytest.approx(1.0)
    assert q.flipped().last_pct == pytest.approx(-1.0)


def test_driftless_paths_have_zero_expectancy_without_cost():
    """추세 없는 경로에 비용 0 이면 어떤 (목표, 손절) 칸이든 기대값 ≈ 0 이어야 한다.

    예전 코드는 시간 만료를 '본전' 으로 쳐서 목표 ≥ 손절 칸이 전부 크게 음수였다
    (목표 2%/손절 0.5% 에서 −0.47R). 우위 검정 도구가 우위를 지워버리고 있었다.
    """
    rng = random.Random(7)
    paths = [Path.from_bars(random_walk_bars(rng), 100.0, True) for _ in range(2000)]
    for target in (0.8, 1.0, 1.5, 2.0):
        for stop in (0.5, 1.0, 1.5):
            if target < stop:
                continue
            cell = evaluate(paths, target, stop, round_trip_pct=0.0)
            assert abs(cell.expectancy_r) < 0.1, (target, stop, cell.expectancy_r)


def test_slippage_is_paid_once_per_side_in_either_direction():
    """편도 슬리피지 s → 진입 1회 + 청산 1회 = 2s. 뒤집어도 같다."""
    flat = Path((0.0,), (0.0,), last_pct=0.0, slippage_pct=0.03)
    assert outcome_r(flat, 1.0, 1.0, round_trip_pct=0.0) == pytest.approx(-0.06)
    assert outcome_r(flat.flipped(), 1.0, 1.0, round_trip_pct=0.0) == pytest.approx(-0.06)
    # 슬리피지를 들고 있는 경로의 기본 비용 = 수수료만 + 슬리피지 2회 = 0.16
    cell = evaluate([flat], 1.0, 1.0)
    assert cell.cost_pct == pytest.approx(0.16)
    assert cell.expectancy_r == pytest.approx(-0.16)
    # 슬리피지가 없는 경로는 전부 합친 0.11
    assert evaluate([Path((0.0,), (0.0,), 0.0)], 1.0, 1.0).cost_pct == pytest.approx(0.11)


def test_direction_control_is_symmetric_with_slippage_on_a_random_walk():
    """우위가 0인 무작위 보행에서 원본과 뒤집기의 기대값은 잡음 안에서 같아야 한다.

    예전에는 롱의 슬리피지가 들어간 체결가를 숏이 그대로 써서 한쪽이 유리했다.
    """
    rng = random.Random(21)
    paths = [Path.from_bars(random_walk_bars(rng, n=120), 100.0, True, 0.03)
             for _ in range(2000)]
    forward, reverse = direction_control(paths)
    assert abs(forward.expectancy_r - reverse.expectancy_r) < 0.05
    assert forward.cost_only_r == pytest.approx(-0.16)
    # 완전 대칭인 표본(경로 + 그 거울)에서는 정확히 같다
    both = paths + [p.flipped() for p in paths]
    f2, r2 = direction_control(both)
    assert f2.expectancy_r == pytest.approx(r2.expectancy_r)


def test_cost_scales_inversely_with_stop_width():
    """손절이 넓을수록 같은 수수료가 차지하는 R 이 작아진다."""
    flat = Path(adverse=(0.0,), favour=(0.0,))
    tight = outcome_r(flat, 1.0, 0.5, round_trip_pct=0.14)
    wide = outcome_r(flat, 1.0, 2.0, round_trip_pct=0.14)
    assert tight < wide < 0


# ── 우위 판정 ────────────────────────────────────────────

def test_edge_signature_is_half_without_an_edge():
    """대칭인 경로 뭉치는 정확히 절반이어야 한다."""
    rng = random.Random(3)
    paths = []
    for _ in range(600):
        a = rng.random() * 2
        f = rng.random() * 2
        paths.append(Path((a,), (f,)))
    assert 0.45 < edge_signature(paths) < 0.55


def test_edge_signature_detects_a_real_edge():
    paths = [winner() for _ in range(100)]
    assert edge_signature(paths) > 0.9


def test_direction_control_flags_a_real_edge():
    """우위가 있으면 뒤집은 쪽이 뚜렷하게 나빠야 한다."""
    paths = [winner() for _ in range(200)]
    forward, reverse = direction_control(paths)
    assert forward.expectancy_r > reverse.expectancy_r + 0.5


def test_direction_control_shows_nothing_when_there_is_no_edge():
    rng = random.Random(11)
    paths = [Path((rng.random()*2,), (rng.random()*2,)) for _ in range(800)]
    forward, reverse = direction_control(paths)
    assert abs(forward.expectancy_r - reverse.expectancy_r) < 0.15


def test_no_edge_expectancy_matches_the_cost_line():
    """우위가 0이면 기대값이 '비용만큼'과 겹친다 — 이게 서명이다."""
    flat = [Path((0.0,), (0.0,)) for _ in range(50)]
    cell = evaluate(flat, 1.0, 1.0)
    assert cell.looks_like_no_edge
    assert cell.expectancy_r == pytest.approx(cell.cost_only_r)


def test_a_winning_cell_is_not_mistaken_for_no_edge():
    cell = evaluate([winner() for _ in range(50)], 1.0, 1.0)
    assert not cell.looks_like_no_edge


def test_grid_covers_every_combination():
    cells = outcome_grid([winner()], targets=[1.0, 1.5], stops=[0.5, 1.0, 1.5])
    assert len(cells) == 6
    assert {(c.target_pct, c.stop_pct) for c in cells} == {
        (t, s) for s in (0.5, 1.0, 1.5) for t in (1.0, 1.5)
    }


def test_mae_before_target_ignores_what_happens_after():
    p = Path(adverse=(0.2, 0.4, 5.0), favour=(0.3, 1.2, 0.0))
    assert p.mae_before(1.0) == pytest.approx(0.4)      # 목표 도달 후의 5.0 은 무관
    assert p.mae_before(9.0) is None                    # 목표에 못 닿음


# ── 종목 성격 ────────────────────────────────────────────

def test_autocorrelation_detects_momentum():
    """같은 방향이 이어지는 계열은 양의 자기상관."""
    prices, p, step = [], 100.0, 0.5
    for i in range(400):
        step = step if i % 40 else -step
        p += step
        prices.append(p)
    ac = autocorrelation([prices[i]/prices[i-1]-1 for i in range(1, len(prices))])
    assert ac > 0.5


def test_autocorrelation_detects_mean_reversion():
    prices = [100.0 + (1.0 if i % 2 else -1.0) for i in range(400)]
    ac = autocorrelation([prices[i]/prices[i-1]-1 for i in range(1, len(prices))])
    assert ac < -0.5


def test_random_walk_has_near_zero_autocorrelation():
    rng = random.Random(5)
    p, prices = 100.0, []
    for _ in range(4000):
        p *= 1 + rng.gauss(0, 0.001)
        prices.append(p)
    ac = autocorrelation([prices[i]/prices[i-1]-1 for i in range(1, len(prices))])
    assert abs(ac) < 0.06


def test_efficiency_ratio_is_one_for_a_straight_line():
    assert efficiency_ratio(bars_from([100 + i for i in range(20)])) == pytest.approx(1.0)


def test_efficiency_ratio_is_zero_for_a_round_trip():
    up = [100 + i for i in range(10)]
    down = [109 - i for i in range(10)]
    assert efficiency_ratio(bars_from(up + down)) == pytest.approx(0.0, abs=1e-9)


def test_random_walk_efficiency_is_around_a_fifth():
    """관측값을 이 기준선과 비교해 '추세 성향'을 판정한다."""
    assert 0.15 < random_walk_efficiency(20) < 0.30


def test_continuation_rate_is_half_on_a_random_walk():
    rng = random.Random(9)
    p, prices = 100.0, []
    for _ in range(6000):
        p *= 1 + rng.gauss(0, 0.001)
        prices.append(p)
    same, gain = continuation_rate(bars_from(prices), 6, 6)
    assert 0.45 < same < 0.55
    assert abs(gain) < 0.05


def test_series_character_labels_a_random_walk_honestly():
    rng = random.Random(13)
    p, prices = 100.0, []
    for _ in range(3000):
        p *= 1 + rng.gauss(0, 0.001)
        prices.append(p)
    ch = series_character(bars_from(prices), 5)
    assert "무작위" in ch.reading_ko


# ── 연구 스크립트 (scripts/) ─────────────────────────────
#
# 스크립트는 패키지 밖에 있지만 결론(docs/11~13)을 만든 엔진이다. 답을 아는 합성
# 데이터로 고정한다.

import math
import pathlib
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def trending_bars(n=3000, seed=4):
    """완만한 상승 + 15봉 주기 눌림 — pullback20 이 실제로 걸리는 계열."""
    rng = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        p *= 1 + 0.0004 + 0.003 * math.sin(i / 15) + rng.gauss(0, 0.0008)
        hi = p * (1 + abs(rng.gauss(0, 0.001)))
        lo = p * (1 - abs(rng.gauss(0, 0.001)))
        out.append((T0 + timedelta(minutes=i), p, hi, lo, p, 1.0))
    return out


def test_swing_stop_keeps_the_same_distance_when_the_direction_is_flipped():
    """뒤집기 대조군은 같은 자리·같은 손절폭이어야 비용이 상쇄된다.

    손절폭이 같고 비용이 0이면, 한 봉 뒤 시장가 청산한 원본과 뒤집기의 R 은 부호만
    다르다 (손절에 먼저 걸린 경우만 예외). 예전에는 뒤집은 쪽이 반대편 스윙으로
    손절폭을 재서 그 대칭이 깨졌다.
    """
    import ema_engine as E

    saved = E.ROUND_TRIP
    E.ROUND_TRIP = 0.0
    try:
        kw = dict(tf=1, align=2, entry="pullback20", stop="swing", exit="r2",
                  long_only=True, max_bars=1)
        bars = trending_bars()
        orig = E.run(bars, E.Config(**kw))
        flip = E.run(bars, E.Config(flip=True, **kw))
    finally:
        E.ROUND_TRIP = saved
    assert orig.n >= 10
    assert flip.n == orig.n
    mirrored = sum(1 for a, b in zip(orig.trades, flip.trades) if abs(a + b) < 1e-9)
    assert mirrored >= orig.n // 2, (mirrored, orig.n)


def test_infinite_buy_funding_follows_elapsed_time_not_bar_index():
    """일봉이든 시간봉이든 8시간마다 한 번 — 하루 세 번."""
    from infinite_buy import Cfg, run

    days = 30
    daily = [(T0 + timedelta(days=i), 100, 100, 100, 100) for i in range(days)]
    hourly = [(T0 + timedelta(hours=i), 100, 100, 100, 100) for i in range(days * 24)]
    cfg = Cfg(seed=10_000, splits=40, leverage=1, buy_hour=T0.hour, fee_pct=0.0, funding_8h=0.01)
    a, b = run(daily, cfg), run(hourly, cfg)
    assert a.max_buys == b.max_buys == days
    # 매수 시각의 차이로 몇 % 정도는 다르지만, 예전처럼 1/3 이 되지는 않는다
    assert 0.9 < a.funding / b.funding < 1.1


def test_infinite_buy_skips_adds_without_free_margin():
    """평가손이 쌓여 가용 증거금이 1분할 미만이면 사지 못한다 — 건너뛴 횟수를 센다."""
    from infinite_buy import Cfg, run

    prices = [100 * 0.97 ** i for i in range(40)]
    bars = [(T0 + timedelta(days=i), p, p, p, p) for i, p in enumerate(prices)]
    r = run(bars, Cfg(seed=10_000, splits=40, leverage=10, buy_hour=T0.hour,
                      fee_pct=0.0, funding_8h=0.0))
    assert r.skipped_buys > 0
    assert r.max_buys + r.skipped_buys < 40 or r.liquidated


def test_tsmom_funding_is_paid_by_longs_and_received_by_shorts():
    from tsmom import run_tsmom

    up = [100 + i for i in range(100)]
    down = [100 - i * 0.5 for i in range(100)]
    kw = dict(cost=0.0, mmr=0.0, long_only=False)
    long_paid = sum(run_tsmom(up, 7, 1, funding_8h=0.0, **kw)) - sum(run_tsmom(up, 7, 1, funding_8h=0.0172, **kw))
    short_got = sum(run_tsmom(down, 7, 1, funding_8h=0.0172, **kw)) - sum(run_tsmom(down, 7, 1, funding_8h=0.0, **kw))
    n = len(run_tsmom(up, 7, 1, **kw))
    assert long_paid == pytest.approx(n * 3 * 0.0172 / 100)
    assert short_got == pytest.approx(n * 3 * 0.0172 / 100)


def test_tsmom_liquidates_a_leveraged_position_on_an_intraday_crash():
    from tsmom import liquidated, run_tsmom

    assert liquidated(10, 100.0, 90.0, 100.0, 0.005)        # 10배 롱, 일중 −10%
    assert not liquidated(1, 100.0, 90.0, 100.0, 0.005)     # 1배는 버틴다
    assert liquidated(-10, 100.0, 100.0, 110.0, 0.005)      # 10배 숏, 일중 +10%

    rng = random.Random(2)
    px = [100.0]
    for _ in range(60):
        px.append(px[-1] * (1 + 0.003 + rng.gauss(0, 0.005)))
    lows = [p * 0.995 for p in px]
    highs = [p * 1.005 for p in px]
    lows[50] = px[49] * 0.88
    kw = dict(max_lev=10, target_vol=5.0, cost=0.0, funding_8h=0.0)
    r = run_tsmom(px, 7, 1, lows=lows, highs=highs, **kw)
    assert -1.0 in r and all(x == 0.0 for x in r[r.index(-1.0) + 1:])
    r_off = run_tsmom(px, 7, 1, lows=lows, highs=highs, mmr=0.0, **kw)
    assert -1.0 not in r_off and len(r_off) == len(r)


def test_knn_volume_mean_never_looks_forward():
    import numpy as np
    from knn_pattern import trailing_mean

    v = np.arange(1.0, 101.0)
    a = trailing_mean(v, 20)
    v2 = v.copy()
    v2[60:] *= 100                       # 60봉 이후를 바꿔도
    b = trailing_mean(v2, 20)
    assert np.array_equal(a[:60], b[:60])   # 그 앞은 그대로여야 한다
    assert a[19] == pytest.approx(10.5)     # 1..20 의 평균
    assert a[0] == pytest.approx(1.0)


def test_knn_null_keeps_label_overlap_and_se_accounts_for_it():
    import numpy as np
    from knn_pattern import block_shuffle, overlap_se

    rng = np.random.default_rng(0)
    vals = np.arange(100.0)
    sh = block_shuffle(vals, 24, rng)
    assert sorted(sh) == list(vals)
    # 덩어리 안은 원래 순서 그대로 (이웃 라벨의 상관이 유지된다)
    starts = [i for i in range(0, 100, 24)]
    assert any(list(sh[:24]) == list(vals[s:s + 24]) for s in starts)

    # 24봉 겹침 합의 평균 — 독립 가정 표준오차는 실제의 1/5 이다
    e = rng.normal(size=3000)
    r = np.convolve(e, np.ones(24), mode="valid")
    bars = np.arange(len(r))
    naive = r.std() / len(r) ** 0.5
    assert overlap_se(r, bars, 48) > 3 * naive
    # 서로 멀리 떨어진 관측치면 독립과 같다
    assert overlap_se(r, bars * 100, 48) == pytest.approx(naive)
