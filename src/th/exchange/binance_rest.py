"""바이낸스 USDⓈ-M 선물 REST 클라이언트 (읽기 전용).

Phase 0 에서 쓰는 것은 전부 공개 엔드포인트라 API 키가 없어도 동작한다.
서명이 필요한 계정 조회는 Phase 2 에서 이 클래스에 추가한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import get_settings
from ..logging_setup import get_logger
from .base import Kline

log = get_logger(__name__)

# 1분봉 최대 조회 개수
MAX_KLINE_LIMIT = 1500


class BinanceRateLimit(Exception):
    pass


class BinanceAPIError(Exception):
    """바이낸스가 4xx 로 거절한 요청. 메시지에 **서명된 URL 을 절대 넣지 않는다** —
    화면·로그·웹으로 그대로 흘러가기 때문이다. 바이낸스의 code/msg 만 담는다."""

    def __init__(self, status: int, code: int | None, msg: str, path: str):
        self.status, self.code, self.msg, self.path = status, code, msg, path
        super().__init__(f"HTTP {status} {path} · code={code} · {msg}")


# 바이낸스 오류 코드 → 초보자용 한 줄 설명
BINANCE_ERROR_KO: dict[int, str] = {
    -2015: "키가 거부됨 — (1) 키 값이 틀렸거나 (2) IP 제한에 지금 IP 가 없거나 (3) 'Enable Futures' 권한이 꺼져 있음. "
           "선물 계정을 한 번도 안 열었다면 바이낸스 앱에서 선물 지갑을 먼저 활성화해야 함",
    -2014: "API 키 형식이 잘못됨 — .env 의 BINANCE_API_KEY 를 다시 붙여넣기 (앞뒤 공백·따옴표 확인)",
    -1022: "서명이 맞지 않음 — .env 의 BINANCE_API_SECRET 이 틀렸거나 앞뒤에 공백이 붙어 있음",
    -1021: "PC 시계가 바이낸스와 어긋남 — Windows 시간 설정에서 '지금 동기화'",
    -1002: "이 요청에 권한이 없음 — 키의 'Enable Reading' 이 켜져 있는지 확인",
    -1099: "찾을 수 없음 — 선물 계정이 아직 개설되지 않았을 수 있음",
    -4003: "수량이 0 이거나 형식이 잘못됨",
}


def explain_binance_error(e: BaseException) -> str | None:
    """알려진 코드면 한국어 설명을, 아니면 None."""
    if isinstance(e, BinanceAPIError) and e.code in BINANCE_ERROR_KO:
        return BINANCE_ERROR_KO[e.code]
    return None


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class BinanceFutures:
    def __init__(self, base: str | None = None, timeout: float = 15.0) -> None:
        s = get_settings()
        self.base = (base or s.fapi_base).rstrip("/")
        self.api_key = s.binance_api_key
        self.api_secret = s.binance_api_secret
        self._client = httpx.AsyncClient(
            base_url=self.base,
            timeout=timeout,
            headers={"User-Agent": "trading-helper/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BinanceFutures":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ── 내부 ──────────────────────────────────────────────
    @retry(
        retry=retry_if_exception_type((httpx.TransportError, BinanceRateLimit)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, Any] | None = None,
                   signed: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}

        if signed:
            if not self.api_key or not self.api_secret:
                raise RuntimeError("서명이 필요한 요청인데 API 키가 설정되어 있지 않습니다")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query = urlencode(params)
            sig = hmac.new(
                self.api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            params["signature"] = sig
            headers["X-MBX-APIKEY"] = self.api_key

        resp = await self._client.get(path, params=params, headers=headers)

        if resp.status_code in (429, 418):
            wait = int(resp.headers.get("Retry-After", "5"))
            log.warning("레이트리밋 %s — %s초 대기", resp.status_code, wait)
            await asyncio.sleep(wait)
            raise BinanceRateLimit(f"{resp.status_code}")

        if resp.status_code >= 400:
            # 바이낸스는 본문에 {"code": -2015, "msg": "..."} 를 준다. 그걸 꺼내야
            # 왜 거절됐는지 알 수 있다. httpx 의 raise_for_status 는 URL(서명 포함)만 보여준다.
            code, msg = None, resp.text[:200]
            try:
                body = resp.json()
                code = int(body.get("code")) if body.get("code") is not None else None
                msg = str(body.get("msg") or msg)
            except Exception:  # noqa: BLE001
                pass
            raise BinanceAPIError(resp.status_code, code, msg, path)
        return resp.json()

    # ── 공개 엔드포인트 ────────────────────────────────────
    async def ping(self) -> bool:
        await self._get("/fapi/v1/ping")
        return True

    async def server_time(self) -> datetime:
        data = await self._get("/fapi/v1/time")
        return _ms_to_dt(int(data["serverTime"]))

    async def exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        return await self._get("/fapi/v1/exchangeInfo", {"symbol": symbol})

    async def symbol_filters(self, symbol: str) -> dict[str, Any]:
        """틱사이즈·수량 스텝 등. 없으면 빈 dict."""
        info = await self.exchange_info(symbol)
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                out: dict[str, Any] = {
                    "status": s.get("status"),
                    "contractType": s.get("contractType"),
                    "pricePrecision": s.get("pricePrecision"),
                    "quantityPrecision": s.get("quantityPrecision"),
                    "onboardDate": s.get("onboardDate"),
                }
                for f in s.get("filters", []):
                    if f.get("filterType") == "PRICE_FILTER":
                        out["tickSize"] = float(f["tickSize"])
                    elif f.get("filterType") == "LOT_SIZE":
                        out["stepSize"] = float(f["stepSize"])
                return out
        return {}

    async def klines(
        self,
        symbol: str,
        interval: str = "1m",
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = MAX_KLINE_LIMIT,
        include_open: bool = False,
    ) -> list[Kline]:
        """공개 klines. 기본은 **닫힌 봉만** 돌려준다.

        `include_open=True` 면 진행 중인 마지막 봉을 `closed=False` 로 붙여 준다 —
        웹 차트가 수집하지 않는 종목의 '살아있는 봉' 출발점으로 쓴다. 적재 경로는
        절대 이 옵션을 켜지 않는다 (진행 중인 봉을 closed 로 저장하면 안 된다).
        """
        raw = await self._get(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": min(limit, MAX_KLINE_LIMIT),
            },
        )
        # 아직 안 닫힌 봉은 버린다 — k[6] 은 봉 마감 시각(ms). 지금보다 나중에
        # 닫히는 봉은 진행 중인 봉이고, 그걸 closed=True 로 저장하면 안 된다.
        now_ms = int(time.time() * 1000)
        out: list[Kline] = []
        for k in raw:
            closed = int(k[6]) < now_ms
            if not closed and not include_open:
                continue
            out.append(
                Kline(
                    symbol=symbol,
                    tf=interval,
                    ts=_ms_to_dt(int(k[0])),
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume=float(k[5]),
                    quote_volume=float(k[7]),
                    trades=int(k[8]),
                    taker_buy_base=float(k[9]),
                    closed=closed,
                )
            )
        return out

    async def open_interest_hist(
        self, symbol: str, period: str = "5m", limit: int = 500,
        start_ms: int | None = None, end_ms: int | None = None,
    ) -> list[tuple]:
        """(symbol, ts, oi, oi_value)"""
        raw = await self._get(
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": period, "limit": min(limit, 500),
             "startTime": start_ms, "endTime": end_ms},
        )
        return [
            (symbol, _ms_to_dt(int(r["timestamp"])),
             float(r["sumOpenInterest"]), float(r["sumOpenInterestValue"]))
            for r in raw
        ]

    async def funding_rate_hist(
        self, symbol: str, limit: int = 1000,
        start_ms: int | None = None, end_ms: int | None = None,
    ) -> list[tuple]:
        """(symbol, ts, rate, None, None, None)"""
        raw = await self._get(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": min(limit, 1000),
             "startTime": start_ms, "endTime": end_ms},
        )
        return [
            (symbol, _ms_to_dt(int(r["fundingTime"])), float(r["fundingRate"]),
             float(r.get("markPrice") or 0) or None, None, None)
            for r in raw
        ]

    async def premium_index(self, symbol: str) -> dict[str, Any]:
        """현재 마크가격 · 인덱스가격 · 직전 펀딩률 · 다음 펀딩 시각.

        바이낸스 원본 키(markPrice/lastFundingRate/nextFundingTime)도 그대로 남긴다 —
        웹 `market` 메시지가 그 이름으로 쓰고, 숫자로 바꾼 값은 snake_case 로 둔다.
        """
        d = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        return {
            "symbol": d["symbol"],
            "mark_price": float(d["markPrice"]),
            "index_price": float(d.get("indexPrice") or 0),
            "last_funding_rate": float(d.get("lastFundingRate") or 0),
            "next_funding_ts": _ms_to_dt(int(d["nextFundingTime"])),
            "ts": _ms_to_dt(int(d.get("time") or time.time() * 1000)),
            "markPrice": float(d["markPrice"]),
            "lastFundingRate": float(d.get("lastFundingRate") or 0),
            "nextFundingTime": int(d["nextFundingTime"]),
        }

    async def ticker_24hr(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """24시간 통계 (공개). 심볼을 안 주면 **전 종목 목록**을 한 번에 받는다 —
        워치리스트는 이 한 번의 호출로 36개를 채운다 (종목마다 부르면 레이트리밋).

        각 항목: symbol · last_price · change_pct · quote_volume · close_time(ms)
        """
        raw = await self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})
        if isinstance(raw, dict):
            raw = [raw]
        out: list[dict[str, Any]] = []
        for r in raw:
            try:
                out.append({
                    "symbol": str(r["symbol"]).upper(),
                    "last_price": float(r.get("lastPrice") or 0),
                    "change_pct": float(r.get("priceChangePercent") or 0),
                    "quote_volume": float(r.get("quoteVolume") or 0),
                    "close_time_ms": int(r.get("closeTime") or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # ── 서명 필요 (Phase 2 에서 사용) ──────────────────────
    async def leverage_bracket(self, symbol: str) -> list[dict[str, Any]]:
        """유지증거금률(MMR). 청산가 계산에 필요하며 API 키가 있어야 한다."""
        return await self._get("/fapi/v1/leverageBracket", {"symbol": symbol}, signed=True)
