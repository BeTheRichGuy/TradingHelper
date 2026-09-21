"""페이퍼 누적 기록 (docs/15 12절) — 닫힌 브래킷 목록에서 화면이 그릴 숫자를 **전부 여기서** 만든다.

순수 함수다. DB 는 `store.history_with_fills()` 가 읽고, 여기는 계산만 한다.
승률·기대값·PF·MDD 의 정의는 백테스트(`ma5m.summarize`)와 같게 둔다 — 두 화면의 숫자가 갈라지면
"백테스트는 됐는데 페이퍼는 안 된다"를 판단할 수 없다.
"""

from __future__ import annotations

import math
import statistics as st
from datetime import datetime
from typing import Any

from ..account.risk import KST
from ..backtest.ma5m import SETUP_ID

MIN_SAMPLE = 30


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _item(r: dict[str, Any]) -> dict[str, Any]:
    entry = _num(r.get("avg_entry")) or _num(r.get("entry_price"))
    exit_px = _num(r.get("exit_price"))
    entry_at: datetime | None = r.get("entry_at") or r.get("created_at")
    exit_at: datetime | None = r.get("exit_at") or r.get("closed_at")
    held_min = None
    if entry_at and exit_at:
        held_min = max(0.0, (exit_at - entry_at).total_seconds() / 60)
    setup = str(r.get("setup_id") or "")
    stop = _num(r.get("stop_price"))
    stop_pct = abs(entry - stop) / entry * 100 if entry and stop else None
    move_pct = None
    if entry and exit_px:
        sign = 1 if r.get("direction") == "LONG" else -1
        move_pct = sign * (exit_px - entry) / entry * 100
    return {
        "id": str(r.get("bracket_id") or "")[-8:],
        "bracket_id": r.get("bracket_id"),
        "symbol": r.get("symbol"),
        "direction": r.get("direction"),
        "setup_id": setup,
        "auto": setup == SETUP_ID,
        "entry_at": entry_at,
        "exit_at": exit_at,
        "held_min": held_min,
        "entry": entry,
        "exit": exit_px,
        "qty": _num(r.get("qty")),
        "stop": stop,
        "stop_pct": stop_pct,
        "move_pct": move_pct,
        "realized_pnl": _num(r.get("realized_pnl")) or 0.0,
        "realized_r": _num(r.get("realized_r")) or 0.0,
        "fees": _num(r.get("fees")) or 0.0,
        "close_reason": r.get("close_reason") or "",
        "reason": r.get("reason") or "",
        "filled": bool(_num(r.get("avg_entry"))),
    }


def _stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    """체결된 것만 센다 — 유효시간 만료로 체결 없이 닫힌 브래킷은 매매가 아니다."""
    done = [i for i in items if i["filled"]]
    if not done:
        return {"n": 0, "unfilled": len(items) - len(done)}
    rs = [i["realized_r"] for i in done]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
    se = st.stdev(rs) / math.sqrt(len(rs)) if len(rs) > 2 else None
    # 시간순 누적 → MDD(R)
    chron = sorted(done, key=lambda i: i["exit_at"] or datetime.min.replace(tzinfo=KST))
    cum = peak = mdd = 0.0
    streak = worst_streak = 0
    for i in chron:
        cum += i["realized_r"]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
        streak = streak + 1 if i["realized_r"] <= 0 else 0
        worst_streak = max(worst_streak, streak)
    return {
        "n": len(done),
        "unfilled": len(items) - len(done),
        "wins": len(wins),
        "win": len(wins) / len(rs),
        "sum_r": sum(rs),
        "exp_r": st.mean(rs),
        "se": se,
        "t": (st.mean(rs) / se) if se else None,
        "pf": pf,
        "avg_win_r": st.mean(wins) if wins else None,
        "avg_loss_r": st.mean(losses) if losses else None,
        "mdd_r": mdd,
        "max_loss_streak": worst_streak,
        "sum_pnl": sum(i["realized_pnl"] for i in done),
        "fees": sum(i["fees"] for i in done),
        "avg_hold_min": st.mean(i["held_min"] for i in done if i["held_min"] is not None)
        if any(i["held_min"] is not None for i in done) else None,
        "enough_sample": len(done) >= MIN_SAMPLE,
        "longs": sum(1 for i in done if i["direction"] == "LONG"),
        "shorts": sum(1 for i in done if i["direction"] == "SHORT"),
    }


def _group(items: list[dict[str, Any]], key) -> list[dict[str, Any]]:
    buckets: dict[str, list] = {}
    for i in items:
        buckets.setdefault(str(key(i)), []).append(i)
    out = []
    for k in sorted(buckets):
        s = _stats(buckets[k])
        s["key"] = k
        out.append(s)
    return out


def build_history(rows: list[dict[str, Any]], opens: list, *, mode: str) -> dict[str, Any]:
    items = [_item(r) for r in rows]
    chron = sorted((i for i in items if i["filled"]), key=lambda i: i["exit_at"])
    cum_r = cum_pnl = 0.0
    curve = []
    for i in chron:
        cum_r += i["realized_r"]
        cum_pnl += i["realized_pnl"]
        curve.append({"t": i["exit_at"], "cum_r": cum_r, "cum_pnl": cum_pnl, "id": i["id"]})
    daily: dict[str, dict[str, Any]] = {}
    for i in chron:
        d = i["exit_at"].astimezone(KST).date().isoformat()
        row = daily.setdefault(d, {"day": d, "n": 0, "sum_r": 0.0, "sum_pnl": 0.0, "wins": 0})
        row["n"] += 1
        row["sum_r"] += i["realized_r"]
        row["sum_pnl"] += i["realized_pnl"]
        row["wins"] += 1 if i["realized_r"] > 0 else 0
    open_items = [
        {"id": s.bracket.bracket_id[-8:], "symbol": s.bracket.symbol, "direction": s.bracket.direction,
         "setup_id": s.bracket.setup_id or "", "auto": (s.bracket.setup_id or "") == SETUP_ID,
         "entry": s.avg_entry, "stop": s.bracket.stop_price, "qty": s.bracket.qty,
         "since": s.bracket.created_at, "has_position": s.has_position}
        for s in opens
    ]
    return {
        "available": True,
        "mode": mode,
        "min_sample": MIN_SAMPLE,
        "summary": _stats(items),
        "auto": _stats([i for i in items if i["auto"]]),
        "manual": _stats([i for i in items if not i["auto"]]),
        "by_symbol": _group(items, lambda i: i["symbol"]),
        "by_direction": _group(items, lambda i: i["direction"]),
        "by_reason": _group(items, lambda i: i["close_reason"] or "?"),
        "curve": curve,
        "daily": [daily[k] for k in sorted(daily)],
        "items": items,
        "open": open_items,
    }


__all__ = ["MIN_SAMPLE", "build_history"]
