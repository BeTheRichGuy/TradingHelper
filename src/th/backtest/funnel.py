"""퍼널 진단 — **신호가 어디서 죽는지** 센다.

3.5개월 5,500번 평가에 트레이드 7건이 나왔다. 설계 의도는 하루 0~3건이었으니
20~30배 적다. 이럴 때 임계값을 아무거나 풀어서 신호가 나오게 만드는 건 최악이다.
그건 이 두 달치 데이터에 커브 피팅하는 것이고, 그렇게 만든 전략은 실전에서 죽는다.

**먼저 어디가 막혔는지 센다.** 그다음에 그게 잘못 잡힌 값인지, 원래 그래야 하는
값인지를 하나씩 따진다.

각 단계는 배타적이다 — 한 평가는 가장 먼저 걸린 곳에서 한 번만 세어진다.
그래야 합계가 맞고, 어디를 풀면 몇 건이 늘어나는지 읽을 수 있다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from ..strategy.indicators import last_swing_high, last_swing_low
from ..strategy.risk import (
    ATR_MULT,
    SL_MAX_PCT,
    SL_MIN_PCT,
    TP1_MIN_PCT,
    TP1_R,
    TP2_MAX_PCT,
    TP2_R,
    stop_loss_price,
)

from .models import BacktestConfig
from .runner import MIN_BARS, iter_evaluations, load_series

# 구조적 손절을 몇 봉 안에서 찾을지 — 이 값이 손절폭을 좌우한다
STRUCTURE_WINDOWS = (10, 15, 20, 30, 60, None)

# 발행 가능한 손절폭 = TP1·4R 조건을 동시에 만족하는 구간. **상수에서 파생시킨다.**
PUBLISHABLE_SL_MIN = TP1_MIN_PCT / TP1_R
PUBLISHABLE_SL_MAX = TP2_MAX_PCT / TP2_R
# 그 손절폭이 나오려면 ATR% 가 이 구간이어야 한다 (구조적 손절이 더 좁을 때)
PUBLISHABLE_ATR_MIN = PUBLISHABLE_SL_MIN / ATR_MULT
PUBLISHABLE_ATR_MAX = PUBLISHABLE_SL_MAX / ATR_MULT


def classify_reject(reason: str) -> str:
    # 라벨의 숫자는 반드시 상수에서 만든다. 예전엔 문자열에 옛 숫자가 박혀 있어
    # 상수를 바꾼 뒤에도 화면이 옛날 기준을 말했다.
    if "너무 가깝" in reason:
        return f"TP1이 너무 가까움 (손절폭 < {PUBLISHABLE_SL_MIN:.2f}%)"
    if "너무 멉" in reason:
        return f"4R이 너무 멂 (손절폭 > {PUBLISHABLE_SL_MAX:.2f}%)"
    if "손익비" in reason:
        return "손익비 미달"
    if "조건 데이터가" in reason:
        return "조건 데이터 부족 (커버리지 미달)"
    if "신뢰도" in reason:
        return "신뢰도 점수 미달"
    if "진입 구간" in reason:
        return "진입 구간 계산 불가"
    return reason[:40]


@dataclass
class FunnelReport:
    symbol: str
    start: datetime
    end: datetime

    evaluated: int = 0
    blocked: Counter = field(default_factory=Counter)          # 세션 게이트
    no_data: int = 0
    filtered: Counter = field(default_factory=Counter)         # 시장 상태 필터
    required_failed: Counter = field(default_factory=Counter)  # 셋업 필수 조건
    rejected: Counter = field(default_factory=Counter)         # 리스크·점수
    published: int = 0

    atr_pct: list[float] = field(default_factory=list)
    scores: list[int] = field(default_factory=list)
    sl_pct: list[float] = field(default_factory=list)
    atr_only_sl_pct: list[float] = field(default_factory=list)
    # 구조 탐색 창을 바꿨을 때 손절폭이 어떻게 되는지 (창 → 손절폭 목록)
    stop_by_window: dict[object, list[float]] = field(default_factory=dict)

    @property
    def reached_setups(self) -> int:
        return self.evaluated - sum(self.blocked.values()) - self.no_data - sum(
            self.filtered.values()
        )

    @property
    def atr_in_band(self) -> int:
        return sum(1 for v in self.atr_pct if PUBLISHABLE_ATR_MIN <= v <= PUBLISHABLE_ATR_MAX)

    @property
    def reached_plan(self) -> int:
        """리스크·점수 판정까지 간 수 (= 필수 조건을 통과한 수)."""
        return sum(self.rejected.values()) + self.published

    def stage_rows(self) -> list[tuple[str, int, int]]:
        """(단계, 여기서 죽은 수, 통과해서 남은 수). 배타적이라 합계가 맞는다."""
        gate = sum(self.blocked.values())
        filt = sum(self.filtered.values())
        required = max(0, self.reached_setups - self.reached_plan)
        risk = sum(self.rejected.values())

        left = self.evaluated
        rows: list[tuple[str, int, int]] = []
        for label, killed in (
            ("세션 게이트 차단", gate),
            ("데이터 부족", self.no_data),
            ("시장 상태 필터", filt),
            ("셋업 필수 조건", required),
            ("리스크·점수 기준", risk),
        ):
            left -= killed
            rows.append((label, killed, left))
        return rows


def probe_stop_windows(r: FunnelReport, candidate, features) -> None:
    """구조 탐색 창을 바꾸면 손절폭이 어떻게 되는지 **재보기만** 한다.

    전략은 바뀌지 않는다. 지금 손절이 왜 넓은지가 ATR 때문인지 구조 때문인지,
    구조라면 얼마나 먼 구조를 잡고 있는지를 숫자로 보려는 것이다.
    """
    plan = candidate.plan
    atr = features.atr_1m
    if plan is None or atr is None or not features.bars_1m:
        return
    direction = candidate.result.direction
    ref, price = plan.entry_ref, features.price

    _, atr_only = stop_loss_price(direction, ref, atr, price, None)
    r.atr_only_sl_pct.append(atr_only)

    for w in STRUCTURE_WINDOWS:
        swing = (
            last_swing_low(features.bars_1m, 5, within=w)
            if direction == "long"
            else last_swing_high(features.bars_1m, 5, within=w)
        )
        _, dist = stop_loss_price(direction, ref, atr, price, swing)
        r.stop_by_window.setdefault(w if w is not None else "전체", []).append(dist)


async def collect_funnel(
    symbol: str,
    start: datetime,
    end: datetime,
    config: BacktestConfig,
    *,
    progress=None,
    dataset=None,
) -> FunnelReport:
    symbol = symbol.upper()
    perp_provider = volume_provider = None
    if dataset is not None:
        series = list(dataset.bars)
        perp_provider = lambda at, price: dataset.perp_at(at, price)   # noqa: E731
        volume_provider = dataset.median_volume_5m
    else:
        series = await load_series(symbol, start, end)
    r = FunnelReport(symbol=symbol, start=start, end=end)
    if len(series) < MIN_BARS:
        return r

    async for ev in iter_evaluations(
        symbol, series, start, end, config,
        perp_provider=perp_provider, volume_provider=volume_provider,
    ):
        r.evaluated += 1
        if progress is not None:
            progress(ev.at, r.evaluated, r.published)

        if ev.session.blocked:
            for b in ev.session.blocks:
                r.blocked[b.reason_ko[:36]] += 1
            if not ev.session.blocks:
                r.blocked["사유 미상"] += 1
            continue

        a = ev.analysis
        if a is None:
            r.no_data += 1
            continue

        if ev.features is not None and ev.features.atr_pct is not None:
            r.atr_pct.append(ev.features.atr_pct)

        if a.failed_filters:
            for f in a.failed_filters:
                r.filtered[f.name] += 1
            continue

        # 필수 조건에서 걸린 셋업 — 어느 조건인지까지 센다
        if not a.candidates:
            for miss in a.near_misses:
                failed = miss.failed_required
                if failed:
                    r.required_failed[f"{miss.setup_id} · {failed[0].label}"] += 1
            continue

        best = a.candidates[0]
        r.scores.append(best.score.final)
        if best.plan is not None:
            r.sl_pct.append(best.plan.risk_pct)
            probe_stop_windows(r, best, ev.features)

        if best.published:
            r.published += 1
        else:
            r.rejected[classify_reject(best.reject_reason)] += 1

    return r


def band_note() -> str:
    return (
        f"발행 가능한 손절폭 {PUBLISHABLE_SL_MIN:.2f}%~{PUBLISHABLE_SL_MAX:.3f}% · "
        f"그러려면 ATR% 가 {PUBLISHABLE_ATR_MIN:.3f}%~{PUBLISHABLE_ATR_MAX:.3f}% "
        f"(손절 하한 {SL_MIN_PCT}% · 상한 {SL_MAX_PCT}%)"
    )
