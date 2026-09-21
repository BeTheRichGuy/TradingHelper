#!/usr/bin/env python3
"""EMA99 터치 매매 — 36개 종목 전부에 같은 규칙을 적용한다.

한 종목에서 좋은 건 우연일 수 있다. 종목을 갈아타도 살아남는지가 진짜 시험이다.
종목마다 **방향 뒤집기 대조군**을 같이 돌린다.

읽는 법 — `차이` 칸이 핵심이다.
  차이 = 원본 기대R − 뒤집기 기대R
  원본과 뒤집기는 **같은 자리, 같은 손절폭**이라 비용이 똑같이 들어간다.
  그래서 차이에서는 **비용이 상쇄되고 방향 예측력만 남는다.**
  차이가 양수인 종목이 많아야 방향 정보가 있는 것이다.

사용법
  python scripts\ema99_multi.py
  python scripts\ema99_multi.py --cost 0.08
  python scripts\ema99_multi.py --take support     지지 롱만
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

# 3배(및 고배율) 레버리지 ETF. 이 묶음이 나머지와 다르게 움직이는지 따로 집계한다.
# 근거: 매일 종가에 배율을 다시 맞추는 리밸런싱 때문에 '오르면 사고 내리면 파는'
#       강제 매매 흐름이 구조적으로 존재한다 — 추세추종이 먹힐 이유가 있는 유일한 묶음.
LEVERAGED = {
    "SOXLUSDT", "SOXSUSDT", "TQQQUSDT", "SQQQUSDT",     # 기존 검증에 쓴 4개
    "SPXLUSDT", "SPXSUSDT", "UPROUSDT", "SPXUUSDT",     # 한 번도 안 쓴 7개 ↓
    "TNAUSDT", "TZAUSDT", "UVXYUSDT",
}
KNOWN = {"SOXLUSDT", "SOXSUSDT", "TQQQUSDT", "SQQQUSDT"}   # 이미 본 종목

BASE = dict(take="both", trend="with", confirm=True, stop_buf=0.50,
            stop_mode="line", target=1.5)

# 일봉 추세 게이트 — '어느 쪽을 잡을지'만 일봉이 정하고, '언제 잡을지'는 5분봉이 정한다.
# 각 순방향 프리셋 바로 뒤에 **역방향 대조군**을 붙였다.
# 게이트가 진짜 일을 한다면 순방향이 역방향보다 뚜렷이 좋아야 한다.
# 역방향이 비슷하거나 더 좋으면 그 게이트는 아무것도 구분하지 못한 것이다.
PRESETS = {
    "0 게이트 없음 (기준선)":            dict(**BASE),
    "1 일봉 7일 추세 방향만":            dict(regime_lb=7, **BASE),
    "1' 일봉 7일 **반대** 방향만 (대조군)": dict(regime_lb=7, regime_mode="against", **BASE),
    "2 일봉 28일 추세 방향만":           dict(regime_lb=28, **BASE),
    "2' 일봉 28일 **반대** 방향만 (대조군)": dict(regime_lb=28, regime_mode="against", **BASE),
}


def wilson(w, n, z=1.96):
    if n <= 0:
        return 0.0, 0.0
    p = w / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.11)
    ap.add_argument("--take", default="both", choices=("both", "support", "resistance"))
    ap.add_argument("--min-bars", type=int, default=50000)
    ap.add_argument("--min-trades", type=int, default=15)
    a = ap.parse_args()

    files = sorted(VISION.glob("*_1m.csv"))
    if not files:
        print(f"❌ {VISION} 에 CSV 가 없다. fetch_vision.py 를 먼저 돌려라.")
        return 1
    E.ROUND_TRIP = a.cost
    print(f"왕복비용 {a.cost:.2f}% · 종목 {len(files)}개 · 잡는 자리 = {a.take}\n")

    cache = {}
    for f in files:
        sym = f.name.replace("_1m.csv", "")
        b = load_bars(f, sym)
        if len(b) >= a.min_bars:
            cache[sym] = b

    for name, kw in PRESETS.items():
        cfg = Config(**{**kw, "take": a.take})
        print("=" * 100)
        print(name)
        print(f"  {'종목':<12}{'체결':>7}{'승률':>7}{'95%CI':>13}{'손절폭':>8}"
              f"{'기대R':>9}{'누적R':>9}{'보유':>7}{'뒤집':>9}{'차이':>8}")
        agg, aggf, wins, tot, beat, cnt = [], [], 0, 0, 0, 0
        groups = {"레버리지ETF(기존)": [], "레버리지ETF(신규)": [], "일반 주식·ETF": []}
        for sym, bars in cache.items():
            r = run(bars, cfg)
            if r.n < a.min_trades:
                continue
            fl = run(bars, flip_of(cfg))   # 같은 신호·같은 손절폭 (엔진이 보장)
            diff = r.exp_r - fl.exp_r
            lo, hi = wilson(r.wins, r.n)
            cnt += 1; beat += diff > 0; wins += r.wins; tot += r.n
            agg += list(r.trades); aggf += list(fl.trades)
            key = ("레버리지ETF(기존)" if sym in KNOWN else
                   "레버리지ETF(신규)" if sym in LEVERAGED else "일반 주식·ETF")
            groups[key].append((sym, r, fl))
            print(f"  {sym:<12}{r.n:>7}{r.win_rate * 100:>6.0f}%"
                  f"{f'{lo * 100:.0f}~{hi * 100:.0f}%':>13}{r.avg_stop_pct:>7.2f}%"
                  f"{r.exp_r:>+9.3f}{r.total_r:>+9.1f}{r.avg_hold:>6.0f}m"
                  f"{fl.exp_r:>+9.3f}{diff:>+8.3f}")
        if not cnt:
            print("  (조건을 만족하는 종목 없음)\n")
            continue
        e = sum(agg) / len(agg)
        ef = sum(aggf) / len(aggf) if aggf else 0.0
        lo, hi = wilson(wins, tot)
        print("  " + "─" * 96)
        print(f"  {'합계':<12}{tot:>7}{wins / tot * 100:>6.0f}%"
              f"{f'{lo * 100:.0f}~{hi * 100:.0f}%':>13}{'':>8}"
              f"{e:>+9.3f}{sum(agg):>+9.1f}{'':>7}{ef:>+9.3f}{e - ef:>+8.3f}")
        verdict = ("방향 정보 있음" if beat >= cnt * 0.67 else
                   "판단 보류" if beat >= cnt * 0.5 else "방향 정보 없음")
        print(f"  → 대조군을 이긴 종목 {beat}/{cnt}   ({verdict})")

        print("\n  묶음별 — 레버리지 ETF 만 다른지 본다")
        print(f"  {'묶음':<20}{'종목':>5}{'체결':>8}{'승률':>7}{'기대R':>9}"
              f"{'방향차이':>10}{'양수종목':>10}")
        for gname, items in groups.items():
            if not items:
                continue
            gn = sum(x[1].n for x in items)
            gnf = sum(x[2].n for x in items)     # 뒤집기는 체결 수가 다를 수 있다 — 자기 n 으로 나눈다
            ge = sum(x[1].exp_r * x[1].n for x in items) / gn
            gf = sum(x[2].exp_r * x[2].n for x in items) / gnf if gnf else 0.0
            gw = sum(x[1].wins for x in items) / gn
            gp = sum(1 for x in items if x[1].exp_r > 0)
            print(f"  {gname:<20}{len(items):>5}{gn:>8}{gw * 100:>6.0f}%"
                  f"{ge:>+9.3f}{ge - gf:>+10.3f}{f'{gp}/{len(items)}':>10}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
