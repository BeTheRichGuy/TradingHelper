"""페이퍼 러너 — 브래킷 매니저를 **살아 있게** 하는 루프 (docs/15 7절).

`th paper run` 의 감시 루프와 `th serve` 의 내장 엔진이 **같은 코드**를 쓴다.
한 반복에서 하는 일:

  ① 열린 브래킷의 종목 + 포커스 종목의 시세를 DB 에서 읽어 PaperClient 에 먹인다
  ② 체결을 회수한다 (`drain_fills`)
  ③ 유효시간 지난 지정가를 걷는다 (`expire_stale_entries`)
  ④ 브라우저의 청산 요청을 처리한다 (`store.serve_close_requests`)
  ⑤ 10초마다 임대 heartbeat

주문 생성(`open_from_request`)도 여기 한 곳이다 — CLI 와 웹이 **완전히 같은 경로**로
`size_from_risk → round_qty/round_price → GuardInput → mgr.open()` 을 밟는다.
경로가 둘이면 언젠가 한쪽만 안전장치를 건너뛴다.

⚠️ 이 러너는 **페이퍼 클라이언트만** 받는다. `LiveClient` 는 여기서도, 어디서도 만들지 않는다.
`th.account` 에는 여전히 주문 코드가 없다.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from ..config import get_settings
from ..logging_setup import get_logger
from . import quotes, store
from .guard import GuardInput, check, size_from_risk
from .live import LIVE_ORDER_REFUSED_KO
from .models import LONG, SHORT, Bracket, OrderType, new_id, round_price, round_qty
from .paper import MARKET_SLIP_PCT, TAKER_FEE_PCT, Quote

log = get_logger(__name__)

MODE = "paper"
LEASE_HEARTBEAT_S = 10
DEFAULT_EXPIRE_S = 300
# 왕복 비용(%) = 테이커 수수료 ×2 + 시장가 슬리피지. 손절폭으로 나누면 'R 당 비용'이다.
ROUND_TRIP_COST_PCT = TAKER_FEE_PCT * 2 + MARKET_SLIP_PCT      # 0.11


class LeaseHeld(RuntimeError):
    """다른 러너가 같은 브래킷을 관리 중이다. 두 번째 러너는 뜨면 안 된다."""

    def __init__(self, holder: str, mode: str = MODE) -> None:
        self.holder, self.mode = holder, mode
        super().__init__(
            f"페이퍼 러너 임대를 '{holder}' 가 잡고 있습니다 — 같은 브래킷을 두 프로세스가 "
            "관리하면 안 됩니다. th serve 가 떠 있으면 웹 주문 패널을 쓰고, "
            "아니면 30초 뒤 다시 시도하세요"
        )


# ─────────────────────────────────────────────────────────
# 요청 · 미리보기 · 결과
# ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class OpenRequest:
    """주문 한 건의 입력. 웹 폼과 CLI 옵션이 **같은 형태**로 들어온다."""

    mode: str
    symbol: str
    direction: str
    stop: float
    entry: float | None = None          # None 이면 시장가
    tp1: float | None = None
    tp2: float | None = None
    risk_pct: float | None = None       # None 이면 settings.risk_pct
    expire_s: int | None = None         # None 이면 300
    reason: str = ""
    equity_usdt: float | None = None
    """None 이면 계정 잔고에서 읽는다. CLI 의 `--equity` 가 여기로 들어온다."""
    setup_id: str = ""
    """어떤 규칙이 낸 주문인가. 손주문은 빈 문자열, 시그널 엔진은 `ma5m.SETUP_ID`."""

    @classmethod
    def from_dict(cls, body: dict[str, Any] | None) -> OpenRequest:
        b = body or {}

        def num(k: str) -> float | None:
            v = b.get(k)
            if v is None or v == "":
                return None
            return float(v)

        exp = b.get("expire_s")
        return cls(
            mode=str(b.get("mode") or "").lower(),
            symbol=str(b.get("symbol") or "").upper(),
            direction=str(b.get("direction") or "").upper(),
            stop=num("stop") or 0.0,
            entry=num("entry"), tp1=num("tp1"), tp2=num("tp2"),
            risk_pct=num("risk_pct"),
            expire_s=int(exp) if exp not in (None, "") else None,
            reason=str(b.get("reason") or ""),
            equity_usdt=num("equity_usdt"),
            setup_id=str(b.get("setup_id") or ""),
        )


@dataclass(slots=True)
class Preview:
    """`POST /api/orders/preview` 응답 + 주문에 쓸 안전장치 입력.

    `reasons` 가 비어 있지 않으면 **가드가 막을 주문**이다. `warnings` 는 막지는 않지만
    사용자가 알아야 하는 것(스텝 조회 실패 등).
    """

    ok: bool
    symbol: str = ""
    direction: str = ""
    ref_price: float | None = None
    qty: float = 0.0
    notional_usdt: float = 0.0
    risk_usdt: float = 0.0
    r_unit_usdt: float = 0.0
    stop_pct: float | None = None
    cost_r: float | None = None
    equity_usdt: float | None = None
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    guard_input: GuardInput | None = None
    quote: Quote | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "symbol": self.symbol, "direction": self.direction,
            "ref_price": self.ref_price, "qty": self.qty,
            "notional_usdt": self.notional_usdt, "risk_usdt": self.risk_usdt,
            "r_unit_usdt": self.r_unit_usdt, "stop_pct": self.stop_pct,
            "cost_r": self.cost_r, "equity_usdt": self.equity_usdt,
            "warnings": list(self.warnings), "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class OrderOutcome:
    ok: bool
    bracket_id: str | None = None
    reasons: tuple[str, ...] = ()
    preview: Preview | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "bracket_id": self.bracket_id}
        return {"ok": False, "reasons": list(self.reasons)}


# ─────────────────────────────────────────────────────────
# 안전장치 입력 조회 (CLI 에서 옮겨 온 것)
# ─────────────────────────────────────────────────────────

async def paper_daily_r() -> tuple[float, bool, bool]:
    """(오늘 실현손익 R, 모름 여부, 잠금 여부) — **페이퍼 기준**.

    페이퍼는 바이낸스 income 이 아니라 페이퍼 브래킷의 결과가 오늘 손익이다.
    못 읽었으면 '모름'이고, 안전장치가 그걸로 막는다. 0.0 으로 뭉개면 잠금이 풀린다.
    """
    try:
        from ..account.risk import kst_today

        return await store.daily_realized_r(MODE, kst_today()), False, False
    except Exception:  # noqa: BLE001
        log.debug("페이퍼 당일 손익 확인 실패", exc_info=True)
        return 0.0, True, False


async def session_gate(symbol: str) -> tuple[bool, str]:
    """(세션 차단 여부, 사유). 세션 상태를 못 읽으면 **차단**으로 본다.

    세션 게이트(docs/05)는 **미국 본주가 있는 주식 perp** 의 규칙이다. BTC·ETH 처럼 본주가
    없는 종목에는 '본주 정지' 개념이 없으므로 게이트를 적용하지 않는다 (v2.2).
    """
    from ..universe import is_stock_perp

    if not is_stock_perp(symbol):
        return False, ""
    try:
        from ..sessions import get_context

        ctx = get_context(symbol)
    except Exception:  # noqa: BLE001
        log.debug("세션 상태 확인 실패", exc_info=True)
        return True, "세션 상태를 확인하지 못했습니다"
    return bool(ctx.blocked), " · ".join(b.reason_ko for b in ctx.blocks)


async def account_equity() -> float:
    """계정 지갑 잔고(USDT). 읽기 전용 조회다 — 실패하면 예외를 그대로 올린다."""
    from ..account.client import AccountClient

    async with AccountClient() as c:
        return (await c.balance()).wallet


# ─────────────────────────────────────────────────────────
class PaperRunner:
    """페이퍼 엔진. `start()` 로 백그라운드에서 돌리거나(`th serve`),
    `run_until_flat()` 으로 앞에서 돌린다(`th paper run`)."""

    def __init__(self, mgr, client, symbols: list[str] | tuple[str, ...], interval: int = 3,
                 *, holder: str = "serve", focus: str | None = None,
                 execution_mode: str | None = None, max_open_brackets: int | None = None) -> None:
        if getattr(client, "mode", None) != MODE:
            # 실거래 클라이언트는 여기 못 들어온다 — 러너는 페이퍼 전용이다
            raise ValueError(f"PaperRunner 는 페이퍼 클라이언트만 받습니다 (mode={getattr(client, 'mode', None)!r})")
        self.mgr = mgr
        self.client = client
        self.symbols = [s.upper() for s in symbols]
        self.interval = max(1, int(interval))
        self.holder = holder
        self.focus = focus.upper() if focus else None
        self.execution_mode = execution_mode or get_settings().execution_mode
        # 동시 브래킷 한도 — 기본은 설정(TH_MAX_OPEN_BRACKETS, 기본 1). 테스트는 명시적으로 준다.
        self.max_open_brackets = int(max_open_brackets if max_open_brackets is not None
                                     else get_settings().max_open_brackets)
        self.mode = MODE
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._leased = False
        self._last_beat: datetime | None = None
        self.last_quote: dict[str, Quote] = {}
        self.stale_since: dict[str, datetime] = {}
        self.started_at: datetime | None = None
        self.steps = 0
        # 훅 — CLI 가 화면에 찍을 때 쓴다. 서버는 안 건다.
        self.on_quote: Callable[[Quote], None] | None = None
        self.on_stale: Callable[[str, bool], None] | None = None
        self.on_closed: Callable[[list[str]], None] | None = None
        self.equity_source: Callable[[], Any] = account_equity

    # ── 상태 ─────────────────────────────────────────────
    @property
    def alive(self) -> bool:
        if self._task is not None:
            return not self._task.done()
        return self._leased and not self._stopping

    @property
    def open_count(self) -> int:
        return self.mgr.open_count

    def feed_symbols(self) -> set[str]:
        """이번 반복에 시세를 먹일 종목 — 열린 브래킷의 종목 + 포커스(수집 종목일 때만)."""
        syms = {st.bracket.symbol for st in self.mgr.states.values()}
        if self.focus and (not self.symbols or self.focus in self.symbols):
            syms.add(self.focus)
        return syms

    # ── 임대 ─────────────────────────────────────────────
    async def acquire(self) -> None:
        """러너 임대를 잡는다. 다른 러너가 살아 있으면 `LeaseHeld`."""
        blocker = await store.lease_acquire(MODE, self.holder)
        if blocker is not None:
            raise LeaseHeld(blocker)
        self._leased = True
        self._last_beat = datetime.now(UTC)
        log.info("페이퍼 러너 임대 획득 (holder=%s)", self.holder)

    async def _heartbeat_if_due(self, now: datetime) -> None:
        if not self._leased:
            return
        if self._last_beat and (now - self._last_beat).total_seconds() < LEASE_HEARTBEAT_S:
            return
        try:
            mine = await store.lease_heartbeat(MODE, self.holder)
        except Exception:  # noqa: BLE001
            log.warning("임대 heartbeat 실패 — 다음 반복에 다시 시도", exc_info=True)
            return
        self._last_beat = now
        if not mine:
            # 누군가 낡은 임대를 넘겨받았다 — 우리가 죽은 줄 알았을 것이다. 다시 잡는다.
            log.warning("임대가 다른 러너에게 넘어갔다 (holder=%s) — 다시 잡는다", self.holder)
            with contextlib.suppress(Exception):
                await store.lease_acquire(MODE, self.holder)

    async def release(self) -> None:
        if not self._leased:
            return
        self._leased = False
        try:
            await store.lease_release(MODE, self.holder)
        except Exception:  # noqa: BLE001
            log.debug("임대 해제 실패 — 30초 뒤 저절로 낡는다", exc_info=True)

    # ── 루프 ─────────────────────────────────────────────
    async def start(self) -> None:
        """임대를 잡고 백그라운드 태스크로 돈다 (`th serve`). 접속자가 없어도 돈다."""
        await self.acquire()
        self._stopping = False
        self.started_at = datetime.now(UTC)
        self._task = asyncio.create_task(self._loop(), name="paper-runner")
        log.info("페이퍼 러너 시작 (holder=%s, %d초, 브래킷 %d개)",
                 self.holder, self.interval, self.open_count)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        await self.release()
        with contextlib.suppress(Exception):
            await self.client.close()
        log.info("페이퍼 러너 종료 (holder=%s)", self.holder)

    async def _loop(self) -> None:
        try:
            while not self._stopping:
                try:
                    await self.step()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    # 한 반복의 실패가 손절 감시를 멈추면 안 된다
                    log.exception("페이퍼 러너 반복 실패 — 다음 반복에 계속")
                await asyncio.sleep(self.interval)
        finally:
            await self.release()

    async def run_until_flat(self) -> None:
        """앞에서 돈다 — 열린 브래킷이 없어질 때까지 (`th paper run` / `resume`)."""
        if not self._leased:
            await self.acquire()
        self.started_at = datetime.now(UTC)
        try:
            while self.open_count and not self._stopping:
                await self.step()
                try:
                    await asyncio.sleep(self.interval)
                except asyncio.CancelledError:
                    break
        finally:
            await self.release()

    async def step(self, now: datetime | None = None) -> dict[str, Any]:
        """한 반복. 돌려주는 dict 는 진단·테스트용이다."""
        now = now or datetime.now(UTC)
        fed: list[str] = []
        for sym in sorted(self.feed_symbols()):
            try:
                q = await quotes.latest_quote(sym)
            except Exception:  # noqa: BLE001
                log.debug("시세 조회 실패 %s", sym, exc_info=True)
                q = None
            if q is None:
                if sym not in self.stale_since:
                    self.stale_since[sym] = now
                    log.warning("%s 시세가 끊겼습니다 — 체결 판정을 멈춥니다", sym)
                    if self.on_stale:
                        self.on_stale(sym, True)
                continue
            if sym in self.stale_since:
                self.stale_since.pop(sym, None)
                log.info("%s 시세 복구", sym)
                if self.on_stale:
                    self.on_stale(sym, False)
            self.client.feed(q)
            self.last_quote[sym] = q
            fed.append(sym)
            if self.on_quote:
                self.on_quote(q)
        fills = await self.mgr.drain_fills() if fed else []
        # 유효시간 만료는 시세와 무관하다 — 시세가 끊겨도 낡은 지정가는 걷어야 한다
        expired = await self.mgr.expire_stale_entries(now)
        # 브라우저의 "청산" 버튼은 DB 에 표시만 남긴다 (docs/15 3절). 여기서 실행한다.
        closed = await self.serve_close_requests()
        await self._heartbeat_if_due(now)
        self.steps += 1
        return {"fed": fed, "fills": len(fills), "expired": expired, "closed": closed}

    async def serve_close_requests(self) -> list[str]:
        done = await store.serve_close_requests(self.mgr, MODE)
        if done and self.on_closed:
            self.on_closed(done)
        return done

    # ── 주문 경로 (CLI 와 웹이 같이 쓴다) ────────────────
    async def _equity(self, req: OpenRequest) -> tuple[float | None, str | None]:
        if req.equity_usdt is not None:
            return float(req.equity_usdt), None
        try:
            return float(await self.equity_source()), None
        except Exception as e:  # noqa: BLE001
            log.warning("계정 잔고 조회 실패 (%s) — 수량을 정할 수 없습니다", type(e).__name__)
            return None, f"계정 잔고를 못 읽었습니다 ({type(e).__name__}) — 수량·1R 을 정할 수 없습니다"

    async def preview(self, req: OpenRequest) -> Preview:
        """수량·노셔널·1R·손절폭·비용/R 을 계산하고 안전장치를 돌려본다. **주문은 내지 않는다.**"""
        s = get_settings()
        symbol = req.symbol.upper()
        direction = req.direction.upper()
        p = Preview(ok=False, symbol=symbol, direction=direction)

        if req.mode != MODE:
            p.reasons.append(LIVE_ORDER_REFUSED_KO if req.mode == "live"
                             else f"페이퍼 주문만 받습니다 (mode={req.mode or '(없음)'})")
            return p
        if direction not in (LONG, SHORT):
            p.reasons.append(f"방향은 LONG 또는 SHORT 여야 합니다: {req.direction!r}")
            return p
        if not symbol:
            p.reasons.append("종목이 없습니다")
            return p
        if self.symbols and symbol not in self.symbols:
            p.reasons.append(
                f"{symbol} 은 수집 종목(TH_SYMBOLS)이 아닙니다 — 시세가 없어 페이퍼 체결을 판정할 수 없습니다"
            )
            return p
        if not req.stop or req.stop <= 0:
            p.reasons.append("손절가가 없다")
            return p

        # 거래소 스텝에 맞춘다 — 안 맞추면 손절 수량과 TP1+TP2 가 어긋난다
        try:
            step, tick = await store.load_symbol_steps(symbol)
        except Exception:  # noqa: BLE001
            log.debug("심볼 스텝 조회 실패 — 반올림 없이 진행", exc_info=True)
            step, tick = None, None
            p.warnings.append("심볼 스텝을 못 읽어 반올림 없이 계산했습니다 (th init)")
        stop_px = round_price(req.stop, tick)
        entry_px = round_price(req.entry, tick)
        tp1_px = round_price(req.tp1, tick)
        tp2_px = round_price(req.tp2, tick)

        # 기준가 — 지정가면 그 값, 시장가면 현재 호가 중간
        q: Quote | None = None
        try:
            q = await quotes.latest_quote(symbol)
        except Exception:  # noqa: BLE001
            log.debug("시세 조회 실패", exc_info=True)
        if q is None and entry_px is None:
            p.reasons.append("시세를 못 읽었습니다 — th stream 이 돌고 있는지 확인하세요")
            return p
        if q is None:
            p.warnings.append("현재 시세가 없어 지정가를 기준으로 계산했습니다")
        ref = entry_px if entry_px is not None else q.mid
        p.ref_price = ref
        p.quote = q

        equity, eq_warn = await self._equity(req)
        daily_unknown = False
        if equity is None:
            equity = 0.0
            daily_unknown = True                  # 모르면 막는다
            p.warnings.append(eq_warn or "계정 잔고를 못 읽었습니다")
        p.equity_usdt = equity

        risk_pct = float(req.risk_pct) if req.risk_pct is not None else float(s.risk_pct)
        qty = round_qty(size_from_risk(equity, risk_pct, ref, stop_px), step)
        p.qty = qty
        p.r_unit_usdt = equity * risk_pct / 100 if equity > 0 else 0.0
        p.notional_usdt = qty * ref
        p.risk_usdt = qty * abs(ref - stop_px)
        p.stop_pct = abs(ref - stop_px) / ref * 100 if ref > 0 else None
        p.cost_r = (ROUND_TRIP_COST_PCT / p.stop_pct) if p.stop_pct else None

        try:
            b = Bracket(
                bracket_id=new_id("br"), symbol=symbol, direction=direction, qty=qty,
                stop_price=stop_px, tp1_price=tp1_px, tp2_price=tp2_px,
                entry_type=OrderType.LIMIT if entry_px is not None else OrderType.MARKET,
                entry_price=entry_px, reason=req.reason, setup_id=req.setup_id,
                expire_after_s=int(req.expire_s or DEFAULT_EXPIRE_S),
                qty_step=step, tick_size=tick,
            )
        except ValueError as e:
            p.reasons.append(str(e))
            return p

        # 안전장치 입력은 전부 **페이퍼 기준**이다 — 실계좌 income 으로 막거나 풀지 않는다
        daily_r, unknown, locked = await paper_daily_r()
        daily_unknown = daily_unknown or unknown
        try:
            open_count = len(await store.load_open_brackets(MODE))
        except Exception:  # noqa: BLE001
            log.debug("열린 브래킷 조회 실패", exc_info=True)
            open_count, daily_unknown = 0, True    # 모르면 막는다
        open_count = max(open_count, self.mgr.open_count)
        blocked, block_reason = await session_gate(symbol)
        inp = GuardInput(
            bracket=b, ref_price=ref, execution_mode=self.execution_mode,
            equity_usdt=equity, risk_pct=risk_pct,
            daily_realized_r=daily_r, daily_unknown=daily_unknown, daily_locked=locked,
            open_bracket_count=open_count, max_open_brackets=self.max_open_brackets,
            session_blocked=blocked, session_reason=block_reason,
        )
        p.guard_input = inp
        g = check(inp)
        p.reasons.extend(g.reasons)
        p.ok = not p.reasons
        return p

    async def open_from_request(self, req: OpenRequest) -> OrderOutcome:
        """미리보기 → 통과하면 `mgr.open()`. 가드에 걸리면 아무것도 열지 않는다."""
        p = await self.preview(req)
        if not p.ok or p.guard_input is None:
            return OrderOutcome(False, reasons=tuple(p.reasons), preview=p)
        if p.quote is not None:
            # 시장가가 다음 시세를 기다리지 않고 바로 체결되게 마지막 호가를 먹여 둔다
            self.client.feed(p.quote)
            self.last_quote[p.symbol] = p.quote
        res = await self.mgr.open(p.guard_input)
        if not res.ok:
            return OrderOutcome(False, reasons=tuple(res.guard.reasons), preview=p)
        return OrderOutcome(True, bracket_id=p.guard_input.bracket.bracket_id, preview=p)


# ─────────────────────────────────────────────────────────
async def build_paper_runner(symbols: list[str] | tuple[str, ...], *, focus: str | None = None,
                             interval: int = 3, holder: str = "serve",
                             execution_mode: str | None = None) -> PaperRunner:
    """PaperClient + BracketManager(store) 를 만들고 열린 브래킷을 복구한 러너.

    서버(`th serve`)가 부른다 — 서버 코드에는 매니저·클라이언트 이름이 나오지 않는다.
    """
    from .manager import BracketManager
    from .paper import PaperClient

    client = PaperClient()
    mgr = BracketManager(client, store=store)
    opens = await store.load_open_brackets(MODE)
    if opens:
        n = await mgr.restore(opens)
        log.info("페이퍼 브래킷 %d개 복구", n)
    return PaperRunner(mgr, client, symbols, interval, holder=holder, focus=focus,
                       execution_mode=execution_mode)
