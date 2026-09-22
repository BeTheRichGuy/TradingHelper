"""v3.0 M1 — 뉴스 수집 계층 (docs/17 §3). 네트워크·DB 없이 못 박을 수 있는 것들.

가장 중요한 것 둘:
  · 소스 하나의 실패가 수집 루프를 죽이지 않는다
  · 영어 단어와 겹치는 티커(ON·COST…)는 이름이나 $ 없이는 붙지 않는다 — 안 그러면 모든 뉴스가 판단으로 간다
"""

from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from th.news import dedupe, tickers
from th.news.collect import Collector
from th.news.models import Judgment, Leg, NewsItem, Plan
from th.news.sources.base import FetchResult, NewsSource, PushSource
from th.news.sources.edgar import build_cik_map, parse_atom
from th.news.sources.finnhub import parse_company_news
from th.news.sources.rss import RssSource, parse_feed
from th.news.sources.telegram import TelegramSource, message_to_item

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def item(title: str, *, source="yahoo", url="", body="", ts=NOW, source_id="", symbols=()) -> NewsItem:
    return NewsItem(source=source, source_id=source_id, ts=ts, title=title, body=body, url=url,
                    symbols=tuple(symbols))


# ─────────────────────────────────────────────────────────
# 종목 매핑
# ─────────────────────────────────────────────────────────

def test_cashtag_and_company_name_map_to_universe_symbols():
    assert tickers.extract("$NVDA jumps after earnings") == ("NVDAUSDT",)
    assert tickers.extract("Nvidia and Advanced Micro Devices rally") == ("AMDUSDT", "NVDAUSDT")
    assert tickers.extract("Micron Technology guides above estimates") == ("MUUSDT",)


def test_ambiguous_word_tickers_need_name_or_cashtag():
    """ON · COST · COIN · META 같은 건 대문자만으로는 안 붙는다."""
    assert tickers.extract("Markets are ON edge; the COST of capital rises") == ()
    assert tickers.extract("onsemi cuts guidance") == ("ONUSDT",)
    assert tickers.extract("Costco same-store sales beat") == ("COSTUSDT",)
    assert tickers.extract("$COIN lists new token") == ("COINUSDT",)
    assert tickers.extract("Meta Platforms unveils new model") == ("METAUSDT",)


def test_two_letter_ticker_needs_cashtag():
    assert tickers.extract("MU shares") == ()
    assert tickers.extract("$MU shares") == ("MUUSDT",)


def test_unknown_tickers_are_ignored_and_result_is_sorted_unique():
    assert tickers.extract("XYZ Corp and ABCD Inc") == ()
    assert tickers.extract("TSLA TSLA Tesla $tsla") == ("TSLAUSDT",)


def test_display_names_exist_for_every_named_symbol():
    for sym in tickers.known_symbols():
        assert tickers.display_name(sym)
        assert tickers.aliases(sym)


# ─────────────────────────────────────────────────────────
# 중복 키
# ─────────────────────────────────────────────────────────

def test_tracking_params_and_fragments_do_not_change_the_key():
    a = item("x", url="https://www.example.com/a/b?utm_source=t&id=1#top")
    b = item("y", url="https://example.com/a/b/?id=1")
    assert dedupe.dedupe_key(a) == dedupe.dedupe_key(b)


def test_same_title_from_different_sources_share_title_key():
    assert dedupe.title_key("Nvidia beats estimates - Reuters") == dedupe.title_key("NVIDIA beats estimates!")
    assert dedupe.title_key("Nvidia beats") != dedupe.title_key("Nvidia misses")


def test_google_news_links_are_opaque_so_key_falls_back_to_source_id():
    a = item("t", source="gnews", source_id="g1", url="https://news.google.com/rss/articles/AAA")
    b = item("t", source="gnews", source_id="g2", url="https://news.google.com/rss/articles/BBB")
    assert dedupe.dedupe_key(a) != dedupe.dedupe_key(b)
    assert dedupe.dedupe_key(a).startswith("i:")


# ─────────────────────────────────────────────────────────
# 소스 파서 (순수 함수)
# ─────────────────────────────────────────────────────────

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Yahoo Finance</title>
<item><title>Nvidia unveils new chip</title><link>https://finance.yahoo.com/news/a?.tsrc=rss</link>
<pubDate>Tue, 22 Sep 2026 11:30:00 GMT</pubDate><guid>a1</guid><description>&lt;p&gt;Big &lt;b&gt;news&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Old story</title><link>https://finance.yahoo.com/news/old</link>
<pubDate>Mon, 21 Sep 2026 11:30:00 GMT</pubDate><guid>a0</guid></item>
<item><title></title><link>https://x/empty</link></item>
</channel></rss>"""


def test_rss_parse_uses_hint_symbol_and_strips_html():
    items = parse_feed(RSS, source="yahoo", ticker_hint="NVDA")
    assert [i.title for i in items] == ["Nvidia unveils new chip", "Old story"]
    a = items[0]
    assert a.symbols == ("NVDAUSDT",)
    assert a.body == "Big news"
    assert a.ts == datetime(2026, 9, 22, 11, 30, tzinfo=UTC)
    assert a.channel == "Yahoo Finance"
    assert items[1].symbols == ("NVDAUSDT",)     # 종목 피드라 이름이 없어도 그 종목이다


def test_rss_source_urls_and_since_filter():
    src = RssSource("yahoo", ["NVDAUSDT"])
    assert "s=NVDA" in src.url_for("NVDAUSDT")
    g = RssSource("gnews", ["NVDAUSDT"])
    assert "Nvidia" in g.url_for("NVDAUSDT") and "stock" in g.url_for("NVDAUSDT")

    class FakeClient:
        async def get(self, url):
            class R:
                content = RSS

                def raise_for_status(self):
                    pass
            return R()

    src = RssSource("yahoo", ["NVDAUSDT"], client=FakeClient())
    res = run(src.fetch(NOW - timedelta(hours=2)))
    assert res.ok and [i.title for i in res.items] == ["Nvidia unveils new chip"]


def test_rss_source_reports_failure_only_when_every_feed_fails():
    class Boom:
        async def get(self, url):
            raise RuntimeError("down")

    src = RssSource("yahoo", ["NVDAUSDT", "AMDUSDT"], client=Boom())
    res = run(src.fetch(NOW))
    assert not res.ok and "전부 실패" in res.detail


ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>8-K - NVIDIA CORP (0001045810) (Filer)</title>
<link href="https://www.sec.gov/Archives/edgar/data/1045810/x-index.htm"/>
<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-09-22 &lt;b&gt;AccNo:&lt;/b&gt; 0001045810-26-000123 &lt;b&gt;Size:&lt;/b&gt; 1 MB</summary>
<updated>2026-09-22T11:45:00-04:00</updated><id>urn:1</id></entry>
<entry><title>8-K - SOME OTHER CO (0009999999) (Filer)</title><link href="https://www.sec.gov/x"/>
<updated>2026-09-22T11:46:00-04:00</updated><id>urn:2</id></entry>
</feed>"""


def test_edgar_parse_keeps_only_universe_filings_via_cik_map():
    cik = build_cik_map({"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
                         "1": {"cik_str": 9999999, "ticker": "ZZZZ", "title": "Other"}},
                        ["NVDAUSDT", "AMDUSDT"])
    assert cik == {1045810: "NVDAUSDT"}
    items = parse_atom(ATOM, cik)
    assert len(items) == 1
    a = items[0]
    assert a.symbols == ("NVDAUSDT",) and a.source == "edgar"
    assert a.source_id == "0001045810-26-000123"
    assert a.title.startswith("[8-K] Nvidia 공시")
    assert a.ts == datetime(2026, 9, 22, 15, 45, tzinfo=UTC)


def test_edgar_parse_falls_back_to_company_name_without_cik_map():
    items = parse_atom(ATOM, {})
    assert [i.symbols for i in items] == [("NVDAUSDT",)]


def test_telegram_message_to_item():
    it = message_to_item(chat_id=-100123, chat_title="Alerts", username="alerts", msg_id=77,
                         date=datetime(2026, 9, 22, 1, 2, tzinfo=UTC),
                         text="  BREAKING: $AMD wins hyperscaler deal\n\nDetails here  ")
    assert it is not None
    assert it.title == "BREAKING: $AMD wins hyperscaler deal" and it.body == "Details here"
    assert it.url == "https://t.me/alerts/77" and it.source_id == "-100123:77"
    assert it.symbols == ("AMDUSDT",) and it.channel == "Alerts"
    assert message_to_item(chat_id=1, chat_title="x", username=None, msg_id=1, date=None, text="   ") is None


def test_telegram_source_says_what_is_missing():
    assert TelegramSource(0, "", []).configured() == (False, "TH_TELEGRAM_API_ID / TH_TELEGRAM_API_HASH 가 없습니다")
    assert TelegramSource(1, "h", [" "]).configured()[1].startswith("TH_TELEGRAM_CHANNELS")
    assert TelegramSource(1, "h", ["@a"]).configured() == (True, "")


def test_finnhub_parse():
    payload = [{"id": 1, "headline": "TSMC raises capex", "datetime": 1790000000, "summary": "…",
                "url": "https://x/1", "source": "Reuters", "category": "company"}, {"headline": ""}]
    items = parse_company_news(payload, "TSMUSDT")
    assert len(items) == 1 and items[0].symbols == ("TSMUSDT",) and items[0].channel == "Reuters"
    assert parse_company_news({"not": "a list"}, "TSMUSDT") == []


# ─────────────────────────────────────────────────────────
# 수집기
# ─────────────────────────────────────────────────────────

class MemSave:
    def __init__(self):
        self.keys: set[str] = set()
        self.saved: list[NewsItem] = []

    async def __call__(self, it: NewsItem) -> int | None:
        k = dedupe.dedupe_key(it)
        if k in self.keys:
            return None
        self.keys.add(k)
        self.saved.append(it)
        return len(self.saved)


class FlakySource:
    """첫 호출은 예외, 둘째는 성공, 셋째에 멈춘다."""

    name = "flaky"
    interval_s = 1

    def __init__(self, stop_evt):
        self.calls = 0
        self.stop_evt = stop_evt

    async def fetch(self, since):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        if self.calls >= 2:
            self.stop_evt.set()
        return FetchResult(source=self.name, ok=True, items=[
            item("Nvidia news", source="flaky", url="https://a/1", ts=NOW),
            item("Nvidia news", source="flaky", url="https://a/1?utm_x=1", ts=NOW),   # 중복
        ])


def test_source_exception_does_not_kill_the_loop_and_duplicates_are_counted_once():
    stop = asyncio.Event()
    src = FlakySource(stop)
    save = MemSave()
    c = Collector([src], save=save)
    run(c.run(stop))
    assert src.calls >= 2
    assert c.status["flaky"]["ok"] is True
    assert c.new_total == 1 and c.seen_total == 2
    assert save.saved[0].symbols == ("NVDAUSDT",)      # 심볼이 없으면 수집기가 붙인다


def test_poll_once_moves_since_forward_with_overlap():
    class Src:
        name = "s"
        interval_s = 60

        async def fetch(self, since):
            return FetchResult(source="s", ok=True, items=[item("t", url="https://a/2", ts=NOW)])

    c = Collector([Src()], save=MemSave())
    res, nxt = run(c.poll_once(Src(), NOW - timedelta(hours=6)))
    assert res.ok and nxt == NOW - timedelta(minutes=5)


def test_save_failure_is_swallowed():
    async def bad(_it):
        raise RuntimeError("db down")

    c = Collector([], save=bad)
    assert run(c.ingest(item("x", url="https://a/3"))) is False


def test_push_source_receives_the_ingest_sink():
    got = []

    class Push:
        name = "push"

        async def run(self, stop_evt, sink):
            got.append(await sink(item("Tesla recall", source="push", source_id="1")))
            stop_evt.set()

    stop = asyncio.Event()
    c = Collector([], [Push()], save=MemSave())
    run(c.run(stop))
    assert got == [True]
    assert isinstance(Push(), PushSource) and isinstance(FlakySource(stop), NewsSource)


# ─────────────────────────────────────────────────────────
# 판단 스키마 범위 (M2 가 쓴다 — 범위 규칙은 지금 못 박는다)
# ─────────────────────────────────────────────────────────

def plan(**kw) -> Plan:
    base = dict(stop_pct=3.0, target_pct=6.0, tp2_pct=None, max_hold_hours=48,
                legs=[Leg(margin_pct=4, entry="market", expire_hours=0.1),
                      Leg(margin_pct=3, entry="limit", limit_offset_pct=-1.5, expire_hours=24),
                      Leg(margin_pct=3, entry="limit", limit_offset_pct=-2.5, expire_hours=24)],
                invalidation_ko="가이던스 철회")
    base.update(kw)
    return Plan(**base)


LIMITS = dict(margin_max=10.0, stop_min=1.0, stop_max=5.0, hold_max=168)


def test_plan_within_limits_has_no_problems():
    assert plan().problems(**LIMITS) == []
    assert plan().margin_total_pct == 10


def test_plan_over_margin_or_stop_range_is_rejected():
    p = plan(legs=[Leg(margin_pct=6, entry="market", expire_hours=0.1),
                   Leg(margin_pct=6, entry="market", expire_hours=0.1)])
    assert any("마진 합계" in x for x in p.problems(**LIMITS))
    assert any("손절" in x for x in plan(stop_pct=6).problems(**LIMITS))
    assert any("손절" in x for x in plan(stop_pct=0.5).problems(**LIMITS))
    assert any("손익비" in x for x in plan(target_pct=2).problems(**LIMITS))
    assert any("오프셋" in x for x in plan(legs=[Leg(margin_pct=5, entry="limit", expire_hours=1)]).problems(**LIMITS))
    assert any("보유" in x for x in plan(max_hold_hours=500).problems(**LIMITS))


def test_judgment_actionable_rules():
    j = Judgment(relevant=True, symbols=["NVDAUSDT"], direction="LONG", confidence=0.8, magnitude="high",
                 horizon_hours=24, thesis_ko="…", risks_ko=[], already_priced_in=False, plan=plan())
    assert j.actionable(0.7) == (True, "")
    assert j.model_copy(update={"confidence": 0.5}).actionable(0.7)[1].startswith("확신도")
    assert j.model_copy(update={"already_priced_in": True}).actionable(0.7) == (False, "이미 반영됨")
    assert j.model_copy(update={"direction": "NONE"}).actionable(0.7) == (False, "방향 없음")
    assert j.model_copy(update={"plan": None}).actionable(0.7) == (False, "계획 없음")


def test_news_item_requires_aware_ts_and_title():
    with pytest.raises(ValueError):
        NewsItem(source="x", source_id="", ts=datetime(2026, 1, 1), title="t")
    with pytest.raises(ValueError):
        NewsItem(source="x", source_id="", ts=NOW, title="  ")


# ─────────────────────────────────────────────────────────
# 배선 · 설정 · 패키지 순수성
# ─────────────────────────────────────────────────────────

def test_stream_이_뉴스_수집을_켠다():
    src = (ROOT / "src" / "th" / "cli.py").read_text(encoding="utf-8")
    body = src[src.index("def stream("):src.index("def gaps(")]
    assert "build_collector" in body and "news_enabled" in body


def test_migration_008_exists_and_is_last():
    from th.migrations import migration_files
    names = [p.name for p in migration_files()]
    assert names[-1] == "008_news.sql"
    sql = (ROOT / "db" / "migrations" / "008_news.sql").read_text(encoding="utf-8")
    for t in ("news_items", "news_judgments", "news_campaigns", "news_campaign_legs"):
        assert f"CREATE TABLE IF NOT EXISTS {t}" in sql


def test_news_settings_defaults_and_parsing(monkeypatch):
    from th.config import Settings
    for k in list(__import__("os").environ):
        if k.startswith("TH_") or k == "ANTHROPIC_API_KEY":
            monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.news_enabled is False and s.news_exec == "paper" and s.news_exec_effective == "paper"
    assert s.news_model == "claude-opus-5" and s.telegram_channels == []
    assert s.news_effort == "high" and s.news_sources == ["telegram", "edgar", "yahoo", "gnews", "finnhub"]
    monkeypatch.setenv("TH_NEWS_SOURCES", "Telegram, edgar")
    monkeypatch.setenv("TH_NEWS_EFFORT", "low")
    assert Settings(_env_file=None).news_sources == ["telegram", "edgar"]
    assert Settings(_env_file=None).news_effort == "low"
    monkeypatch.setenv("TH_TELEGRAM_CHANNELS", "@a, @b ,,")
    monkeypatch.setenv("TH_NEWS_EXEC", "live")
    s = Settings(_env_file=None)
    assert s.telegram_channels == ["@a", "@b"]
    assert s.news_exec == "live" and s.news_exec_effective == "approve"   # 실거래 경로는 아직 없다


def test_build_collector_honours_source_selection():
    from types import SimpleNamespace

    from th.news.collect import build_collector

    base = dict(edgar_user_agent="", finnhub_api_key="k", telegram_api_id=1, telegram_api_hash="h",
                telegram_channels=["@a"])
    c = build_collector(SimpleNamespace(news_sources=["telegram", "edgar", "yahoo", "gnews", "finnhub"], **base))
    assert [s.name for s in c.polling] == ["yahoo", "gnews", "edgar", "finnhub"] and [s.name for s in c.push] == ["telegram"]
    c = build_collector(SimpleNamespace(news_sources=["telegram", "edgar"], **base))
    assert [s.name for s in c.polling] == ["edgar"] and [s.name for s in c.push] == ["telegram"]
    c = build_collector(SimpleNamespace(news_sources=["yahoo"], **base))
    assert [s.name for s in c.polling] == ["yahoo"] and c.push == []


def test_news_package_does_not_pull_in_telethon_feedparser_or_anthropic():
    """`th.sessions` 와 같은 규칙 — 무거운 라이브러리는 함수 안에서만."""
    code = ("import sys, th.news.models, th.news.tickers, th.news.dedupe, th.news.collect, "
            "th.news.sources.rss, th.news.sources.edgar, th.news.sources.telegram, th.news.sources.finnhub; "
            "print(sorted(m for m in ('telethon','feedparser','anthropic') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
