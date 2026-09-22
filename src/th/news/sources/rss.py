"""RSS 소스 — Yahoo Finance 종목별 헤드라인 · Google News 검색.

둘 다 키가 없고 무료다. 대신 느리다(분 단위) — 속보는 텔레그램·SEC 가 먼저 오고,
RSS 는 **놓친 것을 줍는 그물**이다.

`feedparser` 는 함수 안에서만 임포트한다. 없는 환경에서도 이 모듈은 임포트된다.
"""

from __future__ import annotations

import asyncio
import calendar
import re
from datetime import UTC, datetime
from typing import Any, Sequence
from urllib.parse import quote_plus

from ...logging_setup import get_logger
from .. import tickers
from ..models import SOURCE_GNEWS, SOURCE_YAHOO, NewsItem
from .base import FetchResult

log = get_logger(__name__)

YAHOO_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
GNEWS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_MAX_BODY = 2000
_CONCURRENCY = 4


def _clean(html: str | None) -> str:
    if not html:
        return ""
    return _WS.sub(" ", _TAG.sub(" ", html)).strip()[:_MAX_BODY]


def _entry_ts(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), UTC)
            except (OverflowError, ValueError):
                continue
    return None


def parse_feed(data: bytes, *, source: str, ticker_hint: str | None = None,
               fallback_ts: datetime | None = None) -> list[NewsItem]:
    """피드 바이트 → NewsItem 목록. 순수 함수 (네트워크 없음).

    `ticker_hint` 는 이 피드가 어느 종목 것인지 — Yahoo 종목 피드·Google 회사명 검색은
    본문에 이름이 없어도 그 종목 얘기다.
    """
    import feedparser

    d = feedparser.parse(data)
    feed_title = (d.feed.get("title") or "").strip() if getattr(d, "feed", None) else ""
    out: list[NewsItem] = []
    for e in d.entries:
        title = _WS.sub(" ", (e.get("title") or "")).strip()
        if not title:
            continue
        link = (e.get("link") or "").strip()
        ts = _entry_ts(e) or fallback_ts or datetime.now(UTC)
        body = _clean(e.get("summary") or e.get("description"))
        src = e.get("source")
        channel = ""
        if isinstance(src, dict):
            channel = (src.get("title") or "").strip()
        if not channel:
            channel = feed_title
        syms = set(tickers.extract(f"{title}\n{body}"))
        if ticker_hint:
            syms.add(tickers.symbol_of(ticker_hint))
        out.append(NewsItem(
            source=source, source_id=(e.get("id") or link or title)[:400], ts=ts,
            title=title[:400], body=body, url=link, channel=channel[:120],
            symbols=tuple(sorted(syms)),
            raw={"ticker_hint": ticker_hint} if ticker_hint else {},
        ))
    return out


class RssSource:
    """`kind` 는 yahoo | gnews. 종목마다 피드 하나."""

    def __init__(self, kind: str, symbols: Sequence[str], *, interval_s: int | None = None,
                 client=None, timeout_s: float = 15.0) -> None:
        if kind not in (SOURCE_YAHOO, SOURCE_GNEWS):
            raise ValueError(f"알 수 없는 RSS 종류: {kind}")
        self.name = kind
        self.symbols = tuple(symbols)
        self.interval_s = interval_s or (120 if kind == SOURCE_YAHOO else 300)
        self._client = client
        self._timeout_s = timeout_s

    def url_for(self, symbol: str) -> str:
        t = tickers.ticker_of(symbol)
        if self.name == SOURCE_YAHOO:
            return YAHOO_URL.format(ticker=t)
        name = tickers.display_name(symbol)
        q = f'"{name}" stock' if name != t else f"{t} stock"
        return GNEWS_URL.format(query=quote_plus(q))

    async def _get(self, url: str) -> bytes:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_s, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (TradingHelper news collector)"},
            )
        r = await self._client.get(url)
        r.raise_for_status()
        return r.content

    async def fetch(self, since: datetime) -> FetchResult:
        sem = asyncio.Semaphore(_CONCURRENCY)
        items: list[NewsItem] = []
        failed: list[str] = []

        async def one(sym: str) -> None:
            async with sem:
                try:
                    data = await self._get(self.url_for(sym))
                    got = parse_feed(data, source=self.name, ticker_hint=tickers.ticker_of(sym))
                    items.extend(i for i in got if i.ts > since)
                except Exception as e:  # noqa: BLE001 — 소스 하나의 실패가 루프를 죽이면 안 된다
                    failed.append(sym)
                    log.debug("%s %s 실패: %s", self.name, sym, e)

        await asyncio.gather(*(one(s) for s in self.symbols))
        ok = len(failed) < max(1, len(self.symbols))       # 전부 실패했을 때만 실패
        detail = f"{len(items)}건 · 실패 {len(failed)}/{len(self.symbols)}"
        if failed and not ok:
            detail += " (전부 실패)"
        return FetchResult(source=self.name, ok=ok, items=items, detail=detail)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
