#!/usr/bin/env python3
"""횡보 구간 차단 필터 — 어떤 '추세 판정' 로직이 실제로 값을 하는가.

문제: EMA99 를 낀 채 왔다갔다 하면 지지가 깨지고 다시 붙고를 반복하면서 연달아 털린다.
추세추종인데 추세가 없는 구간에서 계속 들어가는 게 승률을 깎는다.

필터를 고르는 기준은 '통과한 신호가 좋아졌나'가 아니다. 그건 표본만 줄여도 좋아 보인다.
**차단된 신호가 통과된 신호보다 실제로 나빴는가**가 기준이다. 그게 게이트 값이다.

    게이트값 = 차단된 신호의 기대R − 통과한 신호의 기대R
    → 음수일수록 좋다 (버린 게 실제로 쓰레기였다는 뜻)

측정은 **독립평가 모드**로 한다. 모든 신호를 각각 평가하므로
필터가 신호를 걸러도 나머지 신호가 밀려나거나 당겨지지 않는다.

사용법
  python scripts\ema99_filters.py                     전 종목 합산
  python scripts\ema99_filters.py --symbol SOXLUSDT
  python scripts\ema99_filters.py --cooldown          손절 후 쿨다운만 따로
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ema99_engine as E
from ema99_engine import Config, flip_of, load_bars, run

ROOT = Path(__file__).resolve().parent.parent
VISION = ROOT / "dataset" / "vision"

BASE = dict(take="both", trend="with", confirm=True, stop_buf=0.50,
            stop_mode="line", target=1.5)

FILTERS = [
    ("추세 안 씀 (기준선)",        dict(filt="none")),
    ("ADX ≥ 15",                dict(filt="adx", filt_min=15)),
    ("ADX ≥ 20",                dict(filt="adx", filt_min=20)),
    ("ADX ≥ 25",                dict(filt="adx", filt_min=25)),
    ("ADX ≥ 30",                dict(filt="adx", filt_min=30)),
    ("효율성비율(20) ≥ 0.15",     dict(filt="er", filt_min=0.15, filt_lb=20)),
    ("효율성비율(20) ≥ 0.25",     dict(filt="er", filt_min=0.25, filt_lb=20)),
    ("효율성비율(20) ≥ 0.35",     dict(filt="er", filt_min=0.35, filt_lb=20)),
    ("효율성비율(50) ≥ 0.25",     dict(filt="er", filt_min=0.25, filt_lb=50)),
    ("EMA99 교차 ≤ 6회/20봉",     dict(filt="crosses", filt_max=6, filt_lb=20)),
    ("EMA99 교차 ≤ 4회/20봉",     dict(filt="crosses", filt_max=4, filt_lb=20)),
    ("EMA99 교차 ≤ 2회/20봉",     dict(filt="crosses", filt_max=2, filt_lb=20)),
    ("EMA99 교차 ≤ 4회/50봉",     dict(filt="crosses", filt_max=4, filt_lb=50)),
    ("EMA99 기울기 ≥ 0.3 ATR",   dict(filt="slope_atr", filt_min=0.3, filt_lb=6)),
    ("EMA99 기울기 ≥ 0.6 ATR",   dict(filt="slope_atr", filt_min=0.6, filt_lb=6)),
    ("EMA99 기울기 ≥ 1.0 ATR",   dict(filt="slope_atr", filt_min=1.0, filt_lb=6)),
    ("EMA99 기울기 ≥ 1.0 ATR(20봉)", dict(filt="slope_atr", filt_min=1.0, filt_lb=20)),
]


def gather(files, cfg):
    """여러 종목을 합쳐서 하나의 표본으로 본다."""
    passed, blocked_n, blocked_sum, wins, n = [], 0, 0.0, 0, 0
    for bars in files:
        r = run(bars, cfg)
        if r.n:
            passed += list(r.trades); wins += r.wins; n += r.n
        blocked_n += r.blocked_n
        blocked_sum += r.blocked_exp_r * r.blocked_n
    exp = sum(passed) / len(passed) if passed else 0.0
    bexp = blocked_sum / blocked_n if blocked_n else 0.0
    wr = wins / n if n else 0.0
    return n, wr, exp, blocked_n, bexp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--cost", type=float, default=0.11)
    ap.add_argument("--min-bars", type=int, default=50000)
    ap.add_argument("--cooldown", action="store_true")
    a = ap.parse_args()

    paths = ([VISION / f"{a.symbol}_1m.csv"] if a.symbol
             else sorted(VISION.glob("*_1m.csv")))
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("❌ dataset/vision 에 CSV 가 없다.")
        return 1
    E.ROUND_TRIP = a.cost

    data = []
    for p in paths:
        b = load_bars(p, p.name.replace("_1m.csv", ""))
        if len(b) >= a.min_bars:
            data.append(b)
    print(f"종목 {len(data)}개 · 왕복비용 {a.cost:.2f}%")
    print("기준 전략: EMA99 터치 · 기울기일치 · 확인후진입 · 손절 0.50% · 익절 1.5%\n")

    if a.cooldown:
        print("손절 직후 쉬어가기 — 몇 봉을 쉬면 좋은가 (실매매 모드)")
        print(f"  {'쿨다운':<16}{'체결':>8}{'승률':>7}{'기대R':>9}{'누적R':>10}")
        for cb in (0, 6, 12, 24, 48, 96):
            tot = wins = 0; acc = []
            for bars in data:
                r = run(bars, Config(cool_bars=cb, **BASE))
                if r.n:
                    acc += list(r.trades); wins += r.wins; tot += r.n
            e = sum(acc) / len(acc) if acc else 0
            lbl = "안 쉼" if cb == 0 else f"{cb}봉 ({cb * 5}분)"
            print(f"  {lbl:<16}{tot:>8}{wins / max(tot,1) * 100:>6.0f}%{e:>+9.3f}{sum(acc):>+10.1f}")
        return 0

    print("독립평가 — 모든 신호를 각각 채점한다 (필터가 다른 신호를 밀어내지 않도록)")
    print(f"  {'필터':<26}{'통과':>7}{'승률':>7}{'통과R':>9}"
          f"{'차단':>7}{'차단R':>9}{'게이트값':>10}  판정")
    base_exp = None
    for name, kw in FILTERS:
        cfg = Config(independent=True, **BASE, **kw)
        n, wr, exp, bn, bexp = gather(data, cfg)
        if base_exp is None:
            base_exp = exp
        gate = bexp - exp if bn else 0.0
        if not bn:
            verdict = "—"
        elif gate < -0.05:
            verdict = "✅ 값을 한다"
        elif gate < -0.01:
            verdict = "△ 약함"
        else:
            verdict = "❌ 쓸모없음"
        print(f"  {name:<26}{n:>7}{wr * 100:>6.0f}%{exp:>+9.3f}"
              f"{bn:>7}{bexp:>+9.3f}{gate:>+10.3f}  {verdict}")

    print("\n방향 대조군 (독립평가 — 같은 신호·같은 손절폭이라 비용이 정확히 상쇄된다)")
    print(f"  {'필터':<26}{'원본R':>9}{'뒤집R':>9}{'차이':>9}")
    for name, kw in FILTERS:
        cfg = Config(independent=True, **BASE, **kw)
        n, _, exp, _, _ = gather(data, cfg)
        if n < 100:
            continue
        _, _, fexp, _, _ = gather(data, flip_of(cfg))
        print(f"  {name:<26}{exp:>+9.3f}{fexp:>+9.3f}{exp - fexp:>+9.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
