"""거래소 추상화.

여기가 확장점이다.

* MarketData  — 시세 읽기. 지금은 Binance 구현 하나뿐이지만,
                나중에 다른 거래소를 붙이거나 백테스트용 리플레이 소스를
                끼울 때 이 인터페이스만 맞추면 된다.

* ExecutionClient — 주문. **Phase 0~5 에서는 NoopExecutor 만 존재한다.**
                자동매매는 이 프로토콜을 구현한 BinanceExecutor 를 추가하고
                TH_EXECUTION_MODE 를 바꾸는 것으로 붙는다. 전략 코드는
                주문 API 를 직접 호출하지 않으므로 고칠 필요가 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Kline:
    symbol: str
    tf: str
    ts: datetime          # 봉 시작 (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float | None = None
    trades: int | None = None
    taker_buy_base: float | None = None
    closed: bool = True

    def as_row(self, source: str = "binance") -> tuple:
        return (
            self.symbol, self.tf, self.ts, self.open, self.high, self.low,
            self.close, self.volume, self.quote_volume, self.trades,
            self.taker_buy_base, source,
        )


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: str            # BUY | SELL
    qty: float
    order_type: str      # MARKET | LIMIT | STOP_MARKET
    price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    client_id: str | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    accepted: bool
    order_id: str | None
    reason: str = ""
    raw: dict[str, Any] | None = None


@runtime_checkable
class MarketData(Protocol):
    async def klines(
        self, symbol: str, interval: str, start_ms: int | None, end_ms: int | None, limit: int
    ) -> list[Kline]: ...

    async def exchange_info(self, symbol: str | None = None) -> dict[str, Any]: ...


@runtime_checkable
class ExecutionClient(Protocol):
    """주문 실행. 구현체 교체만으로 수동→자동매매로 넘어간다."""

    mode: str

    async def submit(self, req: OrderRequest) -> OrderResult: ...
    async def cancel(self, symbol: str, order_id: str) -> OrderResult: ...


class NoopExecutor:
    """주문 기능이 꺼진 상태. 지금 유일하게 사용되는 구현.

    호출되면 거부하고 로그만 남긴다. '실수로 주문이 나가는' 경로를
    코드 레벨에서 원천 차단하기 위한 것이다.
    """

    mode = "none"

    async def submit(self, req: OrderRequest) -> OrderResult:
        return OrderResult(
            accepted=False,
            order_id=None,
            reason="TH_EXECUTION_MODE=none — 주문 기능이 비활성화되어 있습니다",
        )

    async def cancel(self, symbol: str, order_id: str) -> OrderResult:
        return OrderResult(accepted=False, order_id=None, reason="주문 기능 비활성화")


def get_executor(mode: str) -> ExecutionClient:
    """모드에 맞는 실행기를 돌려준다.

    paper / live 는 아직 구현이 없다. 존재하지 않는 모드를 켜면
    조용히 동작하는 대신 즉시 실패시킨다.
    """
    if mode == "none":
        return NoopExecutor()
    raise NotImplementedError(
        f"TH_EXECUTION_MODE={mode} 는 아직 구현되지 않았습니다. "
        "자동매매는 Phase 4 백테스트 통과 이후에 붙입니다."
    )
