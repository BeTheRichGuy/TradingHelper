"""시그널 엔진 — 5분봉이 닫힐 때마다 영상 200/20 규칙을 보고 **페이퍼 브래킷**을 연다 (docs/15 10절).

`th serve` 안에서 `PaperRunner` 옆에 돈다. 하는 일은 딱 셋이다.

  ① 5분 경계 + 몇 초 뒤, 종목마다 바이낸스 공개 klines(닫힌 5분봉)를 받아 `ma5m.latest_state()`
  ② 그 종목에 이 엔진이 연 브래킷이 있으면 ⑥ 청산 규칙(종가가 EMA20 반대로) → `mgr.close_now()`
  ③ 없고 신호가 있으면 `runner.open_from_request()` — **웹 주문 패널·CLI 와 완전히 같은 경로**
     (수량 역산 → 반올림 → 안전장치 → 매니저). 가드가 막으면 건너뛰고 사유를 남긴다.

판단 하나하나를 `signal_log` 에 남긴다. "왜 안 잡았나"가 "왜 잡았나"만큼 중요하다.

⚠️ 이 엔진은 페이퍼 러너만 안다. 실거래 클라이언트는 여기서도 만들지 않는다.
   `TH_EXECUTION_MODE=paper` 가 아니면 서버가 아예 띄우지 않는다.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from ..backtest.ma5m import (
    KIND_KO,
    SETUP_ID,
    Bars,
    Params,
    exit_hit,
    latest_state,
    phase_ko,
)
from ..logging_setup import get_logger
from . import quotes, store
from .guard import STOP_MAX_PCT
from .models import LONG, SHORT
from .runner import OpenRequest

log = get_logger(__name__)

BAR_S = 300
GRACE_S = 6                    # 봉이 닫힌 뒤 거래소가 봉을 확정할 시간
KLINE_LIMIT = 1000             # EMA200 수렴용 (초기값 잔재 e^-10)
ENTRY_EXPIRE_S = 240           # 시장가라 바로 붙지만, 시세가 끊겨 있으면 4분 뒤 걷는다
# 안전 손절은 guard.STOP_MAX_PCT(3%) 안쪽이어야 한다. 틱 반올림 여유를 둔다.
STOP_MARGIN_PCT = 0.05

BarsSource = Callable[[str], Awaitable[Bars]]


def align_next(now: datetime, grace_s: int = GRACE_S) -> datetime:
    """다음 5분 경계 + grace. 이미 경계 직후(grace 안)면 그 경계를 쓴다."""
    epoch = int(now.timestamp())
    boundary = (epoch // BAR_S) * BAR_S
    if epoch < boundary + grace_s:
        target = boundary + grace_s
    else:
        target = boundary + BAR_S + grace_s
    return datetime.fromtimestamp(target, UTC)


async def klines_source(api, symbol: str) -> Bars:
    """바이낸스 공개 5분봉(닫힌 것만). 수집 여부와 무관하게 어느 종목이든 된다."""
    ks = await api.klines(symbol, "5m", limit=KLINE_LIMIT)
    return Bars.from_bars([k for k in ks if getattr(k, "closed", True)], tf_min=5)


class SignalEngine:
    def __init__(self, runner, symbols: list[str] | tuple[str, ...], params: Params, *,
                 bars_source: BarsSource, notional_x: float = 1.0, enabled: bool = True,
                 funding_of: Callable[[str], float] | None = None) -> None:
        self.runner = runner
        self.symbols = [s.upper() for s in symbols]
        self.params = params.normalized()
        self.bars_source = bars_source
        self.notional_x = float(notional_x)
        self.enabled = bool(enabled)
        self.funding_of = funding_of
        self.state: dict[str, dict[str, Any]] = {s: {"symbol": s} for s in self.symbols}
        self._done: dict[str, int] = {}          # symbol → 마지막으로 판단한 bar_ts
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.started_at: datetime | None = None
        self.ticks = 0
        self.next_at: datetime | None = None
        self.recent: list[dict[str, Any]] = []   # 최근 판단 (DB 없이도 화면에 보이게)

    # ── 상태 ─────────────────────────────────────────────
    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return {
            "setup_id": SETUP_ID,
            "enabled": self.enabled,
            "alive": self.alive,
            "symbols": list(self.symbols),
            "params": self.params.as_dict(),
            "label_ko": self.params.label_ko,
            "notional_x": self.notional_x,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "next_at": self.next_at.isoformat() if self.next_at else None,
            "ticks": self.ticks,
            "states": [self._state_view(s) for s in self.symbols],
            "recent": list(self.recent[:50]),
        }

    def _state_view(self, sym: str) -> dict[str, Any]:
        st = dict(self.state.get(sym) or {"symbol": sym})
        st["phase_ko"] = phase_ko(st) if "ok" in st else "아직 확인 전"
        st["kind_ko"] = KIND_KO.get(int(st.get("kind") or 0), "")
        ob = self._open_bracket(sym)
        if ob is not None:
            st["open"] = {
                "id": ob.bracket.bracket_id[-8:], "direction": ob.bracket.direction,
                "mine": ob.bracket.setup_id == SETUP_ID, "entry": ob.avg_entry,
                "stop": ob.bracket.stop_price, "qty": ob.bracket.qty,
                "since": ob.bracket.created_at.isoformat(),
            }
        else:
            st["open"] = None
        return st

    # ── 루프 ─────────────────────────────────────────────
    async def start(self) -> None:
        self._stopping = False
        self.started_at = datetime.now(UTC)
        self._task = asyncio.create_task(self._loop(), name="signal-engine")
        log.info("시그널 엔진 시작 — %s · %s · 노셔널 %.2fx · %s",
                 ",".join(self.symbols), self.params.label_ko, self.notional_x,
                 "켜짐" if self.enabled else "꺼짐(관찰만)")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        log.info("시그널 엔진 종료")

    async def _loop(self) -> None:
        # 시작하자마자 한 번 본다 — 화면에 국면이 바로 뜬다 (마지막 닫힌 봉 기준)
        with contextlib.suppress(Exception):
            await self.tick()
        while not self._stopping:
            now = datetime.now(UTC)
            self.next_at = align_next(now)
            wait = max(1.0, (self.next_at - now).total_seconds())
            await asyncio.sleep(wait)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("시그널 엔진 반복 실패 — 다음 봉에 계속")

    # ── 한 번의 판단 ─────────────────────────────────────
    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(UTC)
        out = []
        for sym in self.symbols:
            try:
                out.append(await self.check_symbol(sym, now))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("%s 시그널 확인 실패 (%s)", sym, type(e).__name__, exc_info=True)
                self.state[sym] = {"symbol": sym, "ok": False,
                                   "error_ko": f"확인 실패 ({type(e).__name__})",
                                   "checked_at": now.isoformat()}
        self.ticks += 1
        return out

    def _open_bracket(self, sym: str):
        for st in self.runner.mgr.states.values():
            if st.bracket.symbol == sym and st.is_open:
                return st
        return None

    def _params_for(self, sym: str) -> Params:
        if self.funding_of is None:
            return self.params
        return Params(**{**self.params.as_dict(), "funding_8h_pct": float(self.funding_of(sym))})

    async def check_symbol(self, sym: str, now: datetime) -> dict[str, Any]:
        bars = await self.bars_source(sym)
        p = self._params_for(sym)
        st = latest_state(bars, p)
        st["symbol"] = sym
        st["checked_at"] = now.isoformat()
        self.state[sym] = st
        bar_ts = int(st.get("bar_ts") or 0)
        if not st.get("ok"):
            return {"symbol": sym, "action": "error", "reason": st.get("error_ko", "")}
        if self._done.get(sym) == bar_ts:
            return {"symbol": sym, "action": "seen"}     # 이 봉은 이미 판단했다
        self._done[sym] = bar_ts
        bar_at = datetime.fromtimestamp(bar_ts, UTC)
        # 봉이 너무 오래됐으면(거래소 지연·정지) 그 봉으로 판단하지 않는다
        if now - bar_at > timedelta(seconds=BAR_S * 3):
            return await self._record(sym, st, bar_at, "error",
                                      reason=f"마지막 닫힌 봉이 {int((now - bar_at).total_seconds() // 60)}분 전 — 낡은 봉으로 판단하지 않음")

        ob = self._open_bracket(sym)
        acted: dict[str, Any] | None = None
        if ob is not None and ob.bracket.setup_id == SETUP_ID:
            direction = 1 if ob.bracket.direction == LONG else -1
            age_bars = (now - ob.bracket.created_at).total_seconds() / BAR_S
            why = None
            if exit_hit(direction, float(st["close"]), float(st["ema20"])):
                why = "EMA20_CROSS"
            elif age_bars >= p.max_hold_bars:
                why = "TIME"
            if why:
                bid = ob.bracket.bracket_id
                await self.runner.mgr.close_now(bid, reason=why)
                log.info("시그널 청산 %s %s — %s (종가 %.4g / EMA20 %.4g)",
                         bid[-8:], sym, why, st["close"], st["ema20"])
                acted = await self._record(sym, st, bar_at, "exit", bracket_id=bid,
                                           direction=ob.bracket.direction,
                                           reason={"EMA20_CROSS": "종가가 20선을 반대로 넘음",
                                                   "TIME": "보유 시간 만료"}[why])
                ob = self._open_bracket(sym)   # 닫혔으면 None — 같은 봉의 재진입 신호를 본다

        sig = int(st.get("signal") or 0)
        if sig == 0:
            return acted or await self._record(sym, st, bar_at, "none", reason="신호 없음")
        direction = LONG if sig > 0 else SHORT
        if ob is not None:
            return await self._record(sym, st, bar_at, "skip", direction=direction,
                                      reason=f"이미 열린 브래킷 {ob.bracket.bracket_id[-8:]}"
                                             + ("" if ob.bracket.setup_id == SETUP_ID else " (손주문)"))
        if not self.enabled:
            return await self._record(sym, st, bar_at, "skip", direction=direction,
                                      reason="엔진이 꺼져 있음 (관찰만)")
        # 손절은 **현재가** 기준으로 다시 잰다 — 신호 봉 종가 기준이면 시가가 뛰었을 때 3% 를 넘겨 가드가 막는다
        q = await quotes.latest_quote(sym)
        if q is None:
            return await self._record(sym, st, bar_at, "skip", direction=direction,
                                      reason="시세 없음 — th stream 이 이 종목을 수집하는지 확인")
        stop_pct = min(float(p.hard_stop_pct), STOP_MAX_PCT - STOP_MARGIN_PCT)
        stop = q.mid * (1 - stop_pct / 100) if sig > 0 else q.mid * (1 + stop_pct / 100)
        # 노셔널 = 시드 × notional_x ⇔ risk_pct = 손절폭 × notional_x  (러너의 수량 역산을 뒤집은 것)
        risk_pct = stop_pct * self.notional_x
        kind_ko = KIND_KO.get(int(st.get("kind") or 0), "")
        reason = (f"자동 · 영상 200/20 {kind_ko} · {phase_ko(st)} · 종가 {st['close']:.4g} "
                  f"EMA20 {st['ema20']:.4g} EMA200 {st['ema200']:.4g}")
        req = OpenRequest(mode="paper", symbol=sym, direction=direction, stop=stop,
                          risk_pct=risk_pct, expire_s=ENTRY_EXPIRE_S, reason=reason,
                          setup_id=SETUP_ID)
        out = await self.runner.open_from_request(req)
        if out.ok:
            log.info("시그널 진입 %s %s %s 손절 %.4g (%s)", out.bracket_id[-8:], sym, direction, stop, kind_ko)
            return await self._record(sym, st, bar_at, "open", direction=direction,
                                      stop_price=stop, bracket_id=out.bracket_id, reason=reason)
        return await self._record(sym, st, bar_at, "skip", direction=direction, stop_price=stop,
                                  reason="가드: " + " · ".join(out.reasons))

    async def _record(self, sym: str, st: dict[str, Any], bar_at: datetime, action: str, *,
                      direction: str | None = None, stop_price: float | None = None,
                      bracket_id: str | None = None, reason: str = "") -> dict[str, Any]:
        row = {
            "ts": datetime.now(UTC).isoformat(), "bar_at": bar_at.isoformat(), "symbol": sym,
            "action": action, "direction": direction, "kind_ko": KIND_KO.get(int(st.get("kind") or 0), ""),
            "close": st.get("close"), "ema20": st.get("ema20"), "ema200": st.get("ema200"),
            "stop_price": stop_price, "bracket_id": bracket_id[-8:] if bracket_id else None,
            "reason": reason,
        }
        self.recent.insert(0, row)
        del self.recent[200:]
        self.state[sym]["last_action"] = action
        self.state[sym]["last_reason"] = reason
        if action != "none":                 # '신호 없음'은 5분마다 4줄씩 쌓인다 — DB 에는 남기지 않는다
            await store.add_signal(
                bar_ts=bar_at, symbol=sym, setup_id=SETUP_ID, action=action, direction=direction,
                kind=row["kind_ko"], close=st.get("close"), ema20=st.get("ema20"),
                ema200=st.get("ema200"), stop_price=stop_price, bracket_id=bracket_id,
                reason=reason, params=self.params.as_dict(),
            )
        return row


def build_signal_engine(runner, api, settings) -> SignalEngine:
    """설정에서 엔진을 만든다. 서버가 부른다 — 서버 코드에는 전략 이름이 나오지 않는다."""
    from ..backtest.ma5m import default_funding

    params = Params(mode=settings.signal_mode, hard_stop_pct=float(settings.signal_hard_stop_pct))
    missing = [s for s in settings.signal_symbols if s not in settings.symbols]
    if missing:
        log.warning("시그널 종목 %s 은 TH_SYMBOLS 에 없어 시세가 없습니다 — 관찰만 하고 주문은 못 냅니다 "
                    "(.env 의 TH_SYMBOLS 에 추가하고 th stream 을 다시 켜세요)", ",".join(missing))
    syms = list(settings.signal_symbols)     # 관찰은 전부 한다. 주문은 러너가 수집 종목만 받는다

    async def source(symbol: str) -> Bars:
        return await klines_source(api, symbol)

    return SignalEngine(runner, syms, params, bars_source=source,
                        notional_x=float(settings.signal_notional_x),
                        enabled=bool(settings.signal_enabled), funding_of=default_funding)


__all__ = ["SignalEngine", "build_signal_engine", "align_next", "klines_source"]
