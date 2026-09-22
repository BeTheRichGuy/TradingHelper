"""뉴스 엔진 자료형.

`NewsItem` 은 소스가 무엇이든 같은 모양이다 — 텔레그램 메시지도, SEC 공시도, RSS 헤드라인도.
`Judgment`·`Plan`·`Leg` 는 Claude 구조화 출력 스키마다 (docs/17 §4). **스키마에는 숫자 범위
제약을 두지 않는다** — 범위는 `Plan.problems()` 와 가드가 확인한다. 모델이 범위를 어기면 그
판단은 버린다. 조용히 잘라서 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SETUP_ID = "news_event"          # 브래킷 setup_id — 매매기록 페이지가 이걸로 엔진 매매를 가른다

SOURCE_TELEGRAM = "telegram"
SOURCE_EDGAR = "edgar"
SOURCE_YAHOO = "yahoo"
SOURCE_GNEWS = "gnews"
SOURCE_FINNHUB = "finnhub"
SOURCES: tuple[str, ...] = (SOURCE_TELEGRAM, SOURCE_EDGAR, SOURCE_YAHOO, SOURCE_GNEWS, SOURCE_FINNHUB)

SOURCE_KO: dict[str, str] = {
    SOURCE_TELEGRAM: "텔레그램",
    SOURCE_EDGAR: "SEC 공시",
    SOURCE_YAHOO: "Yahoo",
    SOURCE_GNEWS: "Google 뉴스",
    SOURCE_FINNHUB: "Finnhub",
}


def now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class NewsItem:
    """수집한 뉴스 한 건. 불변."""

    source: str
    source_id: str
    ts: datetime                      # 발행 시각 (UTC, aware)
    title: str
    body: str = ""
    url: str = ""
    channel: str = ""                 # 텔레그램 채널명 · 매체명
    symbols: tuple[str, ...] = ()     # 유니버스 심볼 (…USDT). 비어 있을 수 있다
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("NewsItem.ts 는 tz-aware(UTC) 여야 합니다")
        if not self.title.strip():
            raise ValueError("NewsItem.title 이 비어 있습니다")

    @property
    def text(self) -> str:
        """종목 매핑·판단에 넣는 전체 텍스트."""
        return f"{self.title}\n{self.body}".strip()

    def with_symbols(self, symbols: tuple[str, ...]) -> NewsItem:
        from dataclasses import replace
        return replace(self, symbols=tuple(symbols))


# ─────────────────────────────────────────────────────────
# Claude 구조화 출력 스키마 (docs/17 §4)
# ─────────────────────────────────────────────────────────

Direction = Literal["LONG", "SHORT", "NONE"]
Magnitude = Literal["low", "medium", "high"]
EntryKind = Literal["market", "limit"]


class Leg(BaseModel):
    """분할 진입 한 다리. 마진은 시드 대비 %."""

    margin_pct: float = Field(description="시드 대비 마진 % (예: 4 = 시드의 4%). 다리 합계는 상한 이하")
    entry: EntryKind = Field(description="market 은 즉시, limit 은 현재가 대비 오프셋 지정가")
    limit_offset_pct: float | None = Field(
        default=None,
        description="entry=limit 일 때 현재가 대비 %. 롱 눌림 매수는 음수(예: -1.5), 숏 되돌림 매도는 양수",
    )
    expire_hours: float = Field(description="지정가가 이 시간 안에 안 붙으면 취소. market 은 0.1")


class Plan(BaseModel):
    """작전 계획. 손절·목표는 **가격 기준 %** 다 (마진 기준이 아니다)."""

    stop_pct: float = Field(description="가격 기준 손절폭 %. 1~5 사이여야 한다")
    target_pct: float = Field(description="가격 기준 1차 목표 %. 10배면 마진 +20%~+100% = 가격 +2%~+10%")
    tp2_pct: float | None = Field(default=None, description="2차 목표 (가격 %). 없으면 null")
    max_hold_hours: int = Field(description="최대 보유 시간. 1~168")
    legs: list[Leg] = Field(description="진입 다리 1~3개")
    invalidation_ko: str = Field(description="어떤 일이 생기면 이 계획이 틀린 것인가 (한국어 한 문장)")

    def problems(self, *, margin_max: float, stop_min: float, stop_max: float,
                 hold_max: int) -> list[str]:
        """범위 위반 목록. 비어 있으면 통과. 엔진은 하나라도 있으면 이 계획을 쓰지 않는다."""
        bad: list[str] = []
        if not (1 <= len(self.legs) <= 3):
            bad.append(f"다리 수 {len(self.legs)} — 1~3 이어야 한다")
        total = sum(l.margin_pct for l in self.legs)
        if total <= 0 or total > margin_max + 1e-9:
            bad.append(f"마진 합계 {total:g}% — 0 초과 {margin_max:g}% 이하여야 한다")
        for i, l in enumerate(self.legs, 1):
            if l.margin_pct <= 0:
                bad.append(f"{i}번 다리 마진 {l.margin_pct:g}% — 0 이하")
            if l.entry == "limit" and l.limit_offset_pct is None:
                bad.append(f"{i}번 다리 지정가인데 오프셋이 없다")
            elif l.entry == "limit" and abs(float(l.limit_offset_pct)) >= self.stop_pct - 0.25:
                # 손절은 현재가 기준 하나다. 지정가가 손절 근처면 체결 즉시 손절이거나 손절이 진입 위에 온다
                bad.append(f"{i}번 다리 오프셋 {l.limit_offset_pct:g}% 가 손절폭 {self.stop_pct:g}% 안에 있지 않다")
            if l.expire_hours <= 0:
                bad.append(f"{i}번 다리 만료 {l.expire_hours:g}h — 0 이하")
        if not (stop_min - 1e-9 <= self.stop_pct <= stop_max + 1e-9):
            bad.append(f"손절 {self.stop_pct:g}% — {stop_min:g}~{stop_max:g}% 밖")
        if self.target_pct <= 0:
            bad.append(f"목표 {self.target_pct:g}% — 0 이하")
        if self.target_pct < self.stop_pct:
            bad.append(f"목표 {self.target_pct:g}% 가 손절 {self.stop_pct:g}% 보다 작다 (손익비 < 1)")
        if self.tp2_pct is not None and self.tp2_pct <= self.target_pct:
            bad.append("2차 목표가 1차 목표 이하")
        if not (1 <= self.max_hold_hours <= hold_max):
            bad.append(f"최대 보유 {self.max_hold_hours}h — 1~{hold_max} 밖")
        return bad

    @property
    def margin_total_pct(self) -> float:
        return sum(l.margin_pct for l in self.legs)


class Judgment(BaseModel):
    """Claude 본판단 출력. `plan` 은 direction 이 LONG/SHORT 이고 매매할 만할 때만."""

    relevant: bool = Field(description="이 뉴스가 유니버스 종목 가격에 영향을 줄 만한가")
    symbols: list[str] = Field(description="영향받는 유니버스 심볼 (…USDT). 유니버스 밖 심볼은 넣지 않는다")
    direction: Direction = Field(description="LONG | SHORT | NONE. 확신이 없으면 NONE")
    confidence: float = Field(description="0~1. 방향에 대한 확신")
    magnitude: Magnitude = Field(description="가격 영향 크기")
    horizon_hours: int = Field(description="영향이 이어질 시간 (1~168)")
    thesis_ko: str = Field(description="왜 이 방향인가. 한국어 3문장 이내")
    risks_ko: list[str] = Field(description="계획을 망칠 수 있는 것들. 한국어")
    already_priced_in: bool = Field(description="뉴스 시각 이후 가격이 이미 그 방향으로 움직여 늦었으면 true")
    plan: Plan | None = Field(default=None, description="매매 계획. direction=NONE 이거나 already_priced_in 이면 null")

    @property
    def primary_symbol(self) -> str | None:
        return self.symbols[0] if self.symbols else None

    def actionable(self, min_confidence: float) -> tuple[bool, str]:
        """(매매로 갈 수 있나, 아니면 이유)."""
        if not self.relevant:
            return False, "관련 없음"
        if self.direction == "NONE":
            return False, "방향 없음"
        if not self.symbols:
            return False, "종목 없음"
        if self.already_priced_in:
            return False, "이미 반영됨"
        if self.confidence < min_confidence:
            return False, f"확신도 {self.confidence:.2f} < {min_confidence:.2f}"
        if self.plan is None:
            return False, "계획 없음"
        return True, ""


class Triage(BaseModel):
    """트리아지(저가 모델) 출력. 본판단으로 보낼지만 정한다."""

    relevant: bool = Field(description="유니버스 종목 가격에 영향을 줄 만한 뉴스인가")
    magnitude: Magnitude = Field(description="영향 크기. low 면 본판단으로 보내지 않는다")
    symbols: list[str] = Field(description="영향받는 유니버스 심볼 (…USDT)")
    reason_ko: str = Field(description="한 줄 이유 (한국어)")
