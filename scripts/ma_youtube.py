#!/usr/bin/env python3
"""유튜브·블로그에서 "좋다"고 하는 이동평균 매매법 7가지 — 5분봉, 같은 잣대로 백테스트.

    python scripts/ma_youtube.py dataset/vision/SOXLUSDT_1m.csv [BTCUSDT_1m.csv ...]
    옵션  --since 2023-01   --cost 0.11   --funding 0.0172   --seed-pct 10 --lev 10

돈 규칙(사용자 지정): 시드의 10% 를 10배 → 노셔널 = 시드 1배. 손절폭 s% 면 매매당 위험 = 시드의 s%.
청산: 격리 10배 기준 진입가에서 −9.5% 역행하면 증거금(시드 10%) 소멸 → 그 매매 −10% 로 마감.

검증 규칙은 docs/11 그대로 — 신호는 봉 종가, 진입은 다음 봉 시가, 같은 봉에 손절·익절 둘 다면 손절,
왕복 비용 0.11%(실측), 방향 뒤집기 대조군(같은 자리·같은 손절폭·방향만 반대), IS(전반 60%)/OOS(후반 40%).
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics as st
from datetime import datetime

import numpy as np

MMR_LIQ = 9.5          # 격리 10배 청산 역행 % (증거금 10% − 유지증거금 0.5%)


# ─────────────────────────────────────────── 데이터
def load_1m(path: str, since: str | None):
    ts, o, h, l, c, v = [], [], [], [], [], []
    with open(path, newline="") as f:
        r = csv.reader(f)
        head = next(r)
        col = {n: i for i, n in enumerate(head)}
        it, io, ih, il, ic, iv = (col[k] for k in ("ts", "open", "high", "low", "close", "volume"))
        for row in r:
            t = row[it]
            if since and t[:7] < since:
                continue
            ts.append(t); o.append(float(row[io])); h.append(float(row[ih]))
            l.append(float(row[il])); c.append(float(row[ic])); v.append(float(row[iv]))
    return ts, np.array(o), np.array(h), np.array(l), np.array(c), np.array(v)


def to_5m(ts, o, h, l, c, v):
    """1분봉 → 5분봉 (시각 경계 :00 :05 …). 빈 분이 있어도 있는 봉만으로 묶는다."""
    epoch = np.array([int(datetime.fromisoformat(t).timestamp()) for t in ts])
    key = epoch // 300
    idx = np.flatnonzero(np.diff(key, prepend=key[0] - 1))       # 새 묶음 시작 위치
    ends = np.append(idx[1:], len(key))
    O = o[idx]; C = c[ends - 1]
    H = np.array([h[a:b].max() for a, b in zip(idx, ends)])
    L = np.array([l[a:b].min() for a, b in zip(idx, ends)])
    V = np.array([v[a:b].sum() for a, b in zip(idx, ends)])
    T = key[idx] * 300
    return T, O, H, L, C, V


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x); k = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = out[i - 1] + k * (x[i] - out[i - 1])
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full_like(x, np.nan)
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def atr(h, l, c, n=14):
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return ema(tr, n)


def rolling_min(x, n):
    out = np.empty_like(x)
    for i in range(len(x)):
        out[i] = x[max(0, i - n + 1):i + 1].min()
    return out


def rolling_max(x, n):
    out = np.empty_like(x)
    for i in range(len(x)):
        out[i] = x[max(0, i - n + 1):i + 1].max()
    return out


# ─────────────────────────────────────────── 전략 (신호 = i번째 봉 종가 기준, 진입 = i+1 시가)
# 각 함수는 (direction[+1/-1/0], stop_price, tp_mult, trail_kind) 배열을 돌려준다.
#   trail_kind: None = 고정 TP,  "x9_20" = 9/20 데드크로스에 청산,  "x5_8" …,  "c50" = 종가가 EMA50 아래

def strat_9_20_pullback(B, stop_kind="swing", tp=2.0, trail=None, filt200=True, rth_only=False, off_only=False):
    e9, e20, e200 = B["e9"], B["e20"], B["e200"]
    c, l, h = B["c"], B["l"], B["h"]
    swl, swh = B["swl10"], B["swh10"]
    n = len(c); d = np.zeros(n, int); sp = np.zeros(n); tpm = np.full(n, tp)
    up = (e9 > e20) & (e9 > np.roll(e9, 1)) & (e20 > np.roll(e20, 1))
    dn = (e9 < e20) & (e9 < np.roll(e9, 1)) & (e20 < np.roll(e20, 1))
    if filt200:
        up &= c > e200; dn &= c < e200
    # 눌림: 직전 3봉 안에 저가가 EMA20 에 닿았고, 이번 봉 종가가 EMA9 위로 복귀
    touched_dn = np.zeros(n, bool); touched_up = np.zeros(n, bool)
    for k in range(0, 3):
        touched_dn |= np.roll(l <= e20, k); touched_up |= np.roll(h >= e20, k)
    long_sig = up & touched_dn & (c > e9) & (np.roll(c, 1) <= np.roll(e9, 1))
    short_sig = dn & touched_up & (c < e9) & (np.roll(c, 1) >= np.roll(e9, 1))
    d[long_sig] = 1; d[short_sig] = -1
    if stop_kind == "swing":
        sp[long_sig] = swl[long_sig] * 0.999; sp[short_sig] = swh[short_sig] * 1.001
    elif stop_kind == "atr25":
        sp[long_sig] = c[long_sig] - 2.5 * B["atr"][long_sig]; sp[short_sig] = c[short_sig] + 2.5 * B["atr"][short_sig]
    else:  # ema99
        sp[long_sig] = B["e99"][long_sig] * 0.998; sp[short_sig] = B["e99"][short_sig] * 1.002
    if rth_only:
        hr = ((B["T"] // 3600) % 24)
        in_rth = (hr * 60 + (B["T"] // 60) % 60 >= 13 * 60 + 30) & (hr < 20)   # 13:30~20:00 UTC (EDT 본장)
        d[~in_rth] = 0
    if off_only:
        hr = ((B["T"] // 3600) % 24)
        in_rth = (hr * 60 + (B["T"] // 60) % 60 >= 13 * 60 + 30) & (hr < 20)
        d[in_rth] = 0
    return d, sp, tpm, trail


def strat_5_8_13(B, tp=2.0, trail="x5_8"):
    e5, e8, e13, e200 = B["e5"], B["e8"], B["e13"], B["e200"]
    c = B["c"]; n = len(c); d = np.zeros(n, int); sp = np.zeros(n); tpm = np.full(n, tp)
    cross_up = (e5 > e8) & (np.roll(e5, 1) <= np.roll(e8, 1)) & (e8 > e13) & (c > e200)
    cross_dn = (e5 < e8) & (np.roll(e5, 1) >= np.roll(e8, 1)) & (e8 < e13) & (c < e200)
    d[cross_up] = 1; d[cross_dn] = -1
    sp[cross_up] = B["swl10"][cross_up] * 0.999; sp[cross_dn] = B["swh10"][cross_dn] * 1.001
    return d, sp, tpm, trail


def strat_ripster(B, tp=2.0, trail="c50"):
    e5, e12, e34, e50, e72, e89 = (B[k] for k in ("e5", "e12", "e34", "e50", "e72", "e89"))
    c, l, h, a = B["c"], B["l"], B["h"], B["atr"]
    n = len(c); d = np.zeros(n, int); sp = np.zeros(n); tpm = np.full(n, tp)
    cloud_lo = np.minimum(e34, e50); cloud_hi = np.maximum(e34, e50)
    up = (c > cloud_hi) & (cloud_lo > np.roll(cloud_lo, 3)) & (np.maximum(e72, e89) < cloud_lo)
    dn = (c < cloud_lo) & (cloud_hi < np.roll(cloud_hi, 3)) & (np.minimum(e72, e89) > cloud_hi)
    touched_dn = np.zeros(n, bool); touched_up = np.zeros(n, bool)
    for k in range(0, 4):
        touched_dn |= np.roll(l <= cloud_hi, k); touched_up |= np.roll(h >= cloud_lo, k)
    trig_up = (c > np.maximum(e5, e12)) & (e5 > e12) & (np.roll(e5, 1) <= np.roll(e12, 1))
    trig_dn = (c < np.minimum(e5, e12)) & (e5 < e12) & (np.roll(e5, 1) >= np.roll(e12, 1))
    long_sig = up & touched_dn & trig_up; short_sig = dn & touched_up & trig_dn
    d[long_sig] = 1; d[short_sig] = -1
    sp[long_sig] = cloud_lo[long_sig] - 0.5 * a[long_sig]
    sp[short_sig] = cloud_hi[short_sig] + 0.5 * a[short_sig]
    return d, sp, tpm, trail


def strat_halmonic(B, tp=1.5, trail=None):
    """25/50/100 EMA '찍먹': 추세 밖에 있다가 25 또는 50 에 닿고 종가가 다시 위로. 손절 = EMA50."""
    e25, e50, e100 = B["e25"], B["e50"], B["e100"]
    c, l, h = B["c"], B["l"], B["h"]
    n = len(c); d = np.zeros(n, int); sp = np.zeros(n); tpm = np.full(n, tp)
    spread_up = (e25 > e50) & (e50 > e100) & ((e25 - e100) > np.roll(e25 - e100, 5))
    spread_dn = (e25 < e50) & (e50 < e100) & ((e100 - e25) > np.roll(e100 - e25, 5))
    long_sig = spread_up & (l <= e25) & (c > e25) & (c > np.roll(c, 1))
    short_sig = spread_dn & (h >= e25) & (c < e25) & (c < np.roll(c, 1))
    d[long_sig] = 1; d[short_sig] = -1
    sp[long_sig] = e50[long_sig] * 0.998; sp[short_sig] = e50[short_sig] * 1.002
    return d, sp, tpm, trail


def strat_video_200_20(B, mode="both", squeeze_pct=0.3, chop_max=6, body_mult=1.5, hard_stop_pct=3.0):
    """사용자가 링크한 영상(200·20 이평 두 개) 규칙:
      추세  = 종가가 EMA200 위 + EMA200 기울기 상승 (숏은 반대)
      횡보  = 최근 40봉 안에 종가가 EMA20 을 chop_max 번 이상 넘나들면 진입 금지
      스퀴즈 = 최근 10봉 안에 |EMA20−EMA200| 이 squeeze_pct% 이하로 좁아졌다가 종가가 둘 다 돌파
      눌림  = 추세 중 저가가 EMA20 에 닿되 EMA200 은 안 깨고, 종가가 EMA20 위로 복귀
      확정  = 몸통이 평균의 body_mult 배 이상이거나, 봉이 EMA20 에 안 닿음
      청산  = 종가가 EMA20 을 반대로 넘으면 (익절·손절 동일 규칙).  안전 손절 = 진입가 −hard_stop_pct%
    """
    e20, e200 = B["e20"], B["e200"]
    c, o, l, h = B["c"], B["o"], B["l"], B["h"]
    n = len(c); d = np.zeros(n, int); sp = np.zeros(n); tpm = np.full(n, 1e9)
    slope_up = e200 > np.roll(e200, 20); slope_dn = e200 < np.roll(e200, 20)
    up = (c > e200) & slope_up; dn = (c < e200) & slope_dn
    body = abs(c - o); avg_body = ema(body, 20)
    big = body >= body_mult * avg_body
    clear_up = l > e20; clear_dn = h < e20
    gap = abs(e20 - e200) / c * 100
    squeezed = np.zeros(n, bool)
    for k in range(1, 11):
        squeezed |= np.roll(gap <= squeeze_pct, k)
    above_both = (c > e20) & (c > e200) & ~((np.roll(c, 1) > np.roll(e20, 1)) & (np.roll(c, 1) > np.roll(e200, 1)))
    below_both = (c < e20) & (c < e200) & ~((np.roll(c, 1) < np.roll(e20, 1)) & (np.roll(c, 1) < np.roll(e200, 1)))
    sq_long = squeezed & above_both & (big | clear_up)
    sq_short = squeezed & below_both & (big | clear_dn)
    touched_dn = np.zeros(n, bool); touched_up = np.zeros(n, bool)
    for k in range(0, 3):
        touched_dn |= np.roll((l <= e20) & (l > e200), k); touched_up |= np.roll((h >= e20) & (h < e200), k)
    pb_long = up & touched_dn & (c > e20) & (np.roll(c, 1) <= np.roll(e20, 1)) & (big | clear_up)
    pb_short = dn & touched_up & (c < e20) & (np.roll(c, 1) >= np.roll(e20, 1)) & (big | clear_dn)
    # 횡보: 최근 40봉 안의 EMA20 교차 횟수
    cross = ((c > e20) != (np.roll(c, 1) > np.roll(e20, 1))).astype(int)
    cs = np.cumsum(cross); crosses40 = cs - np.roll(cs, 40); crosses40[:40] = 0
    chop = crosses40 >= chop_max
    long_sig = ((sq_long & up) | pb_long) & ~chop
    short_sig = ((sq_short & dn) | pb_short) & ~chop
    if mode == "pullback":
        long_sig, short_sig = pb_long & ~chop, pb_short & ~chop
    elif mode == "squeeze":
        long_sig, short_sig = sq_long & up & ~chop, sq_short & dn & ~chop
    d[long_sig] = 1; d[short_sig] = -1
    sp[long_sig] = c[long_sig] * (1 - hard_stop_pct / 100); sp[short_sig] = c[short_sig] * (1 + hard_stop_pct / 100)
    return d, sp, tpm, "c20"


def strat_flip(B, n_ema=94):
    """종가가 EMA(n) 을 넘으면 롱, 깨면 숏 — 항상 시장에 있다 (Astreka NQ 테스트 우승 설정).
    손절은 '반대 신호' 뿐이므로 stop 은 청산선(−9.5%)만, trail='flip'."""
    e = B[f"e{n_ema}"]; c = B["c"]
    n = len(c); d = np.zeros(n, int); sp = np.zeros(n); tpm = np.full(n, 1e9)
    up = (c > e) & (np.roll(c, 1) <= np.roll(e, 1)); dn = (c < e) & (np.roll(c, 1) >= np.roll(e, 1))
    d[up] = 1; d[dn] = -1
    sp[up] = c[up] * (1 - MMR_LIQ / 100); sp[dn] = c[dn] * (1 + MMR_LIQ / 100)
    return d, sp, tpm, f"flip{n_ema}"


# ─────────────────────────────────────────── 시뮬레이터
def simulate(B, sig, cost_pct, fund_8h_pct, flip=False, max_hold=288, tp_ratio_half=False):
    """한 번에 한 포지션. 결과: 매매별 (수익% on notional, R, 보유봉수, 종료사유, idx)."""
    d, sp, tpm, trail = sig
    o, h, l, c, T = B["o"], B["h"], B["l"], B["c"], B["T"]
    n = len(c); trades = []
    i = 1
    while i < n - 1:
        if d[i] == 0:
            i += 1; continue
        direction = d[i] if not flip else -d[i]
        entry = o[i + 1]
        stop_ref = sp[i]
        dist = abs(c[i] - stop_ref)                       # 손절 거리는 뒤집기 전 신호 기준 (비용 상쇄)
        if dist <= 0 or not np.isfinite(dist):
            i += 1; continue
        stop = entry - direction * dist
        stop_pct = dist / entry * 100
        if stop_pct > MMR_LIQ:                             # 손절보다 청산이 먼저
            stop = entry * (1 - direction * MMR_LIQ / 100); stop_pct = MMR_LIQ
        tp = entry + direction * dist * tpm[i] if tpm[i] < 1e8 else None
        exit_px = None; reason = None; j = i + 1
        trail_kind = trail
        resume = None                                       # 다음 신호를 볼 첫 봉 (그 봉 종가 신호 허용)
        for j in range(i + 1, min(n, i + 1 + max_hold)):
            # 갭 관통 (시가가 손절 너머) — 격리 청산선 −9.5% 를 넘으면 청산가로
            if direction == 1:
                gap_hit = o[j] <= stop
                if gap_hit:
                    exit_px = max(o[j], entry * (1 - MMR_LIQ / 100)); reason = "gap"; break
                if l[j] <= stop:
                    exit_px = stop; reason = "stop"; break
                if tp is not None and h[j] >= tp:
                    exit_px = tp; reason = "tp"; break
            else:
                gap_hit = o[j] >= stop
                if gap_hit:
                    exit_px = min(o[j], entry * (1 + MMR_LIQ / 100)); reason = "gap"; break
                if h[j] >= stop:
                    exit_px = stop; reason = "stop"; break
                if tp is not None and l[j] <= tp:
                    exit_px = tp; reason = "tp"; break
            # 추적 청산 (종가 기준, 다음 봉 시가 체결)
            if trail_kind and j + 1 < n:
                t = False
                if trail_kind == "x9_20":
                    t = (B["e9"][j] < B["e20"][j]) if direction == 1 else (B["e9"][j] > B["e20"][j])
                elif trail_kind == "x5_8":
                    t = (B["e5"][j] < B["e8"][j]) if direction == 1 else (B["e5"][j] > B["e8"][j])
                elif trail_kind == "c50":
                    t = (c[j] < B["e50"][j]) if direction == 1 else (c[j] > B["e50"][j])
                elif trail_kind == "c20":
                    t = (c[j] < B["e20"][j]) if direction == 1 else (c[j] > B["e20"][j])
                elif trail_kind.startswith("flip"):
                    e = B["e" + trail_kind[4:]]
                    t = (c[j] < e[j]) if direction == 1 else (c[j] > e[j])
                if t:
                    exit_px = o[j + 1]; reason = "trail"; resume = j; j = j + 1; break
        if exit_px is None:
            exit_px = c[j]; reason = "time"
        if resume is None:
            resume = j                                      # 손절·익절 봉의 종가 신호는 바로 쓴다 (다음 봉 시가 진입)
        held = j - i
        gross = direction * (exit_px - entry) / entry * 100
        fund = fund_8h_pct * (held * 5 / 480) * (1 if direction == 1 else -1)
        net = gross - cost_pct - fund
        trades.append((net, net / stop_pct, held, reason, i, T[i], gross / stop_pct, stop_pct))
        i = max(resume, i + 1)
    return trades


def account_curve(trades, seed_pct=10.0, lev=10.0):
    """노셔널 = 시드 × seed_pct% × lev.  net% 는 노셔널 기준 → 시드 수익 = net% × seed_pct/100 × lev."""
    eq = 1.0; peak = 1.0; mdd = 0.0; curve = []
    mult = seed_pct / 100 * lev
    for net, *_ in trades:
        eq *= 1 + net / 100 * mult
        if eq <= 0:
            eq = 0.0; curve.append(eq); mdd = 1.0; break
        peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak); curve.append(eq)
    return eq, mdd


def summarize(trades, days):
    if not trades:
        return dict(n=0)
    rs = [t[1] for t in trades]; [t[0] for t in trades]
    wins = [r for r in rs if r > 0]; losses = [r for r in rs if r <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf")
    eq, mdd = account_curve(trades)
    se = st.stdev(rs) / math.sqrt(len(rs)) if len(rs) > 2 else float("nan")
    return dict(n=len(trades), win=len(wins) / len(rs), exp_r=st.mean(rs), se=se,
                gross_r=st.mean(t[6] for t in trades), stop_pct=st.mean(t[7] for t in trades),
                t=(st.mean(rs) / se if se and se > 0 else float("nan")), pf=pf,
                eq=eq, mdd=mdd, per_day=len(trades) / max(days, 1),
                avg_hold=st.mean(t[2] for t in trades) * 5,
                reasons={k: sum(1 for t in trades if t[3] == k) for k in ("tp", "stop", "trail", "time", "gap")})


def fmt(s):
    if s.get("n", 0) == 0:
        return "매매 0건"
    return (f"n={s['n']:5d} 승률 {s['win']*100:5.1f}% 기대 {s['exp_r']:+.3f}R (비용전 {s['gross_r']:+.3f}R, 손절폭 {s['stop_pct']:.2f}%) ±{s['se']:.3f} t={s['t']:+5.2f} "
            f"PF {s['pf']:.2f} 시드 {s['eq']:.3f}x MDD {s['mdd']*100:5.1f}% 보유 {s['avg_hold']:.0f}분 "
            f"| tp {s['reasons']['tp']} stop {s['reasons']['stop']} trail {s['reasons']['trail']} gap {s['reasons']['gap']}")


def _off_only(sig, B):
    d, sp, tpm, trail = sig
    hr = ((B["T"] // 3600) % 24)
    in_rth = (hr * 60 + (B["T"] // 60) % 60 >= 13 * 60 + 30) & (hr < 20)
    d = d.copy(); d[in_rth] = 0
    return d, sp, tpm, trail


STRATS = {
    "A1 9/20 눌림 · 스윙손절 · 2R":            lambda B: strat_9_20_pullback(B, "swing", 2.0, None),
    "A2 9/20 눌림 · 스윙손절 · 9/20크로스 청산": lambda B: strat_9_20_pullback(B, "swing", 1e9, "x9_20"),
    "A3 9/20 눌림 · EMA99손절 · 3R (docs/09형)": lambda B: strat_9_20_pullback(B, "ema99", 3.0, None),
    "A4 9/20 눌림 · ATR×2.5손절 · 3R":          lambda B: strat_9_20_pullback(B, "atr25", 3.0, None),
    "A5 9/20 눌림 · EMA99손절 · 2R":            lambda B: strat_9_20_pullback(B, "ema99", 2.0, None),
    "A6 9/20 눌림 · EMA99손절 · 3R · 200필터없음": lambda B: strat_9_20_pullback(B, "ema99", 3.0, None, filt200=False),
    "A7 9/20 눌림 · EMA99손절 · 3R · 본장만":    lambda B: strat_9_20_pullback(B, "ema99", 3.0, None, rth_only=True),
    "A8 9/20 눌림 · EMA99손절 · 3R · 본장 밖만":  lambda B: strat_9_20_pullback(B, "ema99", 3.0, None, off_only=True),
    "A9 9/20 눌림 · ATR×2.5손절 · 3R · 본장 밖만": lambda B: strat_9_20_pullback(B, "atr25", 3.0, None, off_only=True),
    "Y1 영상 200/20 · 눌림+스퀴즈 · 20선 청산":   lambda B: strat_video_200_20(B, "both"),
    "Y2 영상 200/20 · 눌림만":                   lambda B: strat_video_200_20(B, "pullback"),
    "Y3 영상 200/20 · 스퀴즈만":                 lambda B: strat_video_200_20(B, "squeeze"),
    "Y4 영상 200/20 · 눌림+스퀴즈 · 본장 밖만":   lambda B: _off_only(strat_video_200_20(B, "both"), B),
    "B  5/8/13 리본 크로스 · 2R/5-8청산":       lambda B: strat_5_8_13(B, 2.0, "x5_8"),
    "C  Ripster 34/50 구름 눌림 · 2R/50청산":   lambda B: strat_ripster(B, 2.0, "c50"),
    "D  Hal_monic 25/50/100 찍먹 · 1.5R":        lambda B: strat_halmonic(B, 1.5, None),
    "E1 EMA94 종가 플립 (항상 보유)":            lambda B: strat_flip(B, 94),
    "E2 EMA200 종가 플립 (항상 보유)":           lambda B: strat_flip(B, 200),
    "G  MA60 종가 플립 (코인픽 60이평)":         lambda B: strat_flip(B, 60),
}


def build(T, o, h, l, c, v):
    B = dict(T=T, o=o, h=h, l=l, c=c, v=v, atr=atr(h, l, c, 14),
             swl10=rolling_min(l, 10), swh10=rolling_max(h, 10))
    for n_ in (5, 8, 9, 12, 13, 20, 25, 34, 50, 60, 72, 89, 94, 99, 100, 200):
        B[f"e{n_}"] = ema(c, n_)
    return B


def run_symbol(path, args):
    sym = path.split("/")[-1].split("_")[0]
    ts, o, h, l, c, v = load_1m(path, args.since)
    T, O, H, L, C, V = to_5m(ts, o, h, l, c, v)
    days = (T[-1] - T[0]) / 86400
    print(f"\n{'='*100}\n{sym}  5분봉 {len(C):,}개  {datetime.utcfromtimestamp(T[0]):%Y-%m-%d} ~ {datetime.utcfromtimestamp(T[-1]):%Y-%m-%d} ({days:.0f}일)"
          f"  매수보유 {C[-1]/C[0]:.2f}x")
    B = build(T, O, H, L, C, V)
    fund = args.funding if sym.startswith("SOXL") else args.funding_crypto
    cut = int(len(C) * 0.6)
    results = {}
    only = getattr(args, "only", None)
    for name, fn in STRATS.items():
        if only and not any(name.startswith(p) for p in only):
            continue
        sig = fn(B)
        tr = simulate(B, sig, args.cost, fund)
        fl = simulate(B, sig, args.cost, fund, flip=True)
        is_tr = [t for t in tr if t[4] < cut]; oos_tr = [t for t in tr if t[4] >= cut]
        s, sf = summarize(tr, days), summarize(fl, days)
        si, so = summarize(is_tr, days * 0.6), summarize(oos_tr, days * 0.4)
        results[name] = (s, sf, si, so)
        print(f"\n{name}")
        print(f"   전체   {fmt(s)}")
        print(f"   뒤집기 {fmt(sf)}")
        print(f"   IS     {fmt(si)}")
        print(f"   OOS    {fmt(so)}")
    return sym, results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--since", default=None, help="YYYY-MM (BTC 처럼 긴 데이터를 자를 때)")
    ap.add_argument("--cost", type=float, default=0.11, help="왕복 비용 % (실측 0.11)")
    ap.add_argument("--funding", type=float, default=0.0172, help="SOXL 펀딩 %/8h (실측)")
    ap.add_argument("--funding-crypto", type=float, default=0.01, help="BTC/ETH 펀딩 %/8h")
    ap.add_argument("--only", default=None, help="접두어로 전략 고르기 (예: Y,A8)")
    args = ap.parse_args()
    args.only = [p.strip() for p in args.only.split(",")] if args.only else None
    print(f"돈 규칙: 시드 10% × 10배 = 노셔널 1배 · 왕복비용 {args.cost}% · 청산선 −{MMR_LIQ}%")
    for p in args.paths:
        run_symbol(p, args)


if __name__ == "__main__":
    main()
