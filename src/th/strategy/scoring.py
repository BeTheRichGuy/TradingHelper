"""신뢰도 점수 (02 문서 6절).

    score = clamp(base + 세션가감 + 펀딩보정, 0, 100)

**점수는 승률이 아니다.** 조건을 몇 개나 만족했는지일 뿐이고, 승률은 백테스트에서만
나온다. UI 에서 둘을 나란히 놓되 절대 같은 것으로 표시하지 않는다.

문서와 한 군데 다른 점: 02 문서 2.3의 'OI 해석 ±15' 는 점수에 반영하지 않았다.
S1-6·S3-4 가 이미 같은 것을 조건으로 채점하고 있어 이중 계산이 되기 때문이다.
OI 해석은 화면에 문장으로만 보여준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..sessions.models import SessionContext
from .features import Features
from .setups.base import SetupResult

MIN_PUBLISH_SCORE = 55
BAND_WEAK = 55
BAND_NORMAL = 70
BAND_STRONG = 85

FUNDING_EXTREME = 0.0005        # 0.05%
FUNDING_PENALTY = -10

# 선택 조건 가중치의 몇 %를 실제로 볼 수 있어야 점수를 신뢰하는가.
#
# 점수는 '볼 수 있었던 조건 중 몇 %를 만족했나'이므로, 볼 수 있었던 게 20%뿐이면
# 그 점수는 20%짜리 표본에서 나온 값이다. 그걸 100%짜리와 같은 선에서 발행하면
# 데이터가 끊긴 날 신호가 오히려 쉽게 나온다.
MIN_COVERAGE = 0.60


@dataclass(frozen=True, slots=True)
class ScoreAdjustment:
    source: str
    points: int
    reason_ko: str


@dataclass(frozen=True, slots=True)
class Score:
    base: int
    final: int
    adjustments: tuple[ScoreAdjustment, ...] = field(default_factory=tuple)
    coverage: float = 1.0
    """선택 조건 가중치 중 판단이 가능했던 비율."""

    @property
    def coverage_ok(self) -> bool:
        return self.coverage >= MIN_COVERAGE

    @property
    def band(self) -> str:
        if self.final >= BAND_STRONG:
            return "강함"
        if self.final >= BAND_NORMAL:
            return "보통"
        if self.final >= BAND_WEAK:
            return "약함"
        return "미달"

    @property
    def publishable(self) -> bool:
        return self.final >= MIN_PUBLISH_SCORE and self.coverage_ok

    @property
    def session_adj(self) -> int:
        return sum(a.points for a in self.adjustments if a.source == "session")

    @property
    def other_adj(self) -> int:
        return sum(a.points for a in self.adjustments if a.source != "session")


def clamp(v: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, v))


def oi_reading(f: Features, direction: str) -> str | None:
    """가격 × OI 해석표 (02 문서 2.3). 점수에는 반영하지 않고 문장만 만든다."""
    doi = f.perp.oi_change_15m
    if doi is None or not f.bars_5m or len(f.bars_5m) < 2:
        return None
    price_up = f.bars_5m[-1].close > f.bars_5m[-2].close
    oi_up = doi > 0
    if price_up and oi_up:
        return "가격↑ OI↑ — 신규 롱 유입, 추세 지속 가능성"
    if price_up and not oi_up:
        return "가격↑ OI↓ — 숏 커버링, 연료 소진 가능성"
    if not price_up and oi_up:
        return "가격↓ OI↑ — 신규 숏 유입, 하락 지속 가능성"
    return "가격↓ OI↓ — 롱 청산 마무리, 반등 임박 가능성"


def score_setup(
    result: SetupResult,
    f: Features,
    session: SessionContext | None = None,
) -> Score:
    base = result.base_score
    adjustments: list[ScoreAdjustment] = []

    # ── 세션 가감 (05 문서 rules.yaml) ────────────────────
    if session is not None:
        for a in session.penalties:
            if a.matches(result.category):
                adjustments.append(ScoreAdjustment("session", a.points, a.reason_ko))
        for b in session.boosts:
            if b.matches(result.category):
                adjustments.append(ScoreAdjustment("session", b.points, b.reason_ko))

    # ── 펀딩 쏠림 (02 문서 5.7) ───────────────────────────
    fr = f.perp.funding_rate
    if fr is not None and abs(fr) > FUNDING_EXTREME:
        crowded_long = fr > 0
        same_side = (result.direction == "long") == crowded_long
        if same_side:
            adjustments.append(
                ScoreAdjustment(
                    "funding",
                    FUNDING_PENALTY,
                    f"펀딩률 {fr * 100:.3f}% — 같은 방향 쏠림이라 청산 사냥 대상이 됩니다",
                )
            )

    final = clamp(base + sum(a.points for a in adjustments))
    return Score(
        base=base,
        final=final,
        adjustments=tuple(adjustments),
        coverage=result.coverage,
    )
