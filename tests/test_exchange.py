from datetime import UTC, datetime

import pytest

from th.exchange.base import (
    Kline,
    NoopExecutor,
    OrderRequest,
    get_executor,
)
from th.exchange.binance_ws import build_stream_url


def test_stream_url_lowercases_and_includes_all_streams():
    url = build_stream_url("wss://fstream.binance.com", ["SOXLUSDT"])
    assert url.startswith("wss://fstream.binance.com/stream?streams=")
    for s in ("soxlusdt@kline_1m", "soxlusdt@markPrice@1s",
              "soxlusdt@bookTicker", "soxlusdt@forceOrder"):
        assert s in url


def test_stream_url_multiple_symbols():
    url = build_stream_url("wss://fstream.binance.com/", ["SOXLUSDT", "NVDAUSDT"])
    assert url.count("@kline_1m") == 2
    assert "//stream" not in url.replace("wss://", "")


def test_kline_as_row_ordering_matches_insert():
    ts = datetime(2026, 8, 29, 18, 35, tzinfo=UTC)
    k = Kline(symbol="SOXLUSDT", tf="1m", ts=ts, open=1.0, high=2.0, low=0.5,
              close=1.5, volume=100.0, quote_volume=150.0, trades=7,
              taker_buy_base=60.0)
    row = k.as_row()
    assert row == ("SOXLUSDT", "1m", ts, 1.0, 2.0, 0.5, 1.5, 100.0, 150.0, 7, 60.0, "binance")
    assert len(row) == 12          # KLINE_UPSERT 의 $1..$12 와 일치해야 한다


@pytest.mark.asyncio
async def test_noop_executor_always_rejects():
    ex = NoopExecutor()
    res = await ex.submit(OrderRequest(symbol="SOXLUSDT", side="BUY", qty=1, order_type="MARKET"))
    assert res.accepted is False
    assert "none" in res.reason


def test_get_executor_none():
    assert isinstance(get_executor("none"), NoopExecutor)


def test_get_executor_refuses_unimplemented_modes():
    """paper/live 를 잘못 켜면 조용히 도는 대신 즉시 터져야 한다."""
    for mode in ("paper", "live"):
        with pytest.raises(NotImplementedError):
            get_executor(mode)


# ─────────────────────────────────────────────────────────
# 백필 — 진행 중인 봉을 확정봉으로 저장하지 않는다
# ─────────────────────────────────────────────────────────

def _raw_kline(open_ms: int, close_ms: int) -> list:
    # [openTime, o, h, l, c, v, closeTime, qv, n, takerBase, takerQuote, ignore]
    return [open_ms, "1", "2", "0.5", "1.5", "10", close_ms, "15", 3, "6", "9", "0"]


def test_klines_drops_the_bar_that_has_not_closed_yet():
    """바이낸스는 endTime 이 포함 경계라 진행 중인 봉까지 돌려준다.
    closeTime(k[6]) 이 지금보다 뒤면 아직 안 닫힌 봉이다."""
    import asyncio
    import time

    from th.exchange.binance_rest import BinanceFutures

    now_ms = int(time.time() * 1000)
    cur_open = now_ms - now_ms % 60_000
    raw = [
        _raw_kline(cur_open - 120_000, cur_open - 60_001),   # 닫힘
        _raw_kline(cur_open - 60_000, cur_open - 1),         # 닫힘
        _raw_kline(cur_open, cur_open + 59_999),             # 진행 중 → 버린다
    ]
    api = BinanceFutures.__new__(BinanceFutures)

    async def fake_get(path, params=None, signed=False):
        return raw

    api._get = fake_get
    out = asyncio.run(api.klines("SOXLUSDT", "1m", start_ms=0, end_ms=cur_open))
    assert [int(k.ts.timestamp() * 1000) for k in out] == [cur_open - 120_000, cur_open - 60_000]
    assert all(k.closed for k in out)


def test_fetch_range_keeps_the_half_open_bound_against_an_inclusive_api():
    """[start, end) 계약: end_ms 에 열린 봉은 요청하지 않아야 한다."""
    import asyncio

    from th.ingest import backfill

    seen = {}

    class FakeApi:
        async def klines(self, symbol, interval, start_ms, end_ms, limit):
            seen["end_ms"] = end_ms
            return []

        async def close(self):
            pass

    end = 1_800_000_000_000
    asyncio.run(backfill.fetch_range("SOXLUSDT", end - 600_000, end, client=FakeApi()))
    assert seen["end_ms"] < end, "endTime 이 포함 경계인데 end_ms 를 그대로 넘겼다"
    assert seen["end_ms"] // 60_000 == (end - 1) // 60_000     # 직전 분 안에 떨어진다


def test_closed_bar_end_is_the_start_of_the_current_minute():
    from th.ingest.backfill import closed_bar_end

    now = datetime(2026, 9, 9, 14, 7, 33, 123, tzinfo=UTC)
    assert closed_bar_end(now) == datetime(2026, 9, 9, 14, 7, tzinfo=UTC)


# ─────────────────────────────────────────────────────────
# 수집기 — 저장 실패 복구 · 갭 백필 태스크 · 갭 표시
# ─────────────────────────────────────────────────────────

def _collector():
    """설정을 읽지 않고 수집기 인스턴스만 만든다."""
    import asyncio

    from th.exchange.binance_ws import StreamCollector

    c = StreamCollector.__new__(StreamCollector)
    c.symbols = ["SOXLUSDT"]
    c._klines, c._books, c._funding, c._liqs = [], [], [], []
    c._last_book_at, c._last_funding_at, c._last_closed_bar = {}, {}, {}
    c._stop = asyncio.Event()
    c._msg_count = c._bar_count = 0
    c._tasks = set()
    return c


def test_failed_flush_requeues_the_batch_for_the_next_flush():
    """DB 가 잠깐 죽었다고 2초치 봉을 버리면 조용히 구멍이 난다. 되돌려 다시 시도한다."""
    import asyncio

    from th.exchange import binance_ws as ws
    from th import db

    c = _collector()
    c._klines = [("SOXLUSDT", "1m", 1), ("SOXLUSDT", "1m", 2)]
    c._books = [("SOXLUSDT", "b1")]
    calls = []

    async def boom(rows):
        calls.append(len(rows))
        raise RuntimeError("connection refused")

    saved = db.save_klines
    db.save_klines = boom
    try:
        asyncio.run(c._flush())
        # 새로 들어온 행 뒤가 아니라 **앞에** 되돌아와 순서가 유지된다
        c._klines.append(("SOXLUSDT", "1m", 3))
        assert [r[2] for r in c._klines] == [1, 2, 3]
        assert c._books == [("SOXLUSDT", "b1")]
        asyncio.run(c._flush())
        assert calls == [2, 3]
    finally:
        db.save_klines = saved


def test_requeue_is_bounded_and_drops_the_oldest():
    from th.exchange import binance_ws as ws

    failed = [("k", i) for i in range(ws.RETRY_BUFFER_MAX)]
    fresh = [("k", "new")]
    merged = ws.StreamCollector._requeue(failed, fresh, "봉")
    assert len(merged) == ws.RETRY_BUFFER_MAX
    assert merged[-1] == ("k", "new") and merged[0] == ("k", 1)


def test_gap_fill_tasks_are_tracked_and_awaited_before_shutdown():
    """참조 없는 create_task 는 GC 에 사라질 수 있고, 종료 시 기다릴 수도 없다."""
    import asyncio

    c = _collector()
    done = []

    async def slow_gap():
        await asyncio.sleep(0.05)
        done.append(1)

    async def main():
        t = c._spawn(slow_gap(), name="gap-test")
        assert t in c._tasks
        await asyncio.gather(*c._tasks, return_exceptions=True)
        await asyncio.sleep(0)          # done 콜백이 돌 틈
        assert t not in c._tasks

    asyncio.run(main())
    assert done == [1]


def test_gap_fill_task_exception_is_logged_not_swallowed():
    import asyncio
    import logging

    from th.exchange import binance_ws as ws

    class Catch(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, r):
            self.msgs.append(r.getMessage())

    h = Catch()
    logging.getLogger(ws.__name__).addHandler(h)
    c = _collector()

    async def bad():
        raise ValueError("망가짐")

    async def main():
        c._spawn(bad(), name="gap-bad")
        await asyncio.gather(*c._tasks, return_exceptions=True)
        await asyncio.sleep(0)

    try:
        asyncio.run(main())
    finally:
        logging.getLogger(ws.__name__).removeHandler(h)
    assert any("gap-bad" in m and "망가짐" in m for m in h.msgs), h.msgs


def test_fill_gap_marks_only_its_own_gap():
    """갭 하나를 메웠다고 그 심볼의 미완료 갭 전부를 '채움'으로 뒤집으면 안 된다."""
    import asyncio

    from th import db
    from th.ingest import backfill

    c = _collector()
    marked = []
    start = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
    end = datetime(2026, 9, 9, 14, 3, tzinfo=UTC)

    async def record_gap(*a):
        pass

    async def save_klines(rows):
        return len(rows)

    async def mark(symbol, tf, s=None, e=None):
        marked.append((symbol, tf, s, e))
        return 1

    async def fetch_range(symbol, s_ms, e_ms, interval="1m", client=None):
        return [("SOXLUSDT", "1m", start)]

    saved = (db.record_gap, db.save_klines, db.mark_gaps_filled, backfill.fetch_range)
    db.record_gap, db.save_klines, db.mark_gaps_filled = record_gap, save_klines, mark
    backfill.fetch_range = fetch_range
    try:
        asyncio.run(c._fill_gap("SOXLUSDT", start, end, 2))
    finally:
        db.record_gap, db.save_klines, db.mark_gaps_filled, backfill.fetch_range = saved
    assert marked == [("SOXLUSDT", "1m", start, end)]


def test_reconnect_backfill_also_repairs_interior_gaps():
    """꼬리만 채우면 저장 실패로 생긴 중간 구멍은 영영 남는다."""
    import asyncio

    from th import db
    from th.ingest import backfill, gapcheck

    c = _collector()
    called = []

    async def backfill_symbol(symbol, lookback_minutes=None, client=None):
        called.append(("tail", symbol))
        return 0

    async def repair_gaps(symbol, hours=24, tf="1m"):
        called.append(("repair", symbol, hours))
        return 3

    async def last_ts(symbol, tf="1m"):
        return datetime(2026, 9, 9, 14, 0, tzinfo=UTC)

    saved = (backfill.backfill_symbol, gapcheck.repair_gaps, db.get_last_kline_ts)
    backfill.backfill_symbol, gapcheck.repair_gaps, db.get_last_kline_ts = (
        backfill_symbol, repair_gaps, last_ts,
    )
    try:
        asyncio.run(c._backfill_all("테스트"))
    finally:
        backfill.backfill_symbol, gapcheck.repair_gaps, db.get_last_kline_ts = saved
    assert ("tail", "SOXLUSDT") in called
    assert any(k[0] == "repair" for k in called), called
    assert c._last_closed_bar["SOXLUSDT"] == datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


# ── 바이낸스 거절 사유 ─────────────────────────────────────
def test_거절_응답의_code와_msg를_꺼내고_서명된_URL은_남기지_않는다():
    """실제 사례: doctor 가 '400 Bad Request for url ...signature=...' 만 보여줘서
    왜 거절됐는지 알 수 없었고, 서명된 URL 이 화면에 그대로 찍혔다."""
    import asyncio

    from th.exchange.binance_rest import BinanceAPIError, BinanceFutures, explain_binance_error

    class Resp:
        status_code = 400
        text = '{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'
        headers: dict = {}

        def json(self):
            return {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}

    class Client:
        async def get(self, path, params=None, headers=None):
            return Resp()

    bf = BinanceFutures.__new__(BinanceFutures)
    bf._client = Client(); bf.api_key = "k"; bf.api_secret = "s"

    try:
        asyncio.run(bf._get("/fapi/v2/account", signed=True))
        raise AssertionError("거절인데 예외가 안 났다")
    except BinanceAPIError as e:
        assert e.code == -2015 and e.status == 400
        assert "signature" not in str(e) and "timestamp" not in str(e)
        assert "권한" in explain_binance_error(e)


# ── 웹 대시보드 v2 가 쓰는 공개 엔드포인트 ───────────────
def test_ticker_24hr_returns_every_symbol_as_numbers():
    """심볼 없이 한 번 부르면 전 종목 목록. 문자열 숫자는 여기서 한 번만 float 로 바꾼다."""
    import asyncio

    from th.exchange.binance_rest import BinanceFutures

    api = BinanceFutures.__new__(BinanceFutures)
    seen = {}

    async def fake_get(path, params=None, signed=False):
        seen["path"], seen["params"], seen["signed"] = path, params, signed
        return [
            {"symbol": "SOXLUSDT", "lastPrice": "131.80", "priceChangePercent": "0.470",
             "quoteVolume": "12000000.5", "closeTime": 1757520000000},
            {"symbol": "nvdausdt", "lastPrice": "118.2", "priceChangePercent": "-0.8",
             "quoteVolume": "3400000", "closeTime": 1757520000000},
            {"symbol": "BROKEN"},                                   # 값 없음 → 0 으로
            {"lastPrice": "1"},                                     # 심볼 없음 → 건너뜀
        ]

    api._get = fake_get
    out = asyncio.run(api.ticker_24hr())
    assert seen == {"path": "/fapi/v1/ticker/24hr", "params": {"symbol": None}, "signed": False}
    assert out[0] == {"symbol": "SOXLUSDT", "last_price": 131.8, "change_pct": 0.47,
                      "quote_volume": 12000000.5, "close_time_ms": 1757520000000}
    assert out[1]["symbol"] == "NVDAUSDT"
    assert out[2]["last_price"] == 0.0 and len(out) == 3


def test_premium_index_carries_funding_and_next_funding_time():
    import asyncio
    from datetime import UTC, datetime

    from th.exchange.binance_rest import BinanceFutures

    api = BinanceFutures.__new__(BinanceFutures)

    async def fake_get(path, params=None, signed=False):
        assert path == "/fapi/v1/premiumIndex" and params == {"symbol": "SOXLUSDT"}
        return {"symbol": "SOXLUSDT", "markPrice": "131.81", "indexPrice": "131.76",
                "lastFundingRate": "0.00017200", "nextFundingTime": 1757520000000,
                "time": 1757518000000}

    api._get = fake_get
    d = asyncio.run(api.premium_index("SOXLUSDT"))
    assert d["mark_price"] == 131.81 and d["markPrice"] == 131.81
    assert d["last_funding_rate"] == 0.000172 and d["lastFundingRate"] == 0.000172
    assert d["nextFundingTime"] == 1757520000000
    assert d["next_funding_ts"] == datetime.fromtimestamp(1757520000, tz=UTC)


def test_klines_include_open_keeps_the_running_bar_flagged_not_closed():
    """웹 차트 폴백만 쓴다. 적재 경로(기본값)는 여전히 진행 중인 봉을 버린다."""
    import asyncio
    import time

    from th.exchange.binance_rest import BinanceFutures

    now_ms = int(time.time() * 1000)
    cur_open = now_ms - now_ms % 60_000
    raw = [_raw_kline(cur_open - 60_000, cur_open - 1), _raw_kline(cur_open, cur_open + 59_999)]
    api = BinanceFutures.__new__(BinanceFutures)

    async def fake_get(path, params=None, signed=False):
        return raw

    api._get = fake_get
    out = asyncio.run(api.klines("NVDAUSDT", "1m", limit=2, include_open=True))
    assert [k.closed for k in out] == [True, False]
    assert len(asyncio.run(api.klines("NVDAUSDT", "1m", limit=2))) == 1


def test_웹소켓이_봉을_안_줘도_REST_꼬리_폴링이_봉을_채운다():
    """실측(2026-09-10): 주식 perp 은 bookTicker 만 오고 kline/markPrice 는 0건.
    수집기는 20초마다 REST 로 마지막 봉 이후를 받아야 한다."""
    import asyncio
    import inspect

    from th.exchange import binance_ws as m

    assert hasattr(m.StreamCollector, "_tail_poll_loop")
    src = inspect.getsource(m.StreamCollector.run)
    assert "_tail_poll_loop" in src, "run() 이 꼬리 폴링 태스크를 띄워야 한다"
    assert m.TAIL_POLL_SEC <= 30

    # 폴링 루프가 backfill_symbol 을 부르는지 — 짧게 돌려본다
    calls = []

    async def fake_backfill(sym, lookback_minutes=None):
        calls.append(sym); return 3

    c = m.StreamCollector.__new__(m.StreamCollector)
    c.symbols = ["SOXLUSDT"]; c._stop = asyncio.Event()
    m.TAIL_POLL_SEC, keep = 0.01, m.TAIL_POLL_SEC
    try:
        import th.ingest.backfill as bf
        orig = bf.backfill_symbol; bf.backfill_symbol = fake_backfill
        async def go():
            t = asyncio.create_task(c._tail_poll_loop())
            await asyncio.sleep(0.08); c._stop.set(); await t
        asyncio.run(go())
    finally:
        bf.backfill_symbol = orig; m.TAIL_POLL_SEC = keep
    assert calls and calls[0] == "SOXLUSDT"
    assert getattr(c, "_tail_polled", 0) >= 3


def test_exchange_info_는_심볼_없이_전_종목을_한_번에_받는다():
    """유니버스(docs/15 9절)는 서버가 시작할 때 이 한 번의 호출로 정한다."""
    import asyncio

    from th.exchange.binance_rest import BinanceFutures

    api = BinanceFutures.__new__(BinanceFutures)
    seen = []

    async def fake_get(path, params=None, signed=False):
        seen.append((path, params, signed))
        return {"symbols": [{"symbol": "SOXLUSDT", "status": "TRADING",
                             "underlyingType": "STOCK", "underlyingSubType": ["Stocks"]}]}

    api._get = fake_get
    info = asyncio.run(api.exchange_info())
    assert seen == [("/fapi/v1/exchangeInfo", {"symbol": None}, False)]
    assert info["symbols"][0]["symbol"] == "SOXLUSDT"
    from th.universe import looks_like_stock
    assert looks_like_stock(info["symbols"][0])
    assert not looks_like_stock({"underlyingType": "COIN", "underlyingSubType": ["Layer-1"],
                                 "contractType": "PERPETUAL"})
