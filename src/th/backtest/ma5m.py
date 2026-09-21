"""5분봉 **200/20 이평 전략** — 백테스트와 실시간 시그널이 **같은 함수**를 쓴다 (docs/16 · docs/15 10절).

사용자가 링크한 영상의 규칙 여섯 가지를 그대로 옮겼다.

  ① 추세   = 종가가 EMA200 위 + EMA200 기울기 상승 (숏은 반대)
  ② 스퀴즈 = 최근 10봉 안에 |EMA20−EMA200| 이 squeeze_pct% 이하로 좁아졌다가 종가가 둘 다 돌파
  ③ 눌림   = 추세 중 저가가 EMA20 에 닿되 EMA200 은 안 깨고, 종가가 EMA20 위로 복귀
  ④ 횡보   = 최근 40봉 안에 종가가 EMA20 을 chop_max 번 이상 넘나들면 진입 금지
  ⑤ 확정   = 몸통이 평균(EMA20)의 body_mult 배 이상이거나, 봉이 EMA20 에 안 닿음
  ⑥ 청산   = 종가가 EMA20 을 반대로 넘으면 (익절·손절 같은 규칙). 안전 손절 = 진입가 ∓ hard_stop_pct%

검증 규칙은 docs/11 그대로 — 신호는 봉 **종가**, 진입은 **다음 봉 시가**, 같은 봉에 손절·익절이면
손절, 왕복 비용 0.11%(실측), 방향 뒤집기 대조군, IS(전반 60%)/OOS(후반 40%).

`scripts/ma_youtube.py` 의 `strat_video_200_20` + `simulate` 를 패키지로 옮긴 것이다. 스크립트와
이 모듈이 같은 데이터에서 같은 매매를 내는지 `tests/test_ma5m.py` 가 못 박는다.

⚠️ 이 모듈은 DB·네트워크를 모른다. numpy 만 쓴다. 실시간 엔진(`th.execution.signals`)은
여기의 `latest_state()` 를 부르고, 웹 백테스트(`/api/backtests`)는 `run_backtest()` 를 부른다.
"""

from __future__ import annotations

import math
import statistics as st
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

SETUP_ID = "video200_20"
MMR_LIQ = 9.5            # 격리 10배 청산 역행 % (증거금 10% − 유지증거금 0.5%)
BAR_S = 300              # 5분
WARMUP_BARS = 260        # EMA200 + 기울기 20봉 + 횡보창 40봉 — 이보다 짧으면 신호를 믿지 않는다

MODES = ("both", "pullback", "squeeze")
SESSIONS = ("all", "off_rth", "rth", "pre_post")


# ─────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Params:
    """전략 매개변수. 웹 폼·환경변수·테스트가 같은 형태로 만든다."""

    mode: str = "both"               # both | pullback | squeeze
    session: str = "all"             # all | off_rth | rth | pre_post  (미국 동부 기준)
    squeeze_pct: float = 0.3
    chop_max: int = 6
    body_mult: float = 1.5
    hard_stop_pct: float = 3.0
    max_hold_bars: int = 288         # 24시간
    cost_pct: float = 0.11           # 왕복 비용 (실측)
    funding_8h_pct: float = 0.0172   # SOXL 실측. 크립토는 0.01
    seed_pct: float = 10.0           # 시드의 10% 를
    leverage: float = 10.0           # 10배 → 노셔널 = 시드 1배

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Params:
        """웹 폼(JSON)에서. 모르는 키는 무시하고, 빈 값은 기본값으로 둔다."""
        d = d or {}
        kw: dict[str, Any] = {}
        for name, default in _DEFAULTS.items():
            v = d.get(name)
            if v is None or v == "":
                continue
            kw[name] = str(v) if isinstance(default, str) else type(default)(v)
        return cls(**kw).normalized()

    def normalized(self) -> Params:
        mode = str(self.mode).lower() if self.mode in MODES else "both"
        session = str(self.session).lower() if self.session in SESSIONS else "all"
        return Params(
            mode=mode, session=session,
            squeeze_pct=float(self.squeeze_pct), chop_max=int(self.chop_max),
            body_mult=float(self.body_mult), hard_stop_pct=float(self.hard_stop_pct),
            max_hold_bars=int(self.max_hold_bars), cost_pct=float(self.cost_pct),
            funding_8h_pct=float(self.funding_8h_pct), seed_pct=float(self.seed_pct),
            leverage=float(self.leverage),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def label_ko(self) -> str:
        m = {"both": "눌림+스퀴즈", "pullback": "눌림만", "squeeze": "스퀴즈만"}[self.mode]
        s = {"all": "24시간", "off_rth": "본장 밖", "rth": "본장만", "pre_post": "프리·애프터만"}[self.session]
        return f"영상 200/20 · {m} · {s} · 20선 청산 · 안전손절 {self.hard_stop_pct:g}%"


_DEFAULTS = {f.name: f.default for f in Params.__dataclass_fields__.values()}  # type: ignore[attr-defined]
CRYPTO = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"})


def default_funding(symbol: str) -> float:
    """펀딩 기본값 — 크립토 0.01%/8h, 주식 perp 0.0172%/8h (SOXL 실측)."""
    return 0.01 if symbol.upper() in CRYPTO else 0.0172


# ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class Bars:
    """5분봉 배열. T 는 봉 시작 시각(epoch 초, UTC)."""

    T: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray

    def __len__(self) -> int:
        return int(len(self.c))

    @property
    def days(self) -> float:
        return float(self.T[-1] - self.T[0]) / 86400 if len(self.c) > 1 else 0.0

    @classmethod
    def from_rows(cls, rows) -> Bars:
        """(epoch_s, o, h, l, c, v) 튜플 목록 → Bars. 이미 5분봉이어야 한다."""
        a = np.array(list(rows), dtype=float)
        if a.size == 0:
            return cls(*(np.zeros(0) for _ in range(6)))
        return cls(a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5])

    @classmethod
    def from_bars(cls, bars, tf_min: int = 1) -> Bars:
        """`Bar`/`Kline` 객체 목록(ts·open·high·low·close·volume). 1분봉이면 5분으로 묶는다."""
        if not bars:
            return cls(*(np.zeros(0) for _ in range(6)))
        T = np.array([int(b.ts.timestamp()) for b in bars], dtype=np.int64)
        o = np.array([b.open for b in bars], float)
        h = np.array([b.high for b in bars], float)
        l = np.array([b.low for b in bars], float)  # noqa: E741
        c = np.array([b.close for b in bars], float)
        v = np.array([b.volume for b in bars], float)
        if tf_min >= 5:
            return cls(T, o, h, l, c, v)
        return resample_5m(T, o, h, l, c, v)


def resample_5m(T, o, h, lo, c, v) -> Bars:
    """1분봉 → 5분봉 (:00 :05 … 경계). 빈 분이 있어도 있는 봉만으로 묶는다 — 스크립트와 같은 규칙."""
    key = np.asarray(T, dtype=np.int64) // BAR_S
    idx = np.flatnonzero(np.diff(key, prepend=key[0] - 1))
    ends = np.append(idx[1:], len(key))
    op = o[idx]
    C = c[ends - 1]
    H = np.array([h[a:b].max() for a, b in zip(idx, ends, strict=False)])
    L = np.array([lo[a:b].min() for a, b in zip(idx, ends, strict=False)])
    V = np.array([v[a:b].sum() for a, b in zip(idx, ends, strict=False)])
    return Bars(key[idx] * BAR_S, op, H, L, C, V)


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x, dtype=float)
    if len(x) == 0:
        return out
    k = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = out[i - 1] + k * (x[i] - out[i - 1])
    return out


# ─────────────────────────────────────────────────────────
# 세션 마스크 — 미국 동부 기준. 서머타임을 정확히 따르려면 달력이 필요하지만, 백테스트는
# 스크립트와 같은 근사(13:30~20:00 UTC = EDT 본장)를 쓴다. 실시간 엔진은 세션 게이트(docs/05)가 막는다.
def session_mask(T: np.ndarray, session: str) -> np.ndarray:
    """True = 이 봉의 신호를 **버린다**."""
    if session == "all" or len(T) == 0:
        return np.zeros(len(T), bool)
    minute = (T // 60) % 1440
    in_rth = (minute >= 13 * 60 + 30) & (minute < 20 * 60)
    in_pre = (minute >= 8 * 60) & (minute < 13 * 60 + 30)
    in_post = (minute >= 20 * 60) & (minute < 24 * 60)
    weekday = ((T // 86400) + 3) % 7          # 1970-01-01 은 목요일(3) → 0=월 … 6=일
    weekend = weekday >= 5
    if session == "rth":
        return ~(in_rth & ~weekend)
    if session == "off_rth":
        return in_rth & ~weekend
    if session == "pre_post":
        return ~((in_pre | in_post) & ~weekend)
    return np.zeros(len(T), bool)


# ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class Signals:
    d: np.ndarray            # +1 롱 / −1 숏 / 0
    stop: np.ndarray         # 안전 손절가 (신호 봉 종가 기준)
    kind: np.ndarray         # 0 없음 / 1 눌림 / 2 스퀴즈 / 3 둘 다
    e20: np.ndarray
    e200: np.ndarray
    chop: np.ndarray
    squeezed: np.ndarray
    trend: np.ndarray        # +1 상승추세 / −1 하락추세 / 0


def compute_signals(B: Bars, p: Params) -> Signals:
    """스크립트 `strat_video_200_20` 의 벡터 계산 그대로. 인덱스 i 의 신호는 i 번째 봉 **종가**에서 난다."""
    c, o, lo, h = B.c, B.o, B.l, B.h
    n = len(c)
    e20, e200 = ema(c, 20), ema(c, 200)
    d = np.zeros(n, int)
    sp = np.zeros(n)
    kind = np.zeros(n, int)
    if n < 45:
        return Signals(d, sp, kind, e20, e200, np.zeros(n, bool), np.zeros(n, bool), np.zeros(n, int))
    slope_up = e200 > np.roll(e200, 20)
    slope_dn = e200 < np.roll(e200, 20)
    up = (c > e200) & slope_up
    dn = (c < e200) & slope_dn
    body = abs(c - o)
    avg_body = ema(body, 20)
    big = body >= p.body_mult * avg_body
    clear_up = lo > e20
    clear_dn = h < e20
    gap = abs(e20 - e200) / c * 100
    squeezed = np.zeros(n, bool)
    for k in range(1, 11):
        squeezed |= np.roll(gap <= p.squeeze_pct, k)
    prev_c, prev_e20, prev_e200 = np.roll(c, 1), np.roll(e20, 1), np.roll(e200, 1)
    above_both = (c > e20) & (c > e200) & ~((prev_c > prev_e20) & (prev_c > prev_e200))
    below_both = (c < e20) & (c < e200) & ~((prev_c < prev_e20) & (prev_c < prev_e200))
    sq_long = squeezed & above_both & (big | clear_up)
    sq_short = squeezed & below_both & (big | clear_dn)
    touched_dn = np.zeros(n, bool)
    touched_up = np.zeros(n, bool)
    for k in range(0, 3):
        touched_dn |= np.roll((lo <= e20) & (lo > e200), k)
        touched_up |= np.roll((h >= e20) & (h < e200), k)
    pb_long = up & touched_dn & (c > e20) & (prev_c <= prev_e20) & (big | clear_up)
    pb_short = dn & touched_up & (c < e20) & (prev_c >= prev_e20) & (big | clear_dn)
    cross = ((c > e20) != (prev_c > prev_e20)).astype(int)
    cs = np.cumsum(cross)
    crosses40 = cs - np.roll(cs, 40)
    crosses40[:40] = 0
    chop = crosses40 >= p.chop_max

    sq_l, sq_s = sq_long & up & ~chop, sq_short & dn & ~chop
    pb_l, pb_s = pb_long & ~chop, pb_short & ~chop
    if p.mode == "pullback":
        long_sig, short_sig = pb_l, pb_s
    elif p.mode == "squeeze":
        long_sig, short_sig = sq_l, sq_s
    else:
        long_sig, short_sig = sq_l | pb_l, sq_s | pb_s
    skip = session_mask(B.T, p.session)
    long_sig &= ~skip
    short_sig &= ~skip
    d[long_sig] = 1
    d[short_sig] = -1
    kind[pb_l | pb_s] = 1
    kind[sq_l | sq_s] += 2
    kind[d == 0] = 0
    sp[long_sig] = c[long_sig] * (1 - p.hard_stop_pct / 100)
    sp[short_sig] = c[short_sig] * (1 + p.hard_stop_pct / 100)
    trend = np.where(up, 1, np.where(dn, -1, 0))
    return Signals(d, sp, kind, e20, e200, chop, squeezed, trend)


def exit_hit(direction: int, close: float, e20: float) -> bool:
    """⑥ 청산 규칙 — 종가 몸통이 EMA20 을 반대로 넘었나."""
    return (close < e20) if direction > 0 else (close > e20)


# ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class Trade:
    i: int                   # 신호 봉
    j: int                   # 청산 봉
    entry_ts: int
    exit_ts: int
    direction: int
    kind: int
    entry: float
    exit: float
    stop_pct: float
    net_pct: float           # 노셔널 기준 순수익 %
    gross_pct: float
    r: float
    gross_r: float
    held: int                # 봉 수
    reason: str              # tp | stop | trail | time | gap

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["entry_at"] = datetime.fromtimestamp(self.entry_ts, UTC).isoformat()
        d["exit_at"] = datetime.fromtimestamp(self.exit_ts, UTC).isoformat()
        d["held_min"] = self.held * 5
        d["kind_ko"] = KIND_KO.get(self.kind, "")
        return d


KIND_KO = {1: "눌림", 2: "스퀴즈", 3: "눌림+스퀴즈"}
REASON_KO = {"trail": "20선 청산", "stop": "안전손절", "gap": "갭 관통", "time": "시간 만료", "tp": "익절"}


def simulate(B: Bars, S: Signals, p: Params, flip: bool = False) -> list[Trade]:
    """한 번에 한 포지션. 스크립트 `simulate(trail="c20")` 와 같은 규칙.

    · 진입 = 신호 다음 봉 시가.  손절 거리는 뒤집기 전 신호 기준(비용 상쇄용).
    · 갭 관통(시가가 손절 너머)은 시가 체결, 단 격리 청산선 −9.5% 를 넘으면 청산가.
    · 추적 청산(종가가 EMA20 반대로) → 다음 봉 시가 체결. 그 봉의 종가 신호는 바로 쓴다.
    """
    d, sp, e20 = S.d, S.stop, S.e20
    o, h, lo, c, T = B.o, B.h, B.l, B.c, B.T
    n = len(c)
    trades: list[Trade] = []
    i = 1
    while i < n - 1:
        if d[i] == 0:
            i += 1
            continue
        direction = int(d[i]) if not flip else -int(d[i])
        entry = float(o[i + 1])
        dist = abs(float(c[i]) - float(sp[i]))
        if dist <= 0 or not math.isfinite(dist):
            i += 1
            continue
        stop = entry - direction * dist
        stop_pct = dist / entry * 100
        if stop_pct > MMR_LIQ:
            stop = entry * (1 - direction * MMR_LIQ / 100)
            stop_pct = MMR_LIQ
        exit_px = None
        reason = "time"
        resume = None
        j = i + 1
        for j in range(i + 1, min(n, i + 1 + p.max_hold_bars)):
            if direction == 1:
                if o[j] <= stop:
                    exit_px, reason = max(float(o[j]), entry * (1 - MMR_LIQ / 100)), "gap"
                    break
                if lo[j] <= stop:
                    exit_px, reason = stop, "stop"
                    break
            else:
                if o[j] >= stop:
                    exit_px, reason = min(float(o[j]), entry * (1 + MMR_LIQ / 100)), "gap"
                    break
                if h[j] >= stop:
                    exit_px, reason = stop, "stop"
                    break
            if j + 1 < n and exit_hit(direction, float(c[j]), float(e20[j])):
                exit_px, reason, resume = float(o[j + 1]), "trail", j
                j = j + 1
                break
        if exit_px is None:
            exit_px = float(c[j])
            reason = "time"
        if resume is None:
            resume = j
        held = j - i
        gross = direction * (exit_px - entry) / entry * 100
        fund = p.funding_8h_pct * (held * 5 / 480) * (1 if direction == 1 else -1)
        net = gross - p.cost_pct - fund
        trades.append(Trade(
            i=int(i), j=int(j), entry_ts=int(T[i + 1]), exit_ts=int(T[j]),
            direction=direction, kind=int(S.kind[i]), entry=entry, exit=float(exit_px),
            stop_pct=float(stop_pct), net_pct=float(net), gross_pct=float(gross),
            r=float(net / stop_pct), gross_r=float(gross / stop_pct), held=int(held), reason=reason,
        ))
        i = max(resume, i + 1)
    return trades


# ─────────────────────────────────────────────────────────
def account_curve(trades: list[Trade], p: Params) -> tuple[float, float, list[float]]:
    """노셔널 = 시드 × seed_pct% × 배수. 시드 배수, MDD, 매매별 시드 곡선."""
    eq = peak = 1.0
    mdd = 0.0
    curve: list[float] = []
    mult = p.seed_pct / 100 * p.leverage
    for t in trades:
        eq *= 1 + t.net_pct / 100 * mult
        if eq <= 0:
            eq = 0.0
            curve.append(eq)
            mdd = 1.0
            break
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
        curve.append(eq)
    return eq, mdd, curve


def summarize(trades: list[Trade], days: float, p: Params) -> dict[str, Any]:
    if not trades:
        return {"n": 0}
    rs = [t.r for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
    eq, mdd, _ = account_curve(trades, p)
    se = st.stdev(rs) / math.sqrt(len(rs)) if len(rs) > 2 else None
    cum = 0.0
    peak_r = 0.0
    mdd_r = 0.0
    for r in rs:
        cum += r
        peak_r = max(peak_r, cum)
        mdd_r = max(mdd_r, peak_r - cum)
    return {
        "n": len(trades),
        "win": len(wins) / len(rs),
        "exp_r": st.mean(rs),
        "sum_r": sum(rs),
        "se": se,
        "t": (st.mean(rs) / se) if se else None,
        "gross_r": st.mean(t.gross_r for t in trades),
        "stop_pct": st.mean(t.stop_pct for t in trades),
        "pf": pf,
        "eq": eq,
        "mdd": mdd,
        "mdd_r": mdd_r,
        "per_day": len(trades) / max(days, 1e-9),
        "avg_hold_min": st.mean(t.held for t in trades) * 5,
        "avg_win_r": st.mean(wins) if wins else None,
        "avg_loss_r": st.mean(losses) if losses else None,
        "reasons": {k: sum(1 for t in trades if t.reason == k) for k in ("tp", "stop", "trail", "time", "gap")},
        "longs": sum(1 for t in trades if t.direction > 0),
        "shorts": sum(1 for t in trades if t.direction < 0),
    }


def monthly(trades: list[Trade]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        m = datetime.fromtimestamp(t.entry_ts, UTC).strftime("%Y-%m")
        row = out.setdefault(m, {"month": m, "n": 0, "sum_r": 0.0, "wins": 0, "net_pct": 0.0})
        row["n"] += 1
        row["sum_r"] += t.r
        row["wins"] += 1 if t.r > 0 else 0
        row["net_pct"] += t.net_pct
    return [out[k] for k in sorted(out)]


def run_backtest(B: Bars, p: Params, *, max_trades_out: int = 3000) -> dict[str, Any]:
    """전체 · 뒤집기 대조군 · IS/OOS · 월별 · 시드 곡선 · 매매 목록 — JSON 으로 바로 나간다."""
    p = p.normalized()
    n = len(B)
    if n < WARMUP_BARS + 10:
        return {"ok": False, "error_ko": f"5분봉이 {n}개뿐입니다 — 최소 {WARMUP_BARS + 10}개가 필요합니다",
                "bars": n}
    S = compute_signals(B, p)
    tr = simulate(B, S, p)
    fl = simulate(B, S, p, flip=True)
    cut = int(n * 0.6)
    days = B.days
    is_tr = [t for t in tr if t.i < cut]
    oos_tr = [t for t in tr if t.i >= cut]
    _, _, curve = account_curve(tr, p)
    cum = 0.0
    equity = []
    for t, e in zip(tr, curve, strict=False):
        cum += t.r
        equity.append({"t": t.exit_ts, "eq": e, "cum_r": cum})
    signals_n = int((S.d != 0).sum())
    return {
        "ok": True,
        "bars": n,
        "days": days,
        "span": {"from": datetime.fromtimestamp(int(B.T[0]), UTC).isoformat(),
                 "to": datetime.fromtimestamp(int(B.T[-1]), UTC).isoformat()},
        "buy_hold_x": float(B.c[-1] / B.c[0]) if B.c[0] else None,
        "signals": signals_n,
        "params": p.as_dict(),
        "label_ko": p.label_ko,
        "all": summarize(tr, days, p),
        "flip": summarize(fl, days, p),
        "is": summarize(is_tr, days * 0.6, p),
        "oos": summarize(oos_tr, days * 0.4, p),
        "monthly": monthly(tr),
        "equity": equity,
        "trades": [t.as_dict() for t in tr[-max_trades_out:]],
        "trades_truncated": max(0, len(tr) - max_trades_out),
        "verdict": verdict(summarize(tr, days, p), summarize(fl, days, p), summarize(oos_tr, days * 0.4, p)),
    }


def verdict(all_: dict, flip: dict, oos: dict) -> dict[str, Any]:
    """docs/14 의 판정 규칙을 숫자로 — 화면이 색만 칠하게."""
    checks: list[dict[str, Any]] = []
    n = all_.get("n", 0)
    checks.append({"name": "표본 100건 이상", "ok": n >= 100, "value": n})
    exp = all_.get("exp_r")
    checks.append({"name": "기대값 +0.05R 이상", "ok": exp is not None and exp >= 0.05,
                   "value": exp})
    t = all_.get("t")
    checks.append({"name": "t 통계 2 이상", "ok": t is not None and t >= 2.0, "value": t})
    fe = flip.get("exp_r")
    checks.append({"name": "뒤집기 대조군이 더 나쁨", "ok": exp is not None and fe is not None and exp > fe,
                   "value": fe})
    oe = oos.get("exp_r")
    checks.append({"name": "OOS 양수", "ok": oe is not None and oe > 0, "value": oe})
    passed = sum(1 for c in checks if c["ok"])
    if n == 0:
        grade, ko = "none", "매매 없음"
    elif passed == len(checks):
        grade, ko = "pass", "통과 — 페이퍼로 넘길 만함"
    elif passed >= 3:
        grade, ko = "weak", "약함 — 방향 정보는 있으나 표본·크기 부족"
    else:
        grade, ko = "fail", "탈락 — 우위 없음"
    return {"grade": grade, "ko": ko, "checks": checks, "passed": passed, "total": len(checks)}


# ─────────────────────────────────────────────────────────
# 실시간 엔진용 — 마지막 **닫힌** 봉의 상태
def latest_state(B: Bars, p: Params) -> dict[str, Any]:
    """마지막 봉 종가에서 신호가 났는지, 지금 국면이 뭔지. 엔진과 화면이 같은 것을 본다."""
    n = len(B)
    if n == 0:
        return {"ok": False, "error_ko": "봉이 없습니다"}
    S = compute_signals(B, p)
    i = n - 1
    d = int(S.d[i])
    c = float(B.c[i])
    e20, e200 = float(S.e20[i]), float(S.e200[i])
    out = {
        "ok": n >= WARMUP_BARS,
        "bar_ts": int(B.T[i]),
        "bar_at": datetime.fromtimestamp(int(B.T[i]), UTC).isoformat(),
        "close": c, "ema20": e20, "ema200": e200,
        "gap_pct": abs(e20 - e200) / c * 100 if c else None,
        "trend": int(S.trend[i]),
        "chop": bool(S.chop[i]),
        "squeezed": bool(S.squeezed[i]),
        "signal": d,
        "kind": int(S.kind[i]),
        "kind_ko": KIND_KO.get(int(S.kind[i]), ""),
        "stop": float(S.stop[i]) if d else None,
        "bars": n,
    }
    if n < WARMUP_BARS:
        out["error_ko"] = f"봉 {n}개 — 워밍업 {WARMUP_BARS}개 미만이라 신호를 믿지 않습니다"
        out["signal"] = 0
    return out


def phase_ko(state: dict[str, Any]) -> str:
    """국면을 한 줄로."""
    if not state.get("ok"):
        return state.get("error_ko") or "데이터 없음"
    if state.get("chop"):
        return "횡보 — 진입 금지 (20선을 자주 넘나듦)"
    t = state.get("trend")
    base = {1: "상승 추세 (200선 위·기울기 ↑)", -1: "하락 추세 (200선 아래·기울기 ↓)"}.get(t, "추세 없음")
    if state.get("squeezed"):
        base += " · 스퀴즈 중"
    return base


__all__ = [
    "SETUP_ID", "Bars", "Params", "Signals", "Trade", "compute_signals", "default_funding", "CRYPTO",
    "exit_hit", "latest_state", "phase_ko", "resample_5m", "run_backtest", "simulate", "summarize",
    "session_mask", "verdict", "monthly", "account_curve", "ema", "KIND_KO", "REASON_KO", "WARMUP_BARS",
]
