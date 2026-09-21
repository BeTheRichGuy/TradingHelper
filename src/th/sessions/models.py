"""세션 게이트가 주고받는 자료형.

전략 엔진은 이 SessionContext 하나만 보고 "지금 평가해도 되는가"를 판단한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class Regime(str, Enum):
    """미국 시장 기준 4대 레짐. 05-세션-이벤트-타임맵 2절."""

    PRE = "PRE"        # 🟡 프리장   04:00–09:30 ET
    RTH = "RTH"        # 🟢 본장     09:30–16:00 ET
    POST = "POST"      # 🟠 애프터   16:00–20:00 ET
    CLOSED = "CLOSED"  # 🔴 언더라잉 정지 (그 외 전부 · 주말 · 공휴일)

    @property
    def emoji(self) -> str:
        return {"PRE": "🟡", "RTH": "🟢", "POST": "🟠", "CLOSED": "🔴"}[self.value]

    @property
    def label_ko(self) -> str:
        return {
            "PRE": "프리장",
            "RTH": "본장",
            "POST": "애프터",
            "CLOSED": "언더라잉 정지",
        }[self.value]


Action = Literal["BLOCK", "PENALTY", "BOOST", "WARN"]


@dataclass(frozen=True, slots=True)
class Block:
    """신호 발행 금지 사유."""

    rule_id: str
    reason_ko: str


@dataclass(frozen=True, slots=True)
class Adjustment:
    """신뢰도 가감. applies_to 가 비어 있으면 모든 셋업에 적용."""

    rule_id: str
    action: Literal["PENALTY", "BOOST"]
    points: int                      # PENALTY 는 음수, BOOST 는 양수
    applies_to: tuple[str, ...]
    reason_ko: str

    def matches(self, setup: str | None) -> bool:
        if not self.applies_to:
            return True
        if setup is None:
            return False
        return setup in self.applies_to


@dataclass(frozen=True, slots=True)
class Event:
    """캘린더 이벤트 한 건."""

    name: str
    ts_utc: datetime
    kind: str                        # fomc | econ | earnings
    importance: str = "medium"       # high | medium | low
    symbols: tuple[str, ...] = ()
    note: str = ""
    source: str = "manual"           # manual | fred | yfinance | finnhub

    def seconds_from(self, at: datetime) -> float:
        return (self.ts_utc - at).total_seconds()

    @property
    def time_estimated(self) -> bool:
        """발표 시각이 소스에 없어 추정한 값인가."""
        return "time_estimated" in self.note


@dataclass(frozen=True, slots=True)
class SessionContext:
    """전략 엔진이 셋업 평가 전에 조회하는 상태."""

    symbol: str
    at: datetime                     # UTC
    regime: Regime
    regime_ends_at: datetime | None
    is_trading_day: bool
    early_close: bool

    blocked: bool
    blocks: tuple[Block, ...] = ()
    penalties: tuple[Adjustment, ...] = ()
    boosts: tuple[Adjustment, ...] = ()

    next_event: Event | None = None
    warnings: tuple[str, ...] = ()

    # ── 파생 ─────────────────────────────────────────────
    @property
    def regime_ends_in_sec(self) -> int | None:
        if self.regime_ends_at is None:
            return None
        return max(0, int((self.regime_ends_at - self.at).total_seconds()))

    @property
    def next_event_in_sec(self) -> int | None:
        if self.next_event is None:
            return None
        return int(self.next_event.seconds_from(self.at))

    def adjustment_for(self, setup: str | None) -> int:
        """해당 셋업에 적용할 신뢰도 가감 합계."""
        total = 0
        for a in self.penalties:
            if a.matches(setup):
                total += a.points
        for b in self.boosts:
            if b.matches(setup):
                total += b.points
        return total

    def reasons_ko(self) -> list[str]:
        """UI 근거 패널에 그대로 뿌릴 문장들."""
        out = [b.reason_ko for b in self.blocks]
        out += [f"{a.reason_ko} ({a.points:+d})" for a in self.penalties]
        out += [f"{a.reason_ko} ({a.points:+d})" for a in self.boosts]
        return out

    def summary_ko(self) -> str:
        head = f"{self.regime.emoji} {self.regime.label_ko}"
        if self.regime_ends_in_sec is not None:
            m = self.regime_ends_in_sec // 60
            head += f" · 남은시간 {m // 60}h{m % 60:02d}m"
        if self.blocked:
            head += " · 신호 차단"
        return head
