"""전략에 **우위가 있는가** 를 검정하는 도구들 — 전부 순수 함수.

백테스트가 마이너스로 나왔을 때 물어야 할 질문은 "임계값을 어떻게 고칠까"가 아니라
**"애초에 우위가 있기는 한가"** 다. 후자를 먼저 답하지 않으면, 임계값을 만지는 일이
전부 이 표본에 대한 커브 피팅이 된다.

여기 있는 것들은 손익을 최대화하지 않는다. 다음 네 가지를 잰다.

1. `outcome_grid`  — (목표, 손절) 격자 전체. 한 칸이라도 양수인가
2. `direction_control` — 방향을 뒤집으면 나빠지는가 (우위가 있다면 나빠져야 한다)
3. `edge_signature` — 진입 후 순행이 역행보다 큰 비율. 우위가 없으면 50%
4. `series_character` — 종목 자체가 추세형인가 회귀형인가 무작위인가

**우위가 없을 때의 서명**: 기대값 ≈ −(왕복비용 / 손절폭). 정확히 비용만큼 잃는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..strategy.indicators import Bar, resample

# 비용 — 실측(docs/11) 기준. 시장가 왕복 ≈ 0.11% = 테이커 0.05×2 + 스프레드 0.008 + 펀딩.
#   · 경로가 슬리피지를 **직접 들고 있으면**(Path.slippage_pct) 왕복비용은 수수료만 쓴다.
#     슬리피지는 outcome_r 안에서 방향별로 한 번씩 적용된다 (진입 1회 + 청산 1회).
#   · 경로에 슬리피지가 없으면(None) 전부 합친 0.11% 를 쓴다.
#   예전 0.14 = 수수료 0.08 + 슬리피지 0.06 은 체결가에 이미 슬리피지가 들어간 경로에
#   또 슬리피지를 얹어 **진입 슬리피지를 두 번** 내고 있었다.
DEFAULT_FEES_PCT = 0.10             # 왕복 수수료만 (테이커 0.05 × 2)
DEFAULT_SLIPPAGE_PCT = 0.005        # 편도 슬리피지 ≈ 실측 스프레드 0.008%/2 + 여유
DEFAULT_ROUND_TRIP_PCT = DEFAULT_FEES_PCT + 2 * DEFAULT_SLIPPAGE_PCT     # 0.11 — 전부 합친 값


@dataclass(frozen=True, slots=True)
class Path:
    """진입 이후의 역행·순행 폭 (진입가 대비 %). 손절을 걸지 않고 관찰한 것.

    · `adverse`/`favour` 는 **슬리피지를 넣지 않은 트리거 가격** 기준이다. 슬리피지는
      `slippage_pct` 로 따로 들고 `outcome_r` 가 방향에 맞게 적용한다 — 그래야
      `flipped()` 가 반대 방향의 체결가를 제대로 재현한다.
    · `last_pct` 는 관찰 마지막 봉의 종가 (순행 방향 +%). 손절·목표 어느 쪽에도 안 닿고
      시간이 끝나면 이 값으로 시가평가한다 — `simulate.py` 의 time/data_end 청산과 같다.
      None 이면 마지막 봉의 (고+저)/2 로 근사한다 (종가를 모를 때의 차선책).
    """

    adverse: tuple[float, ...]
    favour: tuple[float, ...]
    last_pct: float | None = None
    slippage_pct: float | None = None   # 편도 % — None 이면 왕복비용에 이미 포함된 것으로 본다

    @classmethod
    def from_bars(
        cls, bars: list[Bar], trigger: float, long: bool, slippage_pct: float | None = None
    ) -> Path:
        """체결 트리거(슬리피지 미적용)와 이후 봉들로 경로를 만든다."""
        adv: list[float] = []
        fav: list[float] = []
        last: float | None = None
        for b in bars:
            if long:
                adv.append((trigger - b.low) / trigger * 100)
                fav.append((b.high - trigger) / trigger * 100)
                last = (b.close - trigger) / trigger * 100
            else:
                adv.append((b.high - trigger) / trigger * 100)
                fav.append((trigger - b.low) / trigger * 100)
                last = (trigger - b.close) / trigger * 100
        return cls(tuple(adv), tuple(fav), last, slippage_pct)

    @property
    def mae(self) -> float:
        return max(self.adverse, default=0.0)

    @property
    def mfe(self) -> float:
        return max(self.favour, default=0.0)

    @property
    def mark_pct(self) -> float:
        """시간 만료 시 시가평가에 쓸 값. 종가가 없으면 마지막 봉의 중간값."""
        if self.last_pct is not None:
            return self.last_pct
        if not self.adverse:
            return 0.0
        return (self.favour[-1] - self.adverse[-1]) / 2

    def flipped(self) -> Path:
        """정반대 방향. 역행↔순행이 바뀌고 종가 부호가 뒤집힌다. 슬리피지는 그대로."""
        return Path(
            self.favour, self.adverse,
            None if self.last_pct is None else -self.last_pct,
            self.slippage_pct,
        )

    def mae_before(self, target_pct: float) -> float | None:
        """목표에 닿기 전까지의 최대 역행. 목표에 못 닿았으면 None."""
        worst = 0.0
        for a, f in zip(self.adverse, self.favour, strict=False):
            worst = max(worst, a)
            if f >= target_pct:
                return worst
        return None


def _fees_for(path: Path, round_trip_pct: float | None) -> float:
    if round_trip_pct is not None:
        return round_trip_pct
    return DEFAULT_ROUND_TRIP_PCT if path.slippage_pct is None else DEFAULT_FEES_PCT


def outcome_r(
    path: Path,
    target_pct: float,
    stop_pct: float,
    round_trip_pct: float | None = None,
) -> float:
    """TP1 절반 청산 + 본전 스톱 구조를 그대로 재현한다. **같은 봉이면 손절 먼저.**

    · 시간이 끝나면 남은 물량은 마지막 종가로 시가평가한다 (`Path.mark_pct`).
    · 슬리피지(`path.slippage_pct`)는 진입 때 경로 전체를 불리한 쪽으로 밀고,
      청산 때 한 번 더 낸다 — 왕복 2회. 방향을 뒤집어도 똑같이 적용된다.
    · `round_trip_pct` 가 None 이면 경로가 슬리피지를 들고 있을 때는 수수료만,
      아니면 전부 합친 기본값을 쓴다.
    """
    if stop_pct <= 0:
        return 0.0
    s = path.slippage_pct or 0.0
    fees = _fees_for(path, round_trip_pct)
    cost_r = (fees + s) / stop_pct          # 수수료 + 청산 슬리피지 (진입분은 아래에서 경로를 민다)
    hit = None
    for i, (a, f) in enumerate(zip(path.adverse, path.favour, strict=False)):
        if a + s >= stop_pct:
            return -1.0 - cost_r
        if f - s >= target_pct:
            hit = i
            break
    mark = (path.mark_pct - s) / stop_pct
    if hit is None:
        return mark - cost_r
    r1 = target_pct / stop_pct
    tp2 = target_pct * 2
    for a, f in zip(path.adverse[hit + 1:], path.favour[hit + 1:], strict=False):
        if a + s >= 0.0:
            return 0.5 * r1 - cost_r
        if f - s >= tp2:
            return 0.5 * r1 + 0.5 * (tp2 / stop_pct) - cost_r
    return 0.5 * r1 + 0.5 * mark - cost_r


@dataclass(frozen=True, slots=True)
class Cell:
    target_pct: float
    stop_pct: float
    n: int
    win_rate: float
    expectancy_r: float
    cost_pct: float = DEFAULT_ROUND_TRIP_PCT    # 이 칸에 실제로 적용된 왕복비용 (슬리피지 포함)

    @property
    def cost_only_r(self) -> float:
        """우위가 0일 때 나올 기대값 — 비용만 잃는 값."""
        return -self.cost_pct / self.stop_pct if self.stop_pct else 0.0

    @property
    def looks_like_no_edge(self) -> bool:
        """기대값이 '비용만큼'과 겹치면 우위가 없다는 뜻이다."""
        return abs(self.expectancy_r - self.cost_only_r) < 0.05


def evaluate(
    paths: list[Path], target_pct: float, stop_pct: float,
    round_trip_pct: float | None = None,
) -> Cell:
    rs = [outcome_r(p, target_pct, stop_pct, round_trip_pct) for p in paths]
    if not rs:
        return Cell(target_pct, stop_pct, 0, 0.0, 0.0)
    wins = sum(1 for r in rs if r > 0)
    # 비용선: 수수료 + 슬리피지 왕복(2회). 경로마다 다를 수 있으니 평균을 쓴다.
    cost = sum(_fees_for(p, round_trip_pct) + 2 * (p.slippage_pct or 0.0) for p in paths) / len(paths)
    return Cell(target_pct, stop_pct, len(rs), wins / len(rs), sum(rs) / len(rs), cost)


def outcome_grid(
    paths: list[Path], targets: list[float], stops: list[float]
) -> list[Cell]:
    return [evaluate(paths, t, s) for s in stops for t in targets]


def direction_control(paths: list[Path], target_pct=1.0, stop_pct=1.0) -> tuple[Cell, Cell]:
    """(신호 방향, 정반대 방향). 우위가 있다면 뒤집은 쪽이 뚜렷이 나빠야 한다."""
    return (
        evaluate(paths, target_pct, stop_pct),
        evaluate([p.flipped() for p in paths], target_pct, stop_pct),
    )


def edge_signature(paths: list[Path]) -> float:
    """진입 후 최대 순행 > 최대 역행 인 비율. **우위가 없으면 0.5 근처.**"""
    usable = [p for p in paths if p.favour]
    if not usable:
        return 0.0
    return sum(1 for p in usable if p.mfe > p.mae) / len(usable)


# ── 종목 자체의 성격 ─────────────────────────────────────

def autocorrelation(values: list[float], lag: int = 1) -> float | None:
    n = len(values) - lag
    if n < 30:
        return None
    mean = sum(values) / len(values)
    num = sum((values[i] - mean) * (values[i + lag] - mean) for i in range(n))
    den = sum((v - mean) ** 2 for v in values)
    return num / den if den else None


def returns_pct(bars: list[Bar]) -> list[float]:
    return [
        (bars[i].close / bars[i - 1].close - 1) * 100
        for i in range(1, len(bars))
        if bars[i - 1].close
    ]


def efficiency_ratio(bars: list[Bar]) -> float | None:
    """순이동 / 총이동. 무작위 보행이면 대략 0.2, 1에 가까우면 추세."""
    if len(bars) < 3:
        return None
    net = abs(bars[-1].close - bars[0].close)
    total = sum(abs(bars[i].close - bars[i - 1].close) for i in range(1, len(bars)))
    return net / total if total else None


def continuation_rate(bars: list[Bar], lookback: int, forward: int) -> tuple[float, float]:
    """(방향이 이어진 비율, 방향을 따라갔을 때 평균 수익%). 0.5 / 0 이면 우위 없음."""
    same = total = 0
    gain = 0.0
    for i in range(lookback, len(bars) - forward):
        past = bars[i].close / bars[i - lookback].close - 1
        nxt = bars[i + forward].close / bars[i].close - 1
        if past == 0:
            continue
        total += 1
        if (past > 0) == (nxt > 0):
            same += 1
        gain += nxt * (1 if past > 0 else -1) * 100
    if not total:
        return (0.0, 0.0)
    return (same / total, gain / total)


@dataclass(frozen=True, slots=True)
class Character:
    timeframe_minutes: int
    n: int
    autocorr: float | None
    efficiency_median: float | None

    @property
    def reading_ko(self) -> str:
        a = self.autocorr
        if a is None:
            return "표본 부족"
        if a > 0.05:
            return "추세(모멘텀) 성향"
        if a < -0.05:
            return "평균회귀 성향"
        return "무작위에 가까움 — 추세추종·역추세 모두 근거가 약함"


def series_character(bars: list[Bar], timeframe_minutes: int, window: int = 20) -> Character:
    bs = resample(bars, timeframe_minutes, drop_incomplete=True) if timeframe_minutes > 1 else bars
    rets = returns_pct(bs)
    ers = [
        er
        for i in range(window, len(bs))
        if (er := efficiency_ratio(bs[i - window: i + 1])) is not None
    ]
    ers.sort()
    return Character(
        timeframe_minutes=timeframe_minutes,
        n=len(bs),
        autocorr=autocorrelation(rets, 1),
        efficiency_median=ers[len(ers) // 2] if ers else None,
    )


def random_walk_efficiency(window: int) -> float:
    """무작위 보행의 효율성 비율 기대값 ≈ √(2/(πn)) × ... 대략 √(1/n) 규모.

    정확한 상수보다 자릿수가 중요하다 — 관측값이 이 근처면 추세 성향이 없다는 뜻.
    """
    return math.sqrt(2 / (math.pi * window)) * math.sqrt(math.pi / 2)
