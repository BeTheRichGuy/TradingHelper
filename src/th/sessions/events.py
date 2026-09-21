"""캘린더 이벤트 로딩.

두 파일을 읽는다.

    data/events.yaml            손으로 적은 것 — 항상 최우선
    data/events.generated.yaml  `th calendar sync` 가 만든 자동 수집 결과

게이트(`get_context`)는 동기 함수라 DB를 못 읽는다. 그래서 자동 수집 결과를
**파일 스냅샷**으로 남기고 그걸 읽는다. 덕분에 네트워크가 끊겨도, DB가 죽어도
게이트는 마지막으로 알던 일정으로 계속 돈다.

가장 위험한 건 이벤트가 없는 것이 아니라 **낡은 일정으로 조용히 도는 것**이므로,
스냅샷이 오래됐거나 없으면 경고를 만들어 화면에 띄운다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..logging_setup import get_logger
from .market import NY, et_time_on
from .models import Block, Event

log = get_logger(__name__)

DATA_DIR = Path(__file__).parent / "data"
MANUAL_PATH = DATA_DIR / "events.yaml"
GENERATED_PATH = DATA_DIR / "events.generated.yaml"

# importance=high 이벤트 전후 몇 분을 차단할지
HIGH_IMPACT_BEFORE = timedelta(minutes=10)
HIGH_IMPACT_AFTER = timedelta(minutes=10)

# 자동 수집 스냅샷이 이만큼 지나면 "오래됨" 경고
STALE_AFTER = timedelta(hours=24)


class EventError(ValueError):
    pass


# ─────────────────────────────────────────────────────────
# 자료형
# ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SourceStatus:
    name: str
    ok: bool
    count: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class EventStore:
    events: tuple[Event, ...]
    synced_at: datetime | None
    sources: tuple[SourceStatus, ...] = ()
    has_generated: bool = False

    def age(self, at: datetime) -> timedelta | None:
        if self.synced_at is None:
            return None
        return at - self.synced_at


# ─────────────────────────────────────────────────────────
# 파싱
# ─────────────────────────────────────────────────────────

def parse_note(note: str) -> dict[str, str]:
    """'warn_days_before=3,time_estimated' → {'warn_days_before': '3', 'time_estimated': ''}"""
    out: dict[str, str] = {}
    for part in (note or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            out[part] = ""
    return out


def _parse_time_et(v: Any) -> time:
    """'14:00' → 14:00. 따옴표 없이 적은 `time_et: 14:00` 은 YAML 이 60진수로 읽어
    **정수 840** 이 되므로 그것도 받는다 (840 = 14×60 + 0)."""
    if v is None:
        return time(9, 30)
    if isinstance(v, time):
        return v
    if isinstance(v, bool):
        raise EventError(f"time_et 오류: {v!r}")
    if isinstance(v, int):
        hh, mm = divmod(v, 60)
        return time(hh, mm)
    hh, mm = str(v).split(":")
    return time(int(hh), int(mm))


def _parse_event(raw: dict[str, Any], default_source: str = "manual") -> Event:
    try:
        d = raw["date"]
        if isinstance(d, datetime):
            d = d.date()
        elif isinstance(d, str):
            d = date.fromisoformat(d)
        ts = et_time_on(d, _parse_time_et(raw.get("time_et")))
    except Exception as e:
        raise EventError(f"이벤트 날짜/시각 오류: {raw}") from e

    kind = raw.get("kind", "econ")
    if kind not in ("fomc", "econ", "earnings"):
        raise EventError(f"알 수 없는 이벤트 종류: {kind} ({raw.get('name')})")

    importance = raw.get("importance", "medium")
    if importance not in ("high", "medium", "low"):
        raise EventError(f"importance 오류: {importance}")

    note = str(raw.get("note") or "")
    if raw.get("warn_days_before") and "warn_days_before" not in note:
        extra = f"warn_days_before={int(raw['warn_days_before'])}"
        note = f"{note},{extra}" if note else extra

    return Event(
        name=raw.get("name") or kind,
        ts_utc=ts,
        kind=kind,
        importance=importance,
        symbols=tuple(s.upper() for s in (raw.get("symbols") or ())),
        note=note,
        source=raw.get("source") or default_source,
    )


def event_to_dict(e: Event) -> dict[str, Any]:
    et = e.ts_utc.astimezone(NY)
    return {
        "name": e.name,
        "kind": e.kind,
        "date": et.date().isoformat(),
        "time_et": f"{et:%H:%M}",
        "importance": e.importance,
        "symbols": list(e.symbols),
        "note": e.note,
        "source": e.source,
    }


# ─────────────────────────────────────────────────────────
# 로딩
# ─────────────────────────────────────────────────────────

def load_manual(path: Path | None = None) -> list[Event]:
    p = path or Path(os.environ.get("TH_EVENTS_FILE") or MANUAL_PATH)
    if not p.exists():
        log.warning("수동 이벤트 파일이 없습니다: %s", p)
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    # 한 항목이 잘못됐다고 캘린더 전체를 버리면 안 된다 — 그러면 나머지 FOMC·실적
    # 일정까지 게이트에서 사라져 **조용히** 위험한 시간에 신호가 나간다.
    events: list[Event] = []
    for raw in data.get("events") or []:
        try:
            events.append(_parse_event(raw, "manual"))
        except EventError as e:
            log.warning("수동 이벤트 항목 하나를 건너뜁니다 (%s): %s", p.name, e)
    return events


def load_generated(path: Path | None = None) -> EventStore:
    p = path or GENERATED_PATH
    if not p.exists():
        return EventStore(events=(), synced_at=None, has_generated=False)

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    synced_at = None
    raw_synced = data.get("synced_at")
    if raw_synced:
        try:
            synced_at = datetime.fromisoformat(str(raw_synced).replace("Z", "+00:00"))
            if synced_at.tzinfo is None:
                synced_at = synced_at.replace(tzinfo=UTC)
        except ValueError:
            log.warning("synced_at 형식 오류: %s", raw_synced)

    statuses = tuple(
        SourceStatus(
            name=str(s.get("name", "?")),
            ok=bool(s.get("ok")),
            count=int(s.get("count", 0)),
            detail=str(s.get("detail", "")),
        )
        for s in (data.get("sources") or [])
    )

    events: list[Event] = []
    for raw in data.get("events") or []:
        try:
            events.append(_parse_event(raw, raw.get("source", "auto")))
        except EventError as e:
            log.warning("자동 수집 항목 하나를 건너뜁니다: %s", e)

    return EventStore(
        events=tuple(events),
        synced_at=synced_at,
        sources=statuses,
        has_generated=True,
    )


def write_generated(
    events: list[Event],
    statuses: list[SourceStatus],
    path: Path | None = None,
    synced_at: datetime | None = None,
) -> Path:
    p = path or GENERATED_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "synced_at": (synced_at or datetime.now(UTC)).isoformat(),
        "sources": [
            {"name": s.name, "ok": s.ok, "count": s.count, "detail": s.detail}
            for s in statuses
        ],
        "events": [event_to_dict(e) for e in events],
    }
    header = (
        "# 자동 생성 파일 — 직접 고치지 마세요. `th calendar sync` 가 덮어씁니다.\n"
        "# 손으로 넣을 이벤트는 events.yaml 에 적으면 이 파일보다 우선합니다.\n"
    )
    p.write_text(
        header + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return p


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


@lru_cache(maxsize=8)
def _store_cached(_manual_mtime: float, _generated_mtime: float) -> EventStore:
    from .sources.merge import merge_events

    manual = load_manual()
    gen = load_generated()
    # 우선순위는 각 이벤트의 source 로 결정된다 (manual 100 > fred 50 > yfinance 30)
    merged = merge_events([*manual, *gen.events])
    return EventStore(
        events=tuple(merged),
        synced_at=gen.synced_at,
        sources=gen.sources,
        has_generated=gen.has_generated,
    )


def get_store() -> EventStore:
    return _store_cached(_mtime(MANUAL_PATH), _mtime(GENERATED_PATH))


def get_events(path_str: str | None = None) -> tuple[Event, ...]:
    """게이트가 쓰는 진입점. 수동 + 자동을 병합한 목록."""
    if path_str:                      # 테스트에서 특정 파일만 쓰고 싶을 때
        return tuple(load_manual(Path(path_str)))
    return get_store().events


def load_events(path: Path | None = None) -> list[Event]:
    """하위 호환용 — 수동 파일만."""
    return load_manual(path)


def clear_cache() -> None:
    _store_cached.cache_clear()


# ─────────────────────────────────────────────────────────
# 조회
# ─────────────────────────────────────────────────────────

def _warn_days(ev: Event) -> int:
    v = parse_note(ev.note).get("warn_days_before")
    try:
        return int(v) if v else 0
    except ValueError:
        return 0


def events_on(day_et: date, events: tuple[Event, ...]) -> list[Event]:
    return [e for e in events if e.ts_utc.astimezone(NY).date() == day_et]


def kinds_on(day_et: date, events: tuple[Event, ...]) -> frozenset[str]:
    return frozenset(e.kind for e in events_on(day_et, events))


def next_event_after(at: datetime, events: tuple[Event, ...]) -> Event | None:
    """다음 이벤트 하나 (events 는 시각 오름차순).

    종목이 명시된 이벤트라도 걸러내지 않는다. SOXL 은 SOXX 구성종목 전체의
    영향을 받으므로 NVDA 실적은 SOXLUSDT 를 보는 사람에게도 그대로 이벤트다.
    """
    for e in events:
        if e.ts_utc > at:
            return e
    return None


def high_impact_blocks(at: datetime, events: tuple[Event, ...]) -> list[Block]:
    """importance=high 이벤트 전후 10분이면 차단 사유를 만든다."""
    out: list[Block] = []
    for e in events:
        if e.importance != "high":
            continue
        if e.ts_utc - HIGH_IMPACT_BEFORE <= at < e.ts_utc + HIGH_IMPACT_AFTER:
            delta = int(e.seconds_from(at) // 60)
            when = f"{abs(delta)}분 {'후' if delta >= 0 else '전 발표'}"
            tail = " (발표 시각은 추정값)" if e.time_estimated else ""
            out.append(
                Block(
                    rule_id=f"event:{e.kind}",
                    reason_ko=(
                        f"{e.name} {when} — 발표 전후 10분은 "
                        f"기술적 분석이 무력합니다{tail}"
                    ),
                )
            )
    return out


def upcoming_warnings(at: datetime, events: tuple[Event, ...]) -> list[str]:
    """D-n 카운트다운 배너 문구."""
    out: list[str] = []
    today_et = at.astimezone(NY).date()
    for e in events:
        n = _warn_days(e)
        if n <= 0:
            continue
        d_et = e.ts_utc.astimezone(NY).date()
        days = (d_et - today_et).days
        if 0 <= days <= n:
            label = "오늘" if days == 0 else f"D-{days}"
            out.append(f"{e.name} {label}")
    return out


def freshness_warnings(at: datetime, store: EventStore | None = None) -> list[str]:
    """자동 수집이 낡았거나 실패했으면 알린다.

    이벤트가 '없는 것'과 '모르는 것'은 완전히 다르다. 조용히 낡은 일정으로
    도는 상태를 사용자가 알아채지 못하는 게 가장 위험하다.
    """
    store = store or get_store()
    out: list[str] = []

    if not store.has_generated:
        out.append("이벤트 자동 수집을 아직 실행하지 않았습니다 — `th calendar sync`")
        return out

    age = store.age(at)
    if age is None:
        out.append("이벤트 자동 수집 시각을 알 수 없습니다 — `th calendar sync`")
    elif age > STALE_AFTER:
        hours = int(age.total_seconds() // 3600)
        out.append(f"이벤트 정보가 {hours}시간 전 기준입니다 — `th calendar sync`")

    failed = [s.name for s in store.sources if not s.ok]
    if failed:
        out.append(f"이벤트 소스 수집 실패: {', '.join(failed)}")

    return out


def upcoming(at: datetime, events: tuple[Event, ...], days: int = 14) -> list[Event]:
    end = at + timedelta(days=days)
    return [e for e in events if at < e.ts_utc <= end]


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
