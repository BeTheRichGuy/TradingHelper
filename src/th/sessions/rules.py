"""rules.yaml 로더와 창(window) 매칭.

규칙은 데이터고, 이 파일은 그 데이터를 시각과 대조하는 기계일 뿐이다.
새 규칙이 필요하면 여기가 아니라 rules.yaml 을 고친다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from ..logging_setup import get_logger
from .market import NY, DayWindows, SessionCalendar, et_time_on, nth_weekday_of_month
from .models import Action, Regime

log = get_logger(__name__)

RULES_PATH = Path(__file__).with_name("rules.yaml")

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
SETUP_CATEGORIES = ("trend", "breakout", "reversal", "mean_reversion")

# 02 문서의 셋업 ID → 분류. 규칙의 applies_to 는 분류로 쓴다.
SETUP_CATEGORY: dict[str, str] = {
    "S1_vwap_pullback": "trend",
    "S2_opening_range_breakout": "breakout",
    "S3_sweep_reversal": "reversal",
    "S4_band_mean_reversion": "mean_reversion",
}


def category_of(setup: str | None) -> str | None:
    """셋업 ID 든 분류든 받아서 분류로 정규화."""
    if setup is None:
        return None
    if setup in SETUP_CATEGORIES:
        return setup
    return SETUP_CATEGORY.get(setup)


class RuleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Window:
    kind: Literal["absolute", "anchored", "all_day"]
    start: time | None = None
    end: time | None = None
    anchor: str | None = None          # open | close | post_end
    start_min: int = 0
    end_min: int = 0

    def resolve(self, w: DayWindows) -> tuple[datetime, datetime]:
        """거래일 경계를 받아 실제 (시작, 끝) UTC 로 펼친다."""
        if self.kind == "all_day":
            return w.pre_start, w.post_end
        if self.kind == "absolute":
            day_et = w.market_open.astimezone(NY).date()
            return et_time_on(day_et, self.start), et_time_on(day_et, self.end)
        base = {
            "open": w.market_open,
            "close": w.market_close,
            "post_end": w.post_end,
        }[self.anchor]
        return base + timedelta(minutes=self.start_min), base + timedelta(
            minutes=self.end_min
        )


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    action: Action
    reason_ko: str
    window: Window
    points: int = 0
    sessions: tuple[Regime, ...] = ()
    symbols: tuple[str, ...] = ()
    applies_to: tuple[str, ...] = ()
    weekdays: tuple[int, ...] = ()
    months: tuple[int, ...] = ()
    nth_weekday: tuple[int, int] | None = None      # (weekday, n)
    last_trading_days: int | None = None
    on_event_kind: str | None = None

    # ── 판정 ─────────────────────────────────────────────
    def day_matches(
        self,
        day: date,
        cal: SessionCalendar,
        event_kinds_today: frozenset[str],
    ) -> bool:
        if self.weekdays and day.weekday() not in self.weekdays:
            return False
        if self.months and day.month not in self.months:
            return False
        if self.nth_weekday is not None:
            wd, n = self.nth_weekday
            if day != nth_weekday_of_month(day.year, day.month, wd, n):
                return False
        if self.last_trading_days is not None:
            if not cal.is_last_n_trading_days_of_month(day, self.last_trading_days):
                return False
        if self.on_event_kind and self.on_event_kind not in event_kinds_today:
            return False
        return True

    def symbol_matches(self, symbol: str) -> bool:
        return not self.symbols or symbol.upper() in self.symbols

    def session_matches(self, regime: Regime) -> bool:
        return not self.sessions or regime in self.sessions

    def time_matches(self, at: datetime, w: DayWindows) -> bool:
        start, end = self.window.resolve(w)
        return start <= at < end


# ── 파싱 ───────────────────────────────────────────────────

def _parse_window(raw: dict[str, Any], rule_id: str) -> Window:
    if raw.get("all_day"):
        return Window(kind="all_day")
    if "anchor" in raw:
        anchor = raw["anchor"]
        if anchor not in ("open", "close", "post_end"):
            raise RuleError(f"[{rule_id}] anchor 는 open/close/post_end 중 하나여야 합니다: {anchor}")
        return Window(
            kind="anchored",
            anchor=anchor,
            start_min=int(raw.get("start_min", 0)),
            end_min=int(raw.get("end_min", 0)),
        )
    if "start" in raw and "end" in raw:
        return Window(
            kind="absolute",
            start=_parse_time(raw["start"], rule_id),
            end=_parse_time(raw["end"], rule_id),
        )
    raise RuleError(f"[{rule_id}] window 형식을 알 수 없습니다: {raw}")


def _parse_time(v: Any, rule_id: str) -> time:
    if isinstance(v, time):
        return v
    try:
        hh, mm = str(v).split(":")
        return time(int(hh), int(mm))
    except Exception as e:
        raise RuleError(f"[{rule_id}] 시각 형식 오류 '{v}' (HH:MM 이어야 함)") from e


def _parse_weekdays(raw: Any, rule_id: str) -> tuple[int, ...]:
    if not raw:
        return ()
    out = []
    for w in raw:
        key = str(w).lower()[:3]
        if key not in WEEKDAYS:
            raise RuleError(f"[{rule_id}] 요일 이름 오류: {w}")
        out.append(WEEKDAYS[key])
    return tuple(out)


def parse_rule(raw: dict[str, Any]) -> Rule:
    rid = raw.get("id")
    if not rid:
        raise RuleError(f"id 없는 규칙: {raw}")
    action = raw.get("action")
    if action not in ("BLOCK", "PENALTY", "BOOST", "WARN"):
        raise RuleError(f"[{rid}] action 오류: {action}")
    if action in ("PENALTY", "BOOST") and "points" not in raw:
        raise RuleError(f"[{rid}] {action} 에는 points 가 필요합니다")

    points = int(raw.get("points", 0))
    if action == "PENALTY" and points > 0:
        raise RuleError(f"[{rid}] PENALTY 의 points 는 음수여야 합니다 ({points})")
    if action == "BOOST" and points < 0:
        raise RuleError(f"[{rid}] BOOST 의 points 는 양수여야 합니다 ({points})")

    applies_to = tuple(raw.get("applies_to") or ())
    for c in applies_to:
        if c not in SETUP_CATEGORIES:
            raise RuleError(f"[{rid}] 알 수 없는 셋업 분류: {c}")

    nth = raw.get("nth_weekday")
    nth_parsed = None
    if nth:
        key = str(nth["weekday"]).lower()[:3]
        if key not in WEEKDAYS:
            raise RuleError(f"[{rid}] nth_weekday 요일 오류: {nth['weekday']}")
        nth_parsed = (WEEKDAYS[key], int(nth["n"]))

    return Rule(
        id=rid,
        action=action,
        reason_ko=raw.get("reason_ko", ""),
        window=_parse_window(raw.get("window") or {}, rid),
        points=points,
        sessions=tuple(Regime(s) for s in (raw.get("sessions") or ())),
        symbols=tuple(s.upper() for s in (raw.get("symbols") or ())),
        applies_to=applies_to,
        weekdays=_parse_weekdays(raw.get("weekdays"), rid),
        months=tuple(int(m) for m in (raw.get("months") or ())),
        nth_weekday=nth_parsed,
        last_trading_days=(
            int(raw["last_trading_days"]) if raw.get("last_trading_days") else None
        ),
        on_event_kind=raw.get("on_event_kind"),
    )


def load_rules(path: Path | None = None) -> list[Rule]:
    p = path or RULES_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw_rules = data.get("rules") or []
    rules = [parse_rule(r) for r in raw_rules]

    seen: set[str] = set()
    for r in rules:
        if r.id in seen:
            raise RuleError(f"규칙 id 중복: {r.id}")
        seen.add(r.id)

    log.debug("규칙 %d개 로드", len(rules))
    return rules


@lru_cache(maxsize=4)
def get_rules(path_str: str | None = None) -> tuple[Rule, ...]:
    return tuple(load_rules(Path(path_str) if path_str else None))
