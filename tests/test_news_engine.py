"""v3.0 M2 — 판단·작전·가드 (docs/17 §2·§4·§5). 네트워크·DB 없이.

가장 중요한 것:
  · 판단이 없으면(거절·파싱 실패·범위 위반) 주문이 없다
  · 크기 변환: risk_pct = 손절폭 × (마진%/100 × 배율) → 노셔널 = 시드 × 마진% × 배율 / 100
  · 손절 상한 5% 는 요청이 아무리 올려도 못 넘긴다 · 노출 3.0배 / 종목 1.0배는 가드가 막는다
  · approve 모드에서는 승인 없이 아무것도 열리지 않는다
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from th.execution import guard
from th.execution.guard import GuardInput, check
from th.execution.models import LONG, SHORT, Bracket
from th.execution.paper import Quote
from th.news import engine as eng_mod
from th.news import store
from th.news.engine import NewsEngine, leg_requests
from th.news.judge import Judge, JudgeContext, cost_of, usage_dict
from th.news.models import Judgment, Leg, NewsItem, Plan, Triage

NOW = datetime(2026, 9, 22, 13, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def bracket(**kw) -> Bracket:
    base = dict(bracket_id="br-1", symbol="NVDAUSDT", direction=LONG, qty=10.0, stop_price=97.0)
    base.update(kw)
    return Bracket(**base)


def ginput(b: Bracket, **kw) -> GuardInput:
    base = dict(bracket=b, ref_price=100.0, execution_mode="paper", equity_usdt=10_000.0)
    base.update(kw)
    return GuardInput(**base)


# ─────────────────────────────────────────────────────────
# 가드 확장
# ─────────────────────────────────────────────────────────

def test_stop_max_override_needs_explicit_request_and_never_exceeds_hard_cap():
    b = bracket(stop_price=95.5)                        # 4.5%
    assert not check(ginput(b)).ok                      # 기본 3%
    assert check(ginput(b, stop_max_pct=5.0)).ok        # 요청이 올리면 통과
    b6 = bracket(stop_price=94.0)                       # 6%
    r = check(ginput(b6, stop_max_pct=8.0))
    assert not r.ok and any("최대 5.0%" in x for x in r.reasons)
    assert guard.STOP_MAX_PCT_HARD == 5.0


def test_exposure_caps_are_enforced_only_when_caller_knows_the_numbers():
    b = bracket(qty=60.0)                               # 6,000 USDT = 0.6배
    assert check(ginput(b)).ok                          # None → 검사 안 함
    r = check(ginput(b, open_notional_usdt=25_000.0))   # 2.5 + 0.6 = 3.1배
    assert not r.ok and any("노출 합계" in x for x in r.reasons)
    assert check(ginput(b, open_notional_usdt=24_000.0)).ok
    r = check(ginput(b, symbol_open_notional_usdt=5_000.0))   # 0.5 + 0.6 = 1.1배
    assert not r.ok and any("한 종목 한도" in x for x in r.reasons)
    assert check(ginput(b, symbol_open_notional_usdt=4_000.0)).ok


# ─────────────────────────────────────────────────────────
# 크기 변환 · 다리 요청
# ─────────────────────────────────────────────────────────

def plan(**kw) -> Plan:
    base = dict(stop_pct=3.0, target_pct=6.0, tp2_pct=9.0, max_hold_hours=48,
                legs=[Leg(margin_pct=4, entry="market", expire_hours=0.1),
                      Leg(margin_pct=3, entry="limit", limit_offset_pct=-1.5, expire_hours=24),
                      Leg(margin_pct=3, entry="limit", limit_offset_pct=-2.5, expire_hours=24)],
                invalidation_ko="x")
    base.update(kw)
    return Plan(**base)


def test_leg_requests_sizing_and_prices_long():
    reqs = leg_requests(symbol="NVDAUSDT", direction=LONG, plan=plan(), ref=100.0, leverage=10,
                        stop_max_pct=5.0, reason="t")
    assert [why for _, why in reqs] == ["", "", ""]
    r1, r2, r3 = [r for r, _ in reqs]
    assert r1.entry is None and r2.entry == pytest.approx(98.5) and r3.entry == pytest.approx(97.5)
    for r in (r1, r2, r3):
        assert r.stop == pytest.approx(97.0) and r.tp1 == pytest.approx(106.0) and r.tp2 == pytest.approx(109.0)
        assert r.setup_id == "news_event" and r.stop_max_pct == 5.0 and r.mode == "paper"
    # risk_pct = 손절폭 3% × notional_x(0.4 / 0.3 / 0.3)
    assert r1.risk_pct == pytest.approx(1.2) and r2.risk_pct == pytest.approx(0.9)
    assert r1.expire_s == 360 and r2.expire_s == 24 * 3600
    # 러너의 수량 역산: qty = 시드 × risk% ÷ 손절거리 → 노셔널 = 시드 × 0.4
    equity = 10_000.0
    qty = equity * r1.risk_pct / 100 / abs(100.0 - r1.stop)
    assert qty * 100.0 == pytest.approx(equity * 0.4)


def test_leg_requests_short_and_wrong_side_limit_is_skipped():
    p = plan(legs=[Leg(margin_pct=5, entry="market", expire_hours=0.1),
                   Leg(margin_pct=5, entry="limit", limit_offset_pct=-1.0, expire_hours=2)])
    reqs = leg_requests(symbol="NVDAUSDT", direction=SHORT, plan=p, ref=100.0, leverage=10,
                        stop_max_pct=5.0, reason="t")
    assert reqs[0][0].stop == pytest.approx(103.0) and reqs[0][0].tp1 == pytest.approx(94.0)
    assert reqs[1][0] is None and "방향과 반대" in reqs[1][1]


def test_plan_limit_offset_must_sit_inside_the_stop():
    p = plan(legs=[Leg(margin_pct=5, entry="limit", limit_offset_pct=-3.0, expire_hours=2)])
    assert any("안에 있지 않다" in x for x in p.problems(margin_max=10, stop_min=1, stop_max=5, hold_max=168))


# ─────────────────────────────────────────────────────────
# 판단 (가짜 클라이언트)
# ─────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, parsed, *, stop_reason="end_turn", usage=None):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(**(usage or {"input_tokens": 1000, "output_tokens": 200,
                                                 "cache_read_input_tokens": 500}))
        self.stop_details = None


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(parse=self._parse)

    async def _parse(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def judge_with(responses) -> tuple[Judge, FakeClient]:
    c = FakeClient(responses)
    j = Judge(model="claude-opus-5", triage_model="claude-haiku-4-5", universe=["NVDAUSDT", "AMDUSDT", "SOXLUSDT"],
              margin_max=10, leverage=10, stop_min=1, stop_max=5, hold_max=168, client=c)
    return j, c


def item(**kw) -> NewsItem:
    base = dict(source="telegram", source_id="1", ts=NOW - timedelta(minutes=3), title="Nvidia beats and raises",
                symbols=("NVDAUSDT",))
    base.update(kw)
    return NewsItem(**base)


def good_judgment(**kw) -> Judgment:
    base = dict(relevant=True, symbols=["NVDAUSDT", "TSLAUSDT"], direction="LONG", confidence=0.85, magnitude="high",
                horizon_hours=48, thesis_ko="가이던스 상향", risks_ko=["차익실현"], already_priced_in=False, plan=plan())
    base.update(kw)
    return Judgment(**base)


def test_cost_uses_cache_prices_and_unknown_models_are_priced_high():
    u = usage_dict(SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=500,
                                   cache_creation_input_tokens=0))
    assert u == {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 500}
    assert cost_of("claude-haiku-4-5", u) == pytest.approx((1000 * 1 + 200 * 5 + 500 * 0.1) / 1e6)
    assert cost_of("claude-unknown", u) == cost_of("claude-opus-5", u)


def test_full_judgment_filters_symbols_outside_universe_and_caches_system_prompt():
    j, c = judge_with([FakeResp(good_judgment())])
    out, rec = run(j.full(item(), JudgeContext(now=NOW)))
    assert out is not None and out.symbols == ["NVDAUSDT"]           # TSLA 는 유니버스 밖
    assert rec.stage == "full" and rec.cost_usd > 0 and rec.error == ""
    kw = c.calls[0]
    assert kw["model"] == "claude-opus-5" and kw["output_format"] is Judgment
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Nvidia beats and raises" in kw["messages"][0]["content"]


def test_plan_out_of_range_is_dropped_not_clamped():
    j, _ = judge_with([FakeResp(good_judgment(plan=plan(stop_pct=7.0)))])
    out, rec = run(j.full(item(), JudgeContext(now=NOW)))
    assert out is not None and out.plan is None
    assert rec.error.startswith("계획 범위 위반")
    assert out.actionable(0.7) == (False, "계획 없음")


def test_refusal_and_parse_failure_and_api_errors_become_error_records():
    import anthropic

    j, _ = judge_with([FakeResp(None, stop_reason="refusal"), FakeResp(None, stop_reason="max_tokens"),
                       anthropic.APIConnectionError(request=None)])
    out, rec = run(j.full(item(), JudgeContext(now=NOW)))
    assert out is None and "거절" in rec.error
    out, rec = run(j.full(item(), JudgeContext(now=NOW)))
    assert out is None and "파싱 실패" in rec.error and "max_tokens" in rec.error
    out, rec = run(j.full(item(), JudgeContext(now=NOW)))
    assert out is None and "연결 실패" in rec.error


def test_triage_uses_cheap_model():
    j, c = judge_with([FakeResp(Triage(relevant=True, magnitude="high", symbols=["nvda"], reason_ko="실적"))])
    t, rec = run(j.triage(item(), JudgeContext(now=NOW)))
    assert t.symbols == ["NVDAUSDT"] and c.calls[0]["model"] == "claude-haiku-4-5" and rec.stage == "triage"


def test_effort_goes_to_the_full_judgment_only_and_never_to_haiku():
    c = FakeClient([FakeResp(Triage(relevant=True, magnitude="high", symbols=[], reason_ko="x")),
                    FakeResp(good_judgment())])
    j = Judge(model="claude-opus-5", triage_model="claude-haiku-4-5", universe=["NVDAUSDT"], margin_max=10,
              leverage=10, stop_min=1, stop_max=5, hold_max=168, client=c, effort="low")
    run(j.triage(item(), JudgeContext(now=NOW)))
    run(j.full(item(), JudgeContext(now=NOW)))
    assert "output_config" not in c.calls[0]                      # 트리아지(Haiku)는 effort 를 못 받는다
    assert c.calls[1]["output_config"] == {"effort": "low"}
    c2 = FakeClient([FakeResp(good_judgment())])
    j2 = Judge(model="claude-haiku-4-5", triage_model="claude-haiku-4-5", universe=["NVDAUSDT"], margin_max=10,
               leverage=10, stop_min=1, stop_max=5, hold_max=168, client=c2, effort="low")
    run(j2.full(item(), JudgeContext(now=NOW)))
    assert "output_config" not in c2.calls[0]


# ─────────────────────────────────────────────────────────
# 엔진 (가짜 러너 · 가짜 저장소)
# ─────────────────────────────────────────────────────────

class MemStore:
    """news/store 의 함수들을 메모리로 흉내낸다. monkeypatch 로 모듈에 꽂는다."""

    def __init__(self):
        self.judgments = []
        self.camps: dict[str, dict] = {}
        self.legs: dict[tuple[str, int], dict] = {}
        self.brackets: dict[str, dict] = {}
        self.judged: dict[int, int | None] = {}
        self.cost = 0.0
        self.items: list[dict] = []
        self._n = 0

    async def unjudged(self, limit):
        return [r for r in self.items if r["id"] not in self.judged][:limit]

    async def mark_judged(self, nid, jid=None):
        self.judged[nid] = jid

    async def add_judgment(self, nid, **kw):
        self.judgments.append({"news_id": nid, **kw})
        self.cost += float(kw.get("cost_usd") or 0)
        return len(self.judgments)

    async def cost_today(self, now=None):
        return self.cost

    def day_start_utc(self, now=None):
        return store.day_start_utc(now)

    async def campaign_create(self, **kw):
        self._n += 1
        cid = f"camp-{self._n:012d}"
        self.camps[cid] = {"id": cid, "created_at": NOW, "opened_at": None, "closed_at": None, "close_reason": "",
                           "approval": {}, "summary": {}, "realized_r": None, **kw}
        for i, leg in enumerate(kw["plan"]["legs"], 1):
            self.legs[(cid, i)] = {"campaign_id": cid, "leg_no": i, "bracket_id": None, "status": "", "note": "",
                                   "margin_pct": leg["margin_pct"], "entry_type": leg["entry"]}
        return cid

    async def campaign_update(self, cid, **f):
        self.camps[cid].update(f)

    async def legs_of(self, cid):
        return [dict(v) for k, v in sorted(self.legs.items()) if k[0] == cid]

    async def campaign_get(self, cid):
        c = self.camps.get(cid)
        return None if c is None else {**c, "legs": await self.legs_of(cid)}

    async def open_campaigns(self):
        return [await self.campaign_get(c) for c in self.camps if self.camps[c]["status"] in store.LIVE_STATUSES]

    async def open_campaign_for(self, symbol):
        for c in reversed(list(self.camps)):
            if self.camps[c]["symbol"] == symbol and self.camps[c]["status"] in store.LIVE_STATUSES:
                return await self.campaign_get(c)
        return None

    async def leg_set(self, cid, leg_no, **f):
        self.legs[(cid, leg_no)].update({k: v for k, v in f.items() if v is not None})

    async def brackets_by_ids(self, ids):
        return {i: self.brackets[i] for i in ids if i in self.brackets}

    async def recent(self, *a, **k):
        return []

    def row_to_item(self, d):
        return store.row_to_item(d)


class FakeRunner:
    def __init__(self, ok=True):
        self.reqs = []
        self.ok = ok
        self.closed = []
        self.mgr = SimpleNamespace(close_now=self._close)

    async def open_from_request(self, req):
        self.reqs.append(req)
        if not self.ok:
            return SimpleNamespace(ok=False, bracket_id=None, reasons=("가드 사유",))
        return SimpleNamespace(ok=True, bracket_id=f"br-{len(self.reqs)}", reasons=())

    async def _close(self, bid, reason="MANUAL"):
        self.closed.append((bid, reason))


REFUSE = object()      # FakeJudge(full=REFUSE) → 본판단이 실패(거절)한 상황


class FakeJudge:
    model = "claude-opus-5"
    triage_model = "claude-haiku-4-5"

    def __init__(self, triage=None, full=None):
        self._t = triage or Triage(relevant=True, magnitude="high", symbols=["NVDAUSDT"], reason_ko="실적")
        self._f = None if full is REFUSE else (full or good_judgment(symbols=["NVDAUSDT"]))
        self.calls = 0

    async def triage(self, item, ctx):
        self.calls += 1
        return self._t, SimpleNamespace(stage="triage", model=self.triage_model, result=self._t.model_dump(),
                                        usage={}, cost_usd=0.001, error="")

    async def full(self, item, ctx):
        self.calls += 1
        if self._f is None:
            return None, SimpleNamespace(stage="full", model=self.model, result=None, usage={}, cost_usd=0.0,
                                         error="모델이 거절함")
        return self._f, SimpleNamespace(stage="full", model=self.model, result=self._f.model_dump(), usage={},
                                        cost_usd=0.05, error="")


async def ctx_builder(item, now=None):
    return JudgeContext(now=now or NOW)


@pytest.fixture
def mem(monkeypatch):
    m = MemStore()
    for name in ("unjudged", "mark_judged", "add_judgment", "cost_today", "campaign_create",
                 "campaign_update", "legs_of", "campaign_get", "open_campaigns", "open_campaign_for", "leg_set",
                 "brackets_by_ids", "recent", "row_to_item"):
        monkeypatch.setattr(store, name, getattr(m, name))

    async def fake_quote(symbol, **kw):
        return Quote(symbol, bid=99.9, ask=100.1, at=NOW)

    monkeypatch.setattr(eng_mod.quotes, "latest_quote", fake_quote)
    return m


def engine(runner, judge, mode="paper", **kw) -> NewsEngine:
    base = dict(exec_mode=mode, min_confidence=0.7, leverage=10, stop_max_pct=5.0, approval_ttl_min=30,
                daily_usd_cap=5.0, context_builder=ctx_builder)
    base.update(kw)
    return NewsEngine(runner, judge, **base)


def test_paper_mode_opens_all_legs_through_the_runner(mem):
    r = FakeRunner()
    e = engine(r, FakeJudge())
    out = run(e.process(item(), 1, NOW))
    assert out["action"] == "open" and len(r.reqs) == 3
    assert all(q.setup_id == "news_event" and q.stop_max_pct == 5.0 for q in r.reqs)
    assert r.reqs[0].risk_pct == pytest.approx(1.2)                       # 마진 4% × 10배 × 손절 3%
    camp = mem.camps[out["campaign_id"]]
    assert camp["status"] == "open" and camp["opened_at"] == NOW
    assert [mem.legs[(camp["id"], i)]["bracket_id"] for i in (1, 2, 3)] == ["br-1", "br-2", "br-3"]
    assert mem.judged[1] == 2 and len(mem.judgments) == 2                # 트리아지 + 본판단 기록


def test_no_judgment_means_no_order(mem):
    r = FakeRunner()
    out = run(engine(r, FakeJudge(full=REFUSE)).process(item(), 1, NOW))
    assert out["action"] == "error" and r.reqs == [] and mem.camps == {}
    assert mem.judged[1] is not None                                     # 실패도 기록된다


def test_low_confidence_or_priced_in_is_skipped(mem):
    r = FakeRunner()
    out = run(engine(r, FakeJudge(full=good_judgment(symbols=["NVDAUSDT"], confidence=0.6))).process(item(), 1, NOW))
    assert out["action"] == "skip" and out["reason"].startswith("확신도") and r.reqs == []
    out = run(engine(r, FakeJudge(full=good_judgment(symbols=["NVDAUSDT"], already_priced_in=True))).process(item(), 2, NOW))
    assert out["action"] == "skip" and out["reason"] == "이미 반영됨"


def test_triage_low_does_not_call_the_expensive_model(mem):
    fj = FakeJudge(triage=Triage(relevant=True, magnitude="low", symbols=[], reason_ko="사소"))
    out = run(engine(FakeRunner(), fj).process(item(), 1, NOW))
    assert out["action"] == "skip" and fj.calls == 1 and len(mem.judgments) == 1


def test_approve_mode_opens_nothing_until_approved(mem):
    r = FakeRunner()
    e = engine(r, FakeJudge(), mode="approve")
    out = run(e.process(item(), 1, NOW))
    assert out["action"] == "pending_approval" and r.reqs == []
    cid = out["campaign_id"]
    assert mem.camps[cid]["status"] == "pending_approval"
    ok, msg = run(e.approve(cid, by="tester", via="test"))
    assert ok and len(r.reqs) == 3 and mem.camps[cid]["status"] == "open"
    assert mem.camps[cid]["approval"]["by"] == "tester"
    assert run(e.approve(cid, by="tester", via="test")) == (False, "승인 대기가 아니다 (open)")


def test_pending_approval_expires_after_ttl(mem):
    e = engine(FakeRunner(), FakeJudge(), mode="approve")
    cid = run(e.process(item(), 1, NOW))["campaign_id"]
    run(e.housekeep(NOW + timedelta(minutes=10)))
    assert mem.camps[cid]["status"] == "pending_approval"
    run(e.housekeep(NOW + timedelta(minutes=31)))
    assert mem.camps[cid]["status"] == "expired"


def test_observe_mode_records_a_proposal_only(mem):
    r = FakeRunner()
    out = run(engine(r, FakeJudge(), mode="observe").process(item(), 1, NOW))
    assert out["action"] == "proposed" and r.reqs == []
    assert mem.camps[out["campaign_id"]]["status"] == "proposed"


def test_second_news_on_a_symbol_with_an_open_campaign_is_skipped(mem):
    r = FakeRunner()
    e = engine(r, FakeJudge())
    run(e.process(item(), 1, NOW))
    out = run(e.process(item(source_id="2"), 2, NOW))
    assert out["action"] == "skip" and "이미 열린 작전" in out["reason"] and len(r.reqs) == 3


def test_opposite_high_impact_news_closes_the_open_campaign(mem):
    r = FakeRunner()
    e = engine(r, FakeJudge())
    cid = run(e.process(item(), 1, NOW))["campaign_id"]
    e2 = engine(r, FakeJudge(full=good_judgment(symbols=["NVDAUSDT"], direction="SHORT", confidence=0.9)))
    out = run(e2.process(item(source_id="2", title="Nvidia export ban"), 2, NOW))
    assert out["reversed"] == [cid]
    assert sorted(r.closed) == [("br-1", "NEWS_REVERSAL"), ("br-2", "NEWS_REVERSAL"), ("br-3", "NEWS_REVERSAL")]
    assert mem.camps[cid]["close_reason"] == "NEWS_REVERSAL"


def test_guard_rejecting_every_leg_marks_the_campaign_rejected(mem):
    r = FakeRunner(ok=False)
    out = run(engine(r, FakeJudge()).process(item(), 1, NOW))
    assert out["action"] == "rejected"
    camp = mem.camps[out["campaign_id"]]
    assert camp["status"] == "rejected" and "가드 사유" in camp["close_reason"]
    assert all(mem.legs[(camp["id"], i)]["status"] == "skipped" for i in (1, 2, 3))


def test_daily_cost_cap_stops_judging_but_marks_the_item(mem):
    mem.cost = 5.0
    fj = FakeJudge()
    out = run(engine(FakeRunner(), fj).process(item(), 1, NOW))
    assert out["action"] == "capped" and fj.calls == 0 and mem.judged[1] is None


def test_housekeep_closes_campaign_when_all_legs_closed_and_times_out_open_ones(mem):
    r = FakeRunner()
    e = engine(r, FakeJudge())
    cid = run(e.process(item(), 1, NOW))["campaign_id"]
    for i, bid in enumerate(("br-1", "br-2", "br-3"), 1):
        mem.brackets[bid] = {"closed_at": None, "position_qty": 10.0 if i == 1 else 0.0, "realized_r": 0.0,
                             "realized_pnl": 0.0, "close_reason": ""}
    run(e.housekeep(NOW + timedelta(hours=1)))
    assert mem.legs[(cid, 1)]["status"] == "filled" and mem.camps[cid]["status"] == "open"
    # 시간 만료 (max_hold 48h)
    run(e.housekeep(NOW + timedelta(hours=49)))
    assert [c for c in r.closed] == [("br-1", "TIME"), ("br-2", "TIME"), ("br-3", "TIME")]
    # 전 다리 종료 → 작전 종료, R 합산
    mem.brackets["br-1"].update(closed_at=NOW, realized_r=1.5, close_reason="TP1")
    mem.brackets["br-2"].update(closed_at=NOW, realized_r=0.0, close_reason="ENTRY_EXPIRED")
    mem.brackets["br-3"].update(closed_at=NOW, realized_r=-1.0, close_reason="STOP")
    run(e.housekeep(NOW + timedelta(hours=50)))
    assert mem.camps[cid]["status"] == "closed" and mem.camps[cid]["realized_r"] == pytest.approx(0.5)
    assert mem.legs[(cid, 2)]["status"] == "expired"


def test_status_is_serializable_and_reports_mode():
    e = engine(FakeRunner(), FakeJudge(), mode="approve")
    st = e.status()
    assert st["exec_mode"] == "approve" and st["alive"] is False and st["model"] == "claude-opus-5"


def test_engine_rejects_unknown_exec_mode():
    with pytest.raises(ValueError):
        engine(FakeRunner(), FakeJudge(), mode="live")


# ─────────────────────────────────────────────────────────
# 공개 호가 폴백
# ─────────────────────────────────────────────────────────

def test_public_quote_fallback_only_for_non_collected_symbols(monkeypatch):
    import importlib

    from th.execution import quotes

    importlib.reload(quotes)          # test_ma5m 이 latest_quote 를 통째로 바꿔 두고 복구하지 않는다

    class Api:
        async def book_ticker(self, symbol):
            return {"bidPrice": "99.9", "askPrice": "100.1"}

        async def klines(self, symbol, interval, limit=2, **kw):
            return [SimpleNamespace(high=101.0, low=99.0, ts=NOW)]

    async def no_db(symbol, *, max_age_s):
        return None

    monkeypatch.setattr(quotes, "_db_quote", no_db)
    quotes.set_public_api(Api())
    quotes._bar_cache.clear()
    q = run(quotes.latest_quote("NVDAUSDT", allow_public=True))
    assert q is not None and q.mid == pytest.approx(100.0) and q.high == 101.0 and q.bar_start == NOW
    assert run(quotes.latest_quote("NVDAUSDT", allow_public=False)) is None
    monkeypatch.setattr(quotes, "public_allowed", lambda s: s != "SOXLUSDT")
    assert run(quotes.latest_quote("SOXLUSDT")) is None
    assert run(quotes.latest_quote("AMDUSDT")) is not None
    quotes.set_public_api(None)


def test_server_starts_news_engine_only_when_enabled():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src" / "th" / "api" / "server.py").read_text(encoding="utf-8")
    assert "build_news_engine" in src and "news_enabled" in src
    body = src[src.index("async def stop_runner("):]
    assert "self.news" in body[:600]
