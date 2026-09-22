"""뉴스 엔진 DB (마이그레이션 008).

원칙은 `execution/store.py` 와 같다 — 여기 함수는 **전부 어디선가 불려야 한다.**
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..execution.models import new_id
from ..logging_setup import get_logger
from .dedupe import dedupe_key, title_key
from .models import SOURCE_TELEGRAM, NewsItem

log = get_logger(__name__)

TITLE_WINDOW_H = 48       # 제목이 같으면 이 시간 안에서는 같은 기사로 본다
KST = ZoneInfo("Asia/Seoul")
LIVE_STATUSES = ("pending_approval", "approved", "open")   # 아직 끝나지 않은 작전

_JSON_COLS = ("raw", "plan", "approval", "summary", "risks", "usage")


def _json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _row(r) -> dict[str, Any]:
    d = dict(r)
    if isinstance(d.get("symbols"), (list, tuple)):
        d["symbols"] = list(d["symbols"])
    for k in _JSON_COLS:
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def row_to_item(d: dict[str, Any]) -> NewsItem:
    """DB 행 → NewsItem (판단 계층이 받는 모양)."""
    return NewsItem(
        source=d["source"], source_id=d.get("source_id") or "", ts=d["ts"], title=d["title"],
        body=d.get("body") or "", url=d.get("url") or "", channel=d.get("channel") or "",
        symbols=tuple(d.get("symbols") or ()), raw=dict(d.get("raw") or {}),
    )


# ─────────────────────────────────────────────────────────
# news_items
# ─────────────────────────────────────────────────────────

async def save_item(item: NewsItem) -> int | None:
    """저장하고 id 를 돌려준다. 중복이면 None. 예외는 밖으로 — 수집기가 잡는다."""
    key = dedupe_key(item)
    tkey = title_key(item.title)
    if tkey:
        dup = await db.fetchval(
            "SELECT id FROM news_items WHERE title_key = $1 AND ts > $2 LIMIT 1",
            tkey, item.ts - timedelta(hours=TITLE_WINDOW_H),
        )
        if dup:
            return None
    rows = await db.fetch(
        """
        INSERT INTO news_items (ts, source, source_id, channel, url, title, body, symbols,
                                dedupe_key, title_key, raw)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING id
        """,
        item.ts, item.source, item.source_id, item.channel, item.url, item.title, item.body,
        list(item.symbols), key, tkey, _json(item.raw),
    )
    return int(rows[0]["id"]) if rows else None


async def unjudged(limit: int = 20) -> list[dict[str, Any]]:
    """아직 판단하지 않은 것. 심볼이 없으면 텔레그램만 (docs/17 §3 프리필터)."""
    rows = await db.fetch(
        """
        SELECT * FROM news_items
        WHERE judged_at IS NULL AND (cardinality(symbols) > 0 OR source = $1)
        ORDER BY received_at ASC LIMIT $2
        """,
        SOURCE_TELEGRAM, limit,
    )
    return [_row(r) for r in rows]


async def mark_judged(news_id: int, judgment_id: int | None = None) -> None:
    await db.fetch(
        "UPDATE news_items SET judged_at = now(), judgment_id = $2 WHERE id = $1",
        news_id, judgment_id,
    )


async def get_item(news_id: int) -> dict[str, Any] | None:
    rows = await db.fetch("SELECT * FROM news_items WHERE id = $1", news_id)
    return _row(rows[0]) if rows else None


async def recent(limit: int = 50, *, symbol: str | None = None, source: str | None = None,
                 since: datetime | None = None) -> list[dict[str, Any]]:
    conds: list[str] = []
    args: list[Any] = []
    if symbol:
        args.append(symbol.upper())
        conds.append(f"${len(args)} = ANY(symbols)")
    if source:
        args.append(source)
        conds.append(f"source = ${len(args)}")
    if since is not None:
        args.append(since)
        conds.append(f"ts >= ${len(args)}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    args.append(limit)
    rows = await db.fetch(
        f"SELECT * FROM news_items {where} ORDER BY ts DESC LIMIT ${len(args)}", *args
    )
    return [_row(r) for r in rows]


async def source_status() -> list[dict[str, Any]]:
    """소스별 오늘 건수·마지막 시각 — `th news status` 와 화면용."""
    day0 = datetime.now(UTC) - timedelta(hours=24)
    rows = await db.fetch(
        """
        SELECT source,
               count(*) FILTER (WHERE received_at >= $1) AS last_24h,
               count(*) AS total,
               max(ts) AS last_ts,
               count(*) FILTER (WHERE judged_at IS NULL AND (cardinality(symbols) > 0 OR source = $2)) AS unjudged
        FROM news_items GROUP BY source ORDER BY source
        """,
        day0, SOURCE_TELEGRAM,
    )
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────
# news_judgments
# ─────────────────────────────────────────────────────────

async def add_judgment(news_id: int, *, stage: str, model: str, result: dict[str, Any] | None,
                       usage: dict[str, Any] | None = None, cost_usd: float = 0.0,
                       error: str = "") -> int:
    r = result or {}
    rows = await db.fetch(
        """
        INSERT INTO news_judgments (news_id, stage, model, relevant, symbols, direction, confidence,
                                    magnitude, horizon_hours, thesis_ko, risks, already_priced_in, plan,
                                    usage, cost_usd, error)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13::jsonb, $14::jsonb, $15, $16)
        RETURNING id
        """,
        news_id, stage, model, r.get("relevant"), list(r.get("symbols") or []), r.get("direction"),
        r.get("confidence"), r.get("magnitude"), r.get("horizon_hours"), r.get("thesis_ko") or "",
        _json(r.get("risks_ko") or []), r.get("already_priced_in"),
        _json(r["plan"]) if r.get("plan") is not None else None,
        _json(usage or {}), float(cost_usd or 0.0), error or "",
    )
    return int(rows[0]["id"])


def day_start_utc(now: datetime | None = None) -> datetime:
    """한국 자정 (일일 손익 집계와 같은 기준)."""
    now = (now or datetime.now(UTC)).astimezone(KST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


async def cost_today(now: datetime | None = None) -> float:
    v = await db.fetchval(
        "SELECT coalesce(sum(cost_usd), 0) FROM news_judgments WHERE ts >= $1", day_start_utc(now)
    )
    return float(v or 0.0)


async def judgments_recent(limit: int = 50, *, stage: str | None = None) -> list[dict[str, Any]]:
    if stage:
        rows = await db.fetch(
            "SELECT j.*, i.title, i.url, i.source FROM news_judgments j JOIN news_items i ON i.id = j.news_id "
            "WHERE j.stage = $1 ORDER BY j.ts DESC LIMIT $2", stage, limit,
        )
    else:
        rows = await db.fetch(
            "SELECT j.*, i.title, i.url, i.source FROM news_judgments j JOIN news_items i ON i.id = j.news_id "
            "ORDER BY j.ts DESC LIMIT $1", limit,
        )
    return [_row(r) for r in rows]


# ─────────────────────────────────────────────────────────
# news_campaigns · news_campaign_legs
# ─────────────────────────────────────────────────────────

async def campaign_create(*, symbol: str, direction: str, judgment_id: int | None, news_id: int | None,
                          status: str, exec_mode: str, plan: dict[str, Any], thesis_ko: str) -> str:
    cid = new_id("camp")
    await db.fetch(
        """
        INSERT INTO news_campaigns (id, symbol, direction, judgment_id, news_id, status, exec_mode, plan, thesis_ko)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
        """,
        cid, symbol, direction, judgment_id, news_id, status, exec_mode, _json(plan), thesis_ko,
    )
    for i, leg in enumerate(plan.get("legs") or [], 1):
        await leg_set(cid, i, margin_pct=float(leg.get("margin_pct") or 0), entry_type=str(leg.get("entry") or ""))
    return cid


async def campaign_update(cid: str, **fields: Any) -> None:
    """status · approval · opened_at · closed_at · close_reason · realized_r · summary 만 바꾼다."""
    allowed = {"status", "approval", "opened_at", "closed_at", "close_reason", "realized_r", "summary"}
    sets: list[str] = []
    args: list[Any] = [cid]
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"바꿀 수 없는 컬럼: {k}")
        args.append(_json(v) if k in ("approval", "summary") else v)
        sets.append(f"{k} = ${len(args)}" + ("::jsonb" if k in ("approval", "summary") else ""))
    if not sets:
        return
    await db.fetch(f"UPDATE news_campaigns SET {', '.join(sets)} WHERE id = $1", *args)


async def campaign_get(cid: str) -> dict[str, Any] | None:
    rows = await db.fetch("SELECT * FROM news_campaigns WHERE id = $1", cid)
    if not rows:
        return None
    d = _row(rows[0])
    d["legs"] = await legs_of(cid)
    return d


async def campaigns(limit: int = 50, *, status: str | None = None) -> list[dict[str, Any]]:
    if status:
        rows = await db.fetch(
            "SELECT * FROM news_campaigns WHERE status = $1 ORDER BY created_at DESC LIMIT $2", status, limit)
    else:
        rows = await db.fetch("SELECT * FROM news_campaigns ORDER BY created_at DESC LIMIT $1", limit)
    out = [_row(r) for r in rows]
    for d in out:
        d["legs"] = await legs_of(d["id"])
    return out


async def open_campaigns() -> list[dict[str, Any]]:
    """끝나지 않은 작전 전부 (승인 대기 · 승인됨 · 열림)."""
    rows = await db.fetch(
        "SELECT * FROM news_campaigns WHERE status = ANY($1::text[]) ORDER BY created_at", list(LIVE_STATUSES))
    out = [_row(r) for r in rows]
    for d in out:
        d["legs"] = await legs_of(d["id"])
    return out


async def open_campaign_for(symbol: str) -> dict[str, Any] | None:
    rows = await db.fetch(
        "SELECT * FROM news_campaigns WHERE symbol = $1 AND status = ANY($2::text[]) "
        "ORDER BY created_at DESC LIMIT 1", symbol.upper(), list(LIVE_STATUSES))
    if not rows:
        return None
    d = _row(rows[0])
    d["legs"] = await legs_of(d["id"])
    return d


async def leg_set(cid: str, leg_no: int, *, margin_pct: float | None = None, entry_type: str | None = None,
                  bracket_id: str | None = None, status: str | None = None, note: str | None = None) -> None:
    await db.fetch(
        """
        INSERT INTO news_campaign_legs (campaign_id, leg_no, margin_pct, entry_type, bracket_id, status, note)
        VALUES ($1, $2, coalesce($3, 0), coalesce($4, ''), $5, coalesce($6, ''), coalesce($7, ''))
        ON CONFLICT (campaign_id, leg_no) DO UPDATE SET
            margin_pct = coalesce($3, news_campaign_legs.margin_pct),
            entry_type = coalesce($4, news_campaign_legs.entry_type),
            bracket_id = coalesce($5, news_campaign_legs.bracket_id),
            status     = coalesce($6, news_campaign_legs.status),
            note       = coalesce($7, news_campaign_legs.note)
        """,
        cid, leg_no, margin_pct, entry_type, bracket_id, status, note,
    )


async def legs_of(cid: str) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT * FROM news_campaign_legs WHERE campaign_id = $1 ORDER BY leg_no", cid)
    return [dict(r) for r in rows]


async def brackets_by_ids(ids: list[str]) -> dict[str, dict[str, Any]]:
    """다리가 가리키는 브래킷의 현재 상태 (execution 스키마를 읽기만 한다)."""
    if not ids:
        return {}
    rows = await db.fetch(
        "SELECT bracket_id, symbol, direction, qty, position_qty, avg_entry, realized_pnl, realized_r, "
        "created_at, closed_at, close_reason FROM brackets WHERE bracket_id = ANY($1::text[])", list(ids))
    return {r["bracket_id"]: dict(r) for r in rows}
