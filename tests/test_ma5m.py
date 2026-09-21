"""v2.2 — 영상 200/20 전략 모듈 · 시그널 엔진 · 누적 기록 계산.

핵심은 둘이다.
  · 패키지(`th.backtest.ma5m`)와 검증 스크립트(`scripts/ma_youtube.py`)가 **같은 매매**를 낸다.
    스크립트로 검증한 숫자(docs/16)와 서버가 실제로 돌리는 규칙이 갈라지면 검증이 무의미하다.
  · 시그널 엔진은 **러너의 주문 경로만** 쓴다. 자기 손으로 브래킷을 만들지 않는다.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta

import numpy as np

from th.backtest import ma5m
from th.backtest.ma5m import SETUP_ID, Bars, Params, compute_signals, latest_state, run_backtest, simulate

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run(coro):
    return asyncio.run(coro)


def synthetic(n: int = 4000, seed: int = 7, start: int = 1_750_000_000) -> Bars:
    """추세가 바뀌는 랜덤워크 5분봉. 신호가 여러 번 나오도록 변동성을 준다."""
    rng = np.random.default_rng(seed)
    drift = np.concatenate([np.full(n // 4, 0.0006), np.full(n // 4, -0.0006),
                            np.full(n // 4, 0.0008), np.full(n - 3 * (n // 4), -0.0004)])
    ret = rng.normal(0, 0.004, n) + drift
    c = 100 * np.exp(np.cumsum(ret))
    o = np.concatenate([[100.0], c[:-1]]) * (1 + rng.normal(0, 0.0005, n))
    spread = np.abs(rng.normal(0, 0.003, n))
    h = np.maximum(o, c) * (1 + spread)
    lo = np.minimum(o, c) * (1 - spread)
    v = rng.uniform(100, 1000, n)
    T = (start // 300) * 300 + np.arange(n) * 300
    return Bars(T.astype(np.int64), o, h, lo, c, v)


def _script():
    spec = importlib.util.spec_from_file_location("ma_youtube", ROOT / "scripts" / "ma_youtube.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["ma_youtube"] = m
    spec.loader.exec_module(m)
    return m


# ── 패키지 ↔ 스크립트 동일성 ──────────────────────────────
def test_패키지와_검증_스크립트가_같은_매매를_낸다():
    Y = _script()
    B = synthetic()
    Bd = Y.build(B.T, B.o, B.h, B.l, B.c, B.v)
    total = 0
    for mode in ("both", "pullback", "squeeze"):
        sig = Y.strat_video_200_20(Bd, mode)
        tr = Y.simulate(Bd, sig, 0.11, 0.0172)
        S = compute_signals(B, Params(mode=mode))
        mt = simulate(B, S, Params(mode=mode))
        assert len(tr) == len(mt), (mode, len(tr), len(mt))
        for a, b in zip(tr, mt, strict=True):
            assert a[4] == b.i and a[3] == b.reason
            assert abs(a[1] - b.r) < 1e-9
        total += len(mt)
    assert total >= 10, "합성 데이터에서 매매가 너무 적다 — 테스트가 아무것도 검증하지 않는다"


def test_뒤집기_대조군은_같은_자리_반대_방향이다():
    B = synthetic()
    p = Params()
    S = compute_signals(B, p)
    a, b = simulate(B, S, p), simulate(B, S, p, flip=True)
    assert len(a) == len(b)
    assert all(x.direction == -y.direction and x.i == y.i for x, y in zip(a, b, strict=True))


# ── 규칙 하나하나 ────────────────────────────────────────
def test_청산_규칙은_종가가_20선을_반대로_넘을_때다():
    assert ma5m.exit_hit(1, close=99.0, e20=100.0)
    assert not ma5m.exit_hit(1, close=101.0, e20=100.0)
    assert ma5m.exit_hit(-1, close=101.0, e20=100.0)
    assert not ma5m.exit_hit(-1, close=99.0, e20=100.0)


def test_세션_마스크는_미국_동부_기준이다():
    # 2026-09-09 (수) 14:00 UTC = 10:00 ET 본장 / 22:00 UTC = 애프터 / 03:00 UTC = 정지
    wed = int(datetime(2026, 9, 9, tzinfo=UTC).timestamp())
    sat = int(datetime(2026, 9, 12, tzinfo=UTC).timestamp())
    T = np.array([wed + 14 * 3600, wed + 22 * 3600, wed + 3 * 3600, sat + 14 * 3600])
    rth = ma5m.session_mask(T, "rth")
    assert rth.tolist() == [False, True, True, True]         # 본장만 남긴다 (주말 14시는 본장이 아니다)
    off = ma5m.session_mask(T, "off_rth")
    assert off.tolist() == [True, False, False, False]
    pp = ma5m.session_mask(T, "pre_post")
    assert pp.tolist() == [True, False, True, True]
    assert not ma5m.session_mask(T, "all").any()


def test_안전_손절은_신호_봉_종가에서_hard_stop_만큼이다():
    B = synthetic()
    p = Params(hard_stop_pct=2.5)
    S = compute_signals(B, p)
    idx = np.flatnonzero(S.d != 0)
    assert len(idx)
    for i in idx[:20]:
        expect = B.c[i] * (1 - 0.025) if S.d[i] > 0 else B.c[i] * (1 + 0.025)
        assert abs(S.stop[i] - expect) < 1e-9


def test_횡보면_신호가_없다():
    B = synthetic()
    p = Params()
    S = compute_signals(B, p)
    assert not (S.chop & (S.d != 0)).any()


def test_매개변수는_웹_폼에서_안전하게_만들어진다():
    p = Params.from_dict({"mode": "squeeze", "hard_stop_pct": "2", "chop_max": "5", "unknown": 1,
                          "session": "off_rth", "cost_pct": ""})
    assert p.mode == "squeeze" and p.hard_stop_pct == 2.0 and p.chop_max == 5
    assert p.session == "off_rth" and p.cost_pct == 0.11
    assert Params.from_dict({"mode": "banana"}).mode == "both"
    assert "본장 밖" in p.label_ko


def test_백테스트_결과는_전체_뒤집기_IS_OOS_판정을_다_담는다():
    res = run_backtest(synthetic(), Params())
    assert res["ok"]
    for k in ("all", "flip", "is", "oos", "monthly", "equity", "trades", "verdict", "params"):
        assert k in res, k
    assert res["all"]["n"] == len(res["trades"]) + res["trades_truncated"]
    assert res["is"]["n"] + res["oos"]["n"] == res["all"]["n"]
    assert res["verdict"]["grade"] in ("pass", "weak", "fail", "none")
    assert len(res["equity"]) == res["all"]["n"]
    t = res["trades"][0]
    assert {"entry_at", "exit_at", "r", "reason", "held_min", "kind_ko"} <= set(t)


def test_봉이_모자라면_백테스트가_거절한다():
    res = run_backtest(synthetic(100), Params())
    assert not res["ok"] and "5분봉" in res["error_ko"]


def test_마지막_봉_상태는_워밍업_전엔_신호를_믿지_않는다():
    st = latest_state(synthetic(100), Params())
    assert st["signal"] == 0 and not st["ok"]
    st = latest_state(synthetic(), Params())
    assert st["ok"] and st["bars"] == 4000 and "phase" not in st
    assert ma5m.phase_ko(st)


def test_1분봉을_5분봉으로_묶는_경계가_맞다():
    T = np.arange(0, 600, 60) + 1_750_000_000 // 300 * 300    # 10개 1분봉 = 5분봉 2개
    o = np.arange(10, dtype=float)
    B = ma5m.resample_5m(T, o, o + 1, o - 1, o + 0.5, np.ones(10))
    assert len(B) == 2
    assert B.o[0] == 0 and B.c[0] == 4.5 and B.h[0] == 5 and B.l[0] == -1 and B.v[0] == 5
    assert B.T[1] - B.T[0] == 300


# ── 시그널 엔진 ──────────────────────────────────────────
class FakeState:
    def __init__(self, symbol, direction, setup_id, created_at=None, bid="br_000000001"):
        from th.execution.models import Bracket, BracketState, OrderType

        self.bracket = Bracket(bracket_id=bid, symbol=symbol, direction=direction, qty=1.0,
                               stop_price=97.0, entry_type=OrderType.MARKET, setup_id=setup_id,
                               created_at=created_at or datetime.now(UTC))
        self._st = BracketState(bracket=self.bracket, position_qty=1.0, avg_entry=100.0)
        self.avg_entry = 100.0

    @property
    def is_open(self):
        return self._st.closed_at is None

    @property
    def has_position(self):
        return True


class FakeRunner:
    """주문 경로를 흉내 낸다 — 엔진이 뭘 넘기는지만 본다."""

    def __init__(self):
        self.requests = []
        self.closed = []
        self.ok = True
        self.mgr = self

    @property
    def states(self):
        return self._states

    _states: dict = {}

    async def open_from_request(self, req):
        from th.execution.runner import OrderOutcome

        self.requests.append(req)
        if self.ok:
            return OrderOutcome(True, bracket_id="br_testtest")
        return OrderOutcome(False, reasons=("가드 사유",))

    async def close_now(self, bracket_id, reason="MANUAL"):
        self.closed.append((bracket_id, reason))
        self._states.pop(bracket_id, None)


def _engine(monkeypatch_targets, bars: Bars, *, runner=None, enabled=True, quote_mid=100.0):
    from th.execution import signals

    async def src(_sym):
        return bars

    logged = []

    async def fake_add_signal(**kw):
        logged.append(kw)
        return 1

    class Q:
        mid = quote_mid

    async def fake_quote(_sym, **_kw):
        return Q() if quote_mid else None

    signals.store.add_signal = fake_add_signal
    signals.quotes.latest_quote = fake_quote
    r = runner or FakeRunner()
    r._states = {}
    eng = signals.SignalEngine(r, ["SOXLUSDT"], Params(), bars_source=src, notional_x=1.0,
                               enabled=enabled)
    return eng, r, logged


def _bars_with_signal_at_end() -> tuple[Bars, int]:
    B = synthetic()
    S = compute_signals(B, Params())
    idx = np.flatnonzero(S.d != 0)
    i = int(idx[len(idx) // 2])
    cut = Bars(B.T[:i + 1], B.o[:i + 1], B.h[:i + 1], B.l[:i + 1], B.c[:i + 1], B.v[:i + 1])
    # 잘라도 마지막 봉의 신호가 살아 있어야 한다 (계산은 과거만 본다)
    assert compute_signals(cut, Params()).d[-1] == S.d[i]
    return cut, int(S.d[i])


def test_엔진은_러너의_주문_경로로만_연다():
    from th.execution.signals import STOP_MARGIN_PCT

    bars, d = _bars_with_signal_at_end()
    eng, r, logged = _engine(None, bars, quote_mid=100.0)
    now = datetime.fromtimestamp(int(bars.T[-1]) + 306, UTC)
    out = run(eng.tick(now))
    assert out[0]["action"] == "open", out
    assert len(r.requests) == 1
    req = r.requests[0]
    assert req.mode == "paper" and req.setup_id == SETUP_ID and req.symbol == "SOXLUSDT"
    assert req.direction == ("LONG" if d > 0 else "SHORT")
    stop_pct = abs(100.0 - req.stop) / 100.0 * 100
    assert abs(stop_pct - (3.0 - STOP_MARGIN_PCT)) < 1e-9, "손절은 현재가 기준 3% 안쪽"
    assert abs(req.risk_pct - stop_pct * 1.0) < 1e-9, "노셔널 1배 ⇔ risk_pct = 손절폭 × 1"
    assert req.entry is None, "시장가"
    assert logged and logged[0]["action"] == "open" and logged[0]["bracket_id"] == "br_testtest"
    # 같은 봉은 두 번 판단하지 않는다
    out2 = run(eng.tick(now))
    assert out2[0]["action"] == "seen" and len(r.requests) == 1


def test_가드가_막으면_건너뛰고_사유를_남긴다():
    bars, _ = _bars_with_signal_at_end()
    eng, r, logged = _engine(None, bars)
    r.ok = False
    out = run(eng.tick(datetime.fromtimestamp(int(bars.T[-1]) + 306, UTC)))
    assert out[0]["action"] == "skip" and "가드" in out[0]["reason"]
    assert logged[0]["action"] == "skip" and "가드 사유" in logged[0]["reason"]


def test_꺼져_있으면_관찰만_한다():
    bars, _ = _bars_with_signal_at_end()
    eng, r, logged = _engine(None, bars, enabled=False)
    out = run(eng.tick(datetime.fromtimestamp(int(bars.T[-1]) + 306, UTC)))
    assert out[0]["action"] == "skip" and not r.requests
    assert eng.status()["states"][0]["ok"]


def test_시세가_없으면_열지_않는다():
    bars, _ = _bars_with_signal_at_end()
    eng, r, logged = _engine(None, bars, quote_mid=0.0)
    out = run(eng.tick(datetime.fromtimestamp(int(bars.T[-1]) + 306, UTC)))
    assert out[0]["action"] == "skip" and "시세" in out[0]["reason"] and not r.requests


def test_낡은_봉으로는_판단하지_않는다():
    bars, _ = _bars_with_signal_at_end()
    eng, r, logged = _engine(None, bars)
    out = run(eng.tick(datetime.fromtimestamp(int(bars.T[-1]) + 3600, UTC)))
    assert out[0]["action"] == "error" and not r.requests


def test_종가가_20선을_반대로_넘으면_엔진_브래킷을_닫는다():
    B = synthetic()
    S = compute_signals(B, Params())
    # 롱 청산 조건인 봉을 찾는다: 종가 < EMA20
    i = int(np.flatnonzero((B.c < S.e20) & (np.arange(len(B)) > 300))[0])
    cut = Bars(B.T[:i + 1], B.o[:i + 1], B.h[:i + 1], B.l[:i + 1], B.c[:i + 1], B.v[:i + 1])
    eng, r, logged = _engine(None, cut)
    r._states = {"br_000000001": FakeState("SOXLUSDT", "LONG", SETUP_ID)}
    run(eng.tick(datetime.fromtimestamp(int(cut.T[-1]) + 306, UTC)))
    assert r.closed == [("br_000000001", "EMA20_CROSS")]
    assert any(k["action"] == "exit" for k in logged)


def test_손주문_브래킷은_건드리지_않고_그_종목_진입도_막는다():
    bars, _ = _bars_with_signal_at_end()
    eng, r, logged = _engine(None, bars)
    r._states = {"br_000000001": FakeState("SOXLUSDT", "LONG", "")}      # 손주문
    out = run(eng.tick(datetime.fromtimestamp(int(bars.T[-1]) + 306, UTC)))
    assert not r.closed, "손주문 브래킷을 엔진이 닫으면 안 된다"
    assert out[0]["action"] == "skip" and "손주문" in out[0]["reason"] and not r.requests


def test_다음_판단_시각은_5분_경계_뒤다():
    from th.execution.signals import GRACE_S, align_next

    t = datetime(2026, 9, 9, 10, 2, 30, tzinfo=UTC)
    assert align_next(t) == datetime(2026, 9, 9, 10, 5, GRACE_S, tzinfo=UTC)
    t = datetime(2026, 9, 9, 10, 5, 2, tzinfo=UTC)
    assert align_next(t) == datetime(2026, 9, 9, 10, 5, GRACE_S, tzinfo=UTC)
    t = datetime(2026, 9, 9, 10, 5, 30, tzinfo=UTC)
    assert align_next(t) == datetime(2026, 9, 9, 10, 10, GRACE_S, tzinfo=UTC)


def test_엔진_코드에는_주문을_직접_만드는_코드가_없다():
    src = (ROOT / "src" / "th" / "execution" / "signals.py").read_text(encoding="utf-8")
    for bad in ("Bracket(", "GuardInput", "size_from_risk", "mgr.open(", "place(", "PaperClient",
                "LiveClient", "/fapi/v1/order"):
        assert bad not in src, f"시그널 엔진이 주문 경로를 우회한다: {bad}"
    assert "open_from_request" in src


def test_세션_게이트는_주식_perp_에만_적용된다():
    from th.execution.runner import session_gate

    blocked, why = run(session_gate("BTCUSDT"))
    assert blocked is False and why == ""


def test_동시_브래킷_한도는_설정으로_올릴_수_있다():
    from th.execution import GuardInput, check
    from th.execution.models import Bracket, OrderType

    b = Bracket(bracket_id="br_x", symbol="SOXLUSDT", direction="LONG", qty=1.0, stop_price=99.0,
                entry_type=OrderType.MARKET)
    base = dict(bracket=b, ref_price=100.0, execution_mode="paper", equity_usdt=10_000)
    assert not check(GuardInput(**base, open_bracket_count=1)).ok, "기본값은 여전히 1개"
    assert check(GuardInput(**base, open_bracket_count=1, max_open_brackets=4)).ok
    assert not check(GuardInput(**base, open_bracket_count=4, max_open_brackets=4)).ok


# ── 누적 기록 계산 ────────────────────────────────────────
def test_누적_기록은_체결된_것만_세고_MDD와_연승패를_낸다():
    from th.api.history import build_history

    t0 = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    rows = []
    for k, r in enumerate([1.0, -1.0, -1.0, 2.0, 0.5, -0.5]):
        rows.append({
            "bracket_id": f"br_{k:08d}", "symbol": "SOXLUSDT" if k % 2 else "BTCUSDT",
            "direction": "LONG", "setup_id": SETUP_ID if k < 4 else "", "avg_entry": 100.0,
            "exit_price": 100 + r, "qty": 1.0, "stop_price": 97.0, "realized_pnl": r * 3,
            "realized_r": r, "fees": 0.1, "close_reason": "EMA20_CROSS", "reason": "",
            "entry_at": t0 + timedelta(hours=k), "exit_at": t0 + timedelta(hours=k, minutes=30),
            "created_at": t0 + timedelta(hours=k), "closed_at": t0 + timedelta(hours=k, minutes=30),
        })
    rows.append({"bracket_id": "br_unfilled", "symbol": "ETHUSDT", "direction": "SHORT", "setup_id": "",
                 "avg_entry": None, "exit_price": None, "qty": 1.0, "stop_price": 103.0,
                 "realized_pnl": 0.0, "realized_r": 0.0, "fees": 0.0, "close_reason": "EXPIRED",
                 "reason": "", "entry_at": None, "exit_at": t0, "created_at": t0, "closed_at": t0})
    h = build_history(rows, [], mode="paper")
    s = h["summary"]
    assert s["n"] == 6 and s["unfilled"] == 1
    assert abs(s["sum_r"] - 1.0) < 1e-9 and s["wins"] == 3
    assert s["mdd_r"] == 2.0 and s["max_loss_streak"] == 2
    assert h["auto"]["n"] == 4 and h["manual"]["n"] == 2
    assert [c["cum_r"] for c in h["curve"]] == [1.0, 0.0, -1.0, 1.0, 1.5, 1.0]
    assert {g["key"] for g in h["by_symbol"]} == {"SOXLUSDT", "BTCUSDT", "ETHUSDT"}
    assert h["daily"] and h["daily"][0]["n"] == 6
    assert not s["enough_sample"]
