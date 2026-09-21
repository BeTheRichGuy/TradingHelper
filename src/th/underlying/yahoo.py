"""본주·지수 1분봉 수집 (yfinance).

⚠️ 알아둘 제약 — 설계 문서의 가정을 여기서 한 번 정정한다.
------------------------------------------------------------------
Yahoo 는 **1분봉을 최근 30일치까지만** 주고, 한 번에 최대 8일 구간만
받을 수 있다. 즉 "본주 SOXL 1분봉 10년치"는 yfinance 로 불가능하다.

따라서:
  * Phase 0~3 : yfinance 로 최근 30일 1분봉만 받아 basis 계산과 개발에 쓴다.
                프리·애프터 장 포함하려면 prepost=True.
  * Phase 4~6 : k-NN 코퍼스와 장기 백테스트에 긴 히스토리가 필요해지면
                Alpaca(무료 티어, 수년치 분봉 제공 · IEX 피드) 어댑터를
                이 폴더에 추가한다. 인터페이스는 아래와 동일하게 맞춘다.

일봉은 제약이 없으므로 장기 맥락용으로 따로 받아둔다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from .. import db
from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

YAHOO_1M_MAX_DAYS = 30      # 1분봉 보관 한도
YAHOO_1M_CHUNK_DAYS = 7     # 한 요청당 최대 구간


def _download_sync(ticker: str, start: datetime, end: datetime, interval: str, prepost: bool):
    import yfinance as yf

    return yf.download(
        tickers=ticker,
        start=start,
        end=end,
        interval=interval,
        prepost=prepost,
        auto_adjust=False,
        progress=False,
        threads=False,
    )


def _to_rows(ticker: str, df, tf: str) -> list[tuple]:
    if df is None or len(df) == 0:
        return []
    # yfinance 가 멀티인덱스 컬럼을 주는 경우가 있어 평탄화
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df = df.droplevel(1, axis=1)

    rows: list[tuple] = []
    for idx, r in df.iterrows():
        ts = idx.to_pydatetime()
        ts = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
        close = r.get("Close")
        if close is None or close != close:      # NaN
            continue
        rows.append((
            ticker, tf, ts,
            _f(r.get("Open")), _f(r.get("High")), _f(r.get("Low")),
            float(close), _f(r.get("Volume")), "yfinance",
        ))
    return rows


def _f(v):
    try:
        v = float(v)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


async def fetch_minutes(ticker: str, days: int = 7, prepost: bool = True) -> int:
    """최근 N일 1분봉. days 는 30 이하로 잘린다."""
    days = min(days, YAHOO_1M_MAX_DAYS)
    end = datetime.now(UTC)
    start = end - timedelta(days=days)

    total = 0
    cursor = start
    loop = asyncio.get_running_loop()
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=YAHOO_1M_CHUNK_DAYS), end)
        try:
            df = await loop.run_in_executor(
                None, _download_sync, ticker, cursor, chunk_end, "1m", prepost
            )
            rows = _to_rows(ticker, df, "1m")
            total += await db.save_underlying(rows)
        except Exception:
            log.exception("%s %s~%s 1분봉 수집 실패", ticker, cursor.date(), chunk_end.date())
        cursor = chunk_end
        await asyncio.sleep(0.5)

    if total:
        await db.set_ingest_state("underlying", ticker, end)
    log.info("%s 1분봉 %d행 저장 (최근 %d일)", ticker, total, days)
    return total


async def fetch_daily(ticker: str, years: int = 10) -> int:
    """일봉은 장기 조회 가능. 장기 맥락·변동성 퍼센타일 계산용."""
    end = datetime.now(UTC)
    start = end - timedelta(days=365 * years)
    loop = asyncio.get_running_loop()
    try:
        df = await loop.run_in_executor(
            None, _download_sync, ticker, start, end, "1d", False
        )
        rows = _to_rows(ticker, df, "1d")
        n = await db.save_underlying(rows)
        log.info("%s 일봉 %d행 저장", ticker, n)
        return n
    except Exception:
        log.exception("%s 일봉 수집 실패", ticker)
        return 0


async def sync_all(days: int = 7, with_daily: bool = True) -> dict[str, int]:
    s = get_settings()
    out: dict[str, int] = {}
    for ticker in s.all_underlying_tickers:
        out[ticker] = await fetch_minutes(ticker, days=days)
        if with_daily:
            await fetch_daily(ticker)
    return out
