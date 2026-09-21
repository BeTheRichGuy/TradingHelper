#!/usr/bin/env python3
"""3배 ETF 짝 매매 — 방향을 맞히지 않고도 남는 게 있는지 본다.

EMA99 노선은 끝났다(docs/11). 방향 예측으로는 우위가 없었다.
그러면 **방향을 안 맞혀도 되는 구조적 우위**가 있는지가 다음 질문이다.

아이디어
--------
SOXL = 반도체 +3배, SOXS = 반도체 −3배. **둘을 동시에 공매도**하면
방향 노출이 서로 상쇄된다(−3 + 3 = 0). 그런데 3배 ETF 는 매일 배율을 다시 맞추기
때문에 **변동성이 클수록 둘 다 장기적으로 가치가 깎인다**(변동성 감쇠).
그 감쇠분이 둘을 공매도한 쪽으로 넘어온다.

    예) 기초자산이 +10% 뒤 −9.09% (제자리) 라면
        3배 ETF 는 +30% 뒤 −27.3% → 0.945  (5.5% 손실)
        역3배 ETF 는 −30% 뒤 +27.3% → 0.891  (10.9% 손실)
        둘 다 깎인다. 공매도한 쪽이 가져간다.

이건 예측이 아니라 **수학적으로 정해진 성질**이다. 다만 공짜는 아니다 —
한쪽으로 강하게 추세가 나면 그쪽 공매도가 크게 밀린다. 그 위험이 얼마나 되는지를
실제 데이터로 재는 게 이 스크립트다.

사용법
  python scripts\pair_decay.py                        SOXL/SOXS
  python scripts\pair_decay.py --pair TQQQUSDT,SQQQUSDT
  python scripts\pair_decay.py --rebalance-hour 20    미국 장마감(UTC)
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ema99_engine import load_bars

ROOT = Path(__file__).resolve().parent.parent
VISION = ROOT / "dataset" / "vision"


def series_at(bars, hour):
    """매일 지정한 UTC 시각의 마지막 종가."""
    out = {}
    for ts, o, h, lo, c, v in bars:
        if ts.hour == hour:
            out[ts.date()] = c
    return out


def simulate(days, a, b, side_a, side_b, fee_bps=4.0):
    """매일 같은 명목가로 리밸런싱. side = -1 공매도, +1 매수.

    수익률은 '전체 명목가 대비'로 낸다. 양쪽에 절반씩 넣는다고 본다.
    """
    eq, curve = 1.0, []
    for k in range(1, len(days)):
        d0, d1 = days[k - 1], days[k]
        ra = (a[d1] - a[d0]) / a[d0]
        rb = (b[d1] - b[d0]) / b[d0]
        day = 0.5 * side_a * ra + 0.5 * side_b * rb
        day -= fee_bps / 10000                    # 매일 리밸런싱 비용
        eq *= (1 + day)
        curve.append((d1, eq, day))
    return curve


def stats(curve, label):
    if len(curve) < 5:
        print(f"  {label:<22} 표본 부족 ({len(curve)}일)")
        return
    rets = [d for _, _, d in curve]
    n = len(rets)
    mean = sum(rets) / n
    sd = (sum((r - mean) ** 2 for r in rets) / (n - 1)) ** 0.5
    peak = dd = 0.0
    for _, eq, _ in curve:
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    total = curve[-1][1] - 1
    # 연율화: 이 계열이 **실제로** 1년에 몇 번 관측되는지로 한다. perp 은 연중무휴라
    # 하루 한 번 리밸런싱이면 365 이지 252 가 아니다. 기초자산 장이 닫힌 날(주말)에는
    # 수익이 거의 0 이라 그 날들을 빼고 252 로 해도 되지만, 그러면 주말 펀딩·감쇠가
    # 빠진다. 여기서는 **달력일 기준 관측 빈도**(n / 경과연수)를 쓴다.
    span_days = (curve[-1][0] - curve[0][0]).days
    per_year = n / (span_days / 365) if span_days > 0 else 365.0
    sharpe = (mean / sd * (per_year ** 0.5)) if sd else 0.0
    wins = sum(1 for r in rets if r > 0)
    print(f"  {label:<22}{n:>6}일{total * 100:>+9.1f}%{mean * 100:>+9.3f}%"
          f"{sd * 100:>8.2f}%{sharpe:>8.2f}{dd * 100:>9.1f}%{wins / n * 100:>7.0f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="SOXLUSDT,SOXSUSDT")
    ap.add_argument("--rebalance-hour", type=int, default=20,
                    help="UTC 기준 리밸런싱 시각 (기본 20 = 미국 장마감 근처)")
    ap.add_argument("--fee-bps", type=float, default=4.0,
                    help="매일 리밸런싱 왕복 비용 bps (기본 4 = 0.04%%)")
    args = ap.parse_args()
    a_sym, b_sym = [s.strip().upper() for s in args.pair.split(",")]

    pa, pb = VISION / f"{a_sym}_1m.csv", VISION / f"{b_sym}_1m.csv"
    for p in (pa, pb):
        if not p.exists():
            print(f"❌ {p.name} 이 없다. fetch_vision.py --symbols leveraged 먼저.")
            return 1

    A = series_at(load_bars(pa, a_sym), args.rebalance_hour)
    B = series_at(load_bars(pb, b_sym), args.rebalance_hour)
    days = sorted(set(A) & set(B))
    if len(days) < 10:
        print(f"❌ 겹치는 날이 {len(days)}일뿐이다. 다른 시각(--rebalance-hour)을 시도해봐.")
        return 1

    print(f"{a_sym} / {b_sym} · 겹치는 날 {len(days)}일 "
          f"({days[0]} ~ {days[-1]}) · 매일 {args.rebalance_hour}:00 UTC 리밸런싱")
    print(f"리밸런싱 비용 {args.fee_bps:.1f}bps/일 · 샤프 연율화 = 실제 관측 빈도(달력일 기준, ≈365)\n")
    print(f"  {'전략':<22}{'일수':>7}{'누적':>10}{'일평균':>10}"
          f"{'일변동성':>9}{'샤프':>8}{'최대낙폭':>10}{'승률':>8}")

    stats(simulate(days, A, B, -1, -1, args.fee_bps), "둘 다 공매도 ★")
    stats(simulate(days, A, B, +1, +1, args.fee_bps), "둘 다 매수 (대조군)")
    stats(simulate(days, A, B, +1, -1, args.fee_bps), f"{a_sym[:4]} 롱 / {b_sym[:4]} 숏")
    stats(simulate(days, A, B, -1, +1, args.fee_bps), f"{a_sym[:4]} 숏 / {b_sym[:4]} 롱")
    stats(simulate(days, A, B, -1, -1, 0.0), "둘 다 공매도 (비용0)")

    print("\n읽는 법")
    print("  · '둘 다 공매도' 가 양수이고 '둘 다 매수' 가 그만큼 음수면 → 감쇠가 실재한다")
    print("  · 최대낙폭이 크면 → 한쪽으로 추세가 났을 때 버틸 증거금이 필요하다는 뜻")
    print("  · 여기에 **펀딩비가 아직 안 들어가 있다.** 스테이블 perp 을 공매도하면")
    print("    펀딩이 양수일 때 추가로 받는다 — 실측 평균 0.0172%/8h ≈ 연 19%.")
    print("  ⚠️ 표본이 두 달 남짓이다. 결과가 좋아도 그대로 믿지 말 것.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
