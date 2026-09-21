"""웹 백테스트 실행 (docs/15 11절) — 데이터를 읽어 `ma5m.run_backtest()` 를 돌리고 DB 에 남긴다.

데이터는 세 곳 중 하나에서 온다. 화면은 어디에 뭐가 있는지 먼저 보여준다 (`inventory()`).

  csv     : `dataset/vision/{SYMBOL}_1m.csv` — `scripts/fetch_vision.py` 가 받은 바이낸스 아카이브. 가장 길다.
  db      : `th stream`/`th backfill` 이 적재한 1분봉. 수집 종목만, 보통 며칠~몇 주.
  binance : 공개 klines 5분봉을 뒤로 넘기며 받는다 (1회 1500봉 = 5.2일). 최대 max_days.

이 모듈은 DB 드라이버를 **함수 안에서만** 임포트한다 — `th.backtest` 는 DB 없이 임포트되어야 한다.
"""

from __future__ import annotations

import asyncio
import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from ..logging_setup import get_logger
from .ma5m import SETUP_ID, Bars, Params, default_funding, resample_5m, run_backtest

log = get_logger(__name__)

# src/th/backtest/ma5m_runs.py → 프로젝트 루트
ROOT = Path(__file__).resolve().parents[3]
VISION_DIR = ROOT / "dataset" / "vision"
BINANCE_MAX_DAYS = 400
BINANCE_PAGE = 1500
SOURCES = ("csv", "db", "binance")


def csv_path(symbol: str) -> Path:
    return VISION_DIR / f"{symbol.upper()}_1m.csv"


def _since_ts(since: str) -> int | None:
    """'YYYY-MM' → epoch 초. 비면 None."""
    since = (since or "").strip()
    if not since:
        return None
    y, m = since.split("-")[:2]
    return int(datetime(int(y), int(m), 1, tzinfo=UTC).timestamp())


def load_csv_5m(path: Path, since: str = "") -> Bars:
    """CSV 1분봉 → 5분봉. pandas 가 있으면 빠르게, 없으면 csv 모듈로."""
    cut = _since_ts(since)
    try:
        import pandas as pd

        df = pd.read_csv(path, usecols=["ts", "open", "high", "low", "close", "volume"])
        # pandas 버전에 따라 내부 단위(ns/ms)가 달라 astype(int64) 는 믿을 수 없다 — 초 단위로 명시한다
        ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(None)
        T = ts.to_numpy().astype("datetime64[s]").astype(np.int64)
        if cut is not None:
            keep = T >= cut
            df = df[keep]
            T = T[keep]
        if len(T) == 0:
            return Bars.from_rows([])
        return resample_5m(T, df["open"].to_numpy(float), df["high"].to_numpy(float),
                           df["low"].to_numpy(float), df["close"].to_numpy(float),
                           df["volume"].to_numpy(float))
    except ImportError:
        pass
    cols: dict[str, list[float]] = {k: [] for k in ("T", "open", "high", "low", "close", "volume")}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = int(datetime.fromisoformat(row["ts"]).timestamp())
            if cut is not None and t < cut:
                continue
            cols["T"].append(t)
            for k in ("open", "high", "low", "close", "volume"):
                cols[k].append(float(row[k]))
    if not cols["T"]:
        return Bars.from_rows([])
    return resample_5m(np.array(cols["T"], np.int64), *(np.array(cols[k]) for k in
                                                        ("open", "high", "low", "close", "volume")))


async def load_db_5m(symbol: str, since: str = "") -> Bars:
    from .. import db

    cut = _since_ts(since)
    start = datetime.fromtimestamp(cut, UTC) if cut else datetime(2000, 1, 1, tzinfo=UTC)
    rows = await db.fetch(
        "SELECT ts, open, high, low, close, volume FROM klines "
        "WHERE symbol = $1 AND tf = '1m' AND ts >= $2 ORDER BY ts",
        symbol.upper(), start,
    )
    if not rows:
        return Bars.from_rows([])
    T = np.array([int(r["ts"].timestamp()) for r in rows], np.int64)
    cols = [np.array([float(r[k]) for r in rows]) for k in ("open", "high", "low", "close", "volume")]
    return resample_5m(T, *cols)


async def load_binance_5m(api, symbol: str, since: str = "", max_days: int = BINANCE_MAX_DAYS) -> Bars:
    """공개 klines 5분봉을 뒤로 넘기며 받는다. 레이트리밋을 아끼려고 페이지 사이에 잠깐 쉰다."""
    cut = _since_ts(since)
    now = datetime.now(UTC)
    floor = int((now - timedelta(days=max_days)).timestamp())
    if cut is not None:
        floor = max(floor, cut)
    end_ms: int | None = None
    chunks: list[list] = []
    for _ in range(400):                       # 400 × 5.2일 ≈ 5.7년 상한
        ks = await api.klines(symbol, "5m", end_ms=end_ms, limit=BINANCE_PAGE)
        ks = [k for k in ks if getattr(k, "closed", True)]
        if not ks:
            break
        chunks.append(ks)
        first = int(ks[0].ts.timestamp())
        if first <= floor or len(ks) < BINANCE_PAGE:
            break
        end_ms = first * 1000 - 1
        await asyncio.sleep(0.25)
    rows = [k for ch in reversed(chunks) for k in ch if int(k.ts.timestamp()) >= floor]
    return Bars.from_bars(rows, tf_min=5)


async def load_bars_5m(source: str, symbol: str, since: str = "", api=None) -> Bars:
    if source == "csv":
        p = csv_path(symbol)
        if not p.exists():
            raise FileNotFoundError(f"{p.name} 이 없습니다 — python scripts\\fetch_vision.py --symbols {symbol} 로 받으세요")
        return await asyncio.to_thread(load_csv_5m, p, since)
    if source == "db":
        return await load_db_5m(symbol, since)
    if source == "binance":
        if api is None:
            raise ValueError("binance 소스에는 공개 API 클라이언트가 필요합니다")
        return await load_binance_5m(api, symbol, since)
    raise ValueError(f"모르는 데이터 소스: {source}")


def summary_of(result: dict[str, Any]) -> dict[str, Any]:
    """목록 화면에 실을 핵심 지표만."""
    if not result.get("ok"):
        return {"ok": False, "error_ko": result.get("error_ko", "")}
    a, f, o = result["all"], result["flip"], result["oos"]
    return {
        "ok": True, "bars": result["bars"], "days": result["days"], "span": result["span"],
        "n": a.get("n", 0), "win": a.get("win"), "exp_r": a.get("exp_r"), "t": a.get("t"),
        "pf": a.get("pf"), "eq": a.get("eq"), "mdd": a.get("mdd"),
        "flip_exp_r": f.get("exp_r"), "oos_exp_r": o.get("exp_r"), "oos_n": o.get("n", 0),
        "verdict": result["verdict"], "label_ko": result["label_ko"],
        "buy_hold_x": result.get("buy_hold_x"),
    }


async def run_job(run_id: int, *, symbol: str, source: str, since: str, params: Params,
                  api=None) -> dict[str, Any]:
    """큐에 든 실행 하나를 끝까지. 실패는 DB 에 status='error' 로 남긴다 — 화면이 이유를 보여준다."""
    from ..execution import store

    await store.backtest_update(run_id, status="running")
    try:
        bars = await load_bars_5m(source, symbol, since, api)
        result = await asyncio.to_thread(run_backtest, bars, params)
        result["symbol"] = symbol
        result["source"] = source
        result["since"] = since
        if not result.get("ok"):
            await store.backtest_update(run_id, status="error", summary=summary_of(result),
                                        result=result, error=result.get("error_ko", ""), finished=True)
            return result
        await store.backtest_update(run_id, status="done", summary=summary_of(result), result=result,
                                    finished=True)
        log.info("백테스트 #%d 완료 — %s %s n=%d 기대 %+.3fR", run_id, symbol, params.label_ko,
                 result["all"].get("n", 0), result["all"].get("exp_r") or 0.0)
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("백테스트 #%d 실패", run_id)
        await store.backtest_update(run_id, status="error", error=f"{type(e).__name__}: {e}", finished=True)
        return {"ok": False, "error_ko": str(e)}


async def submit(*, symbol: str, source: str, since: str, params_raw: dict | None,
                 api=None) -> tuple[int | None, str | None]:
    """요청 검증 → DB 행 생성 → 백그라운드 실행. (run_id, error_ko)."""
    from ..execution import store

    symbol = (symbol or "").upper().strip()
    if not symbol.endswith("USDT"):
        return None, f"종목 이름이 이상합니다: {symbol or '(없음)'}"
    if source not in SOURCES:
        return None, f"데이터 소스는 {SOURCES} 중 하나여야 합니다"
    since = (since or "").strip()
    if since and (len(since) != 7 or since[4] != "-"):
        return None, "시작은 YYYY-MM 형식입니다 (예: 2024-01)"
    raw = dict(params_raw or {})
    if raw.get("funding_8h_pct") in (None, ""):
        raw["funding_8h_pct"] = default_funding(symbol)
    try:
        params = Params.from_dict(raw)
    except (TypeError, ValueError) as e:
        return None, f"매개변수가 잘못됐습니다: {e}"
    run_id = await store.backtest_create(symbol=symbol, setup_id=SETUP_ID, source=source,
                                         since=since, params=params.as_dict())
    asyncio.create_task(run_job(run_id, symbol=symbol, source=source, since=since, params=params, api=api),
                        name=f"backtest-{run_id}")
    return run_id, None


async def inventory(symbols: list[str]) -> dict[str, Any]:
    """종목별로 어디에 데이터가 있는지 — 화면의 소스 선택 도우미."""
    out = []
    csvs = {p.name.split("_")[0]: p for p in VISION_DIR.glob("*_1m.csv")} if VISION_DIR.exists() else {}
    db_span: dict[str, tuple] = {}
    try:
        from .. import db

        rows = await db.fetch(
            "SELECT symbol, min(ts) AS a, max(ts) AS b, count(*) AS n FROM klines "
            "WHERE tf = '1m' GROUP BY symbol"
        )
        db_span = {r["symbol"]: (r["a"], r["b"], int(r["n"])) for r in rows}
    except Exception:  # noqa: BLE001
        log.debug("DB 봉 범위 조회 실패", exc_info=True)
    for s in sorted(set(symbols) | set(csvs) | set(db_span)):
        item: dict[str, Any] = {"symbol": s, "csv": None, "db": None}
        if s in csvs:
            p = csvs[s]
            item["csv"] = {"path": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else p.name,
                           "mb": round(p.stat().st_size / 1e6, 1)}
        if s in db_span:
            a, b, n = db_span[s]
            item["db"] = {"from": a.isoformat(), "to": b.isoformat(), "bars_1m": n}
        out.append(item)
    return {"items": out, "vision_dir": str(VISION_DIR), "binance_max_days": BINANCE_MAX_DAYS}


__all__ = ["SOURCES", "csv_path", "inventory", "load_bars_5m", "load_csv_5m", "run_job", "submit",
           "summary_of"]
