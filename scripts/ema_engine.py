"""EMA 9/20/99/200 스택 전략 — 빠른 전용 백테스터.

사용자가 실제로 쓰던 방법이다. 1분봉·5분봉에서 EMA 네 줄을 기준으로
추세추종하고, 같은 선이 손절 기준도 된다.

기존 엔진(th.strategy)을 쓰지 않는 이유는 속도다. 조합을 수백 개 훑어야 하는데
피처 파이프라인은 한 번에 21ms 다. 여기서는 EMA 를 전체 구간에 한 번만 계산하고
봉을 한 번 훑는다 — 조합당 몇 초면 끝난다.

**규칙은 시뮬레이터와 동일하게 보수적으로 맞춘다.**
  · 신호 봉의 종가로 판단하고 진입은 **다음 봉 시가**
  · 같은 봉에서 손절과 익절이 둘 다 닿으면 **손절**
  · 왕복 비용 0.14% (수수료 0.08 + 슬리피지 0.06)
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ROUND_TRIP = 0.14


def load_bars(path, symbol="SOXLUSDT"):
    out = []
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("symbol", symbol).upper() != symbol:
                continue
            out.append((
                datetime.fromisoformat(r["ts"]).astimezone(UTC),
                float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                float(r["volume"]),
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
                out.append(merge(bucket))
            bucket, key = [], k
        bucket.append(b)
    if len(bucket) == minutes:
        out.append(merge(bucket))
    return out


def merge(g):
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


def atr(bars, period=14):
    n = len(bars)
    out = [None] * n
    if n < period + 1:
        return out
    trs = []
    for i in range(1, n):
        h, low, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    prev = sum(trs[:period]) / period
    out[period] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i + 1] = prev
    return out


@dataclass(frozen=True, slots=True)
class Config:
    tf: int = 5
    align: int = 4          # 몇 줄까지 정배열을 요구하나 (0=없음, 2=9>20, 3=+99, 4=+200)
    entry: str = "pullback20"   # cross920 | pullback20 | pullback99 | pullback9
    stop: str = "ema20"     # ema20 | ema99 | atr15 | atr25 | swing
    exit: str = "ema9"      # ema9 | ema20 | r2 | r3 | trail20
    long_only: bool = False
    flip: bool = False
    max_bars: int = 0        # >0 이면 이 봉 수 지나면 시장가 강제청산 (오버나이트 차단)      # 대조군 — 방향 반대로


@dataclass(frozen=True, slots=True)
class Result:
    n: int
    wins: int
    expectancy_r: float
    total_r: float
    win_rate: float
    max_dd_r: float
    avg_hold: float
    profit_factor: float | None
    trades: tuple = ()


def run(bars, cfg: Config) -> Result:
    bs = resample(bars, cfg.tf)
    if len(bs) < 300:
        return Result(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, None)

    closes = [b[4] for b in bs]
    e9, e20 = ema(closes, 9), ema(closes, 20)
    e99, e200 = ema(closes, 99), ema(closes, 200)
    a = atr(bs, 14)

    def aligned(i, up):
        if cfg.align == 0:
            return True
        v = [e9[i], e20[i], e99[i], e200[i]][: cfg.align]
        if any(x is None for x in v):
            return False
        return all(v[j] > v[j + 1] for j in range(len(v) - 1)) if up else \
               all(v[j] < v[j + 1] for j in range(len(v) - 1))

    def ref_line(i, name):
        return {"ema20": e20[i], "ema99": e99[i], "ema9": e9[i]}.get(name)

    rs, holds = [], []
    i = 210
    pos = None
    while i < len(bs) - 1:
        b = bs[i]
        if pos is None:
            for up in (True, False):
                if cfg.long_only and not up:
                    continue
                if not aligned(i, up):
                    continue
                trig = False
                if cfg.entry == "cross920":
                    p9, p20 = e9[i - 1], e20[i - 1]
                    if None not in (p9, p20, e9[i], e20[i]):
                        trig = (p9 <= p20 and e9[i] > e20[i]) if up else (p9 >= p20 and e9[i] < e20[i])
                else:
                    line = ref_line(i, cfg.entry.replace("pullback", "ema"))
                    if line is not None:
                        touched = any(
                            (bs[j][3] <= line if up else bs[j][2] >= line)
                            for j in range(max(0, i - 2), i + 1)
                        )
                        back = b[4] > line if up else b[4] < line
                        trig = touched and back
                if not trig:
                    continue

                direction = up if not cfg.flip else (not up)
                entry = bs[i + 1][1]        # 다음 봉 시가
                if cfg.stop.startswith("atr"):
                    mult = 1.5 if cfg.stop == "atr15" else 2.5
                    if a[i] is None:
                        continue
                    dist = mult * a[i]
                elif cfg.stop == "swing":
                    # 손절폭은 **신호 방향(up)** 의 스윙으로 잰다. flip 대조군은 방향만 뒤집고
                    # 같은 자리·같은 손절폭을 써야 비용이 상쇄된다 — direction 으로 재면
                    # 뒤집은 쪽이 반대편 스윙을 쓰게 되어 손절폭 자체가 달라진다.
                    win = bs[max(0, i - 20): i + 1]
                    lvl = min(x[3] for x in win) if up else max(x[2] for x in win)
                    dist = abs(entry - lvl)
                else:
                    lvl = ref_line(i, cfg.stop)
                    if lvl is None:
                        continue
                    dist = abs(entry - lvl)
                if dist <= 0 or entry <= 0 or dist / entry * 100 < 0.05:
                    continue
                pos = (direction, entry, dist, i + 1)
                break
            i += 1
            continue

        direction, entry, dist, start = pos
        sign = 1 if direction else -1
        stop_px = entry - sign * dist
        exited = None
        j = i
        while j < len(bs):
            bar = bs[j]
            if (direction and bar[3] <= stop_px) or (not direction and bar[2] >= stop_px):
                exited = (stop_px, j, "stop"); break
            if cfg.exit in ("r2", "r3"):
                mult = 2.0 if cfg.exit == "r2" else 3.0
                tp = entry + sign * mult * dist
                if (direction and bar[2] >= tp) or (not direction and bar[3] <= tp):
                    exited = (tp, j, "tp"); break
            if cfg.exit in ("ema9", "ema20", "trail20"):
                line = e9[j] if cfg.exit == "ema9" else e20[j]
                if line is not None:
                    broke = bar[4] < line if direction else bar[4] > line
                    if broke and j > start:
                        exited = (bar[4], j, "ema_exit"); break
            if cfg.max_bars and (j - start) >= cfg.max_bars:
                exited = (bar[4], j, "time"); break
            j += 1
        if exited is None:
            exited = (bs[-1][4], len(bs) - 1, "end")
        px, j, why = exited
        gross = sign * (px - entry) / dist
        cost = ROUND_TRIP / (dist / entry * 100)
        rs.append(gross - cost)
        holds.append((j - start) * cfg.tf)
        pos = None
        i = j + 1

    if not rs:
        return Result(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, None)
    wins = sum(1 for r in rs if r > 0)
    peak = cum = dd = 0.0
    for r in rs:
        cum += r; peak = max(peak, cum); dd = min(dd, cum - peak)
    gain = sum(r for r in rs if r > 0); loss = -sum(r for r in rs if r < 0)
    return Result(
        n=len(rs), wins=wins, expectancy_r=sum(rs) / len(rs), total_r=sum(rs),
        win_rate=wins / len(rs), max_dd_r=abs(dd),
        avg_hold=sum(holds) / len(holds),
        profit_factor=(gain / loss) if loss > 0 else None,
        trades=tuple(rs),
    )
