#!/usr/bin/env python3
"""시계열 모멘텀 (Time-Series Momentum) — 다일 추세추종.

왜 이걸 하나
------------
암호화폐에서 **비용을 빼고도 살아남았다고 보고된 유일한 계열**이다.
(Han·Kang·Ryu, AUT/SSRN: 룩백 28일 / 보유 5일에서 15bp 비용 차감 후 Sharpe 1.51)
그리고 우리 첫 EMA 실험에서 "보유를 몇 시간으로 자르면 우위가 사라진다"고 나온 것과
방향이 같다. 우위는 긴 쪽에 있었다.

⚠️ 여기서 제일 중요한 대조군은 방향 뒤집기가 아니라 **그냥 사서 들고 있기**다.
   BTC 는 2020~2026 에 크게 올랐다. 롱 위주 전략은 실력이 없어도 좋아 보인다.
   **매수 후 보유를 못 이기면 아무 의미 없다.**

규칙
----
  · UTC 하루 단위 봉으로 본다
  · 최근 L일 수익률이 양수면 롱, 음수면 숏 (long_only 면 양수일 때만 롱, 아니면 현금)
  · H일마다 신호를 다시 본다
  · 변동성 타겟팅 — 최근 변동성이 크면 사이즈를 줄인다 (레버리지 상품에선 필수)
  · 포지션이 바뀐 만큼만 비용을 낸다
  · 펀딩 — 실측 0.0172%/8h (docs/11). 롱이 내고 숏이 받는다. 하루 3회
  · 청산 — 크로스 마진, 유지증거금률 0.5%. 일중 저가(롱)/고가(숏)로 판정한다.
    청산되면 그 날 −100% 이고 이후는 0 (다른 엔진과 같은 기준으로 비교하기 위해서다)
    끄려면 --funding-8h 0 --mmr 0

사용법
  python scripts\tsmom.py                          BTCUSDT
  python scripts\tsmom.py --symbol ETHUSDT
  python scripts\tsmom.py --long-only
  python scripts\tsmom.py --target-vol 0.20 --max-leverage 2
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ema99_engine import load_bars

ROOT = Path(__file__).resolve().parent.parent
VISION = ROOT / "dataset" / "vision"

ONE_WAY = 0.055        # % — 편도 비용 (테이커 0.05 + 스프레드)
YEAR = 365             # 암호화폐는 연중무휴
FUNDING_8H = 0.0172    # % / 8h — SOXLUSDT 실측 평균 (docs/11). 롱이 낸다
MMR = 0.005            # 유지증거금률 (크로스 마진)


# ── 데이터 ──────────────────────────────────────────────────────────────
def to_daily(bars):
    """1분봉 → UTC 일봉 (종가, 저가, 고가). 저·고는 청산 판정에 쓴다."""
    per = defaultdict(list)
    for ts, o, h, lo, c, v in bars:
        per[ts.date()].append((c, lo, h))
    days = sorted(per)
    px = [per[d][-1][0] for d in days]
    lows = [min(x[1] for x in per[d]) for d in days]
    highs = [max(x[2] for x in per[d]) for d in days]
    return days, px, lows, highs


def stats(rets, label, extra=""):
    n = len(rets)
    if n < 30:
        print(f"  {label:<26} 표본 부족 ({n}일)")
        return None
    mean = sum(rets) / n
    sd = (sum((r - mean) ** 2 for r in rets) / (n - 1)) ** 0.5
    ann_v = sd * (YEAR ** 0.5)
    sharpe = (mean * YEAR) / ann_v if ann_v else 0.0
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    # 연수익은 **실제로 불어난 것**(CAGR)으로 낸다. 산술평균×365 는 변동성이 크면
    # 실제보다 훨씬 좋아 보인다 — 하루 +50%/−50% 를 반복하면 평균은 0인데 돈은 사라진다.
    ann_r = (eq ** (YEAR / n) - 1) if eq > 0 else -1.0
    # 샤프의 표준오차 ≈ 1/√(연수) — 이게 없으면 샤프 1.5 를 그냥 믿게 된다
    se = 1 / ((n / YEAR) ** 0.5)
    print(f"  {label:<26}{ann_r * 100:>+9.1f}%{ann_v * 100:>8.1f}%{sharpe:>8.2f}"
          f"{f'±{se:.2f}':>7}{dd * 100:>9.1f}%{(eq - 1) * 100:>+11.0f}%  {extra}")
    return dict(ann_r=ann_r, ann_v=ann_v, sharpe=sharpe, mdd=dd, total=eq - 1, se=se)


# ── 전략 ────────────────────────────────────────────────────────────────
def liquidated(pos, prev_close, low, high, mmr):
    """크로스 마진 청산 — 일중 최악 지점에서 자산이 유지증거금 밑으로 갔는가.

    자산 E, 포지션 배율 |pos|. 가격이 r 만큼 움직이면 자산 E(1 + pos·r),
    유지증거금 mmr·|pos|·E(1 + r). 롱은 저가, 숏은 고가로 본다.
    """
    if pos == 0 or mmr <= 0:
        return False
    r = (low / prev_close - 1) if pos > 0 else (high / prev_close - 1)
    return 1 + pos * r <= mmr * abs(pos) * (1 + r)


def run_tsmom(px, L, H, long_only=False, flip=False,
              target_vol=0.0, max_lev=1.0, vol_lb=30, cost=ONE_WAY,
              lows=None, highs=None, funding_8h=FUNDING_8H, mmr=MMR):
    """일별 수익률 시계열을 돌려준다 (비용·펀딩 차감 후, 청산 반영).

    lows/highs 가 없으면 청산 판정은 하지 않는다. funding_8h=0, mmr=0 이면 예전 동작.
    """
    n = len(px)
    rets = [0.0] * n
    for i in range(1, n):
        rets[i] = px[i] / px[i - 1] - 1
    funding_day = 3 * funding_8h / 100          # 하루 3회, 명목 대비

    out = []
    pos = 0.0
    for i in range(max(L, vol_lb) + 1, n):
        # 청산은 그 날의 포지션(전날 정한 것)으로 일중에 난다 — 리밸런싱보다 먼저 본다
        if lows is not None and highs is not None and liquidated(pos, px[i - 1], lows[i], highs[i], mmr):
            out.append(-1.0)
            out.extend([0.0] * (n - 1 - i))
            return out
        if (i - (max(L, vol_lb) + 1)) % H == 0:      # 리밸런싱 날
            mom = px[i - 1] / px[i - 1 - L] - 1
            sig = 1.0 if mom > 0 else (0.0 if long_only else -1.0)
            if flip:
                sig = -sig
            size = 1.0
            if target_vol > 0:
                w = rets[i - vol_lb:i]
                m = sum(w) / len(w)
                sd = (sum((r - m) ** 2 for r in w) / (len(w) - 1)) ** 0.5
                rv = sd * (YEAR ** 0.5)
                size = min(max_lev, target_vol / rv) if rv > 0 else 0.0
            new = sig * size
            turn = abs(new - pos)
            pos = new
            out.append(pos * rets[i] - turn * cost / 100 - pos * funding_day)
        else:
            # 펀딩: 롱(pos>0)이 내고 숏(pos<0)이 받는다 — 부호가 pos 를 따라간다
            out.append(pos * rets[i] - pos * funding_day)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--target-vol", type=float, default=0.0,
                    help="연 변동성 목표 (예: 0.20). 0 이면 사이징 안 함")
    ap.add_argument("--max-leverage", type=float, default=1.0)
    ap.add_argument("--cost", type=float, default=ONE_WAY, help="편도 비용 %%")
    ap.add_argument("--funding-8h", type=float, default=FUNDING_8H,
                    help="8시간 펀딩 %% (롱이 낸다). 0 이면 없음")
    ap.add_argument("--mmr", type=float, default=MMR,
                    help="유지증거금률 (크로스 마진 청산 판정). 0 이면 청산 없음")
    ap.add_argument("--split", type=float, default=0.6, help="IS 비율")
    a = ap.parse_args()

    p = VISION / f"{a.symbol}_1m.csv"
    if not p.exists():
        print(f"❌ {p.name} 이 없다.")
        print(f"   python scripts\\fetch_vision.py --symbols {a.symbol} --since 2020-01")
        return 1

    days, px, lows, highs = to_daily(load_bars(p, a.symbol))
    print(f"{a.symbol} · {len(days)}일 ({days[0]} ~ {days[-1]}) "
          f"· 편도비용 {a.cost:.3f}% · 펀딩 {a.funding_8h:.4f}%/8h · 유지증거금 {a.mmr:.2%}"
          f" · {'롱만' if a.long_only else '롱숏'}"
          + (f" · 변동성목표 {a.target_vol:.0%} (최대 {a.max_leverage:.1f}배)"
             if a.target_vol else " · 사이징 없음"))
    cut = int(len(px) * a.split)
    print(f"  IS  {days[0]} ~ {days[cut - 1]}  ({cut}일)")
    print(f"  OOS {days[cut]} ~ {days[-1]}  ({len(px) - cut}일)\n")

    kw = dict(long_only=a.long_only, target_vol=a.target_vol,
              max_lev=a.max_leverage, cost=a.cost, funding_8h=a.funding_8h, mmr=a.mmr)
    HDR = (f"  {'':<26}{'연수익':>9}{'연변동성':>8}{'샤프':>8}{'오차':>7}"
           f"{'최대낙폭':>9}{'누적':>11}")

    # ── 대조군: 그냥 사서 들고 있기 ─────────────────────
    print("대조군 — 이걸 못 이기면 전부 무의미하다")
    print(HDR)
    bh = [px[i] / px[i - 1] - 1 for i in range(1, len(px))]
    bh_all = stats(bh, "매수 후 보유 (전 구간)")
    stats(bh[:cut], "매수 후 보유 (IS)")
    bh_oos = stats(bh[cut:], "매수 후 보유 (OOS)")

    # ── 격자: IS 로만 고른다 ────────────────────────────
    LS = (7, 14, 21, 28, 45, 60, 90)
    HS = (1, 3, 5, 10, 20)
    print(f"\n격자 {len(LS)}×{len(HS)} = {len(LS) * len(HS)}개 — IS 샤프로만 순위를 매긴다")
    scored = []
    for L in LS:
        for H in HS:
            r = run_tsmom(px[:cut], L, H, lows=lows[:cut], highs=highs[:cut], **kw)
            if len(r) < 60:
                continue
            m = sum(r) / len(r)
            sd = (sum((x - m) ** 2 for x in r) / (len(r) - 1)) ** 0.5
            if sd <= 0:
                continue
            scored.append((m * YEAR / (sd * YEAR ** 0.5), L, H))
    scored.sort(reverse=True)
    pos_is = sum(1 for s, _, _ in scored if s > 0)
    print(f"  IS 샤프 양수 {pos_is}/{len(scored)}개\n")

    print("IS 상위 8 → OOS 채점")
    print(HDR)
    oos_sharpes = []
    for s_is, L, H in scored[:8]:
        r = run_tsmom(px[cut:], L, H, lows=lows[cut:], highs=highs[cut:], **kw)
        st = stats(r, f"룩백{L}일 / 보유{H}일", f"IS샤프 {s_is:+.2f}")
        if st:
            oos_sharpes.append(st["sharpe"])
    if oos_sharpes:
        beat = sum(1 for s in oos_sharpes if bh_oos and s > bh_oos["sharpe"])
        print(f"\n  OOS 샤프 양수 {sum(1 for s in oos_sharpes if s > 0)}/{len(oos_sharpes)}"
              f"  ·  매수후보유를 이긴 것 {beat}/{len(oos_sharpes)}")

    # ── 방향 대조군 ─────────────────────────────────────
    if not a.long_only:
        print("\n방향 뒤집기 대조군 (전 구간)")
        print(HDR)
        for _, L, H in scored[:3]:
            stats(run_tsmom(px, L, H, lows=lows, highs=highs, **kw), f"룩백{L}/보유{H} 원본")
            stats(run_tsmom(px, L, H, flip=True, lows=lows, highs=highs, **kw),
                  f"룩백{L}/보유{H} 뒤집기")

    print("\n판정 기준")
    print("  ① OOS 샤프가 매수후보유보다 높은가 (8개 중 6개 이상)")
    print("  ② OOS 최대낙폭이 매수후보유보다 작은가")
    print("  ③ 샤프 오차(±)를 감안해도 차이가 남는가")
    print("  → 셋 다 아니면, 그냥 사서 들고 있는 게 낫다는 뜻이다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
