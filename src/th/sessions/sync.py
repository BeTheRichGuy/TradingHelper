"""캘린더 동기화 — 여러 소스를 긁어 하나로 합치고 스냅샷을 남긴다.

    th calendar sync

결과는 두 곳에 쓴다.
  1. `data/events.generated.yaml`  ← 게이트가 읽는 곳. 오프라인에서도 동작한다
  2. DB `calendar_events`          ← 기록·이력용. Phase 5 에서 API 가 읽는다

한 소스가 실패해도 나머지는 진행한다. 안전장치가 외부 API 때문에 멈추면 안 된다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import db
from ..config import get_settings
from ..logging_setup import get_logger
from .events import SourceStatus, clear_cache, load_manual, write_generated
from .market import NY
from .models import Event
from .sources.base import FetchResult
from .sources.earnings import EarningsSource
from .sources.fred import FredSource
from .sources.merge import merge_events, stable_id

log = get_logger(__name__)

# 과거 며칠까지 함께 담을지 — 지나간 이벤트도 백테스트·복기에 쓰인다
LOOKBACK_DAYS = 14


@dataclass(slots=True)
class SyncReport:
    started_at: datetime
    finished_at: datetime
    statuses: list[SourceStatus] = field(default_factory=list)
    total_events: int = 0
    manual_events: int = 0
    snapshot_path: Path | None = None
    db_rows: int | None = None
    db_error: str = ""

    @property
    def ok(self) -> bool:
        return any(s.ok for s in self.statuses)

    @property
    def duration_sec(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def build_sources(settings=None) -> list:
    s = settings or get_settings()
    sources = []

    if s.fred_api_key:
        sources.append(FredSource(api_key=s.fred_api_key))
    else:
        log.warning(
            "TH_FRED_API_KEY 가 없어 경제지표 수집을 건너뜁니다 "
            "(무료 키: https://fred.stlouisfed.org/docs/api/api_key.html)"
        )

    sources.append(EarningsSource(symbols=s.earnings_symbols))
    return sources


async def sync_calendar(
    days: int = 90,
    save_db: bool = True,
    snapshot_path: Path | None = None,
) -> SyncReport:
    started = datetime.now(UTC)
    s = get_settings()

    today_et = started.astimezone(NY).date()
    start = today_et - timedelta(days=LOOKBACK_DAYS)
    end = today_et + timedelta(days=days)

    sources = build_sources(s)
    log.info(
        "캘린더 동기화 %s ~ %s · 소스 %d개",
        start, end, len(sources),
    )

    results: list[FetchResult] = []
    if sources:
        gathered = await asyncio.gather(
            *(src.fetch(start, end) for src in sources), return_exceptions=True
        )
        for src, res in zip(sources, gathered, strict=True):
            if isinstance(res, BaseException):
                log.warning("%s 수집 중 예외: %s", src.name, res)
                results.append(
                    FetchResult(src.name, ok=False, detail=f"{type(res).__name__}: {res}")
                )
            else:
                results.append(res)

    manual = load_manual()
    auto: list[Event] = []
    for r in results:
        auto.extend(r.events)

    merged = merge_events([*manual, *auto])

    statuses = [
        SourceStatus(name="manual", ok=True, count=len(manual), detail="events.yaml"),
        *[
            SourceStatus(name=r.source, ok=r.ok, count=r.count, detail=r.detail)
            for r in results
        ],
    ]

    # 스냅샷에는 자동 항목만 남긴다 — 수동 항목은 events.yaml 이 원본이다
    auto_merged = [e for e in merged if e.source != "manual"]
    path = write_generated(auto_merged, statuses, path=snapshot_path, synced_at=started)
    clear_cache()

    report = SyncReport(
        started_at=started,
        finished_at=datetime.now(UTC),
        statuses=statuses,
        total_events=len(merged),
        manual_events=len(manual),
        snapshot_path=path,
    )

    if save_db:
        try:
            report.db_rows = await save_events_to_db(merged, statuses, started)
        except Exception as e:
            report.db_error = _db_hint(e)
            log.warning("DB 저장 실패 (스냅샷은 정상 저장됨): %s", report.db_error)

    report.finished_at = datetime.now(UTC)
    return report


def _db_hint(e: Exception) -> str:
    text = f"{type(e).__name__}: {e}"
    if "calendar_events" in text and "does not exist" in text:
        return "calendar_events 테이블이 없습니다 — `th migrate` 를 먼저 실행하세요"
    return text


UPSERT_EVENT = """
INSERT INTO calendar_events
    (id, ts_utc, kind, name, importance, symbols, source, note, synced_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
ON CONFLICT (id) DO UPDATE SET
    ts_utc     = EXCLUDED.ts_utc,
    kind       = EXCLUDED.kind,
    name       = EXCLUDED.name,
    importance = EXCLUDED.importance,
    symbols    = EXCLUDED.symbols,
    source     = EXCLUDED.source,
    note       = EXCLUDED.note,
    synced_at  = EXCLUDED.synced_at
"""

UPSERT_STATE = """
INSERT INTO calendar_sync_state (source, last_ok_at, last_try_at, ok, count, detail)
VALUES ($1,$2, now(), $3,$4,$5)
ON CONFLICT (source) DO UPDATE SET
    last_ok_at  = COALESCE(EXCLUDED.last_ok_at, calendar_sync_state.last_ok_at),
    last_try_at = now(),
    ok          = EXCLUDED.ok,
    count       = EXCLUDED.count,
    detail      = EXCLUDED.detail
"""


async def save_events_to_db(
    events: list[Event], statuses: list[SourceStatus], synced_at: datetime
) -> int:
    rows = [
        (
            stable_id(e), e.ts_utc, e.kind, e.name, e.importance,
            list(e.symbols), e.source, e.note, synced_at,
        )
        for e in events
    ]
    async with db.acquire() as conn:
        if rows:
            await conn.executemany(UPSERT_EVENT, rows)
        await conn.executemany(
            UPSERT_STATE,
            [
                (s.name, synced_at if s.ok else None, s.ok, s.count, s.detail)
                for s in statuses
            ],
        )
    return len(rows)


async def db_sync_state() -> list[dict]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source, last_ok_at, last_try_at, ok, count, detail "
            "FROM calendar_sync_state ORDER BY source"
        )
    return [dict(r) for r in rows]

async def auto_sync_loop(stop_evt, *, hour_utc: int = 11, check_every_s: int = 600) -> None:
    """하루 1회 캘린더를 갱신하는 배경 작업 (`th stream` 안에서 돈다).

    손으로 `th calendar sync` 를 치는 방식은 **언젠가 반드시 빠뜨리고, 빠뜨린 날이
    하필 NVDA 실적일이다.** 그러면 SOXL 이 10~20% 움직이는 밤에 게이트가 아무
    경고도 못 한다. 이건 편의가 아니라 안전 문제다.

    기본값 11시 UTC = 한국 20시 = 미국 장 시작 전. 그때 한 번 돈다.
    실패해도 수집을 멈추지 않는다 — 마지막 스냅샷으로 게이트는 계속 돌아가고,
    신선도 경고가 화면에 뜬다.
    """
    from datetime import UTC, datetime

    from ..logging_setup import get_logger

    log = get_logger(__name__)
    last_day = None
    while not stop_evt.is_set():
        now = datetime.now(UTC)
        if now.hour >= hour_utc and last_day != now.date():
            try:
                res = await sync_calendar()
                log.info("캘린더 자동 동기화 완료 — 이벤트 %d건", res.total_events)
            except Exception:
                log.exception("캘린더 자동 동기화 실패 — 마지막 스냅샷으로 계속합니다")
            last_day = now.date()      # 실패해도 그날은 다시 시도하지 않는다
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=check_every_s)
        except (TimeoutError, asyncio.TimeoutError):
            pass
