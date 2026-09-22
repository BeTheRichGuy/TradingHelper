"""뉴스 엔진 — 판단 → 작전 → 가드 → 페이퍼 주문 (docs/17 §5).

`th serve` 안에서 시그널 엔진과 나란히 돈다. 주문은 **오직** `PaperRunner.open_from_request()` 로만 낸다 —
웹 주문 패널·CLI·시그널 엔진과 완전히 같은 경로(수량 역산 → 반올림 → 안전장치 → 매니저)다.

실행 모드 (`TH_NEWS_EXEC`):
  observe  판단만. 작전은 proposed 로 남고 주문 없음
  paper    확신도 ≥ 임계값이면 바로 페이퍼 주문
  approve  pending_approval 로 두고 승인(`approve()`)된 것만 페이퍼 주문. 미승인은 TTL 뒤 expired
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from ..execution import quotes
from ..execution.models import LONG, SHORT
from ..execution.runner import OpenRequest
from ..logging_setup import get_logger
from . import store, tickers
from .judge import Judge, JudgeContext, make_context_builder
from .models import SETUP_ID, Judgment, Plan

log = get_logger(__name__)

TICK_S = 20
JUDGE_BATCH = 5
EXEC_MODES = ("observe", "paper", "approve")
CLOSE_TIME = "TIME"
CLOSE_REVERSAL = "NEWS_REVERSAL"

ContextBuilder = Callable[..., Awaitable[JudgeContext]]


def leg_requests(*, symbol: str, direction: str, plan: Plan, ref: float, leverage: float,
                 stop_max_pct: float, reason: str) -> list[tuple[OpenRequest | None, str]]:
    """계획 → 다리별 OpenRequest. 순수 함수. (요청, 건너뛴 이유) — 요청이 None 이면 그 다리는 안 연다.

    크기: 노셔널 = 시드 × (margin_pct/100 × leverage). 러너는 `qty = 시드 × risk_pct/100 ÷ 손절거리` 로
    수량을 역산하므로 `risk_pct = stop_pct × notional_x` 로 넘긴다 (시그널 엔진과 같은 변환).
    """
    sign = 1 if direction == LONG else -1
    stop = ref * (1 - sign * plan.stop_pct / 100)
    tp1 = ref * (1 + sign * plan.target_pct / 100)
    tp2 = ref * (1 + sign * plan.tp2_pct / 100) if plan.tp2_pct else None
    out: list[tuple[OpenRequest | None, str]] = []
    for i, leg in enumerate(plan.legs, 1):
        entry: float | None = None
        if leg.entry == "limit":
            off = float(leg.limit_offset_pct or 0.0)
            # 롱 눌림 매수는 아래(음수), 숏 되돌림 매도는 위(양수). 반대면 시장가보다 불리한 지정가라 안 연다
            if (direction == LONG and off > 0) or (direction == SHORT and off < 0):
                out.append((None, f"{i}번 다리 지정가 오프셋 {off:+g}% 가 방향과 반대"))
                continue
            entry = ref * (1 + off / 100)
        notional_x = leg.margin_pct / 100 * leverage
        risk_pct = plan.stop_pct * notional_x
        out.append((OpenRequest(
            mode="paper", symbol=symbol, direction=direction, stop=stop, entry=entry, tp1=tp1, tp2=tp2,
            risk_pct=risk_pct, expire_s=max(5, int(leg.expire_hours * 3600)),
            reason=f"뉴스 {i}/{len(plan.legs)} · {reason}"[:300], setup_id=SETUP_ID, stop_max_pct=stop_max_pct,
        ), ""))
    return out


class NewsEngine:
    def __init__(self, runner: Any, judge: Judge, *, exec_mode: str, min_confidence: float,
                 leverage: float, stop_max_pct: float, approval_ttl_min: int, daily_usd_cap: float,
                 context_builder: ContextBuilder, interval_s: int = TICK_S) -> None:
        if exec_mode not in EXEC_MODES:
            raise ValueError(f"실행 모드는 {EXEC_MODES} 중 하나: {exec_mode!r}")
        self.runner = runner
        self.judge = judge
        self.exec_mode = exec_mode
        self.min_confidence = float(min_confidence)
        self.leverage = float(leverage)
        self.stop_max_pct = float(stop_max_pct)
        self.approval_ttl = timedelta(minutes=int(approval_ttl_min))
        self.daily_usd_cap = float(daily_usd_cap)
        self.context_builder = context_builder
        self.interval_s = max(2, int(interval_s))
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.ticks = 0
        self.last_tick: datetime | None = None
        self.recent: list[dict[str, Any]] = []
        self.capped_day: datetime | None = None
        self.cost_today = 0.0
        self.on_campaign: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        """알림 훅 (M3 텔레그램). (event, campaign) — proposed | pending_approval | opened | closed | rejected."""

    # ── 수명 ─────────────────────────────────────────────
    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="news-engine")
        log.info("뉴스 엔진 시작 — 모드 %s · 확신도 ≥ %.2f · 손절 ≤ %g%% · 배율 %g · 일일 API 상한 $%g",
                 self.exec_mode, self.min_confidence, self.stop_max_pct, self.leverage, self.daily_usd_cap)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("뉴스 엔진 반복 실패 — 다음 반복에 계속")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except (TimeoutError, asyncio.TimeoutError):
                pass

    # ── 한 반복 ───────────────────────────────────────────
    async def tick(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        self.ticks += 1
        self.last_tick = now
        housekept = await self.housekeep(now)
        judged = await self.judge_pending(now)
        return {"housekept": housekept, "judged": judged}

    async def housekeep(self, now: datetime) -> dict[str, int]:
        """열린 작전 정리: 다리 상태 갱신 · 전부 닫히면 작전 종료 · 시간 만료 · 승인 TTL."""
        n = {"closed": 0, "expired": 0, "timed_out": 0}
        for camp in await store.open_campaigns():
            cid = camp["id"]
            if camp["status"] == "pending_approval":
                if now - camp["created_at"] > self.approval_ttl:
                    await store.campaign_update(cid, status="expired", closed_at=now, close_reason="승인 시간 초과")
                    n["expired"] += 1
                    await self._emit("expired", camp)
                continue
            if camp["status"] != "open":
                continue
            plan = camp.get("plan") or {}
            legs = camp.get("legs") or []
            ids = [l["bracket_id"] for l in legs if l.get("bracket_id")]
            rows = await store.brackets_by_ids(ids)
            all_done = bool(ids)
            realized_r = 0.0
            realized_pnl = 0.0
            for leg in legs:
                bid = leg.get("bracket_id")
                if not bid:
                    continue
                br = rows.get(bid)
                if br is None:
                    continue
                if br.get("closed_at") is not None:
                    st = "closed" if br.get("close_reason") != "ENTRY_EXPIRED" else "expired"
                    if leg.get("status") != st:
                        await store.leg_set(cid, int(leg["leg_no"]), status=st, note=str(br.get("close_reason") or ""))
                    realized_r += float(br.get("realized_r") or 0.0)
                    realized_pnl += float(br.get("realized_pnl") or 0.0)
                else:
                    all_done = False
                    if abs(float(br.get("position_qty") or 0.0)) > 1e-12 and leg.get("status") != "filled":
                        await store.leg_set(cid, int(leg["leg_no"]), status="filled")
            if all_done:
                await store.campaign_update(cid, status="closed", closed_at=now, realized_r=realized_r,
                                            close_reason=camp.get("close_reason") or "전 다리 종료",
                                            summary={"realized_pnl": realized_pnl})
                n["closed"] += 1
                await self._emit("closed", {**camp, "realized_r": realized_r})
                continue
            opened_at = camp.get("opened_at") or camp["created_at"]
            max_hold = int(plan.get("max_hold_hours") or 0)
            if max_hold and now - opened_at > timedelta(hours=max_hold):
                await self.close_campaign(camp, CLOSE_TIME, now=now)
                n["timed_out"] += 1
        return n

    async def judge_pending(self, now: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in await store.unjudged(JUDGE_BATCH):
            out.append(await self.process(store.row_to_item(row), int(row["id"]), now))
        return out

    # ── 판단 한 건 ────────────────────────────────────────
    async def _capped(self, now: datetime) -> bool:
        self.cost_today = await store.cost_today(now)
        if self.cost_today < self.daily_usd_cap:
            self.capped_day = None
            return False
        day = store.day_start_utc(now)
        if self.capped_day != day:
            self.capped_day = day
            log.warning("뉴스 판단 일일 비용 상한 도달 ($%.2f ≥ $%.2f) — 오늘은 판단을 멈춥니다 (수집은 계속)",
                        self.cost_today, self.daily_usd_cap)
        return True

    def _note(self, **row: Any) -> dict[str, Any]:
        row.setdefault("ts", datetime.now(UTC).isoformat())
        self.recent.insert(0, row)
        del self.recent[200:]
        return row

    async def process(self, item, news_id: int, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        base = {"news_id": news_id, "title": item.title[:120], "symbols": list(item.symbols)}
        if await self._capped(now):
            await store.mark_judged(news_id, None)
            return self._note(**base, action="capped", reason="일일 API 비용 상한")

        ctx = await self.context_builder(item, now)

        triage, rec = await self.judge.triage(item, ctx)
        tid = await store.add_judgment(news_id, stage=rec.stage, model=rec.model, result=rec.result,
                                       usage=rec.usage, cost_usd=rec.cost_usd, error=rec.error)
        if triage is None:
            await store.mark_judged(news_id, tid)
            return self._note(**base, action="error", stage="triage", reason=rec.error)
        if not triage.relevant or triage.magnitude == "low":
            await store.mark_judged(news_id, tid)
            return self._note(**base, action="skip", stage="triage",
                              reason=f"트리아지: {'관련 없음' if not triage.relevant else '영향 작음'} — {triage.reason_ko}")
        if triage.symbols and not item.symbols:
            item = item.with_symbols(tuple(triage.symbols))
            ctx = await self.context_builder(item, now)

        judgment, rec = await self.judge.full(item, ctx)
        jid = await store.add_judgment(news_id, stage=rec.stage, model=rec.model, result=rec.result,
                                       usage=rec.usage, cost_usd=rec.cost_usd, error=rec.error)
        await store.mark_judged(news_id, jid)
        if judgment is None:
            return self._note(**base, action="error", stage="full", reason=rec.error)

        reversed_ids = await self.check_reversal(judgment, now)
        ok, why = judgment.actionable(self.min_confidence)
        summary = dict(base, judgment_id=jid, direction=judgment.direction, confidence=judgment.confidence,
                       magnitude=judgment.magnitude, thesis=judgment.thesis_ko, reversed=reversed_ids)
        if not ok:
            return self._note(**summary, action="skip", stage="full", reason=(why + (f" · {rec.error}" if rec.error else "")))

        symbol = judgment.primary_symbol or ""
        plan = judgment.plan
        assert plan is not None
        if self.exec_mode == "observe":
            cid = await store.campaign_create(symbol=symbol, direction=judgment.direction, judgment_id=jid,
                                              news_id=news_id, status="proposed", exec_mode=self.exec_mode,
                                              plan=plan.model_dump(), thesis_ko=judgment.thesis_ko)
            await self._emit("proposed", await store.campaign_get(cid) or {"id": cid})
            return self._note(**summary, action="proposed", campaign_id=cid, reason="관찰 모드 — 주문 없음")

        existing = await store.open_campaign_for(symbol)
        if existing is not None:
            return self._note(**summary, action="skip", reason=f"이미 열린 작전 {existing['id'][-8:]} ({existing['direction']})")

        status = "pending_approval" if self.exec_mode == "approve" else "approved"
        cid = await store.campaign_create(symbol=symbol, direction=judgment.direction, judgment_id=jid,
                                          news_id=news_id, status=status, exec_mode=self.exec_mode,
                                          plan=plan.model_dump(), thesis_ko=judgment.thesis_ko)
        if self.exec_mode == "approve":
            await self._emit("pending_approval", await store.campaign_get(cid) or {"id": cid})
            return self._note(**summary, action="pending_approval", campaign_id=cid, reason="승인 대기")
        opened = await self.open_campaign(cid, symbol, judgment.direction, plan, judgment.thesis_ko, now=now)
        return self._note(**summary, action="open" if opened else "rejected", campaign_id=cid,
                          reason=f"다리 {len(opened)}/{len(plan.legs)} 열림" if opened else "가드가 전부 막음")

    async def check_reversal(self, judgment: Judgment, now: datetime) -> list[str]:
        """반대 방향·큰 뉴스면 열린 작전을 닫는다. 청산은 위험을 줄이는 쪽이라 승인 없이 한다 (docs/17 §5)."""
        if judgment.direction == "NONE" or judgment.magnitude != "high":
            return []
        closed: list[str] = []
        for sym in judgment.symbols:
            camp = await store.open_campaign_for(sym)
            if camp is None or camp["status"] != "open" or camp["direction"] == judgment.direction:
                continue
            await self.close_campaign(camp, CLOSE_REVERSAL, now=now)
            closed.append(camp["id"])
        return closed

    # ── 작전 열기 · 닫기 · 승인 ───────────────────────────
    async def open_campaign(self, cid: str, symbol: str, direction: str, plan: Plan, thesis: str, *,
                            now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        q = None
        try:
            q = await quotes.latest_quote(symbol)
        except Exception:  # noqa: BLE001
            log.debug("시세 조회 실패 %s", symbol, exc_info=True)
        if q is None:
            await store.campaign_update(cid, status="rejected", closed_at=now, close_reason="시세 없음")
            await self._emit("rejected", {"id": cid, "symbol": symbol, "reason": "시세 없음"})
            return []
        opened: list[str] = []
        notes: list[str] = []
        for i, (req, why) in enumerate(leg_requests(symbol=symbol, direction=direction, plan=plan, ref=q.mid,
                                                    leverage=self.leverage, stop_max_pct=self.stop_max_pct,
                                                    reason=thesis), 1):
            if req is None:
                await store.leg_set(cid, i, status="skipped", note=why)
                notes.append(why)
                continue
            out = await self.runner.open_from_request(req)
            if out.ok and out.bracket_id:
                await store.leg_set(cid, i, bracket_id=out.bracket_id, status="open")
                opened.append(out.bracket_id)
                log.info("뉴스 작전 %s %s %s 다리 %d 열림 → %s (손절 %.4g · 위험 %.2f%%)",
                         cid[-8:], symbol, direction, i, out.bracket_id[-8:], req.stop, req.risk_pct or 0)
            else:
                why = "가드: " + " · ".join(out.reasons)
                await store.leg_set(cid, i, status="skipped", note=why)
                notes.append(why)
                log.info("뉴스 작전 %s 다리 %d 건너뜀 — %s", cid[-8:], i, why)
        if opened:
            await store.campaign_update(cid, status="open", opened_at=now,
                                        summary={"ref_price": q.mid, "notes": notes})
            await self._emit("opened", await store.campaign_get(cid) or {"id": cid})
        else:
            await store.campaign_update(cid, status="rejected", closed_at=now,
                                        close_reason=" / ".join(notes)[:400] or "가드")
            await self._emit("rejected", {"id": cid, "symbol": symbol, "reason": " / ".join(notes)})
        return opened

    async def close_campaign(self, camp: dict[str, Any], reason: str, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        n = 0
        for leg in camp.get("legs") or []:
            bid = leg.get("bracket_id")
            if not bid or leg.get("status") in ("closed", "expired", "skipped"):
                continue
            try:
                await self.runner.mgr.close_now(bid, reason=reason)
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("작전 %s 다리 청산 실패 %s", camp["id"][-8:], bid[-8:])
        await store.campaign_update(camp["id"], close_reason=reason)
        log.info("뉴스 작전 %s 청산 요청 — %s (다리 %d)", camp["id"][-8:], reason, n)
        return n

    async def approve(self, cid: str, *, by: str, via: str, note: str = "") -> tuple[bool, str]:
        camp = await store.campaign_get(cid)
        if camp is None:
            return False, "작전 없음"
        if camp["status"] != "pending_approval":
            return False, f"승인 대기가 아니다 ({camp['status']})"
        now = datetime.now(UTC)
        await store.campaign_update(cid, status="approved",
                                    approval={"by": by, "via": via, "at": now.isoformat(), "note": note})
        plan = Plan(**camp["plan"])
        opened = await self.open_campaign(cid, camp["symbol"], camp["direction"], plan, camp.get("thesis_ko") or "",
                                          now=now)
        return (True, f"다리 {len(opened)}/{len(plan.legs)} 열림") if opened else (False, "가드가 전부 막음")

    async def reject(self, cid: str, *, by: str, via: str, note: str = "") -> tuple[bool, str]:
        camp = await store.campaign_get(cid)
        if camp is None:
            return False, "작전 없음"
        if camp["status"] != "pending_approval":
            return False, f"승인 대기가 아니다 ({camp['status']})"
        now = datetime.now(UTC)
        await store.campaign_update(cid, status="rejected", closed_at=now, close_reason="사용자 거절",
                                    approval={"by": by, "via": via, "at": now.isoformat(), "note": note})
        await self._emit("rejected", {**camp, "reason": "사용자 거절"})
        return True, "거절"

    async def _emit(self, event: str, camp: dict[str, Any]) -> None:
        if self.on_campaign is None:
            return
        try:
            await self.on_campaign(event, camp)
        except Exception:  # noqa: BLE001
            log.exception("작전 알림 실패 (%s)", event)

    # ── 상태 ─────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        return {
            "available": True, "alive": self.alive, "exec_mode": self.exec_mode,
            "min_confidence": self.min_confidence, "leverage": self.leverage, "stop_max_pct": self.stop_max_pct,
            "model": self.judge.model, "triage_model": self.judge.triage_model,
            "daily_usd_cap": self.daily_usd_cap, "cost_today": round(self.cost_today, 4),
            "capped": self.capped_day is not None, "ticks": self.ticks,
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "recent": self.recent[:50],
        }


def build_news_engine(runner: Any, api: Any, settings: Any) -> NewsEngine:
    """설정에서 엔진을 만든다. 서버가 부른다."""
    from ..universe import active_symbols

    quotes.set_public_api(api)
    universe = list(active_symbols())
    judge = Judge(model=settings.news_model, triage_model=settings.news_triage_model, universe=universe,
                  margin_max=float(settings.news_margin_pct_max), leverage=float(settings.news_leverage),
                  stop_min=float(settings.news_stop_min_pct), stop_max=float(settings.news_stop_max_pct),
                  hold_max=int(settings.news_max_hold_hours), api_key=settings.anthropic_api_key,
                  effort=settings.news_effort)
    mode = settings.news_exec_effective
    if settings.news_exec == "live":
        log.warning("TH_NEWS_EXEC=live 는 아직 경로가 없습니다 — approve(승인 후 페이퍼) 로 동작합니다 (docs/17 §5)")
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY 가 비어 있습니다 — 환경의 자격(ant auth login)으로 시도하고, 없으면 판단이 전부 실패로 기록됩니다")
    return NewsEngine(runner, judge, exec_mode=mode, min_confidence=float(settings.news_min_confidence),
                      leverage=float(settings.news_leverage), stop_max_pct=float(settings.news_stop_max_pct),
                      approval_ttl_min=int(settings.news_approval_ttl_min),
                      daily_usd_cap=float(settings.news_daily_usd_cap),
                      context_builder=make_context_builder(api))
