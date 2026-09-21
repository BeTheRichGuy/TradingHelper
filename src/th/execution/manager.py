"""브래킷 생명주기 — 진입이 체결되면 **즉시** 손절을 건다.

순서가 중요하다. 진입 체결 → 손절 주문. 그 사이에 아무것도 끼워넣지 않는다.
`th watch` 가 잡아내던 `NO_STOP_ORDER` 상태가 여기서는 논리적으로 생길 수 없다.

DB 도 네트워크도 여기서 만지지 않는다. `ExecutionClient` 와 상태 객체만 다룬다.
그래야 테스트가 가짜 클라이언트 하나로 전 과정을 돌릴 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..logging_setup import get_logger
from .base import ExecutionClient
from .guard import GuardInput, GuardResult, check
from .models import (
    LONG,
    BracketState,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Purpose,
    Side,
    new_id,
    round_qty,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OpenResult:
    ok: bool
    guard: GuardResult
    state: BracketState | None = None

    @property
    def message(self) -> str:
        return self.guard.message


class BracketManager:
    """열려 있는 브래킷들을 관리한다.

    `store` 를 주면 상태가 바뀔 때마다 저장한다. 안 주면 저장하지 않는다(테스트용).
    ⚠️ 실매매 경로에서는 **반드시** 넣어야 한다. 저장이 없으면 프로세스가 죽었을 때
    '손절이 걸려 있다고 믿는데 실제로는 없는' 상태가 된다.
    """

    def __init__(self, client: ExecutionClient, store=None) -> None:
        self.client = client
        self.store = store
        self.states: dict[str, BracketState] = {}
        self.closed: list[BracketState] = []

    # ── 영속화 ─────────────────────────────────────────
    async def _save(self, st: BracketState, *, orders: bool = True,
                    fill: Fill | None = None) -> None:
        """기록은 **전송보다 먼저**가 원칙이지만, 페이퍼는 즉시 체결이라
        상태가 바뀐 직후에 한 번에 남긴다. 저장 실패가 매매를 멈추면 안 되므로
        예외는 삼키되 로그에는 반드시 남긴다."""
        if self.store is None:
            return
        try:
            await self.store.save_bracket(st, self.client.mode)
            if orders:
                for o in st.orders.values():
                    await self.store.save_order(o)
            if fill is not None:
                await self.store.save_fill(fill)
        except Exception:
            log.exception("브래킷 저장 실패 %s — 재시작 복구가 불완전해집니다",
                          st.bracket.bracket_id)

    async def restore(self, states: list[BracketState]) -> int:
        """재시작 시 DB 에서 읽은 상태를 되살린다.

        되살린 뒤 **포지션은 있는데 손절이 없는** 브래킷은 여기서 즉시 다시 건다.
        일부 청산 뒤 죽었으면 손절 수량이 포지션과 어긋나 있을 수 있으므로 그것도 맞춘다.
        복구했다고 믿는데 보호가 없는 상태가 재시작 직후 가장 흔한 사고다.
        """
        for st in states:
            if not st.is_open:
                continue
            self.states[st.bracket.bracket_id] = st
            # 저장돼 있던 주문을 체결 엔진에도 다시 올린다 (client_id 로 멱등)
            for o in list(st.orders.values()):
                if o.is_open:
                    st.orders[o.client_id] = await self.client.place(o)
            if not st.has_position:
                continue
            if st.live_stop is None:
                log.warning("복구된 브래킷 %s 에 손절이 없다 — 다시 건다",
                            st.bracket.bracket_id)
            try:
                await self._resize_protection(st)
            except Exception:
                log.critical("복구 중 손절 재설정 실패 — 보호 없는 포지션 %s",
                             st.bracket.bracket_id, exc_info=True)
            await self._save(st)
        return len(self.states)

    # ── 열기 ───────────────────────────────────────────
    async def open(self, inp: GuardInput) -> OpenResult:
        g = check(inp)
        if not g.ok:
            log.warning("브래킷 거부: %s", g.message)
            return OpenResult(False, g)

        b = inp.bracket
        st = BracketState(bracket=b)
        entry = Order(
            client_id=new_id("ent"), bracket_id=b.bracket_id, symbol=b.symbol,
            side=b.entry_side, type=b.entry_type, purpose=Purpose.ENTRY,
            qty=b.qty, price=b.entry_price if b.entry_type is OrderType.LIMIT else None,
            note=b.reason,
        )
        placed = await self.client.place(entry)
        st.orders[placed.client_id] = placed
        self.states[b.bracket_id] = st
        log.info("브래킷 열림 %s %s %s qty=%g 손절=%g",
                 b.bracket_id, b.symbol, b.direction, b.qty, b.stop_price)
        await self._save(st)

        # 시장가면 place() 안에서 이미 체결됐을 수 있다 — 바로 회수한다
        await self.drain_fills()
        return OpenResult(True, g, self.states.get(b.bracket_id, st))

    # ── 체결 반영 ──────────────────────────────────────
    async def drain_fills(self) -> list[Fill]:
        fills = await self.client.poll_fills()
        for f in fills:
            await self.on_fill(f)
        return fills

    async def on_fill(self, f: Fill) -> None:
        st = self.states.get(f.bracket_id)
        if st is None:
            log.warning("모르는 브래킷의 체결: %s", f.bracket_id)
            return
        st.fills.append(f)
        o = st.orders.get(f.order_client_id)
        if o is not None:
            st.orders[o.client_id] = o.with_status(
                OrderStatus.FILLED, filled_qty=f.qty, avg_price=f.price
            )

        sign = 1 if f.side is Side.BUY else -1
        before = st.position_qty
        st.position_qty = before + sign * f.qty

        if f.purpose is Purpose.ENTRY:
            st.avg_entry = f.price if st.avg_entry is None else (
                (st.avg_entry * abs(before) + f.price * f.qty) / abs(st.position_qty)
            )
            # 진입 수수료도 손익이다 — 청산 때만 빼면 R 이 실제보다 좋게 나온다
            st.realized_pnl -= f.fee
            self._update_r(st)
            # 체결을 **먼저** 남기고 손절을 건다. 손절 걸기가 실패해 청산까지 실패해도
            # '포지션이 있다'는 사실만은 DB 에 남아 있어야 한다.
            await self._save(st, fill=f)
            await self._arm_protection(st)
            await self._save(st)
            return

        # 청산 방향 체결 — 실현손익 계산
        if st.avg_entry is not None:
            direction = 1 if st.bracket.direction == LONG else -1
            st.realized_pnl += direction * (f.price - st.avg_entry) * f.qty - f.fee
            self._update_r(st)

        if abs(st.position_qty) < 1e-9:
            await self._finish(st, reason=f.purpose.value, fill=f)
        else:
            await self._resize_protection(st)
            await self._save(st, fill=f)

    @staticmethod
    def _update_r(st: BracketState) -> None:
        """R 은 브래킷 **전체** 초기 위험(손절폭 × 전체 수량) 기준이다.

        청산된 조각의 수량으로 나누면 절반 익절이 1R 로 잡혀 성적이 두 배로 부풀려진다.
        """
        if st.avg_entry is None:
            return
        risk_total = st.bracket.risk_per_unit(st.avg_entry) * st.bracket.qty
        if risk_total > 0:
            st.realized_r = st.realized_pnl / risk_total

    # ── 보호 주문 ──────────────────────────────────────
    async def _arm_protection(self, st: BracketState) -> None:
        """진입 체결 직후 손절을 건다. **이 함수가 실패하면 즉시 청산한다.**"""
        b = st.bracket
        qty = abs(st.position_qty)
        if qty <= 0:
            return
        try:
            await self._place_stop(st, qty)
        except Exception:
            log.exception("손절 주문 실패 — 포지션을 즉시 정리한다 %s", b.bracket_id)
            try:
                await self.close_now(b.bracket_id, reason="STOP_PLACE_FAILED")
            except Exception:
                # 손절도 청산도 못 걸었다. 여기서 예외를 올리면 상태 저장까지 건너뛰어
                # '포지션이 있다'는 사실조차 사라진다 — 최소한 그것만은 남긴다.
                log.critical("손절도 강제 청산도 실패 — 보호 없는 포지션 %s. 수동 정리 필요",
                             b.bracket_id, exc_info=True)
                await self._save(st)
            return

        # TP1 이 없으면 TP2 가 전량을 맡는다. 수량은 거래소 스텝에 맞춰 나누되
        # 합이 손절 수량과 **정확히** 같아야 한다 (남는 조각이 보호 없이 떠돌면 안 된다).
        tp1_q = round_qty(qty * b.tp1_ratio, b.qty_step) if b.tp1_price else 0.0
        tp2_q = round_qty(qty - tp1_q, b.qty_step) if b.tp2_price else 0.0
        for purpose, price, q in (
            (Purpose.TP1, b.tp1_price, tp1_q),
            (Purpose.TP2, b.tp2_price, tp2_q),
        ):
            if not price or q <= 0:
                continue
            tp = Order(
                client_id=new_id("tp"), bracket_id=b.bracket_id, symbol=b.symbol,
                side=b.exit_side, type=OrderType.TAKE_PROFIT_MARKET, purpose=purpose,
                qty=q, trigger=price, reduce_only=True,
            )
            st.orders[tp.client_id] = await self.client.place(tp)

    async def _place_stop(self, st: BracketState, qty: float) -> None:
        b = st.bracket
        stop = Order(
            client_id=new_id("stp"), bracket_id=b.bracket_id, symbol=b.symbol,
            side=b.exit_side, type=OrderType.STOP_MARKET, purpose=Purpose.STOP,
            qty=qty, trigger=b.stop_price, reduce_only=True,
        )
        st.orders[stop.client_id] = await self.client.place(stop)

    async def _resize_protection(self, st: BracketState) -> None:
        """손절 수량을 남은 포지션에 맞춘다. 손절이 없으면 새로 건다 (익절은 건드리지 않는다)."""
        qty = abs(st.position_qty)
        old = st.live_stop
        if old is None:
            await self._place_stop(st, qty)
            return
        if abs(old.qty - qty) < 1e-9:
            return
        st.orders[old.client_id] = await self.client.cancel(old)
        await self._place_stop(st, qty)

    # ── 닫기 ───────────────────────────────────────────
    async def close_now(self, bracket_id: str, reason: str = "MANUAL") -> None:
        """남은 주문을 모두 취소하고 포지션을 시장가로 정리한다."""
        st = self.states.get(bracket_id)
        if st is None:
            return
        ex: Order | None = None
        if st.has_position:
            # 청산 주문을 **먼저** 낸다. 손절부터 취소하면 청산 주문이 거절됐을 때
            # 보호 없는 포지션이 남는다. 접수된 뒤에야 나머지를 걷는다.
            ex = Order(
                client_id=new_id("cls"), bracket_id=bracket_id, symbol=st.bracket.symbol,
                side=st.bracket.exit_side, type=OrderType.MARKET, purpose=Purpose.EXIT,
                qty=abs(st.position_qty), reduce_only=True, note=reason,
            )
            st.orders[ex.client_id] = await self.client.place(ex)
            await self._save(st)
        for o in list(st.orders.values()):
            if o.is_open and (ex is None or o.client_id != ex.client_id):
                st.orders[o.client_id] = await self.client.cancel(o)
        await self._save(st)
        if ex is not None:
            await self.drain_fills()
        if st.is_open and not st.has_position:
            await self._finish(st, reason)

    async def _finish(self, st: BracketState, reason: str,
                      fill: Fill | None = None) -> None:
        for o in list(st.orders.values()):
            if o.is_open:
                st.orders[o.client_id] = await self.client.cancel(o)
        st.closed_at = datetime.now(UTC)
        st.close_reason = reason
        self.states.pop(st.bracket.bracket_id, None)
        self.closed.append(st)
        await self._save(st, fill=fill)
        log.info("브래킷 종료 %s — %s · %+.3fR (%+.2f USDT)",
                 st.bracket.bracket_id, reason, st.realized_r, st.realized_pnl)

    async def expire_stale_entries(self, now: datetime | None = None) -> int:
        """진입 지정가가 유효시간을 넘겼으면 취소한다."""
        now = now or datetime.now(UTC)
        n = 0
        for st in list(self.states.values()):
            if st.has_position:
                continue
            e = st.order_by_purpose(Purpose.ENTRY)
            if e is None:
                continue
            age = (now - e.created_at).total_seconds()
            if age >= st.bracket.expire_after_s:
                st.orders[e.client_id] = await self.client.cancel(e)
                await self._finish(st, reason="ENTRY_EXPIRED")
                n += 1
        return n

    # ── 요약 ───────────────────────────────────────────
    @property
    def open_count(self) -> int:
        return len(self.states)

    def summary(self) -> dict:
        done = self.closed
        wins = [s for s in done if s.realized_r > 0]
        total_r = sum(s.realized_r for s in done)
        return {
            "open": self.open_count,
            "closed": len(done),
            "wins": len(wins),
            "win_rate": (len(wins) / len(done)) if done else 0.0,
            "total_r": total_r,
            "expectancy_r": (total_r / len(done)) if done else 0.0,
            "total_pnl": sum(s.realized_pnl for s in done),
        }
