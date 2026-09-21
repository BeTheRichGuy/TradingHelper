"""FRED 릴리스 캘린더 → 경제지표 발표 일정.

세인트루이스 연준이 운영하는 공식 소스다. 무료 API 키만 있으면 되고
(https://fred.stlouisfed.org/docs/api/api_key.html), **미래 발표일**도 준다.

두 가지 한계를 여기서 메운다.
  1. 발표 시각을 안 준다 → `data/econ_releases.yaml` 의 time_et 매핑으로 채운다
  2. 중요도를 안 준다   → 같은 파일의 importance 로 정한다
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any

import httpx
import yaml

from ...logging_setup import get_logger
from ..market import et_time_on
from ..models import Event
from .base import FetchResult

log = get_logger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"
WHITELIST_PATH = Path(__file__).parents[1] / "data" / "econ_releases.yaml"


@dataclass(frozen=True, slots=True)
class ReleaseRule:
    match: str
    label: str
    time_et: time
    importance: str

    def matches(self, release_name: str) -> bool:
        return self.match in release_name.lower()


def _parse_time(v: Any, default: time) -> time:
    try:
        hh, mm = str(v).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return default


def load_whitelist(path: Path | None = None) -> list[ReleaseRule]:
    p = path or WHITELIST_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    default_time = _parse_time(data.get("default_time_et", "08:30"), time(8, 30))
    default_imp = data.get("default_importance", "medium")

    out: list[ReleaseRule] = []
    for r in data.get("releases") or []:
        out.append(
            ReleaseRule(
                match=str(r["match"]).lower(),
                label=r.get("label") or r["match"],
                time_et=_parse_time(r.get("time_et"), default_time),
                importance=r.get("importance", default_imp),
            )
        )
    return out


class FredSource:
    """FRED 경제지표 캘린더."""

    name = "fred"

    def __init__(
        self,
        api_key: str,
        whitelist: list[ReleaseRule] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.whitelist = whitelist if whitelist is not None else load_whitelist()
        self.timeout = timeout

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "api_key": self.api_key, "file_type": "json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{FRED_BASE}{path}", params=params)
            r.raise_for_status()
            return r.json()

    async def list_releases(self, search: str | None = None) -> list[dict[str, Any]]:
        """등록된 릴리스 목록. 화이트리스트에 넣을 이름을 찾을 때 쓴다."""
        data = await self._get("/releases", {"limit": 1000})
        items = data.get("releases") or []
        if search:
            s = search.lower()
            items = [r for r in items if s in str(r.get("name", "")).lower()]
        return items

    async def fetch(self, start: date, end: date) -> FetchResult:
        if not self.api_key:
            return FetchResult(
                self.name,
                ok=False,
                detail="TH_FRED_API_KEY 가 비어 있습니다 (무료 키: fred.stlouisfed.org)",
            )
        try:
            data = await self._get(
                "/releases/dates",
                {
                    "realtime_start": start.isoformat(),
                    "realtime_end": end.isoformat(),
                    # 이 값이 true 여야 '아직 발표되지 않은 미래 일정'이 나온다
                    "include_release_dates_with_no_data": "true",
                    "sort_order": "asc",
                    "limit": 1000,
                },
            )
        except httpx.HTTPStatusError as e:
            hint = ""
            if e.response.status_code in (400, 403):
                hint = " — API 키를 확인하세요"
            return FetchResult(
                self.name, ok=False, detail=f"HTTP {e.response.status_code}{hint}"
            )
        except Exception as e:  # 네트워크 등
            return FetchResult(self.name, ok=False, detail=f"{type(e).__name__}: {e}")

        rows = data.get("release_dates") or []
        events = self._to_events(rows, start, end)
        log.info("FRED 릴리스 %d건 중 관심 지표 %d건", len(rows), len(events))
        return FetchResult(
            self.name, ok=True, events=events, detail=f"릴리스 {len(rows)}건 조회"
        )

    # ── 변환 ──────────────────────────────────────────────
    def _to_events(
        self, rows: list[dict[str, Any]], start: date, end: date
    ) -> list[Event]:
        out: list[Event] = []
        seen: set[tuple[str, date]] = set()

        for row in rows:
            raw_name = str(row.get("release_name") or "")
            rule = self._match(raw_name)
            if rule is None:
                continue
            try:
                d = date.fromisoformat(str(row["date"]))
            except Exception:
                continue
            if not (start <= d <= end):
                continue
            key = (rule.label, d)
            if key in seen:
                continue
            seen.add(key)

            out.append(
                Event(
                    name=rule.label,
                    ts_utc=et_time_on(d, rule.time_et),
                    kind="econ",
                    importance=rule.importance,
                    note="time_estimated",   # FRED 는 시각을 주지 않는다
                    source=self.name,
                )
            )
        return out

    def _match(self, release_name: str) -> ReleaseRule | None:
        if not release_name:
            return None
        for rule in self.whitelist:
            if rule.matches(release_name):
                return rule
        return None
