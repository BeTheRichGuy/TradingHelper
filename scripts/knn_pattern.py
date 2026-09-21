#!/usr/bin/env python3
r"""유사 패턴 검색 (k-NN) — docs/06 A모듈. **셔플 대조군을 먼저 세우고 시작한다.**

왜 대조군부터인가
-----------------
33,000봉에서 240차원 이웃을 찾으면 우연히 좋아 보이는 패턴은 **반드시** 나온다.
검색공간이 사실상 무한대이기 때문이다. 우리는 검색공간 640개에서도 네 번 속았다.

그래서 정직한 질문은 "좋은 패턴이 있나"가 아니라
**"검색을 안 했을 때보다 좋은가"** 다. 이걸 재는 방법이 라벨 셔플이다.

    미래 수익률만 무작위로 섞고 똑같이 돌린다.
    → 진짜 패턴은 사라지고, '검색 때문에 생긴 착시'만 남는다.
    → 셔플에서 나온 최고 성적 ≈ 실제 성적 이면, 전부 착시다.

규칙
----
  · 5분봉 60개(=5시간)를 하나의 모양으로 본다
  · 봉마다 4개 피처, 창 전체를 z-score → **가격 수준을 지우고 모양만 남긴다**
  · 이웃은 **과거 구간(IS)에서만** 찾는다. 질의는 미래 구간(OOS)에서만 한다
  · 시간이 겹치는 이웃은 버린다 (같은 사건이 여러 번 뽑히는 것 방지)
  · 결과는 이후 24봉(2시간) 수익률

사용법
  python scripts\knn_pattern.py
  python scripts\knn_pattern.py --symbol NVDAUSDT --horizon 12
  python scripts\knn_pattern.py --shuffles 30
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ema99_engine import load_bars, resample

ROOT = Path(__file__).resolve().parent.parent
VISION = ROOT / "dataset" / "vision"


def trailing_mean(x, n):
    """직전 n개(자기 포함)의 평균. 앞부분은 있는 만큼만으로 나눈다."""
    cs = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    idx = np.arange(len(x))
    lo = np.maximum(idx - n + 1, 0)
    return (cs[idx + 1] - cs[lo]) / (idx + 1 - lo)


def block_shuffle(vals, block, rng):
    """겹치는 라벨용 셔플 — `block` 길이 덩어리째 섞는다.

    24봉 앞 수익률은 이웃한 봉끼리 23봉을 공유한다. 봉 단위로 섞으면 그 자기상관이
    깨져 대조군의 분산이 실제보다 작아지고, 진짜 신호가 없어도 p 값이 작게 나온다.
    덩어리(≥ horizon)로 섞으면 덩어리 안의 상관은 그대로 남는다.
    """
    n = len(vals)
    block = max(1, min(block, n))
    starts = np.arange(0, n, block)
    order = rng.permutation(len(starts))
    out = np.concatenate([vals[starts[k]:starts[k] + block] for k in order])
    return out[:n]


def overlap_se(r, bars, lag):
    """겹치는 관측치의 표준오차 — Newey-West(Bartlett), 거리는 **봉 간격**으로 잰다.

    진입 신호가 봉 i 와 i+3 에서 났으면 두 결과는 같은 21봉을 공유한다. 독립으로 치면
    표준오차가 √(겹침 배수) 만큼 작아져 t 값이 부풀려진다.
    """
    r = np.asarray(r, dtype=np.float64)
    bars = np.asarray(bars)
    n = len(r)
    if n < 2:
        return float("nan")
    d = r - r.mean()
    var = float((d * d).sum())
    for k in range(1, n):
        gap = bars[k:] - bars[:-k]
        w = np.clip(1.0 - gap / (lag + 1.0), 0.0, None)
        if not w.any():
            break
        var += 2.0 * float((w * d[k:] * d[:-k]).sum())
    return max(var, 0.0) ** 0.5 / n


def build_features(bs, win):
    """봉마다 4개 피처 → 창 단위로 z-score 정규화한 벡터."""
    n = len(bs)
    o = np.array([b[1] for b in bs], dtype=np.float64)
    h = np.array([b[2] for b in bs], dtype=np.float64)
    lo = np.array([b[3] for b in bs], dtype=np.float64)
    c = np.array([b[4] for b in bs], dtype=np.float64)
    v = np.array([b[5] for b in bs], dtype=np.float64)

    ret = np.zeros(n); ret[1:] = np.log(c[1:] / c[:-1])
    rng = (h - lo) / np.maximum(c, 1e-9)
    posn = np.where(h > lo, (c - lo) / np.maximum(h - lo, 1e-12), 0.5)
    # 거래량 평균은 **뒤로만** 본다 (직전 20봉, 자기 자신 포함). mode="same" 컨볼루션은
    # 창이 가운데 놓여 앞 10봉의 거래량을 피처에 섞는다 — 미래 누수.
    vma = trailing_mean(v, 20)
    relv = v / np.maximum(vma, 1e-9)

    F = np.stack([ret, rng, posn, relv], axis=1)          # (n, 4)
    m = F.shape[1]
    out = np.zeros((n - win + 1, win * m), dtype=np.float32)
    for i in range(win - 1, n):
        w = F[i - win + 1:i + 1]                          # (win, 4)
        mu, sd = w.mean(0), w.std(0)
        out[i - win + 1] = ((w - mu) / np.maximum(sd, 1e-9)).ravel()
    # 코사인 유사도용 정규화
    out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)
    return out


def evaluate(V, fwd, is_hi, q_lo, win, K, sim_min, gap, thresh, rng=None, horizon=1):
    """OOS 질의 → IS 이웃 → 이웃 평균으로 방향 예측 → 실제 결과.

    rng 가 주어지면 fwd 를 **덩어리째** 섞어서 쓴다 (셔플 대조군, 덩어리 = 2×horizon).
    (수익률, 방향, 진입 봉 인덱스) 를 돌려준다.
    """
    y = fwd.copy()
    if rng is not None:
        ok = ~np.isnan(y)
        y[ok] = block_shuffle(y[ok], 2 * horizon, rng)

    corpus = V[:is_hi]
    picks_r, picks_dir, picks_bar = [], [], []
    B = 2000
    for s in range(q_lo, len(V), B):
        Q = V[s:s + B]
        sims = Q @ corpus.T                                # (b, is_hi)
        for qi in range(Q.shape[0]):
            gi = s + qi                                    # 벡터 인덱스
            bar = gi + win - 1                             # 해당 봉 인덱스
            if np.isnan(y[bar]):
                continue
            row = sims[qi]
            idx = np.argpartition(-row, min(K * 3, len(row) - 1))[:K * 3]
            idx = idx[row[idx] >= sim_min]
            if len(idx) < 10:
                continue
            # 시간이 겹치는 이웃 제거 — 같은 사건 중복 방지
            idx = idx[np.argsort(-row[idx])]
            keep, used = [], []
            for j in idx:
                if all(abs(int(j) - u) >= gap for u in used):
                    keep.append(int(j)); used.append(int(j))
                if len(keep) >= K:
                    break
            if len(keep) < 10:
                continue
            nb = y[np.array(keep) + win - 1]
            nb = nb[~np.isnan(nb)]
            if len(nb) < 10:
                continue
            pred = float(nb.mean())
            if abs(pred) < thresh:
                continue
            picks_r.append(float(y[bar]) * (1 if pred > 0 else -1))
            picks_dir.append(1 if pred > 0 else -1)
            picks_bar.append(bar)
    return np.array(picks_r), np.array(picks_dir), np.array(picks_bar)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SOXLUSDT")
    ap.add_argument("--tf", type=int, default=5)
    ap.add_argument("--window", type=int, default=60, help="모양을 볼 봉 수")
    ap.add_argument("--horizon", type=int, default=24, help="이후 몇 봉의 수익률")
    ap.add_argument("--k", type=int, default=50)
    ap.add_argument("--sim", type=float, default=0.0,
                    help="절대 유사도 하한. 240차원에서는 최고 이웃도 0.5 근처라\n"
                         "기본은 끄고 **상위 K개**로 뽑는다 (docs/06 의 0.85 는 도달 불가)")
    ap.add_argument("--thresh", type=float, default=0.0,
                    help="이웃 평균 수익률이 이 %% 넘을 때만 진입")
    ap.add_argument("--shuffles", type=int, default=20)
    ap.add_argument("--split", type=float, default=0.65)
    ap.add_argument("--cost", type=float, default=0.11, help="왕복 비용 %%")
    a = ap.parse_args()

    p = VISION / f"{a.symbol}_1m.csv"
    if not p.exists():
        print(f"❌ {p.name} 이 없다.")
        return 1
    bs = resample(load_bars(p, a.symbol), a.tf)
    print(f"{a.symbol} · {a.tf}분봉 {len(bs):,}개 · 모양 {a.window}봉({a.window * a.tf}분) "
          f"· 결과 {a.horizon}봉({a.horizon * a.tf}분) 뒤")

    V = build_features(bs, a.window)
    c = np.array([b[4] for b in bs], dtype=np.float64)
    fwd = np.full(len(bs), np.nan)
    fwd[:len(bs) - a.horizon] = (c[a.horizon:] / c[:len(bs) - a.horizon] - 1) * 100

    is_hi = int(len(V) * a.split)
    gap = int(288 / a.tf * 5)                    # 하루치 봉 — 시간 겹침 차단
    q_lo = is_hi + gap
    print(f"  이웃 검색 구간(IS): 벡터 0 ~ {is_hi:,}")
    print(f"  질의 구간(OOS):     벡터 {q_lo:,} ~ {len(V):,}   (사이 {gap}봉 공백)")
    print(f"  K={a.k} · 유사도 ≥ {a.sim} · 진입 임계 {a.thresh}%\n")

    base = fwd[q_lo + a.window - 1:]
    base = base[~np.isnan(base)]
    print(f"기준선 — OOS 에서 아무때나 {a.horizon * a.tf}분 보유: "
          f"평균 {base.mean():+.4f}% · 표준편차 {base.std():.3f}%")

    r, d, bar_ix = evaluate(V, fwd, is_hi, q_lo, a.window, a.k, a.sim, gap, a.thresh,
                            horizon=a.horizon)
    if len(r) < 30:
        print(f"❌ 진입 신호가 {len(r)}건뿐이다. --sim 을 낮추거나 --thresh 를 낮춰봐.")
        return 1
    net = r - a.cost
    se_naive = r.std() / len(r) ** 0.5
    # 겹치는 결과는 독립이 아니다. Bartlett 가중은 절단 지연에서 아래로 치우치므로
    # 지연을 horizon 의 2배로 잡는다 (24봉 겹침 합성 실험: 1배 −20%, 2배 −5%).
    se = overlap_se(r, bar_ix, 2 * a.horizon)
    print("\n실제 패턴 검색")
    print(f"  진입 {len(r)}건 · 롱 {int((d > 0).sum())} / 숏 {int((d < 0).sum())}")
    print(f"  비용 전 평균 {r.mean():+.4f}%  (겹침 보정 표준오차 ±{se:.4f}, t = {r.mean() / se:+.2f}"
          f" · 독립 가정이면 ±{se_naive:.4f})")
    print(f"  비용 후 평균 {net.mean():+.4f}%  · 승률 {(net > 0).mean() * 100:.1f}%")

    print(f"\n라벨 셔플 대조군 {a.shuffles}회 — 미래를 {2 * a.horizon}봉 덩어리째 섞고 똑같이 검색한다")
    print("  (진짜 패턴은 사라지고 '검색 때문에 생긴 착시'만 남는다 — 라벨의 겹침 구조는 유지)")
    sh = []
    for s in range(a.shuffles):
        rs, _, _ = evaluate(V, fwd, is_hi, q_lo, a.window, a.k, a.sim, gap, a.thresh,
                            rng=np.random.default_rng(1000 + s), horizon=a.horizon)
        if len(rs) >= 30:
            sh.append(rs.mean())
        print(f"\r  {s + 1}/{a.shuffles}", end="", flush=True)
    sh = np.array(sh)
    print(f"\r  셔플 {len(sh)}회 완료" + " " * 20)
    print(f"  셔플 평균 {sh.mean():+.4f}% · 표준편차 {sh.std():.4f}%")
    print(f"  셔플 최고 {sh.max():+.4f}% · 최저 {sh.min():+.4f}%")

    beat = int((sh >= r.mean()).sum())
    pval = (beat + 1) / (len(sh) + 1)
    print("\n판정")
    print(f"  실제 {r.mean():+.4f}%  vs  셔플 최고 {sh.max():+.4f}%")
    print(f"  셔플이 실제를 이긴 횟수 {beat}/{len(sh)} → 경험적 p = {pval:.3f}")
    if pval < 0.05 and r.mean() > a.cost:
        print("  ✅ 검색 착시로 설명되지 않고, 비용도 넘는다 — 더 파볼 가치 있음")
    elif pval < 0.05:
        print(f"  △ 착시는 아니지만 비용({a.cost}%)을 못 넘는다 — 우위가 너무 작다")
    else:
        print("  ❌ 셔플로 설명된다 — 패턴이 아니라 검색의 부산물이다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
