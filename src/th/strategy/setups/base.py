"""셋업 공통 규약.

조건 하나하나가 **사람이 읽을 문장**을 갖는다. 근거 없는 점수는 표시하지 않는다는
원칙(01 문서 7절) 때문이다. 미충족 조건도 그대로 남겨서, 사용자가 "왜 이 정도
점수인지"를 되짚을 수 있어야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ..features import Features

Direction = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class Condition:
    id: str
    label: str
    passed: bool
    detail: str
    required: bool = False
    weight: int = 0
    available: bool = True
    """입력 데이터가 있어서 **판단 자체가 가능했는가.**

    `passed=False, available=False` 는 "조건을 못 만족했다"가 아니라 "볼 수가
    없었다"는 뜻이다. 둘을 섞으면, 데이터가 없던 과거 구간에서 점수가 조용히
    0에 수렴한다 — 실제로 그랬다. OI·펀딩·basis 를 수집하기 전 구간에서
    S1 의 최대 획득 가능 점수가 115점 중 80점(70점 환산)이었고, 발행 기준 55점을
    넘으려면 남은 조건을 거의 다 맞춰야 했다.
    """

    def as_evidence(self) -> dict:
        """03 문서 5.2 의 evidence 스키마 그대로."""
        return {
            "id": self.id,
            "label": self.label,
            "required": self.required,
            "weight": self.weight,
            "passed": self.passed,
            "available": self.available,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class EntryZone:
    low: float
    high: float

    def reference(self, direction: Direction) -> float:
        """기준 진입가 — 최악의 체결을 가정한다 (02 문서 5.1).

        롱을 구간 상단에서 잡았다고 보면 1R 이 커지고, 포지션은 작아지고
        목표는 멀어진다. 셋 다 보수적인 방향이다.
        """
        return self.high if direction == "long" else self.low


@dataclass(frozen=True, slots=True)
class SetupResult:
    setup_id: str
    category: str                       # trend | breakout | reversal | mean_reversion
    direction: Direction
    conditions: tuple[Condition, ...]
    entry: EntryZone | None = None
    note: str = ""

    # ── 판정 ─────────────────────────────────────────────
    @property
    def required(self) -> tuple[Condition, ...]:
        return tuple(c for c in self.conditions if c.required)

    @property
    def optional(self) -> tuple[Condition, ...]:
        return tuple(c for c in self.conditions if not c.required)

    @property
    def required_ok(self) -> bool:
        return all(c.passed for c in self.required)

    @property
    def failed_required(self) -> tuple[Condition, ...]:
        return tuple(c for c in self.required if not c.passed)

    @property
    def evaluable(self) -> tuple[Condition, ...]:
        """선택 조건 중 **판단이 가능했던** 것만."""
        return tuple(c for c in self.optional if c.available)

    @property
    def raw_score(self) -> int:
        return sum(c.weight for c in self.optional if c.passed)

    @property
    def max_score(self) -> int:
        """분모 — 볼 수 있었던 조건의 가중치 합.

        볼 수 없었던 조건을 분모에 넣으면 '데이터가 없다'가 '조건을 못 맞췄다'로
        둔갑한다. 분자에도 넣지 않으므로 통과로 쳐주는 것도 아니다.
        """
        return sum(c.weight for c in self.evaluable)

    @property
    def total_weight(self) -> int:
        return sum(c.weight for c in self.optional)

    @property
    def coverage(self) -> float:
        """선택 조건 가중치 중 몇 %를 실제로 볼 수 있었나."""
        total = self.total_weight
        return self.max_score / total if total > 0 else 0.0

    @property
    def base_score(self) -> int:
        """필수 조건은 분모에 넣지 않는다 (02 문서 6절)."""
        if self.max_score <= 0:
            return 0
        return round(self.raw_score / self.max_score * 100)

    @property
    def valid(self) -> bool:
        return self.required_ok and self.entry is not None


@runtime_checkable
class Setup(Protocol):
    setup_id: str
    category: str
    regimes: tuple[str, ...]

    def evaluate(self, f: Features, direction: Direction) -> SetupResult: ...


# ── 조건을 만들 때 쓰는 작은 도우미 ─────────────────────────

def cond(
    cid: str,
    label: str,
    passed: bool | None,
    detail: str,
    *,
    required: bool = False,
    weight: int = 0,
) -> Condition:
    """데이터가 없어 판단 불가(None)면 **미충족이면서 '판단 불가'로 표시**한다.

    모르는 것을 통과로 처리하면 데이터가 빠진 날 신호가 과하게 나온다. 그래서
    `passed=False` 는 유지한다. 다만 `available=False` 를 함께 달아, 점수를 낼 때
    분모에서도 빼도록 한다 — **필수 조건이면 여전히 탈락이다.**
    """
    if passed is None:
        return Condition(
            cid, label, False, f"{detail} (데이터 없음)", required, weight, available=False
        )
    return Condition(cid, label, bool(passed), detail, required, weight)


def pct(v: float | None, digits: int = 2) -> str:
    return "-" if v is None else f"{v:.{digits}f}%"


def num(v: float | None, digits: int = 2) -> str:
    return "-" if v is None else f"{v:,.{digits}f}"
