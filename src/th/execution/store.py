"""브래킷 상태를 DB 에 남기고 되살린다.

**이게 이 계층에서 가장 중요한 파일이다.** 프로세스가 죽었을 때
"손절이 걸려 있다고 믿는데 실제로는 없는" 상태가 제일 위험하다.
그래서 모든 주문은 거래소로 보내기 **전에** 먼저 기록한다 (선기록·후전송).

  기록 → 전송 → 결과로 갱신

이 순서면 최악의 경우 '보냈는지 모르는 주문'이 남는데, 그건 재시작 때
거래소 상태와 대조해서 정리할 수 있다. 반대 순서면 '보냈는데 기록이 없는 주문'이
남고, 그건 영영 못 찾는다.
"""

from __future__ import annotations

from datetime import date, datetime

from ..db import get_pool
from ..logging_setup import get_logger
from .models import (
    Bracket,
    BracketState,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Purpose,
    Side,
)

log = get_logger(__name__)


# ── 저장 ────────────────────────────────────────────────
async def save_bracket(st: BracketState, mode: str) -> None:
    b = st.bracket
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO brackets (bracket_id, symbol, direction, qty, stop_price,
            entry_type, entry_price, tp1_price, tp2_price, tp1_ratio, reason,
            setup_id, mode, position_qty, avg_entry, realized_pnl, realized_r,
            created_at, closed_at, close_reason, expire_after_s, qty_step, tick_size)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
                $21,$22,$23)
        ON CONFLICT (bracket_id) DO UPDATE SET
            position_qty = EXCLUDED.position_qty,
            avg_entry    = EXCLUDED.avg_entry,
            realized_pnl = EXCLUDED.realized_pnl,
            realized_r   = EXCLUDED.realized_r,
            closed_at    = EXCLUDED.closed_at,
            close_reason = EXCLUDED.close_reason
        """,
        b.bracket_id, b.symbol, b.direction, b.qty, b.stop_price,
        b.entry_type.value, b.entry_price, b.tp1_price, b.tp2_price, b.tp1_ratio,
        b.reason, b.setup_id, mode, st.position_qty, st.avg_entry,
        st.realized_pnl, st.realized_r, b.created_at, st.closed_at, st.close_reason,
        b.expire_after_s, b.qty_step, b.tick_size,
    )


async def save_order(o: Order) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO exec_orders (client_id, bracket_id, symbol, side, type, purpose,
            qty, price, trigger, reduce_only, status, filled_qty, avg_price,
            exchange_id, note, created_at, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
        ON CONFLICT (client_id) DO UPDATE SET
            status      = EXCLUDED.status,
            filled_qty  = EXCLUDED.filled_qty,
            avg_price   = EXCLUDED.avg_price,
            exchange_id = EXCLUDED.exchange_id,
            updated_at  = EXCLUDED.updated_at
        """,
        o.client_id, o.bracket_id, o.symbol, o.side.value, o.type.value,
        o.purpose.value, o.qty, o.price, o.trigger, o.reduce_only, o.status.value,
        o.filled_qty, o.avg_price, o.exchange_id, o.note, o.created_at, o.updated_at,
    )


async def save_fill(f: Fill) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO exec_fills (order_client_id, bracket_id, symbol, side, purpose,
            qty, price, fee, slippage_pct, filled_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """,
        f.order_client_id, f.bracket_id, f.symbol, f.side.value, f.purpose.value,
        f.qty, f.price, f.fee, f.slippage_pct, f.at,
    )


# ── 복구 ────────────────────────────────────────────────
def _row_to_bracket(r) -> Bracket:
    return Bracket(
        bracket_id=r["bracket_id"], symbol=r["symbol"], direction=r["direction"],
        qty=r["qty"], stop_price=r["stop_price"],
        entry_type=OrderType(r["entry_type"]), entry_price=r["entry_price"],
        tp1_price=r["tp1_price"], tp2_price=r["tp2_price"], tp1_ratio=r["tp1_ratio"],
        reason=r["reason"], setup_id=r["setup_id"], created_at=r["created_at"],
        expire_after_s=int(r["expire_after_s"] or 300),
        qty_step=r["qty_step"], tick_size=r["tick_size"],
    )


def _row_to_order(r) -> Order:
    return Order(
        client_id=r["client_id"], bracket_id=r["bracket_id"], symbol=r["symbol"],
        side=Side(r["side"]), type=OrderType(r["type"]), purpose=Purpose(r["purpose"]),
        qty=r["qty"], price=r["price"], trigger=r["trigger"],
        reduce_only=r["reduce_only"], status=OrderStatus(r["status"]),
        filled_qty=r["filled_qty"], avg_price=r["avg_price"],
        exchange_id=r["exchange_id"], created_at=r["created_at"],
        updated_at=r["updated_at"], note=r["note"],
    )


async def load_open_brackets(mode: str) -> list[BracketState]:
    """아직 닫히지 않은 브래킷을 주문까지 통째로 되살린다."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM brackets WHERE closed_at IS NULL AND mode = $1 "
        "ORDER BY created_at", mode
    )
    out: list[BracketState] = []
    for r in rows:
        st = BracketState(
            bracket=_row_to_bracket(r), position_qty=r["position_qty"],
            avg_entry=r["avg_entry"], realized_pnl=r["realized_pnl"],
            realized_r=r["realized_r"],
        )
        orders = await pool.fetch(
            "SELECT * FROM exec_orders WHERE bracket_id = $1", r["bracket_id"]
        )
        for o in orders:
            od = _row_to_order(o)
            st.orders[od.client_id] = od
        out.append(st)
    if out:
        log.info("복구할 브래킷 %d개 (mode=%s)", len(out), mode)
    return out


async def daily_realized_r(mode: str, day_kst: date) -> float:
    """오늘(KST) 닫힌 브래킷의 실현 R 합. 페이퍼는 바이낸스 income 이 아니라 **이것**이 오늘 손익이다.

    ⚠️ 조회 실패는 여기서 삼키지 않는다 — 호출자가 '모름'으로 막아야 한다.
    """
    pool = await get_pool()
    v = await pool.fetchval(
        "SELECT coalesce(sum(realized_r), 0) FROM brackets "
        "WHERE mode = $1 AND closed_at IS NOT NULL "
        "AND (closed_at AT TIME ZONE 'Asia/Seoul')::date = $2",
        mode, day_kst,
    )
    return float(v or 0.0)


# ── 청산 요청 (브라우저 → 러너) ────────────────────────
# 웹 서버에는 매니저가 없다. 표시만 남기고, `th paper run` 루프가 처리한 뒤 지운다.
async def request_close(bracket_id: str, mode: str) -> bool:
    """열린 브래킷에 청산 요청을 표시한다. 닫혔거나 모르는 브래킷이면 False."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE brackets SET close_requested_at = coalesce(close_requested_at, now()) "
        "WHERE bracket_id = $1 AND mode = $2 AND closed_at IS NULL "
        "RETURNING bracket_id",
        bracket_id, mode,
    )
    return row is not None


async def pending_close_requests(mode: str) -> list[str]:
    """아직 처리되지 않은 청산 요청의 bracket_id 목록 (오래된 것부터)."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT bracket_id FROM brackets WHERE mode = $1 AND closed_at IS NULL "
        "AND close_requested_at IS NOT NULL ORDER BY close_requested_at",
        mode,
    )
    return [r["bracket_id"] for r in rows]


async def clear_close_request(bracket_id: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE brackets SET close_requested_at = NULL WHERE bracket_id = $1", bracket_id
    )


_foreign_close_logged: set[str] = set()


async def serve_close_requests(mgr, mode: str = "paper") -> list[str]:
    """대기 중인 청산 요청을 매니저로 실행하고 표시를 지운다. 처리한 bracket_id 목록.

    `th paper run` 루프가 매 반복마다 부른다. 규칙 둘:
      · 이 매니저가 관리하지 않는 브래킷의 요청은 **건드리지 않는다** — 표시를 지우면
        화면은 '처리됨'으로 읽는데 실제로는 아무도 청산하지 않은 상태가 된다.
      · DB 가 잠깐 흔들려도 예외를 올리지 않는다 — 요청 조회 실패가 손절 감시를 멈추면 안 된다.
    """
    try:
        pending = await pending_close_requests(mode)
    except Exception as e:  # noqa: BLE001
        log.warning("청산 요청 조회 실패 (%s) — 다음 반복에 다시 봅니다", type(e).__name__)
        return []
    done: list[str] = []
    for bracket_id in pending:
        short = bracket_id[-8:]
        if bracket_id not in mgr.states:
            if bracket_id not in _foreign_close_logged:
                _foreign_close_logged.add(bracket_id)
                log.info("청산 요청 %s — 이 러너가 관리하는 브래킷이 아님 · 그대로 둠", short)
            continue
        log.info("웹 청산 요청 %s — 시장가로 정리합니다", short)
        try:
            await mgr.close_now(bracket_id, reason="WEB_CLOSE")
        except Exception:  # noqa: BLE001
            log.exception("청산 실패 %s — 표시를 남겨 두고 다음 반복에 다시 시도", short)
            continue
        done.append(bracket_id)
        try:
            await clear_close_request(bracket_id)
        except Exception:  # noqa: BLE001
            log.warning("청산 요청 표시 삭제 실패 %s — 다음 반복에 다시 지웁니다", short)
    return done


# ── 러너 임대 (docs/15 7절) ──────────────────────────────
# 같은 브래킷을 두 프로세스(`th serve` 의 내장 러너와 `th paper run`)가 관리하면 안 된다.
# 러너는 시작할 때 임대를 잡고 10초마다 heartbeat 를 찍는다. 다른 holder 의 heartbeat 가
# LEASE_FRESH_S 이내면 임대를 못 잡는다.
LEASE_FRESH_S = 30


def lease_blocked_by(
    row: dict | None, holder: str, now: datetime, fresh_s: int = LEASE_FRESH_S,
) -> str | None:
    """임대 행을 보고 **누가 막고 있는지** 돌려준다. 순수 함수.

    같은 holder 면 언제나 통과(재시작·heartbeat). 다른 holder 는 heartbeat 가 낡았을 때만
    넘겨받는다 — 죽은 러너의 임대가 영원히 남으면 아무도 못 뜬다.
    """
    if row is None:
        return None
    other = str(row.get("holder") or "")
    if other == holder:
        return None
    beat = row.get("heartbeat_at")
    if beat is None:
        return None
    age = (now - beat).total_seconds()
    return other if age < fresh_s else None


async def lease_acquire(mode: str, holder: str, fresh_s: int = LEASE_FRESH_S) -> str | None:
    """임대를 잡는다. 잡았으면 None, 다른 러너가 살아 있으면 그 holder 이름."""
    from datetime import UTC

    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT holder, heartbeat_at FROM runner_lease WHERE mode = $1", mode
    )
    blocker = lease_blocked_by(dict(row) if row else None, holder, datetime.now(UTC), fresh_s)
    if blocker is not None:
        return blocker
    await pool.execute(
        "INSERT INTO runner_lease (mode, holder, heartbeat_at) VALUES ($1, $2, now()) "
        "ON CONFLICT (mode) DO UPDATE SET holder = EXCLUDED.holder, heartbeat_at = now()",
        mode, holder,
    )
    return None


async def lease_heartbeat(mode: str, holder: str) -> bool:
    """내 임대의 heartbeat 를 갱신한다. 임대가 남에게 넘어갔으면 False."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE runner_lease SET heartbeat_at = now() WHERE mode = $1 AND holder = $2 "
        "RETURNING holder",
        mode, holder,
    )
    return row is not None


async def lease_release(mode: str, holder: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM runner_lease WHERE mode = $1 AND holder = $2", mode, holder
    )


async def load_symbol_steps(symbol: str) -> tuple[float | None, float | None]:
    """(qty_step, tick_size). `th backfill` 이 적재한 symbols 테이블에서 읽는다. 없으면 (None, None)."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT qty_step, tick_size FROM symbols WHERE symbol = $1", symbol.upper()
    )
    if row is None:
        return None, None
    step = float(row["qty_step"]) if row["qty_step"] else None
    tick = float(row["tick_size"]) if row["tick_size"] else None
    return step, tick


async def history(limit: int = 50, mode: str | None = None) -> list[dict]:
    pool = await get_pool()
    q = ("SELECT * FROM brackets WHERE closed_at IS NOT NULL"
         + (" AND mode = $2" if mode else "")
         + " ORDER BY closed_at DESC LIMIT $1")
    rows = await pool.fetch(q, limit, *( [mode] if mode else [] ))
    return [dict(r) for r in rows]


# ── 수동 매매 기록 ──────────────────────────────────────
async def add_journal(
    *, symbol: str, direction: str, entry_at: datetime, entry_price: float,
    stop_price: float | None = None, qty: float | None = None,
    reason: str = "", setup_tag: str = "", note: str = "",
) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        """
        INSERT INTO trade_journal (symbol, direction, entry_at, entry_price,
            stop_price, qty, reason, setup_tag, note)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id
        """,
        symbol.upper(), direction.upper(), entry_at, entry_price, stop_price,
        qty, reason, setup_tag, note,
    )


async def close_journal(
    entry_id: int, *, exit_at: datetime, exit_price: float, note: str = ""
) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT direction, entry_price, stop_price FROM trade_journal WHERE id = $1",
        entry_id,
    )
    if row is None:
        return False
    r_val = None
    if row["stop_price"]:
        risk = abs(row["entry_price"] - row["stop_price"])
        if risk > 0:
            sign = 1 if row["direction"] == "LONG" else -1
            r_val = sign * (exit_price - row["entry_price"]) / risk
    await pool.execute(
        "UPDATE trade_journal SET exit_at=$2, exit_price=$3, outcome_r=$4, "
        "note = CASE WHEN $5 = '' THEN note ELSE $5 END WHERE id=$1",
        entry_id, exit_at, exit_price, r_val, note,
    )
    return True


async def journal_entries(limit: int = 50) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM trade_journal ORDER BY entry_at DESC LIMIT $1", limit
    )
    return [dict(r) for r in rows]


# ── v2.2 시그널 엔진 기록 (docs/15 10절) ─────────────────
async def add_signal(
    *, bar_ts: datetime, symbol: str, setup_id: str, action: str, direction: str | None = None,
    kind: str = "", close: float | None = None, ema20: float | None = None,
    ema200: float | None = None, stop_price: float | None = None, bracket_id: str | None = None,
    reason: str = "", params: dict | None = None,
) -> int | None:
    """엔진이 한 판단 한 줄. 실패해도 예외를 올리지 않는다 — 기록 실패가 매매를 멈추면 안 된다."""
    import json

    pool = await get_pool()
    try:
        return await pool.fetchval(
            """
            INSERT INTO signal_log (bar_ts, symbol, setup_id, action, direction, kind, close,
                ema20, ema200, stop_price, bracket_id, reason, params)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb) RETURNING id
            """,
            bar_ts, symbol.upper(), setup_id, action, direction, kind, close, ema20, ema200,
            stop_price, bracket_id, reason, json.dumps(params or {}),
        )
    except Exception:  # noqa: BLE001
        log.warning("시그널 기록 실패 %s %s", symbol, action, exc_info=True)
        return None


async def signal_log(limit: int = 200, symbol: str | None = None,
                     actions: tuple[str, ...] | None = None) -> list[dict]:
    pool = await get_pool()
    conds, args = [], []
    if symbol:
        args.append(symbol.upper())
        conds.append(f"symbol = ${len(args)}")
    if actions:
        args.append(list(actions))
        conds.append(f"action = ANY(${len(args)})")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    args.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM signal_log{where} ORDER BY ts DESC LIMIT ${len(args)}", *args
    )
    return [dict(r) for r in rows]


# ── v2.2 웹 백테스트 실행 기록 (docs/15 11절) ─────────────
async def backtest_create(*, symbol: str, setup_id: str, source: str, since: str,
                          params: dict) -> int:
    import json

    pool = await get_pool()
    return await pool.fetchval(
        """
        INSERT INTO backtest_runs (symbol, setup_id, source, since, status, params)
        VALUES ($1,$2,$3,$4,'queued',$5::jsonb) RETURNING id
        """,
        symbol.upper(), setup_id, source, since, json.dumps(params),
    )


async def backtest_update(run_id: int, *, status: str, summary: dict | None = None,
                          result: dict | None = None, error: str = "",
                          finished: bool = False) -> None:
    import json

    pool = await get_pool()
    await pool.execute(
        """
        UPDATE backtest_runs SET status = $2,
            summary = coalesce($3::jsonb, summary),
            result  = coalesce($4::jsonb, result),
            error   = $5,
            finished_at = CASE WHEN $6 THEN now() ELSE finished_at END
        WHERE id = $1
        """,
        run_id, status,
        json.dumps(summary) if summary is not None else None,
        json.dumps(result) if result is not None else None,
        error, finished,
    )


def _jsonb(v):
    """asyncpg 는 jsonb 를 문자열로 돌려준다 (codec 미설정). dict 로 풀어 준다."""
    import json

    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v if v is not None else {}


async def backtest_list(limit: int = 50) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id, created_at, finished_at, symbol, setup_id, source, since, status, params, "
        "summary, error FROM backtest_runs ORDER BY created_at DESC LIMIT $1", limit
    )
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = _jsonb(d.get("params"))
        d["summary"] = _jsonb(d.get("summary"))
        out.append(d)
    return out


async def backtest_get(run_id: int) -> dict | None:
    pool = await get_pool()
    r = await pool.fetchrow("SELECT * FROM backtest_runs WHERE id = $1", run_id)
    if r is None:
        return None
    d = dict(r)
    for k in ("params", "summary", "result"):
        d[k] = _jsonb(d.get(k))
    return d


async def backtest_delete(run_id: int) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow("DELETE FROM backtest_runs WHERE id = $1 RETURNING id", run_id)
    return row is not None


# ── v2.2 페이퍼 누적 기록 (docs/15 12절) ──────────────────
async def history_with_fills(limit: int = 1000, mode: str = "paper",
                             setup_id: str | None = None) -> list[dict]:
    """닫힌 브래킷 + 체결 목록. 진입·청산 평균가와 보유 시간을 여기서 만든다 — 화면은 그리기만."""
    pool = await get_pool()
    if setup_id is None:
        rows = await pool.fetch(
            "SELECT * FROM brackets WHERE closed_at IS NOT NULL AND mode = $1 "
            "ORDER BY closed_at DESC LIMIT $2", mode, limit,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM brackets WHERE closed_at IS NOT NULL AND mode = $1 AND setup_id = $2 "
            "ORDER BY closed_at DESC LIMIT $3", mode, setup_id, limit,
        )
    if not rows:
        return []
    ids = [r["bracket_id"] for r in rows]
    fills = await pool.fetch(
        "SELECT bracket_id, purpose, side, qty, price, fee, filled_at FROM exec_fills "
        "WHERE bracket_id = ANY($1) ORDER BY filled_at", ids,
    )
    by: dict[str, list] = {}
    for f in fills:
        by.setdefault(f["bracket_id"], []).append(dict(f))
    out = []
    for r in rows:
        d = dict(r)
        fs = by.get(d["bracket_id"], [])
        ent = [f for f in fs if f["purpose"] == "ENTRY"]
        ex = [f for f in fs if f["purpose"] != "ENTRY"]
        d["entry_at"] = ent[0]["filled_at"] if ent else None
        d["exit_at"] = ex[-1]["filled_at"] if ex else d["closed_at"]
        d["exit_price"] = (sum(f["qty"] * f["price"] for f in ex) / sum(f["qty"] for f in ex)) \
            if ex and sum(f["qty"] for f in ex) > 0 else None
        d["fees"] = sum(f["fee"] for f in fs)
        d["fills"] = fs
        out.append(d)
    return out
