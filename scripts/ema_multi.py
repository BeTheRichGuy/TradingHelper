#!/usr/bin/env python3
"""여러 종목에 같은 규칙을 그대로 적용해 본다 — 종목을 갈아타도 살아남는가.

SOXLUSDT 는 2026-05-15 상장이라 과거가 115일뿐이다. 기간을 늘릴 수 없으면
**종목을 늘리는 게 유일한 표본 확대 방법**이다. 그리고 사실 이게 더 강한 검증이다.
기간을 늘리면 '그 종목의 그 시절'을 더 볼 뿐이지만, 종목을 늘리면
'이 규칙이 SOXL 만의 우연이 아닌가'를 직접 묻게 된다.

각 종목마다 원본과 **방향 뒤집기 대조군**을 같이 돌린다.
원본이 대조군보다 일관되게 나아야 방향 정보가 있다고 말할 수 있다.

사용법
------
  python scripts/ema_multi.py                     dataset/vision 안의 모든 종목
  python scripts/ema_multi.py --cost 0.08         지정가 체결 가정
  python scripts/ema_multi.py --min-bars 50000    봉이 너무 적은 종목 제외
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ema_engine
from ema_engine import Config, load_bars, run

ROOT = Path(__file__).resolve().parent.parent
VISION = ROOT / "dataset" / "vision"

# docs/09 에서 640개 중 다중검정을 통과한 조합들
CFGS = {
    "A  EMA9눌림/EMA99손절/3R":  dict(tf=5, align=4, entry="pullback9",  stop="ema99", exit="r3"),
    "B  EMA9눌림/EMA99손절/3R(99정배열)": dict(tf=5, align=3, entry="pullback9", stop="ema99", exit="r3"),
    "C  EMA20눌림/ATR2.5/3R":    dict(tf=5, align=4, entry="pullback20", stop="atr25", exit="r3"),
    "D  EMA9눌림/ATR2.5/3R":     dict(tf=5, align=4, entry="pullback9",  stop="atr25", exit="r3"),
}


def wilson(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.14, help="왕복 비용 %% (기본 0.14)")
    ap.add_argument("--min-bars", type=int, default=20000)
    ap.add_argument("--min-trades", type=int, default=15)
    a = ap.parse_args()

    files = sorted(VISION.glob("*_1m.csv"))
    if not files:
        print(f"❌ {VISION} 에 CSV 가 없다. 먼저 scripts/fetch_vision.py 를 돌려라.")
        return 1

    ema_engine.ROUND_TRIP = a.cost
    print(f"왕복비용 {a.cost:.2f}% · 종목 {len(files)}개\n")

    for label, kw in CFGS.items():
        cfg = Config(**kw)
        flip = Config(flip=True, **kw)
        print("=" * 96)
        print(f"{label}")
        print(f"  {'종목':<12}{'봉':>10}{'체결':>7}{'승률':>7}{'95%CI':>13}"
              f"{'기대값':>9}{'누적R':>9}{'뒤집기':>9}{'차이':>8}")
        agg_r: list[float] = []
        agg_f: list[float] = []
        wins = tot = beat = counted = 0
        for f in files:
            sym = f.name.replace("_1m.csv", "")
            bars = load_bars(f, symbol=sym)
            if len(bars) < a.min_bars:
                continue
            r = run(bars, cfg)
            fl = run(bars, flip)
            if r.n < a.min_trades:
                continue
            lo, hi = wilson(r.wins, r.n)
            diff = r.expectancy_r - fl.expectancy_r
            counted += 1
            beat += diff > 0
            wins += r.wins
            tot += r.n
            agg_r += list(r.trades)
            agg_f += list(fl.trades)
            print(f"  {sym:<12}{len(bars):>10,}{r.n:>7}{r.win_rate * 100:>6.0f}%"
                  f"{f'{lo * 100:.0f}~{hi * 100:.0f}%':>13}"
                  f"{r.expectancy_r:>+9.3f}{r.total_r:>+9.1f}"
                  f"{fl.expectancy_r:>+9.3f}{diff:>+8.3f}")
        if not counted:
            print("  (조건을 만족하는 종목 없음)")
            continue
        e = sum(agg_r) / len(agg_r)
        ef = sum(agg_f) / len(agg_f) if agg_f else 0.0
        lo, hi = wilson(wins, tot)
        print(f"  {'─' * 92}")
        print(f"  {'합계':<12}{'':>10}{tot:>7}{wins / tot * 100:>6.0f}%"
              f"{f'{lo * 100:.0f}~{hi * 100:.0f}%':>13}{e:>+9.3f}{sum(agg_r):>+9.1f}"
              f"{ef:>+9.3f}{e - ef:>+8.3f}")
        print(f"  → 대조군을 이긴 종목 {beat}/{counted}  "
              f"({'방향 정보 있음' if beat >= counted * 0.7 else '방향 정보 불확실'})")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
