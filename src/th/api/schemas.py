"""WebSocket 메시지 스키마 (03 문서 5절, v0.3).

**v0.3 에는 `signal` 메시지가 없습니다.** Phase 4 에서 셋업에 방향성 우위가
확인되지 않아 신호 카드를 뺐고 (`08-전략-검증-결과.md`), 서버는 방향을 제시하는
어떤 메시지도 보내지 않습니다.

그 자리를 `no_trade` 가 대신합니다. 여기서 제일 조심한 것은 **빈 목록이 매수
허가로 읽히는 것**입니다. `reasons` 가 비었다는 건 "막을 이유를 못 찾았다"는
뜻이지 "사도 된다"가 아닙니다. 사람은 빈 경고창을 초록불로 읽습니다.

그래서 스키마에 못을 박았습니다.

  · `is_permission_to_trade` 는 **항상 False** 입니다 (타입 자체가 리터럴)
  · `reasons` 가 비면 `note_ko` 에 그 문장이 반드시 들어갑니다

클라이언트가 이 필드를 무시하고 초록불을 그릴 수는 있지만, 그건 스키마를 어긴
것이고 테스트가 그 사실을 기록해 둡니다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

NO_PERMISSION_NOTE = (
    "지금 막을 이유는 찾지 못했습니다. "
    "매매해도 된다는 뜻이 아닙니다 — 방향 판단은 이 앱이 하지 않습니다."
)

ReasonSource = Literal["session", "market", "event", "account"]
AlertLevel = Literal["critical", "warning", "info"]

# 03 문서 5.3. 앞의 셋은 '손절이 있는데 소용없는' 상태를 포함한다.
ALERT_CODES = (
    "NO_STOP_ORDER",
    "STOP_BEYOND_LIQUIDATION",
    "STOP_ALREADY_PASSED",
    "LIQ_PROXIMITY",
    "EVENT_IMMINENT",
    "DAILY_LOSS_LIMIT",
    "REGIME_CLOSING",
    "VOL_SPIKE",
    "LIQUIDATION_CASCADE",
)


class Envelope(BaseModel):
    """모든 메시지의 공통 머리."""

    type: str
    symbol: str
    ts: datetime


class NoTradeReason(BaseModel):
    source: ReasonSource
    code: str
    text_ko: str


class NoTradeMessage(Envelope):
    """신호 카드가 있던 자리. **허가가 아니라 금지 사유 목록이다.**"""

    type: Literal["no_trade"] = "no_trade"
    reasons: list[NoTradeReason] = Field(default_factory=list)

    is_permission_to_trade: Literal[False] = False
    """**항상 False.** 이 앱은 어떤 상황에서도 매매를 권하지 않는다.

    빈 `reasons` 를 초록불로 읽는 것을 막기 위해 타입 수준에서 고정했다.
    """

    note_ko: str = ""

    @model_validator(mode="after")
    def _note_required_when_empty(self) -> NoTradeMessage:
        if not self.reasons and not self.note_ko:
            object.__setattr__(self, "note_ko", NO_PERMISSION_NOTE)
        return self

    @property
    def blocked(self) -> bool:
        return bool(self.reasons)


class AlertMessage(Envelope):
    type: Literal["alert"] = "alert"
    level: AlertLevel
    code: str
    message_ko: str
    detail_ko: str = ""


class RiskMessage(Envelope):
    type: Literal["risk"] = "risk"

    # ⚠️ 조회 실패를 '포지션 없음'으로 그리면 안 된다.
    # 10배에서 없다고 믿고 또 잡으면 두 배로 물린다. 그래서 실패 사실을
    # 반드시 내려보내고, 화면은 마지막으로 알던 값을 '오래됨'으로 표시한다.
    errors: list[str] = Field(default_factory=list)
    stale: bool = False
    """직전 값을 그대로 쓰고 있다는 뜻. 지금 조회된 값이 아니다."""

    has_position: bool = False
    side: str | None = None

    # 화면이 **매초 스스로** 평가손익을 계산하려면 이 넷이 필요하다.
    # 그래야 1초 갱신을 하면서도 계정 API 를 1초마다 두드리지 않는다.
    entry_price: float | None = None
    qty: float = 0.0
    stop_price: float | None = None
    liquidation_price: float | None = None
    unrealized_pnl: float | None = None

    liq_distance_pct: float | None = None
    underlying_move_to_liq_pct: float | None = None
    underlying_multiplier: float = 1.0
    has_stop: bool = False
    stop_distance_pct: float | None = None
    account_leverage: float = 0.0
    daily_r: float | None = None
    daily_locked: bool = False


class SessionMessage(Envelope):
    type: Literal["session"] = "session"
    regime: str
    regime_label_ko: str
    regime_ends_in_sec: int | None = None
    blocked: bool = False
    next_event_name: str | None = None
    next_event_in_sec: int | None = None
    warnings: list[str] = Field(default_factory=list)


class CoverageItem(BaseModel):
    name: str
    rows: int
    first: datetime | None = None
    last: datetime | None = None
    status: str
    depends: str


class CoverageMessage(Envelope):
    """어떤 조건이 '데이터 없음'으로 죽어 있는지 화면에서 보이게 한다."""

    type: Literal["coverage"] = "coverage"
    items: list[CoverageItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DisciplineMessage(BaseModel):
    """규율 지표 (docs/15 1-3). 방향이 아니라 **한도 대비 지금 위치**만 담는다.

    `ok=False` 면 계정 조회가 실패한 턴 — 계정 숫자는 None 이다. 브라우저는
    마지막 값을 유지하고 흐리게 그린다. 0 으로 보내면 '노출 없음'으로 읽힌다.
    """

    total_exposure_x: float | None = None
    total_limit_x: float = 3.0
    max_symbol: str | None = None
    max_symbol_exposure_x: float | None = None
    symbol_limit_x: float = 1.0
    daily_pnl_pct: float | None = None
    drawdown_limit_pct: float = 15.0
    remaining_r: float | None = None
    r_limit: float = 3.0
    event_blocked: bool = False
    event_name: str | None = None
    event_in_sec: int | None = None
    closes_in_sec: int | None = None
    ok: bool = True


class SnapshotMessage(Envelope):
    """접속 시 한 번. 위 블록들의 현재 상태를 모아서 보낸다."""

    type: Literal["snapshot"] = "snapshot"
    risk: RiskMessage | None = None
    session: SessionMessage | None = None
    alerts: list[AlertMessage] = Field(default_factory=list)
    no_trade: NoTradeMessage | None = None
    coverage: CoverageMessage | None = None
    discipline: DisciplineMessage | None = None


class WatchlistItem(BaseModel):
    symbol: str
    price: float | None = None
    change_pct: float | None = None
    quote_volume: float | None = None
    lev_tag: str | None = None
    collected: bool = False
    group: str = "기타"
    """docs/15 9절 — 워치리스트 그룹 칩 (반도체 · 지수ETF · 대형기술주 · 기타)."""


class WatchlistMessage(BaseModel):
    """docs/15 1-1. `th.universe.STOCK_PERPS` 순서, 바이낸스에 없는 심볼은 빠진다."""

    type: Literal["watchlist"] = "watchlist"
    ts: datetime
    focus: str
    items: list[WatchlistItem] = Field(default_factory=list)
    basket_exposure_x: float | None = None


class MarketMessage(Envelope):
    """docs/15 1-2. 펀딩·마크가·본주·베이시스. 카운트다운은 브라우저가 센다."""

    type: Literal["market"] = "market"
    funding_rate: float | None = None
    next_funding_at: datetime | None = None
    mark_price: float | None = None
    underlying_price: float | None = None
    basis_pct: float | None = None
    underlying_ticker: str | None = None


class BracketItem(BaseModel):
    id: str
    symbol: str
    direction: str
    entry: float | None = None
    stop: float
    tp1: float | None = None
    tp2: float | None = None
    qty: float
    position_qty: float = 0.0
    has_stop: bool = False
    status_ko: str = ""
    realized_r: float = 0.0
    risk_per_unit: float | None = None
    close_requested: bool = False
    setup_id: str = ""            # v2.2 — 시그널 엔진이 연 브래킷은 "video200_20"


class BracketsMessage(BaseModel):
    """docs/15 1-4. 페이퍼 브래킷 패널. 평가 R 은 브라우저가 매초 계산한다."""

    type: Literal["brackets"] = "brackets"
    ts: datetime
    mode: str
    available: bool = True
    message_ko: str = ""
    open: list[BracketItem] = Field(default_factory=list)
    today_realized_r: float | None = None
    today_count: int = 0
    expectancy_r: float | None = None


class FocusMessage(BaseModel):
    """docs/15 1-5. 포커스 종목이 바뀌면 즉시."""

    type: Literal["focus"] = "focus"
    symbol: str
    collected: bool = False


class PositionItem(BaseModel):
    """실매매 포지션 한 줄 (docs/15 8절). 손절 없음(`has_stop=False`)은 화면이 붉게 칠한다."""

    symbol: str
    side: str
    qty: float
    entry_price: float
    mark_price: float
    notional_usdt: float
    unrealized_pnl: float
    unrealized_pct: float | None = None
    liquidation_price: float | None = None
    liq_distance_pct: float | None = None
    underlying_move_to_liq_pct: float | None = None
    leverage_eff: float | None = None
    has_stop: bool = False
    stop_price: float | None = None
    stop_distance_pct: float | None = None


class PositionsMessage(BaseModel):
    """docs/15 8절. 전 종목 실매매 포지션 (3초).

    `ok=False` 면 계정 조회가 실패한 턴 — `items` 는 **마지막 값 그대로**, `wallet_usdt` 는 None.
    빈 목록으로 보내면 '포지션 없음'으로 읽힌다. 10배에서 그건 사고다.
    """

    type: Literal["positions"] = "positions"
    ts: datetime
    ok: bool = True
    wallet_usdt: float | None = None
    items: list[PositionItem] = Field(default_factory=list)


class GateCondition(BaseModel):
    text_ko: str
    met: bool = False


class GateResponse(BaseModel):
    """`GET /api/orders/gate`. `live_allowed` 는 타입 수준에서 **항상 False** 다."""

    mode: str
    live_allowed: Literal[False] = False
    conditions: list[GateCondition] = Field(default_factory=list)


class OrderPreviewResponse(BaseModel):
    """`POST /api/orders/preview`. `reasons` 가 비어 있지 않으면 가드가 막을 주문이다."""

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
    warnings: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


# v0.3 서버가 보낼 수 있는 전부. `signal` 도 `briefing` 도 없다.
OUTBOUND_TYPES = frozenset(
    {"snapshot", "risk", "session", "alert", "no_trade", "coverage", "bar",
     "watchlist", "market", "brackets", "focus", "positions"}
)
FORBIDDEN_TYPES = frozenset({"signal", "briefing"})
