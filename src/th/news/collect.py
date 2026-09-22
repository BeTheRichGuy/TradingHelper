"""뉴스 수집 루프 — `th stream` 안에서 돈다 (docs/17 §3).

소스마다 태스크 하나. **소스 하나가 죽어도 나머지는 계속 돈다.** 저장은 `store.save_item` 이
중복을 거른다. 여기서는 세고 로그만 남긴다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Sequence

from ..logging_setup import get_logger
from ..universe import STOCK_PERPS
from . import tickers
from .models import NewsItem
from .sources.base import FetchResult, NewsSource, PushSource

log = get_logger(__name__)

Saver = Callable[[NewsItem], Awaitable[int | None]]

LOOKBACK_H = 6            # 처음 켤 때 이만큼 과거까지 줍는다
OVERLAP_MIN = 5           # 다음 폴링 기준 시각을 이만큼 겹친다 — 중복은 저장소가 걸러준다


class Collector:
    def __init__(self, polling: Sequence[NewsSource], push: Sequence[PushSource] = (), *,
                 save: Saver | None = None, lookback_h: float = LOOKBACK_H) -> None:
        self.polling = list(polling)
        self.push = list(push)
        self._save = save
        self.lookback_h = lookback_h
        self.status: dict[str, dict[str, Any]] = {}
        for s in self.polling + self.push:
            self.status[s.name] = {"ok": None, "detail": "", "last_run": None,
                                   "last_item_ts": None, "seen": 0, "new": 0}
        self.new_total = 0
        self.seen_total = 0

    async def _saver(self, item: NewsItem) -> int | None:
        if self._save is None:
            from . import store
            self._save = store.save_item
        return await self._save(item)

    async def ingest(self, item: NewsItem) -> bool:
        """한 건 저장. 새 것이면 True. 예외는 여기서 삼킨다 (수집이 멈추면 안 된다)."""
        if not item.symbols:
            item = item.with_symbols(tickers.extract(item.text))
        try:
            new_id = await self._saver(item)
        except Exception:  # noqa: BLE001
            log.exception("뉴스 저장 실패 (%s) — 건너뜀", item.source)
            return False
        st = self.status.setdefault(item.source, {"seen": 0, "new": 0, "last_item_ts": None})
        st["seen"] = st.get("seen", 0) + 1
        self.seen_total += 1
        if st.get("last_item_ts") is None or item.ts > st["last_item_ts"]:
            st["last_item_ts"] = item.ts
        if new_id is None:
            return False
        st["new"] = st.get("new", 0) + 1
        self.new_total += 1
        log.info("뉴스 [%s] %s · %s", item.source, ",".join(tickers.ticker_of(s) for s in item.symbols) or "-",
                 item.title[:90])
        return True

    async def poll_once(self, src: NewsSource, since: datetime) -> tuple[FetchResult, datetime]:
        """한 번 폴링하고 (결과, 다음 since) 를 돌려준다."""
        try:
            res = await src.fetch(since)
        except Exception as e:  # noqa: BLE001
            res = FetchResult(source=src.name, ok=False, detail=f"예외: {e}")
        st = self.status[src.name]
        was_ok = st.get("ok")
        st["ok"], st["detail"], st["last_run"] = res.ok, res.detail, datetime.now(UTC)
        if not res.ok:
            if was_ok is not False:
                log.warning("뉴스 소스 %s 실패 — %s (다른 소스는 계속 돕니다)", src.name, res.detail)
            return res, since
        if was_ok is False:
            log.info("뉴스 소스 %s 복구", src.name)
        newest = since
        for item in sorted(res.items, key=lambda i: i.ts):
            await self.ingest(item)
            if item.ts > newest:
                newest = item.ts
        return res, newest - timedelta(minutes=OVERLAP_MIN)

    async def poll_loop(self, src: NewsSource, stop_evt: asyncio.Event) -> None:
        since = datetime.now(UTC) - timedelta(hours=self.lookback_h)
        while not stop_evt.is_set():
            _, since = await self.poll_once(src, since)
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=max(1, int(src.interval_s)))
            except (TimeoutError, asyncio.TimeoutError):
                pass

    async def run(self, stop_evt: asyncio.Event) -> None:
        if not self.polling and not self.push:
            log.warning("뉴스 소스가 하나도 없습니다 — 수집을 건너뜁니다")
            await stop_evt.wait()
            return
        tasks = [asyncio.create_task(self.poll_loop(s, stop_evt), name=f"news:{s.name}") for s in self.polling]
        tasks += [asyncio.create_task(s.run(stop_evt, self.ingest), name=f"news:{s.name}") for s in self.push]
        log.info("뉴스 수집 시작 — 폴링 %s · 푸시 %s",
                 ",".join(s.name for s in self.polling) or "-", ",".join(s.name for s in self.push) or "-")
        try:
            await stop_evt.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for s in self.polling + self.push:
                close = getattr(s, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        pass
            log.info("뉴스 수집 종료 — 새 항목 %d / 본 항목 %d", self.new_total, self.seen_total)


def build_collector(settings, *, universe: Sequence[str] | None = None) -> Collector:
    """설정에서 수집기를 만든다. 키가 없는 소스는 빠진다."""
    from .sources.edgar import EdgarSource
    from .sources.rss import RssSource

    uni = tuple(universe) if universe else tuple(STOCK_PERPS)
    named = [s for s in uni if s in set(tickers.known_symbols())] or list(uni)
    want = set(getattr(settings, "news_sources", None) or ("telegram", "edgar", "yahoo", "gnews", "finnhub"))
    polling: list[NewsSource] = []
    if "yahoo" in want:
        polling.append(RssSource("yahoo", named))
    if "gnews" in want:
        polling.append(RssSource("gnews", named, interval_s=600))
    if "edgar" in want:
        polling.append(EdgarSource(uni, user_agent=settings.edgar_user_agent))
    if "finnhub" in want and settings.finnhub_api_key:
        from .sources.finnhub import FinnhubSource
        polling.append(FinnhubSource(settings.finnhub_api_key, named))
    push: list[PushSource] = []
    if "telegram" in want and settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_channels:
        from .sources.telegram import TelegramSource
        push.append(TelegramSource(settings.telegram_api_id, settings.telegram_api_hash,
                                   settings.telegram_channels))
    elif "telegram" in want:
        log.info("텔레그램 채널 읽기는 꺼져 있습니다 (TH_TELEGRAM_API_ID/HASH/CHANNELS)")
    off = sorted({"telegram", "edgar", "yahoo", "gnews", "finnhub"} - want)
    if off:
        log.info("TH_NEWS_SOURCES 로 꺼 둔 소스: %s", ",".join(off))
    return Collector(polling, push)
