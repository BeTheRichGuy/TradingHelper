"""바이낸스 계정 조회 (읽기 전용).

**이 파일에는 주문을 내는 코드가 없다.** 그리고 앞으로도 없다 —
주문은 `exchange/base.py` 의 ExecutionClient 를 통해서만 나간다.

사용하는 키는 "Enable Reading" 만 켠 것이어야 하며, 거래·출금 권한을 준 키는
절대 여기 넣지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import get_settings
from ..exchange.binance_rest import BinanceFutures
from ..logging_setup import get_logger
from .models import Balance, OpenOrder, Position
from .risk import KST

log = get_logger(__name__)


class NoApiKey(RuntimeError):
    pass


class AccountClient:
    """읽기 전용 계정 엔드포인트 모음."""

    def __init__(self, api: BinanceFutures | None = None) -> None:
        s = get_settings()
        self._own = api is None
        self.api = api or BinanceFutures()
        self.has_key = bool(s.binance_api_key and s.binance_api_secret)

    async def close(self) -> None:
        if self._own:
            await self.api.close()

    async def __aenter__(self) -> "AccountClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def _require_key(self) -> None:
        if not self.has_key:
            raise NoApiKey(
                "TH_BINANCE_API_KEY / TH_BINANCE_API_SECRET 가 필요합니다 "
                "(읽기 전용 키만 사용하세요)"
            )

    # ── 포지션 ────────────────────────────────────────────
    async def positions(self, symbol: str | None = None) -> list[Position]:
        self._require_key()
        raw = await self.api._get(
            "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True
        )
        if isinstance(raw, dict):
            raw = [raw]
        return [Position.from_api(r) for r in raw]

    async def position(self, symbol: str) -> Position | None:
        for p in await self.positions(symbol):
            if p.symbol == symbol.upper():
                return p
        return None

    # ── 잔고 ──────────────────────────────────────────────
    async def balance(self) -> Balance:
        self._require_key()
        raw = await self.api._get("/fapi/v2/account", {}, signed=True)
        return Balance.from_api(raw)

    # ── 미체결 주문 ───────────────────────────────────────
    async def open_orders(self, symbol: str | None = None) -> list[OpenOrder]:
        self._require_key()
        raw = await self.api._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
        return [OpenOrder.from_api(r) for r in raw]

    # ── 당일 실현손익 ─────────────────────────────────────
    async def income_since(
        self, start: datetime, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        self._require_key()
        return await self.api._get(
            "/fapi/v1/income",
            {
                "symbol": symbol,
                "startTime": int(start.timestamp() * 1000),
                "limit": 1000,
            },
            signed=True,
        )

    async def daily_income(
        self, symbol: str | None = None, at: datetime | None = None
    ) -> tuple[float, float, float, int]:
        """(실현손익, 수수료, 펀딩, 매매횟수) — 한국 날짜 기준 당일.

        하루의 경계를 한국 자정으로 잡는 이유: 사용자가 한국 시간으로 살기 때문이다.
        미국 본장은 한국 밤~새벽이라 UTC 기준으로 끊으면 한 세션이 두 날로 쪼개진다.
        """
        at = at or datetime.now(UTC)
        kst_midnight = datetime.combine(
            at.astimezone(KST).date(), datetime.min.time(), tzinfo=KST
        ).astimezone(UTC)

        rows = await self.income_since(kst_midnight, symbol)
        realized = commission = funding = 0.0
        trades = 0
        for r in rows:
            kind = str(r.get("incomeType", "")).upper()
            amount = float(r.get("income") or 0)
            if kind == "REALIZED_PNL":
                realized += amount
                trades += 1
            elif kind == "COMMISSION":
                commission += amount
            elif kind == "FUNDING_FEE":
                funding += amount
        return realized, commission, funding, trades

    # ── 유지증거금률 ──────────────────────────────────────
    async def maintenance_margin_rate(self, symbol: str, notional: float) -> float | None:
        """명목가 구간에 해당하는 MMR. 청산가 검증에 쓴다.

        평소에는 바이낸스가 주는 liquidationPrice 를 그대로 쓰고,
        이 값은 '내 계산과 거래소 계산이 맞는지' 대조용이다.
        """
        self._require_key()
        try:
            raw = await self.api.leverage_bracket(symbol)
        except Exception as e:
            log.debug("leverageBracket 실패: %s", e)
            return None

        entries = []
        for item in raw if isinstance(raw, list) else [raw]:
            if str(item.get("symbol", "")).upper() == symbol.upper():
                entries = item.get("brackets") or []
                break
        for b in entries:
            cap = float(b.get("notionalCap") or 0)
            floor = float(b.get("notionalFloor") or 0)
            if floor <= notional <= cap:
                return float(b.get("maintMarginRatio") or 0)
        return None

    # ── 진단 ──────────────────────────────────────────────
    async def check(self) -> tuple[bool, str]:
        """키가 살아 있고 읽기 권한이 있는지 확인."""
        if not self.has_key:
            return False, "API 키가 설정되지 않았습니다"
        try:
            await self.balance()
            return True, "읽기 권한 정상"
        except Exception as e:
            from th.exchange.binance_rest import BinanceAPIError, explain_binance_error

            ko = explain_binance_error(e)
            if ko:
                return False, f"바이낸스 code {e.code}: {ko}"
            if isinstance(e, BinanceAPIError):
                return False, f"바이낸스 거절 HTTP {e.status} code={e.code}: {e.msg}"
            return False, f"{type(e).__name__}: {e}"


async def one_minute_ago(minutes: int = 1) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)
