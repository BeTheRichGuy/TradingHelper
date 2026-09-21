"""백테스트 실행기 통합 테스트 — DB와 캘린더를 가짜로 갈아끼우고 끝까지 돌린다.

`runner.py` 는 순수 함수가 아니라 **접착제**다. 지금까지 이 프로젝트에서 난 사고는
계산이 아니라 접착제에서 났다 (임포트 누락, `__init__` 오염, 명령어 이름 오타).
그래서 여기는 계산이 아니라 **연결이 실제로 돌아가는지**를 본다.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from th.backtest import runner as R
from th.backtest.models import BacktestConfig
from th.sessions.market import DayWindows
from th.sessions.models import Regime, SessionContext
from th.strategy import features as F
from th.strategy.features import PerpContext

DAY = datetime(2026, 9, 10, tzinfo=UTC)
OPEN = DAY.replace(hour=13, minute=30)         # 09:30 ET
CLOSE = DAY.replace(hour=20, minute=0)         # 16:00 ET


def make_klines(start: datetime, minutes: int, base: float = 100.0, seed: int = 11):
    """완만한 상승 + 20분 주기 눌림.

    이 값들은 **신호가 실제로 나오도록 맞춘 것**이다. 신호가 0건인 데이터로는
    파이프라인이 돌아간다는 걸 증명할 수 없다 — 아래 테스트들이 전부 공허해진다.
    """
    rng = random.Random(seed)
    rows = []
    for i in range(minutes):
        d = i % 1440
        price = base * (
            1 + 0.00003 * (d % 390) + 0.002 * math.sin(d / 20.0) + rng.gauss(0, 0.0003)
        )
        high = price * (1 + abs(rng.gauss(0, 0.0022)))
        low = price * (1 - abs(rng.gauss(0, 0.0022)))
        rising = math.cos(d / 20.0) > 0
        vol = 1100.0 if rising else 380.0
        rows.append(
            {
                "ts": start + timedelta(minutes=i),
                "open": price,
                "high": max(high, price),
                "low": min(low, price),
                "close": price,
                "volume": vol,
                "taker_buy_base": vol * (0.63 if rising else 0.42),
            }
        )
    return rows


@dataclass
class FakeDB:
    rows: list[dict]
    calls: int = 0

    async def fetch(self, query: str, *args):
        self.calls += 1
        if "FROM klines" in query:
            _sym, since, until = args
            return [r for r in self.rows if since <= r["ts"] <= until]
        return []


class FakeCalendar:
    """평일이면 전부 정규 거래일. NYSE 달력(pandas_market_calendars) 대신."""

    def windows(self, day):
        if day.weekday() >= 5:
            return None
        base = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return DayWindows(
            day=day,
            pre_start=base.replace(hour=8),        # 04:00 ET
            market_open=base.replace(hour=13, minute=30),
            market_close=base.replace(hour=20),
            post_end=base + timedelta(hours=24),
            early_close=False,
        )

    def is_trading_day(self, day) -> bool:
        return self.windows(day) is not None

    def previous_trading_day(self, day):
        for i in range(1, 12):
            cand = day - timedelta(days=i)
            if self.is_trading_day(cand):
                return cand
        return None


def install(monkey_rows, session_regime=Regime.RTH, blocked=False):
    """runner 가 쓰는 바깥 세계를 전부 가짜로 바꾼다. 원복 함수를 돌려준다."""
    fake = FakeDB(monkey_rows)
    original = (R.sdata.db, R.get_context, R.sdata.load_perp_context,
                R.sdata.median_volume_5m, F.get_calendar)
    F.get_calendar = FakeCalendar

    def ctx(symbol: str, at: datetime) -> SessionContext:
        return SessionContext(
            symbol=symbol, at=at, regime=session_regime,
            regime_ends_at=CLOSE, is_trading_day=True, early_close=False,
            blocked=blocked,
        )

    async def perp(symbol: str, at: datetime) -> PerpContext:
        return PerpContext(
            funding_rate=0.0001,
            next_funding_at=at + timedelta(hours=6),
            oi_change_5m=0.3, oi_change_15m=0.9,
            spread_bps=1.0, underlying_price=None,
        )

    async def median(symbol: str, at: datetime) -> float:
        return 4000.0

    R.sdata.db = fake
    R.get_context = ctx
    R.sdata.load_perp_context = perp
    R.sdata.median_volume_5m = median

    def restore():
        (R.sdata.db, R.get_context, R.sdata.load_perp_context,
         R.sdata.median_volume_5m, F.get_calendar) = original

    return fake, restore


def run(rows, cfg=None, start=None, end=None, **kw):
    import asyncio

    fake, restore = install(rows, **kw)
    try:
        return asyncio.run(
            R.run_backtest(
                "SOXLUSDT",
                start or OPEN + timedelta(minutes=30),
                end or CLOSE - timedelta(minutes=30),
                cfg or BacktestConfig(step_minutes=5),
            )
        )
    finally:
        restore()


def test_runner_actually_produces_trades():
    """이게 0이면 아래 테스트가 전부 공허해진다. 먼저 여기부터 본다."""
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    r = run(rows)
    assert r.evaluated > 0
    assert r.signals > 0, "합성 데이터가 신호를 하나도 안 냅니다"
    assert r.trades, f"신호 {r.signals}건이 전부 무산됐습니다: {r.skip_reasons}"
    assert r.signals >= len(r.trades)
    assert r.blocked + r.filtered <= r.evaluated
    for t in r.trades:
        assert t.entry_at > t.signal_at            # 미래 누수 방지
        assert t.exit_at >= t.entry_at
        assert t.exit_reason


def test_skipped_signals_are_recorded_not_dropped():
    """진입 못 한 신호를 조용히 버리면 성과가 실제보다 좋아진다."""
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    r = run(rows)
    assert len(r.trades) + len(r.skipped) == r.signals


def test_runner_reads_klines_only_once():
    """매 스텝 DB를 다시 때리면 몇 시간짜리가 된다."""
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    fake, restore = install(rows)
    try:
        import asyncio

        asyncio.run(
            R.run_backtest("SOXLUSDT", OPEN + timedelta(minutes=30),
                           OPEN + timedelta(hours=3), BacktestConfig(step_minutes=5))
        )
        kline_calls = fake.calls
    finally:
        restore()
    assert kline_calls == 1


def test_blocked_session_produces_no_trades():
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    r = run(rows, blocked=True)
    assert r.blocked == r.evaluated
    assert r.trades == []


def test_regime_filter_skips_everything_when_it_does_not_match():
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    r = run(rows, cfg=BacktestConfig(step_minutes=5, regimes=("PRE",)))
    assert r.evaluated == 0
    assert r.trades == []


def test_no_data_returns_empty_result_instead_of_crashing():
    r = run([])
    assert r.trades == [] and r.evaluated == 0


def test_window_matches_live_lookback():
    """백테스트 슬라이스 폭이 실전 load_bars 와 같아야 한다."""
    from th.strategy import data as sdata

    assert R.LOOKBACK_DAYS == sdata.DEFAULT_LOOKBACK_DAYS


def test_bisect_finds_the_last_closed_bar():
    rows = make_klines(OPEN, 30)
    bars = [
        R.Bar(ts=r["ts"], open=r["open"], high=r["high"], low=r["low"],
              close=r["close"], volume=r["volume"], taker_buy=r["taker_buy_base"])
        for r in rows
    ]
    i = R.bisect_ts(bars, OPEN + timedelta(minutes=9))
    assert i == 10
    assert bars[i - 1].ts == OPEN + timedelta(minutes=9)
    assert R.bisect_ts(bars, OPEN - timedelta(minutes=1)) == 0


def test_steps_are_inclusive_and_spaced():
    got = list(R.steps(OPEN, OPEN + timedelta(minutes=10), 5))
    assert got == [OPEN, OPEN + timedelta(minutes=5), OPEN + timedelta(minutes=10)]


def test_positions_never_overlap():
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    r = run(rows)
    ordered = sorted(r.trades, key=lambda t: t.entry_at)
    for a, b in zip(ordered, ordered[1:]):
        assert b.signal_at >= a.exit_at, "앞 포지션을 들고 있는데 새로 들어갔습니다"


def test_parse_day():
    assert R.parse_day("2026-09-10") == datetime(2026, 9, 10, tzinfo=UTC)
    with pytest.raises(ValueError):
        R.parse_day("2026/09/10")


# ─────────────────────────────────────────────────────────
# 퍼널 진단 — 백테스트와 같은 루프를 쓰는지가 핵심
# ─────────────────────────────────────────────────────────

def collect(rows, cfg=None, **kw):
    import asyncio

    from th.backtest.funnel import collect_funnel

    fake, restore = install(rows, **kw)
    try:
        return asyncio.run(
            collect_funnel(
                "SOXLUSDT", OPEN + timedelta(minutes=30), CLOSE - timedelta(minutes=30),
                cfg or BacktestConfig(step_minutes=5),
            )
        )
    finally:
        restore()


def test_funnel_and_backtest_agree():
    """둘이 각자 루프를 돌면 언젠가 다른 숫자를 말한다. 같은 값이어야 한다."""
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    f = collect(rows)
    b = run(rows)
    assert f.evaluated == b.evaluated
    assert f.published == b.signals
    assert sum(f.blocked.values()) == b.blocked


def test_funnel_stages_are_exclusive_and_add_up():
    """한 평가는 가장 먼저 걸린 곳에서 한 번만 세어져야 합계가 맞는다."""
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    f = collect(rows)
    rows_out = f.stage_rows()
    assert rows_out[-1][2] == f.published
    killed = sum(k for _, k, _ in rows_out)
    assert killed + f.published == f.evaluated


def test_funnel_counts_where_setups_die():
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    f = collect(rows)
    assert f.required_failed, "필수 조건 실패가 하나도 안 잡혔습니다"
    # 라벨이 '셋업 · 조건' 꼴이어야 어디를 손볼지 읽을 수 있다
    assert all(" · " in k for k in f.required_failed)


def test_publishable_band_matches_the_risk_constants():
    """이 구간이 문서·risk.py 와 어긋나면 진단이 거짓말을 한다."""
    from th.backtest import funnel as FN
    from th.strategy import risk

    assert FN.PUBLISHABLE_SL_MIN == pytest.approx(risk.TP1_MIN_PCT / risk.TP1_R)
    assert FN.PUBLISHABLE_SL_MAX == pytest.approx(risk.TP2_MAX_PCT / risk.TP2_R)
    assert FN.PUBLISHABLE_ATR_MIN == pytest.approx(FN.PUBLISHABLE_SL_MIN / risk.ATR_MULT)


def test_reject_classification():
    from th.backtest.funnel import classify_reject

    assert "너무 가까" in classify_reject("1차 목표가 0.60% 로 너무 가깝습니다 (기준 0.8%)")
    assert "너무 멂" in classify_reject("목표가 3.10% 로 너무 멉니다 (기준 2.5%) — 변동성이")
    assert classify_reject("신뢰도 45점이 기준(55) 미만입니다") == "신뢰도 점수 미달"


def test_blocked_session_is_attributed_to_a_reason():
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    f = collect(rows, blocked=True)
    assert sum(f.blocked.values()) == f.evaluated
    assert f.published == 0


def test_stop_window_probe_measures_without_changing_the_strategy():
    """진단은 재보기만 해야 한다. 전략 결과가 달라지면 안 된다."""
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    before = run(rows)
    f = collect(rows)
    after = run(rows)
    assert before.signals == after.signals == f.published

    assert f.stop_by_window, "구조 창 진단이 하나도 안 쌓였습니다"
    assert set(f.stop_by_window) >= {10, 15, 60, "전체"}
    for vals in f.stop_by_window.values():
        assert len(vals) == len(f.sl_pct)          # 매 건마다 다 재야 비교가 된다
        assert all(v > 0 for v in vals)


def test_wider_structure_window_never_gives_a_tighter_stop():
    """창을 넓히면 더 낮은 저점을 찾을 수 있을 뿐이라 손절이 좁아질 수 없다."""
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    f = collect(rows)
    narrow = f.stop_by_window[10]
    wide = f.stop_by_window[60]
    assert all(w >= n - 1e-9 for n, w in zip(narrow, wide))


def test_atr_only_stop_is_never_wider_than_the_final_stop():
    """구조는 손절을 넓히기만 한다 (02 문서 5.2 의 '더 넓은 쪽')."""
    rows = make_klines(OPEN - timedelta(days=3), 3 * 1440 + 400)
    f = collect(rows)
    assert all(a <= s + 1e-9 for a, s in zip(f.atr_only_sl_pct, f.sl_pct))


# ── 봉 조회의 1분 미리보기 ─────────────────────────────────
#
# ts 봉은 ts+1분에야 닫힌다. `load_bars` 는 그래서 `at − 1분` 까지만 읽는데, 본주·VIX
# 1분봉과 5분 거래량 버킷은 `ts <= at` 로 읽어 형성 중인 봉을 미리 보고 있었다.

class RecordingDB:
    """질의와 인자를 기록만 하고 빈 결과를 돌려준다."""

    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.queries.append((" ".join(query.split()), args))
        return []


class FakeSettings:
    underlying_map = {"SOXLUSDT": "SOXL"}


def _with_recording_db(coro):
    import asyncio

    db = RecordingDB()
    original = (R.sdata.db, R.sdata.get_settings)
    R.sdata.db = db
    R.sdata.get_settings = lambda: FakeSettings()
    try:
        asyncio.run(coro())
    finally:
        R.sdata.db, R.sdata.get_settings = original
    return db


def test_underlying_and_vix_bars_are_read_up_to_the_last_closed_minute():
    at = OPEN + timedelta(minutes=30)

    async def go():
        await R.sdata.load_perp_context("SOXLUSDT", at)

    db = _with_recording_db(go)
    bar_queries = [(q, a) for q, a in db.queries if "underlying_prices" in q]
    assert len(bar_queries) == 2, "본주 + VIX"
    for q, args in bar_queries:
        ts_arg = [x for x in args if isinstance(x, datetime)]
        assert ts_arg == [at - timedelta(minutes=1)], q
    # 스냅샷(펀딩·호가·OI)은 관측 시각 그대로 — 봉이 아니다
    snap = [(q, a) for q, a in db.queries if "FROM funding" in q or "book_ticker" in q]
    assert snap and all(at in a for _, a in snap)


def test_median_volume_uses_only_completed_5m_buckets():
    at = OPEN + timedelta(minutes=33)

    async def go():
        await R.sdata.median_volume_5m("SOXLUSDT", at)

    db = _with_recording_db(go)
    (q, args), = db.queries
    assert "klines_5m" in q
    assert args[1] == at - timedelta(minutes=5)


def test_csv_dataset_does_not_read_the_forming_underlying_bar():
    from th.backtest.csvdata import CsvDataset

    t = OPEN
    ds = CsvDataset()
    ds.underlying["SOXL"] = [(t, 100.0), (t + timedelta(minutes=1), 200.0)]
    # t+1 봉은 t+2 에야 닫힌다. at = t+1 에서 보이는 마지막 종가는 t 봉의 100 이다.
    assert ds.underlying_at("SOXL", t + timedelta(minutes=1)) == 100.0
    assert ds.underlying_at("SOXL", t + timedelta(minutes=2)) == 200.0
    # 1분봉 거래량 중앙값도 같은 규칙 — 정확히 at 에 붙은 봉은 세지 않는다
    ds.bars = [
        R.Bar(ts=t + timedelta(minutes=i), open=1, high=1, low=1, close=1,
            volume=1.0 if i < 600 else 1e9)
        for i in range(601)
    ]
    assert ds.median_volume_5m(t + timedelta(minutes=600)) == pytest.approx(5.0)
