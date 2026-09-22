"""Finnhub company-news — 키가 있을 때만 (무료 티어 분당 60회).

종목당 호출 1회라 60종목이면 폴링 한 번에 60회다. 1.2초 간격으로 돌려 분당 50회 아래로 둔다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

from ...logging_setup import get_logger
from .. import tickers
from ..models import SOURCE_FINNHUB, NewsItem
from .base import FetchResult

log = get_logger(__name__)

URL = "https://finnhub.io/api/v1/company-news"
_PACE_S = 1.2
_MAX_BODY = 2000


def parse_company_news(payload: Any, symbol: str) -> list[NewsItem]:
    """Finnhub 응답(list[dict]) → NewsItem. 순수 함수."""
    out: list[NewsItem] = []
    if not isinstance(payload, list):
        return out
    for row in payload:
        try:
            title = str(row.get("headline") or "").strip()
            if not title:
                continue
            ts = datetime.fromtimestamp(int(row.get("datetime") or 0), UTC)
            body = str(row.get("summary") or "").strip()[:_MAX_BODY]
            syms = set(tickers.extract(f"{title}\n{body}"))
            syms.add(symbol)
            out.append(NewsItem(
                source=SOURCE_FINNHUB, source_id=str(row.get("id") or row.get("url") or title)[:200],
                ts=ts, title=title[:400], body=body, url=str(row.get("url") or ""),
                channel=str(row.get("source") or "")[:120], symbols=tuple(sorted(syms)),
                raw={"category": row.get("category")},
            ))
        except (TypeError, ValueError, OSError):
            continue
    return out


class FinnhubSource:
    name = SOURCE_FINNHUB

    def __init__(self, api_key: str, symbols: Sequence[str], *, interval_s: int = 300,
                 client=None, timeout_s: float = 15.0) -> None:
        self.api_key = api_key
        self.symbols = tuple(symbols)
        self.interval_s = interval_s
        self._client = client
        self._timeout_s = timeout_s

    async def _get(self, params: dict[str, str]) -> Any:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        r = await self._client.get(URL, params={**params, "token": self.api_key})
        r.raise_for_status()
        return r.json()

    async def fetch(self, since: datetime) -> FetchResult:
        if not self.api_key:
            return FetchResult(source=self.name, ok=False, detail="TH_FINNHUB_API_KEY 없음")
        day_from = (since - timedelta(days=1)).date().isoformat()
        day_to = datetime.now(UTC).date().isoformat()
        items: list[NewsItem] = []
        failed: list[str] = []
        for i, sym in enumerate(self.symbols):
            if i:
                await asyncio.sleep(_PACE_S)
            try:
                payload = await self._get({"symbol": tickers.ticker_of(sym), "from": day_from, "to": day_to})
                items.extend(x for x in parse_company_news(payload, sym) if x.ts > since)
            except Exception as e:  # noqa: BLE001
                failed.append(sym)
                log.debug("finnhub %s 실패: %s", sym, e)
                if "429" in str(e):
                    await asyncio.sleep(10)
        ok = len(failed) < max(1, len(self.symbols))
        return FetchResult(source=self.name, ok=ok, items=items,
                           detail=f"{len(items)}건 · 실패 {len(failed)}/{len(self.symbols)}")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
