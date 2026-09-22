"""SEC EDGAR 최신 공시 (8-K · 6-K) — 공식 · 무료 · 빠름.

실적·인수·경영진 교체·유상증자 같은 것은 보도자료보다 **공시가 먼저** 다.
EDGAR 는 User-Agent 에 연락처를 요구한다 (`TH_EDGAR_USER_AGENT`). 초당 10회 제한 — 우리는 분당 2회.

CIK → 티커는 SEC 의 `company_tickers.json` 으로 푼다 (한 번 받아 메모리에 둔다).
못 받으면 회사명 사전(`tickers.py`)으로 대신한다.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime
from typing import Any, Sequence

from ...logging_setup import get_logger
from .. import tickers
from ..models import SOURCE_EDGAR, NewsItem
from .base import FetchResult

log = get_logger(__name__)

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&count=100&output=atom"
CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
FORMS: tuple[str, ...] = ("8-K", "6-K")
DEFAULT_USER_AGENT = "TradingHelper/0.1 (personal research tool; set TH_EDGAR_USER_AGENT)"

_CIK_IN_TITLE = re.compile(r"\((\d{10})\)")
_ACC_NO = re.compile(r"AccNo:\s*([\d-]+)")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def build_cik_map(payload: dict[str, Any], universe: Sequence[str]) -> dict[int, str]:
    """company_tickers.json → {cik: 심볼}. 유니버스 안의 것만."""
    want = {tickers.ticker_of(s): s for s in universe}
    out: dict[int, str] = {}
    for row in payload.values():
        try:
            t = str(row["ticker"]).upper()
            cik = int(row["cik_str"])
        except (KeyError, TypeError, ValueError):
            continue
        if t in want:
            out[cik] = want[t]
    return out


def _entry_ts(entry: Any) -> datetime | None:
    for key in ("updated_parsed", "published_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), UTC)
            except (OverflowError, ValueError):
                continue
    return None


def parse_atom(data: bytes, cik_to_symbol: dict[int, str], *,
               fallback_ts: datetime | None = None) -> list[NewsItem]:
    """EDGAR Atom → NewsItem. 유니버스 종목의 공시만 남긴다. 순수 함수."""
    import feedparser

    d = feedparser.parse(data)
    out: list[NewsItem] = []
    for e in d.entries:
        title = _WS.sub(" ", (e.get("title") or "")).strip()
        m = _CIK_IN_TITLE.search(title)
        symbol: str | None = None
        if m:
            symbol = cik_to_symbol.get(int(m.group(1)))
        if symbol is None:
            # CIK 맵이 없거나 못 찾았으면 회사명으로 한 번 더
            found = tickers.extract(title)
            symbol = found[0] if len(found) == 1 else None
        if symbol is None:
            continue
        summary_html = e.get("summary") or ""
        summary = _WS.sub(" ", _TAG.sub(" ", summary_html)).strip()
        acc = _ACC_NO.search(summary)
        link = (e.get("link") or "").strip()
        form = title.split(" - ", 1)[0].strip() if " - " in title else ""
        out.append(NewsItem(
            source=SOURCE_EDGAR,
            source_id=(acc.group(1) if acc else (e.get("id") or link))[:200],
            ts=_entry_ts(e) or fallback_ts or datetime.now(UTC),
            title=f"[{form}] {tickers.display_name(symbol)} 공시 — {title}"[:400] if form else title[:400],
            body=summary[:1000],
            url=link,
            channel="SEC EDGAR",
            symbols=(symbol,),
            raw={"form": form},
        ))
    return out


class EdgarSource:
    name = SOURCE_EDGAR

    def __init__(self, universe: Sequence[str], *, user_agent: str = DEFAULT_USER_AGENT,
                 interval_s: int = 60, client=None, timeout_s: float = 20.0) -> None:
        self.universe = tuple(universe)
        self.interval_s = interval_s
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self._client = client
        self._timeout_s = timeout_s
        self.cik_map: dict[int, str] = {}
        self._cik_tried = False

    async def _get(self, url: str) -> bytes:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_s, follow_redirects=True,
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            )
        r = await self._client.get(url)
        r.raise_for_status()
        return r.content

    async def _ensure_cik_map(self) -> None:
        if self.cik_map or self._cik_tried:
            return
        self._cik_tried = True
        try:
            import json
            payload = json.loads(await self._get(CIK_MAP_URL))
            self.cik_map = build_cik_map(payload, self.universe)
            log.info("EDGAR CIK 맵 %d개 (유니버스 %d)", len(self.cik_map), len(self.universe))
        except Exception as e:  # noqa: BLE001
            log.warning("EDGAR CIK 맵을 못 받았습니다 — 회사명으로 대신합니다: %s", e)

    async def fetch(self, since: datetime) -> FetchResult:
        await self._ensure_cik_map()
        items: list[NewsItem] = []
        failed: list[str] = []
        for form in FORMS:
            try:
                data = await self._get(FEED_URL.format(form=form))
                got = parse_atom(data, self.cik_map)
                items.extend(i for i in got if i.ts > since)
            except Exception as e:  # noqa: BLE001
                failed.append(form)
                log.debug("EDGAR %s 실패: %s", form, e)
        ok = len(failed) < len(FORMS)
        return FetchResult(source=self.name, ok=ok, items=items,
                           detail=f"{len(items)}건 · 실패 {len(failed)}/{len(FORMS)}")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
