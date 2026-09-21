"""EMA99 터치 매매 엔진 v2 — **어느 쪽에서 닿았는가**로 방향을 정한다.

핵심 규칙 (사용자 설명 그대로)
  · 차트가 EMA99 **위에 있다가 내려와서** 터치  → EMA99 는 **지지** → **롱**
  · 차트가 EMA99 **아래에 있다가 올라와서** 터치 → EMA99 는 **저항** → **숏**
  · EMA99 기울기가 양수여도 아래에서 올라와 닿으면 그건 저항이다. 기울기는 방향을
    정하는 기준이 아니라 **거를지 말지를 정하는 필터**일 뿐이다.

  손절: EMA99 **바로 아래**(지지) / **바로 위**(저항), 버퍼 %
  익절: 고정 % 목표 (주가 1~2%)

규칙은 보수적으로.
  · 신호는 봉 종가로 판단, 진입은 **다음 봉 시가**
  · 같은 봉에서 손절·익절이 둘 다 닿으면 **손절**
  · 왕복 비용을 수익률에서 그대로 뺀다

⚠️ 안전장치: 손절가가 진입가의 **손실 쪽**에 있지 않으면 그 매매는 버린다.
   (v1 에서 이 검사가 없어서 '손절인데 이익' 이 나오는 버그가 있었다.)
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import UTC, datetime

# 왕복 비용 (%, 명목가 기준) — 추정이 아니라 **실측**으로 잡은 값이다.
#
#   수수료   테이커 0.05% × 2 = 0.10%   (BNB 할인 시 0.09%)
#            메이커 0.02% × 2 = 0.04%
#   스프레드 실측 중앙값 0.79bps → 왕복 0.008%
#            (dataset/book_ticker.csv 4,413건, 90%분위도 0.79bps 로 매우 안정적)
#   펀딩비   실측 평균 0.0172%/8h → 1.5시간 보유 기대 0.003%
#            (dataset/funding.csv 21건)
#
#   → 시장가 왕복      ≈ 0.11%   ← 기본값
#   → 지정가 진입/시장가 청산 ≈ 0.08%
#   → 지정가 양쪽      ≈ 0.05%   (단 미체결 위험)
#
# ⚠️ 레버리지와 무관하다. 수수료도 손익도 똑같이 명목가에 비례하므로
#    배율을 올려도 '비용 ÷ 손절폭' 비율(=R 기준 비용)은 변하지 않는다.
ROUND_TRIP = 0.11


# ── 데이터 ──────────────────────────────────────────────────────────────
def load_bars(path, symbol=None):
    out = []
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if symbol and r.get("symbol", symbol).upper() != symbol:
                continue
            out.append((
                datetime.fromisoformat(r["ts"]).astimezone(UTC),
                float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r["volume"]),
            ))
    out.sort()
    return out


def resample(bars, minutes):
    if minutes <= 1:
        return bars
    out, bucket, key = [], [], None
    for b in bars:
        k = int(b[0].timestamp()) // (minutes * 60)
        if key is None:
            key = k
        if k != key:
            if len(bucket) == minutes:
                out.append(_merge(bucket))
            bucket, key = [], k
        bucket.append(b)
    if len(bucket) == minutes:
        out.append(_merge(bucket))
    return out


def _merge(g):
    return (g[0][0], g[0][1], max(x[2] for x in g), min(x[3] for x in g),
            g[-1][4], sum(x[5] for x in g))


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(bs, period=14):
    n = len(bs)
    out = [None] * n
    if n < period + 1:
        return out
    trs = []
    for i in range(1, n):
        h, lo, pc = bs[i][2], bs[i][3], bs[i - 1][4]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    prev = sum(trs[:period]) / period
    out[period] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i + 1] = prev
    return out


def adx(bs, period=14):
    """Wilder ADX. 추세의 **세기**를 잰다 (방향이 아니라).

    ADX 낮음 = 방향 없이 왔다갔다 = 우리가 털리는 구간.
    """
    n = len(bs)
    out = [None] * n
    if n < period * 2 + 2:
        return out
    tr, pdm, ndm = [], [], []
    for i in range(1, n):
        h, lo, ph, pl, pc = bs[i][2], bs[i][3], bs[i - 1][2], bs[i - 1][3], bs[i - 1][4]
        tr.append(max(h - lo, abs(h - pc), abs(lo - pc)))
        up, dn = h - ph, pl - lo
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)

    def rma(xs):
        r = [None] * len(xs)
        prev = sum(xs[:period]) / period
        r[period - 1] = prev
        for i in range(period, len(xs)):
            prev = (prev * (period - 1) + xs[i]) / period
            r[i] = prev
        return r

    rtr, rp, rn = rma(tr), rma(pdm), rma(ndm)
    dx = [None] * len(tr)
    for i in range(len(tr)):
        if rtr[i] is None or rtr[i] == 0:
            continue
        p = 100 * rp[i] / rtr[i]
        m = 100 * rn[i] / rtr[i]
        dx[i] = 100 * abs(p - m) / (p + m) if (p + m) else 0.0
    vals = [x for x in dx if x is not None]
    if len(vals) < period:
        return out
    start = dx.index(vals[0])
    prev = sum(dx[start:start + period]) / period
    out[start + period] = prev
    for i in range(start + period, len(dx)):
        if dx[i] is None:
            continue
        prev = (prev * (period - 1) + dx[i]) / period
        out[i + 1] = prev
    return out


def efficiency_ratio(closes, n=20):
    """카우프만 효율성 비율. 직선으로 갔으면 1, 왔다갔다면 0 에 가깝다.

        ER = |구간 순변화| / 구간 내 절대변화 합
    """
    out = [None] * len(closes)
    for i in range(n, len(closes)):
        net = abs(closes[i] - closes[i - n])
        path = sum(abs(closes[j] - closes[j - 1]) for j in range(i - n + 1, i + 1))
        out[i] = (net / path) if path else 0.0
    return out


def cross_count(closes, line, n=20):
    """최근 n 봉 동안 종가가 그 선을 몇 번 넘나들었나. 많을수록 횡보."""
    out = [None] * len(closes)
    sign = [None] * len(closes)
    for i, c in enumerate(closes):
        if line[i] is not None:
            sign[i] = 1 if c > line[i] else -1
    for i in range(n, len(closes)):
        w = [s for s in sign[i - n + 1:i + 1] if s is not None]
        if len(w) < n // 2:
            continue
        out[i] = sum(1 for a, b in zip(w, w[1:]) if a != b)
    return out


# ── 설정 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Config:
    tf: int = 5

    # 어느 터치를 잡을 것인가
    take: str = "both"          # both | support (지지 롱만) | resistance (저항 숏만)

    # 접근 방향 판정
    approach_min: float = 0.40  # % — EMA99 에서 이만큼 떨어져 있던 적이 있어야 '위/아래'로 인정
    approach_lb: int = 40       # 봉 — 그걸 찾아볼 최대 거리

    # 터치 판정
    touch_tol: float = 0.05     # % — 이 이내로 접근하면 닿은 것
    confirm: bool = False       # True 면 터치 봉이 원래 방향으로 되돌아 마감해야

    # 필터 (방향은 이미 정해졌고, 걸러낼지만 결정한다)
    trend: str = "none"         # none | with (기울기 일치) | stack (EMA9/20 정렬까지)
    slope_lb: int = 6           # 봉 — 기울기 볼 거리

    # 손절 / 익절
    stop_buf: float = 0.20      # % — EMA99 에서 이만큼 바깥
    stop_mode: str = "line"     # line = EMA99 기준 | wick = 터치봉 꼬리 기준
    target: float = 1.50        # % — 고정 익절 목표 (주가 기준)
    max_bars: int = 0           # >0 이면 이 봉 수 뒤 시장가 강제청산

    reset: float = 0.40         # % — EMA99 에서 이만큼 멀어져야 다음 진입 허용

    # ── 횡보 차단 필터 ──────────────────────────────────────
    # EMA99 를 낀 채 왔다갔다 하는 구간에서 연달아 털리는 걸 막는다.
    filt: str = "none"          # none | adx | er | crosses | slope_atr | dist_atr
    filt_min: float | None = None   # 이 값 **이상**이어야 통과
    filt_max: float | None = None   # 이 값 **이하**여야 통과
    filt_lb: int = 20           # 필터 계산 구간 (봉)

    # ── 상위 시간프레임(일봉) 추세 게이트 ────────────────────
    # 5분봉 안에서 추세를 재려던 시도(ADX·ER·기울기)는 전부 실패했다.
    # 반면 일봉 모멘텀은 방향 대조군이 압도적으로 깨끗했다(뒤집으면 샤프 −1.3).
    # 그래서 '어느 쪽을 잡을지'는 일봉이 정하고, '언제 잡을지'만 5분봉이 정한다.
    regime_lb: int = 0          # >0 이면 최근 N일 수익률 부호로 방향을 제한
    regime_mode: str = "with"   # with = 일봉 방향만 / against = 반대만(대조군)

    # ── 손절 후 쿨다운 ──────────────────────────────────────
    cool_bars: int = 0          # 손절 직후 이 봉 수 동안은 진입 금지
    cool_trend: bool = False    # 손절 후에는 필터를 통과해야만 재진입 허용

    # ── 평가 방식 ───────────────────────────────────────────
    independent: bool = False   # True 면 포지션 점유를 무시하고 **모든 신호**를 각각 평가
                                # (대조군 비교용 — 원본과 뒤집기가 완전히 같은 신호를 쓴다)
    flip: bool = False          # 대조군 — 같은 자리에서 방향만 반대로


@dataclass(frozen=True, slots=True)
class Result:
    n: int = 0
    wins: int = 0
    win_rate: float = 0.0
    exp_pct: float = 0.0
    total_pct: float = 0.0
    exp_r: float = 0.0
    total_r: float = 0.0
    max_dd_r: float = 0.0
    avg_hold: float = 0.0
    avg_stop_pct: float = 0.0
    profit_factor: float | None = None
    by_exit: tuple = (0, 0, 0)   # (익절, 손절, 시간청산)
    by_kind: tuple = (0, 0)      # (지지롱 건수, 저항숏 건수)
    kind_exp: tuple = (0.0, 0.0) # (지지롱 기대R, 저항숏 기대R)
    skipped_bad_stop: int = 0    # 손절가가 잘못된 쪽이라 버린 신호 수
    blocked_n: int = 0           # 필터가 막은 신호 수
    blocked_exp_r: float = 0.0   # 막힌 신호들의 기대R — 필터가 값을 하는지 보는 눈금
    trades: tuple = ()


def flip_of(c: Config) -> Config:
    return replace(c, flip=True)


# ── 신호 생성 (포지션과 무관) ────────────────────────────────────────────
def _metric(bs, closes, e99, a, cfg):
    """필터로 쓸 값의 시계열. 값이 클수록 '추세', 작을수록 '횡보'가 되게 맞춘다."""
    n = len(bs)
    if cfg.filt == "none":
        return [0.0] * n
    if cfg.filt == "adx":
        return adx(bs, 14)
    if cfg.filt == "er":
        return efficiency_ratio(closes, cfg.filt_lb)
    if cfg.filt == "crosses":
        return cross_count(closes, e99, cfg.filt_lb)     # 클수록 횡보 → filt_max 로 쓴다
    if cfg.filt == "slope_atr":
        out = [None] * n
        for i in range(cfg.filt_lb, n):
            if e99[i] is None or e99[i - cfg.filt_lb] is None or not a[i]:
                continue
            out[i] = abs(e99[i] - e99[i - cfg.filt_lb]) / a[i]
        return out
    if cfg.filt == "dist_atr":
        out = [None] * n
        for i in range(n):
            if e99[i] is None or not a[i]:
                continue
            out[i] = abs(closes[i] - e99[i]) / a[i]
        return out
    raise ValueError(f"모르는 필터: {cfg.filt}")


def _passes(v, cfg) -> bool:
    if cfg.filt == "none":
        return True
    if v is None:
        return False
    if cfg.filt_min is not None and v < cfg.filt_min:
        return False
    if cfg.filt_max is not None and v > cfg.filt_max:
        return False
    return True


def _daily_regime(bs, lb):
    """봉마다 '그 시점에 알 수 있는' 일봉 모멘텀 부호를 붙인다.

    미래를 보지 않도록 **전날 종가까지만** 쓴다.
    각 봉 i 에서: (전날 종가 / lb일 전 종가 − 1) 의 부호.
    """
    day_close, order = {}, []
    for k, b in enumerate(bs):
        d = b[0].date()
        if d not in day_close:
            order.append(d)
        day_close[d] = (b[4], k)

    mom_of_day = {}
    for j, d in enumerate(order):
        if j < lb + 1:
            continue
        prev = day_close[order[j - 1]][0]        # 전날 종가
        base = day_close[order[j - 1 - lb]][0]   # 그보다 lb일 전
        mom_of_day[d] = 1 if prev > base else (-1 if prev < base else 0)

    out = [0] * len(bs)
    for k, b in enumerate(bs):
        out[k] = mom_of_day.get(b[0].date(), 0)
    return out


def _candidates(bs, e9, e20, e99, cfg):
    """조건을 만족하는 모든 터치 지점. 포지션 점유는 여기서 따지지 않는다."""
    closes = [b[4] for b in bs]
    a = atr(bs, 14)
    met = _metric(bs, closes, e99, a, cfg)
    reg = _daily_regime(bs, cfg.regime_lb) if cfg.regime_lb else None

    def approach_side(i: int) -> int:
        for j in range(i - 1, max(i - cfg.approach_lb, 0) - 1, -1):
            v = e99[j]
            if v is None:
                break
            d = (closes[j] - v) / v * 100
            if d >= cfg.approach_min:
                return 1
            if d <= -cfg.approach_min:
                return -1
        return 0

    out = []
    for i in range(110, len(bs) - 1):
        v99 = e99[i]
        if v99 is None:
            continue
        b = bs[i]
        side = approach_side(i)
        kind = None
        if side == 1 and b[3] <= v99 * (1 + cfg.touch_tol / 100):
            kind = "support"
        elif side == -1 and b[2] >= v99 * (1 - cfg.touch_tol / 100):
            kind = "resistance"
        if kind is None or (cfg.take != "both" and cfg.take != kind):
            continue
        long_side = kind == "support"

        if cfg.confirm and not ((b[4] > v99) if long_side else (b[4] < v99)):
            continue
        if cfg.trend in ("with", "stack"):
            prev = e99[i - cfg.slope_lb]
            if prev is None or long_side != (v99 > prev):
                continue
        if cfg.trend == "stack":
            if e9[i] is None or e20[i] is None:
                continue
            if not ((e9[i] > e20[i] > v99) if long_side else (e9[i] < e20[i] < v99)):
                continue

        if reg is not None:
            want = 1 if long_side else -1
            if cfg.regime_mode == "against":
                want = -want
            if reg[i] != want:
                continue

        out.append((i, long_side, kind, _passes(met[i], cfg), met[i]))
    return out


def _trade(bs, e99, i, long_side, cfg):
    """신호 봉 i 에서 다음 봉 시가에 들어가 청산까지 간다. (결과, 마지막 봉) 또는 None."""
    # 손절 거리는 **신호가 가리키는 원래 방향**으로 계산한다.
    # 대조군(flip)은 방향만 뒤집고 이 거리를 그대로 쓴다 — 그래야 같은 신호·같은 손절폭이
    # 되어 비용이 정확히 상쇄되고, 순수하게 방향만 비교된다.
    entry = bs[i + 1][1]
    base = e99[i]
    if cfg.stop_mode == "wick":
        base = min(base, bs[i][3]) if long_side else max(base, bs[i][2])
    ref_stop = base * (1 - cfg.stop_buf / 100) if long_side else base * (1 + cfg.stop_buf / 100)
    if (long_side and ref_stop >= entry) or (not long_side and ref_stop <= entry):
        return None, None                      # 손절이 이익 쪽 — 버린다
    dist = abs(entry - ref_stop)
    if entry <= 0 or dist / entry * 100 < 0.03:
        return None, None

    direction = long_side if not cfg.flip else (not long_side)
    sign = 1 if direction else -1
    stop_px = entry - sign * dist
    tp_px = entry * (1 + sign * cfg.target / 100)
    start = i + 1
    exited, j = None, start
    while j < len(bs):
        bar = bs[j]
        if (direction and bar[3] <= stop_px) or (not direction and bar[2] >= stop_px):
            exited = (stop_px, j, "sl"); break
        if (direction and bar[2] >= tp_px) or (not direction and bar[3] <= tp_px):
            exited = (tp_px, j, "tp"); break
        if cfg.max_bars and (j - start) >= cfg.max_bars:
            exited = (bar[4], j, "time"); break
        j += 1
    if exited is None:
        exited = (bs[-1][4], len(bs) - 1, "time")

    px, j, why = exited
    net_pct = sign * (px - entry) / entry * 100 - ROUND_TRIP
    stop_pct = dist / entry * 100
    return (net_pct / stop_pct, net_pct, stop_pct, (j - start) * cfg.tf, why), j


# ── 본체 ────────────────────────────────────────────────────────────────
def run(bars, cfg: Config) -> Result:
    bs = resample(bars, cfg.tf)
    if len(bs) < 300:
        return Result()
    closes = [b[4] for b in bs]
    e9, e20, e99 = ema(closes, 9), ema(closes, 20), ema(closes, 99)
    cands = _candidates(bs, e9, e20, e99, cfg)
    if not cands:
        return Result()

    rs, pcts, holds, stops = [], [], [], []
    kind_rs = {"support": [], "resistance": []}
    exits = {"tp": 0, "sl": 0, "time": 0}
    blocked = []
    bad_stop = 0

    if cfg.independent:
        # 모든 신호를 각각 독립적으로 평가한다.
        # 원본과 뒤집기가 **완전히 같은 신호·같은 손절폭**을 쓰므로 비용이 정확히 상쇄된다.
        for i, long_side, kind, ok, _v in cands:
            res, _ = _trade(bs, e99, i, long_side, cfg)
            if res is None:
                bad_stop += 1
                continue
            r, net, stop_pct, hold, why = res
            if not ok:
                blocked.append(r)
                continue
            rs.append(r); pcts.append(net); stops.append(stop_pct); holds.append(hold)
            exits[why] += 1; kind_rs[kind].append(r)
    else:
        # 실제 매매처럼 — 한 번에 한 포지션, 되돌아옴 방지, 손절 후 쿨다운
        # '다시 무장' 판정은 **모든 봉**을 봐야 한다. 후보 봉은 정의상 EMA99 에 붙어
        # 있으므로, 후보에서만 거리를 재면 영영 재무장이 안 된다.
        last_away = [-1] * len(bs)
        prev = -1
        for k in range(len(bs)):
            v = e99[k]
            if v and abs(closes[k] - v) / v * 100 >= cfg.reset:
                prev = k
            last_away[k] = prev

        free_at, last_exit, cool_until = 0, -1, 0
        for i, long_side, kind, ok, _v in cands:
            if i < free_at or i < cool_until:
                continue
            if last_exit >= 0 and last_away[i] <= last_exit:
                continue                     # 직전 청산 이후 EMA99 에서 떨어진 적이 없다
            if not ok:
                blocked.append(0.0)      # 자리표시 — 아래에서 실제 성적으로 채운다
                blocked[-1] = None
                res_b, _ = _trade(bs, e99, i, long_side, cfg)
                blocked[-1] = res_b[0] if res_b else None
                continue
            res, j = _trade(bs, e99, i, long_side, cfg)
            if res is None:
                bad_stop += 1
                continue
            r, net, stop_pct, hold, why = res
            rs.append(r); pcts.append(net); stops.append(stop_pct); holds.append(hold)
            exits[why] += 1; kind_rs[kind].append(r)
            free_at, last_exit = j + 1, j
            if why == "sl":
                cool_until = j + 1 + cfg.cool_bars
        blocked = [x for x in blocked if x is not None]

    if not rs:
        return Result(skipped_bad_stop=bad_stop,
                      blocked_n=len(blocked),
                      blocked_exp_r=(sum(blocked) / len(blocked)) if blocked else 0.0)

    wins = sum(1 for r in rs if r > 0)
    peak = cum = dd = 0.0
    for r in rs:
        cum += r; peak = max(peak, cum); dd = min(dd, cum - peak)
    gain = sum(r for r in rs if r > 0)
    loss = -sum(r for r in rs if r < 0)

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    return Result(
        n=len(rs), wins=wins, win_rate=wins / len(rs),
        exp_pct=mean(pcts), total_pct=sum(pcts),
        exp_r=mean(rs), total_r=sum(rs), max_dd_r=abs(dd),
        avg_hold=mean(holds), avg_stop_pct=mean(stops),
        profit_factor=(gain / loss) if loss > 0 else None,
        by_exit=(exits["tp"], exits["sl"], exits["time"]),
        by_kind=(len(kind_rs["support"]), len(kind_rs["resistance"])),
        kind_exp=(mean(kind_rs["support"]), mean(kind_rs["resistance"])),
        skipped_bad_stop=bad_stop,
        blocked_n=len(blocked), blocked_exp_r=mean(blocked),
        trades=tuple(rs),
    )
