"""성과 지표 — 순수 함수.

**승률은 표본이 작으면 의미가 없다.** 그래서 승률은 항상 Wilson 95% 신뢰구간과
함께만 내놓는다. 20트레이드에서 60%가 나와도 실제 구간은 39~78% 라 아무 말도
할 수 없다는 걸 숫자로 보여주기 위해서다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import Trade

# 04 문서 — 못 넘으면 신호 기능을 출시하지 않는다
GATE_MIN_EXPECTANCY_R = 0.20
GATE_MIN_TRADES = 300
GATE_MAX_DD_RATIO = 0.40
GATE_MAX_LOSS_STREAK = 6


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 점수 구간. 표본이 작을 때 정규근사보다 정직하다."""
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def equity_curve(trades: list[Trade]) -> list[float]:
    """R 누적. 시간순으로 정렬된 트레이드를 전제한다."""
    out, total = [], 0.0
    for t in trades:
        total += t.net_r
        out.append(total)
    return out


def max_drawdown_r(curve: list[float]) -> float:
    peak, worst = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        worst = min(worst, v - peak)
    return abs(worst)


def max_loss_streak(trades: list[Trade]) -> int:
    best = run = 0
    for t in trades:
        run = run + 1 if t.net_r <= 0 else 0
        best = max(best, run)
    return best


def quartiles(values: list[float]) -> tuple[float, float, float] | None:
    if not values:
        return None
    v = sorted(values)

    def q(p: float) -> float:
        if len(v) == 1:
            return v[0]
        i = p * (len(v) - 1)
        lo, hi = int(math.floor(i)), int(math.ceil(i))
        return v[lo] + (v[hi] - v[lo]) * (i - lo)

    return (q(0.25), q(0.5), q(0.75))


def histogram(values: list[float], lo: float, hi: float, bins: int) -> list[tuple[float, float, int]]:
    """(구간시작, 구간끝, 개수). 범위 밖은 양 끝 통에 넣는다."""
    if bins <= 0:
        return []
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        i = int((v - lo) // width) if width else 0
        counts[min(bins - 1, max(0, i))] += 1
    return [(lo + i * width, lo + (i + 1) * width, c) for i, c in enumerate(counts)]


@dataclass(frozen=True, slots=True)
class Stats:
    n: int
    wins: int
    win_rate: float
    win_rate_ci: tuple[float, float]
    expectancy_r: float
    total_r: float
    gross_r: float
    cost_r: float
    stdev_r: float
    profit_factor: float | None
    max_drawdown_r: float
    max_loss_streak: int
    tp1_rate: float
    tp1_bars_quartiles: tuple[float, float, float] | None
    hold_minutes_quartiles: tuple[float, float, float] | None
    exit_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def dd_ratio(self) -> float | None:
        """최대낙폭 / 총수익. 총수익이 0 이하면 비율을 말할 수 없다."""
        return self.max_drawdown_r / self.total_r if self.total_r > 0 else None


def summarize(trades: list[Trade]) -> Stats:
    n = len(trades)
    if n == 0:
        return Stats(0, 0, 0.0, (0.0, 0.0), 0.0, 0.0, 0.0, 0.0, 0.0, None, 0.0, 0, 0.0, None, None, {})

    rs = [t.net_r for t in trades]
    wins = sum(1 for r in rs if r > 0)
    total = sum(rs)
    mean = total / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1) if n > 1 else 0.0

    gain = sum(r for r in rs if r > 0)
    loss = -sum(r for r in rs if r < 0)

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    tp1_bars = [float(t.bars_to_tp1) for t in trades if t.bars_to_tp1 is not None]

    return Stats(
        n=n,
        wins=wins,
        win_rate=wins / n,
        win_rate_ci=wilson_ci(wins, n),
        expectancy_r=mean,
        total_r=total,
        gross_r=sum(t.gross_r for t in trades),
        cost_r=sum(t.cost_r for t in trades),
        stdev_r=math.sqrt(var),
        profit_factor=(gain / loss) if loss > 0 else None,
        max_drawdown_r=max_drawdown_r(equity_curve(trades)),
        max_loss_streak=max_loss_streak(trades),
        tp1_rate=len(tp1_bars) / n,
        tp1_bars_quartiles=quartiles(tp1_bars),
        hold_minutes_quartiles=quartiles([float(t.hold_minutes) for t in trades]),
        exit_reasons=reasons,
    )


def group_by(trades: list[Trade], key: str) -> dict[str, Stats]:
    buckets: dict[str, list[Trade]] = {}
    for t in trades:
        buckets.setdefault(str(getattr(t, key)), []).append(t)
    return {k: summarize(v) for k, v in sorted(buckets.items())}


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    passed: bool
    detail: str


def evaluate_gates(oos: Stats, by_regime: dict[str, Stats]) -> list[Gate]:
    """04 문서의 통과 기준. **out-of-sample 성적으로만 판정한다.**"""
    rth = by_regime.get("RTH")
    dd = oos.dd_ratio

    return [
        Gate(
            "기대값 ≥ +0.20R",
            oos.expectancy_r >= GATE_MIN_EXPECTANCY_R,
            f"{oos.expectancy_r:+.3f}R (비용 차감 후)",
        ),
        Gate(
            f"표본 ≥ {GATE_MIN_TRADES}",
            oos.n >= GATE_MIN_TRADES,
            f"{oos.n}트레이드",
        ),
        Gate(
            "최대낙폭 ≤ 총수익의 40%",
            dd is not None and dd <= GATE_MAX_DD_RATIO,
            f"낙폭 {oos.max_drawdown_r:.1f}R / 총수익 {oos.total_r:+.1f}R"
            + (f" = {dd:.0%}" if dd is not None else " — 총수익이 0 이하라 비율을 말할 수 없습니다"),
        ),
        Gate(
            "RTH 단독으로도 양수",
            rth is not None and rth.expectancy_r > 0,
            f"RTH {rth.expectancy_r:+.3f}R ({rth.n}건)" if rth else "RTH 표본 없음",
        ),
        Gate(
            f"최대 연속 손절 ≤ {GATE_MAX_LOSS_STREAK}",
            oos.max_loss_streak <= GATE_MAX_LOSS_STREAK,
            f"{oos.max_loss_streak}회 연속",
        ),
    ]
