#!/usr/bin/env python3
"""EMA99 터치 매매 — 한 종목에서 파라미터를 훑는다.

고르는 건 **앞구간(IS)** 으로만, 채점은 **뒷구간(OOS)** 으로만 한다.
전체 구간 성적으로 고르면 이미 미래를 본 것이라 OOS 가 OOS 가 아니게 된다.

사용법
  python scripts\ema99_sweep.py                    SOXLUSDT
  python scripts\ema99_sweep.py --symbol NVDAUSDT
  python scripts\ema99_sweep.py --cost 0.08
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ema99_engine as E
from ema99_engine import Config, flip_of, load_bars, run

ROOT = Path(__file__).resolve().parent.parent
VISION = ROOT / "dataset" / "vision"

GRID = dict(
    take=("both", "support", "resistance"),
    trend=("none", "with", "stack"),
    confirm=(False, True),
    stop_buf=(0.20, 0.35, 0.50, 0.80),
    stop_mode=("line", "wick"),
    target=(1.0, 1.5, 2.0),
    max_bars=(0, 78),
)
TAKE_KR = {"both": "지지+저항", "support": "지지롱만", "resistance": "저항숏만"}
TREND_KR = {"none": "필터없음", "with": "기울기일치", "stack": "9/20정렬"}


def wilson(w, n, z=1.96):
    if n <= 0:
        return 0.0, 0.0
    p = w / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def label(c: Config) -> str:
    return (f"{TAKE_KR[c.take]} {TREND_KR[c.trend]} {'확인' if c.confirm else '즉시'} "
            f"손절{c.stop_buf:.2f}{'선' if c.stop_mode == 'line' else '꼬'} "
            f"익절{c.target:.1f}% {'6.5h' if c.max_bars else '무제한'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SOXLUSDT")
    ap.add_argument("--cost", type=float, default=0.11)
    ap.add_argument("--min-trades", type=int, default=25)
    a = ap.parse_args()

    path = VISION / f"{a.symbol}_1m.csv"
    if not path.exists():
        path = ROOT / "dataset" / "klines_1m.csv"
        if not path.exists():
            print(f"❌ {a.symbol} 데이터가 없다. fetch_vision.py 를 먼저 돌려라.")
            return 1
    bars = load_bars(path, a.symbol)
    E.ROUND_TRIP = a.cost

    cut = bars[0][0] + (bars[-1][0] - bars[0][0]) * 0.65
    is_b = [b for b in bars if b[0] < cut]
    oos_b = [b for b in bars if b[0] >= cut]
    print(f"{a.symbol} · {len(bars):,}봉 · 왕복비용 {a.cost:.2f}%")
    print(f"  IS  {bars[0][0].date()} ~ {cut.date()}  ({len(is_b):,}봉)")
    print(f"  OOS {cut.date()} ~ {bars[-1][0].date()}  ({len(oos_b):,}봉)\n")

    combos = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())]
    print(f"조합 {len(combos)}개 — IS 로만 순위를 매긴다...", flush=True)
    scored = []
    for k, kw in enumerate(combos, 1):
        cfg = Config(**kw)
        r = run(is_b, cfg)
        if r.n >= a.min_trades:
            scored.append((r.exp_r, cfg, r))
        if k % 200 == 0:
            print(f"  {k}/{len(combos)}", flush=True)
    scored.sort(key=lambda x: -x[0])
    pos = sum(1 for e, _, _ in scored if e > 0)
    print(f"\nIS 표본 충분 {len(scored)}개 · IS 양수 {pos}개 "
          f"({pos / max(len(scored), 1) * 100:.0f}%)\n")

    print("IS 상위 15 → OOS 채점 + 방향뒤집기 대조군")
    print(f"  {'설정':<52}{'IS':>8}{'ISn':>6}{'OOS':>9}{'OOSn':>6}"
          f"{'승률':>6}{'95%CI':>12}{'뒤집':>8}{'차이':>8}")
    shown = 0
    for e_is, cfg, r_is in scored:
        if shown >= 15:
            break
        r = run(oos_b, cfg)
        if r.n < 8:
            continue
        f = run(oos_b, flip_of(cfg))
        lo, hi = wilson(r.wins, r.n)
        print(f"  {label(cfg):<52}{e_is:>+8.3f}{r_is.n:>6}{r.exp_r:>+9.3f}{r.n:>6}"
              f"{r.win_rate * 100:>5.0f}%{f'{lo * 100:.0f}~{hi * 100:.0f}%':>12}"
              f"{f.exp_r:>+8.3f}{r.exp_r - f.exp_r:>+8.3f}")
        shown += 1

    print("\n" + "=" * 96)
    print("지지(롱) vs 저항(숏) — 같은 설정에서 어느 쪽이 실제로 되는가 (전 구간)")
    print(f"  {'설정':<40}{'전체n':>7}{'지지n':>7}{'지지R':>9}{'저항n':>7}{'저항R':>9}")
    seen = set()
    for _, cfg, _ in scored[:60]:
        key = (cfg.trend, cfg.confirm, cfg.stop_buf, cfg.stop_mode, cfg.target, cfg.max_bars)
        if cfg.take != "both" or key in seen:
            continue
        seen.add(key)
        r = run(bars, cfg)
        if r.n < 20:
            continue
        print(f"  {label(cfg):<40}{r.n:>7}{r.by_kind[0]:>7}{r.kind_exp[0]:>+9.3f}"
              f"{r.by_kind[1]:>7}{r.kind_exp[1]:>+9.3f}")
        if len(seen) >= 10:
            break

    print("\n" + "=" * 96)
    print("최상위 3개 자세히 (전 구간)")
    for _, cfg, _ in scored[:3]:
        r = run(bars, cfg)
        if not r.n:
            continue
        tp, sl, tm = r.by_exit
        print(f"\n  {label(cfg)}")
        print(f"    체결 {r.n} · 승률 {r.win_rate * 100:.0f}% · 평균 손절폭 "
              f"{r.avg_stop_pct:.2f}% · 평균보유 {r.avg_hold:.0f}분")
        print(f"    익절 {tp} / 손절 {sl} / 시간청산 {tm}"
              f"   (손절가 오류로 버린 신호 {r.skipped_bad_stop})")
        print(f"    매매당 {r.exp_pct:+.3f}% ({r.exp_r:+.3f}R) · 누적 {r.total_pct:+.1f}% "
              f"· 최대낙폭 {r.max_dd_r:.1f}R · PF {r.profit_factor or 0:.2f}")
        print(f"    비용이 먹는 몫: 매매당 {a.cost / r.avg_stop_pct:.2f}R")
    return 0


if __name__ == "__main__":
    sys.exit(main())
