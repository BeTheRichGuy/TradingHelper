"""계정 조회 + 리스크 계산 + 경고를 한 번에 묶는 층.

CLI 와 (나중의) 웹 API 는 여기만 부른다.

원칙 하나: **한 조각이 실패해도 나머지는 보여준다.** 예를 들어 DB가 죽어
변동성 통계를 못 구해도 청산가 거리는 계속 떠야 한다. 안전장치가 통째로
꺼지는 것이 가장 나쁘다.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .. import db
from ..config import get_settings
from ..logging_setup import get_logger
from ..sessions.models import SessionContext
from .alerts import Alert, MarketStats, build_alerts
from .client import AccountClient, NoApiKey
from .models import Balance, DailyPnl, Discipline, Position, RiskSnapshot
from .risk import (
    DAILY_LOSS_LIMIT_R,
    DRAWDOWN_LIMIT_PCT,
    EXPOSURE_LIMIT_X,
    SYMBOL_LIMIT_X,
    build_snapshot,
    kst_today,
    r_unit,
)

log = get_logger(__name__)

ATR_PERIOD = 14
ATR_LOOKBACK = 200


@dataclass(slots=True)
class RiskView:
    """화면 하나를 그리는 데 필요한 전부."""

    risk: RiskSnapshot
    alerts: list[Alert]
    market: MarketStats
    session: SessionContext | None = None
    positions: tuple[Position, ...] = ()
    """**전 종목** 포지션 (positionRisk 전체 조회). 규율 지표의 총 노출은 이걸로 센다."""
    discipline: Discipline | None = None


# ─────────────────────────────────────────────────────────
# 시장 통계 (DB)
# ─────────────────────────────────────────────────────────

async def market_stats(symbol: str) -> MarketStats:
    atr_pct = atr_pctl = spread = burst = None

    try:
        rows = await db.fetch(
            """SELECT high, low, close FROM klines
               WHERE symbol = $1 AND tf = '1m'
               ORDER BY ts DESC LIMIT $2""",
            symbol,
            ATR_LOOKBACK + ATR_PERIOD + 2,
        )
        atr_pct, atr_pctl = _atr_from_rows(rows)
    except Exception as e:
        log.debug("ATR 계산 실패: %s", e)

    try:
        row = await db.fetch(
            """SELECT spread_bps FROM book_ticker
               WHERE symbol = $1 ORDER BY ts DESC LIMIT 1""",
            symbol,
        )
        if row:
            spread = float(row[0]["spread_bps"])
    except Exception as e:
        log.debug("스프레드 조회 실패: %s", e)

    try:
        burst = await _liquidation_burst(symbol)
    except Exception as e:
        log.debug("청산 캐스케이드 계산 실패: %s", e)

    return MarketStats(
        atr_pct=atr_pct,
        atr_percentile=atr_pctl,
        spread_bps=spread,
        liquidation_burst_ratio=burst,
    )


def _atr_from_rows(rows) -> tuple[float | None, float | None]:
    """(현재 ATR%, 최근 구간 내 퍼센타일). 봉은 최신순으로 들어온다."""
    if len(rows) < ATR_PERIOD + 5:
        return None, None

    bars = list(reversed([(float(r["high"]), float(r["low"]), float(r["close"])) for r in rows]))

    trs: list[float] = []
    for i in range(1, len(bars)):
        h, low, _ = bars[i]
        prev_close = bars[i - 1][2]
        trs.append(max(h - low, abs(h - prev_close), abs(low - prev_close)))

    if len(trs) < ATR_PERIOD:
        return None, None

    atrs: list[float] = []
    for i in range(ATR_PERIOD, len(trs) + 1):
        window = trs[i - ATR_PERIOD : i]
        close = bars[i][2] if i < len(bars) else bars[-1][2]
        if close > 0:
            atrs.append(sum(window) / ATR_PERIOD / close * 100)

    if not atrs:
        return None, None

    current = atrs[-1]
    history = atrs[-ATR_LOOKBACK:]
    if len(history) < 20:
        return current, None
    below = sum(1 for v in history if v <= current)
    return current, below / len(history) * 100


async def _liquidation_burst(symbol: str) -> float | None:
    rows = await db.fetch(
        """
        WITH buckets AS (
            SELECT floor(extract(epoch FROM ts) / 300) AS b, sum(notional) AS n
            FROM liquidations
            WHERE symbol = $1 AND ts > now() - interval '24 hours'
            GROUP BY 1
        )
        SELECT
            (SELECT coalesce(sum(notional), 0) FROM liquidations
             WHERE symbol = $1 AND ts > now() - interval '5 minutes') AS recent,
            (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY n) FROM buckets) AS median,
            (SELECT count(*) FROM buckets) AS n_buckets
        """,
        symbol,
    )
    if not rows:
        return None
    r = rows[0]
    median = r["median"]
    if not median or float(median) <= 0 or int(r["n_buckets"] or 0) < 12:
        return None       # 표본이 부족하면 판단하지 않는다
    return float(r["recent"]) / float(median)


# ─────────────────────────────────────────────────────────
# 일일 손익 · 한도 락
# ─────────────────────────────────────────────────────────

class DailyStateUnavailable(Exception):
    """오늘 상태를 읽지 못했다. **'한도에 안 걸림'과 절대 같지 않다.**"""


async def load_limit_hit_at(day: date) -> datetime | None:
    """오늘 한도에 닿은 시각. 기록이 없으면 None.

    ⚠️ 조회가 **실패**했을 때 None 을 돌려주면 '한도에 안 걸렸다'로 읽힌다.
    그러면 DB 가 잠깐 흔들린 사이에 일일 손실 잠금이 조용히 풀린다.
    안전장치는 모르면 막아야 하므로 여기서는 예외를 올린다.
    """
    try:
        rows = await db.fetch(
            "SELECT limit_hit_at FROM daily_pnl WHERE date_kst = $1", day
        )
    except Exception as e:
        log.warning("daily_pnl 조회 실패 — 한도 상태를 알 수 없습니다: %s", e)
        raise DailyStateUnavailable(str(e)) from e
    return rows[0]["limit_hit_at"] if rows else None


async def save_daily(daily: DailyPnl) -> None:
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO daily_pnl
                     (date_kst, realized, commission, funding, trade_count,
                      r_unit, r_value, limit_hit_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())
                   ON CONFLICT (date_kst) DO UPDATE SET
                     realized = EXCLUDED.realized,
                     commission = EXCLUDED.commission,
                     funding = EXCLUDED.funding,
                     trade_count = EXCLUDED.trade_count,
                     r_unit = EXCLUDED.r_unit,
                     r_value = EXCLUDED.r_value,
                     -- 한 번 찍힌 락 시각은 덮어쓰지 않는다
                     limit_hit_at = COALESCE(daily_pnl.limit_hit_at, EXCLUDED.limit_hit_at),
                     updated_at = now()""",
                daily.date_kst, daily.realized_pnl, daily.commission, daily.funding,
                daily.trade_count, daily.r_unit, daily.r_value, daily.limit_hit_at,
            )
    except Exception as e:
        log.debug("daily_pnl 저장 실패: %s", e)


# ─────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────


def carry_forward(view: RiskView, prev: RiskView | None) -> RiskView:
    """이번 턴에 못 가져온 값은 직전 값을 그대로 쓴다.

    ⚠️ 여기서 제일 중요한 건 **조회 실패를 '포지션 없음'으로 만들지 않는 것**이다.
    `get_risk_view` 는 계정 조회가 실패하면 `position=None` 인 스냅샷을 돌려준다.
    그걸 그대로 그리면 화면에는 "열린 포지션이 없습니다"가 뜬다. 10배 레버리지에서
    없다고 믿고 또 잡으면 두 배로 물린다.

    그래서 실패한 턴에는 **마지막으로 알던 포지션을 유지**하고, 대신
    '지금 값이 아니다'라는 사실(`errors`)을 반드시 남긴다.

    터미널(`th watch`)과 웹이 같은 함수를 쓴다. 규칙을 두 곳에 적으면 갈라진다.
    """
    from dataclasses import replace

    if prev is None:
        return view

    # 주기를 나눠 도느라 이번 턴에 안 부른 것들
    if view.risk.daily is None and prev.risk.daily is not None:
        view.risk = replace(view.risk, daily=prev.risk.daily)
    if view.market.atr_pct is None:
        view.market = prev.market

    # **계정 조회 자체**가 실패했는데 직전에 포지션이 있었다면 그대로 유지한다.
    # (income·DB 조회만 실패한 턴은 포지션을 제대로 봤으므로 그대로 믿는다 —
    #  아니면 이미 닫은 포지션이 화면에 계속 남는다)
    account_failed = not view.risk.account_ok
    if account_failed and not view.risk.has_position and prev.risk.has_position:
        # `account_ok=False` 는 그대로 남긴다 — '이번 턴에 못 봤다'는 사실이지 직전 값이 아니다
        view.risk = replace(
            prev.risk,
            at=view.risk.at,
            daily=view.risk.daily or prev.risk.daily,
            errors=view.risk.errors,
            account_ok=False,
        )
    # 전 종목 포지션과 규율 지표도 같은 규칙 — 실패한 턴에는 마지막으로 알던 값을
    # 남기되, `ok=False` 로 '지금 값이 아니다'를 반드시 표시한다.
    if account_failed:
        if not view.positions and prev.positions:
            view.positions = prev.positions
        if view.discipline is None and prev.discipline is not None:
            view.discipline = replace(prev.discipline, ok=False)
    return view


async def get_risk_view(
    symbol: str | None = None,
    session: SessionContext | None = None,
    client: AccountClient | None = None,
    with_income: bool = True,
    with_market: bool = True,
) -> RiskView:
    s = get_settings()
    symbol = (symbol or s.symbols[0]).upper()
    at = datetime.now(UTC)

    own_client = client is None
    client = client or AccountClient()

    errors: list[str] = []
    position: Position | None = None
    positions: list[Position] = []
    balance = Balance(0.0, 0.0, 0.0, 0.0)
    orders: list = []
    daily: DailyPnl | None = None
    account_ok = True

    try:
        # positionRisk 를 심볼 없이 **한 번** 부른다 — 이 심볼의 포지션도 여기서 고르고,
        # 규율 지표의 총 노출(전 종목)도 같은 응답으로 센다. 두 번 부를 이유가 없다.
        positions = [p for p in await client.positions() if p.is_open]
        position = next((p for p in positions if p.symbol == symbol), None)
        balance = await client.balance()
        # 미체결 주문도 **전 종목** 한 번에 — 이 심볼의 손절은 여기서 고르고(find_stop_orders 가
        # 심볼로 거른다), 실매매 포지션 표(docs/15 8절)는 종목마다 손절 주문을 매핑한다.
        orders = await client.open_orders()
    except NoApiKey as e:
        account_ok = False
        errors.append(str(e))
    except Exception as e:
        account_ok = False
        # 예외 문자열에는 서명된 URL(API 키 포함)이 들어 있을 수 있다 — 타입과 상태코드만 남긴다
        from ..exchange.binance_rest import BinanceAPIError, explain_binance_error

        status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status_code", None)
        ko = explain_binance_error(e)
        if ko:
            errors.append(f"계정 조회 거절 (code {e.code}): {ko}")
        elif isinstance(e, BinanceAPIError):
            errors.append(f"계정 조회 거절 (HTTP {e.status} code={e.code}): {e.msg}")
        else:
            errors.append(
                f"계정 조회 실패: {type(e).__name__}" + (f" (HTTP {status})" if status else "")
            )
        log.debug("계정 조회 실패", exc_info=True)

    if with_income and client.has_key and not errors:
        try:
            if balance.wallet <= 0:
                # 지갑이 0 이면 R 단위를 정할 수 없다. 0.0R 로 두면 '한도 미달'로 읽힌다
                raise ValueError("지갑 잔고가 0 이하 — R 단위를 정할 수 없습니다")
            realized, commission, funding, trades = await client.daily_income(symbol, at)
            day = kst_today(at)
            unit = r_unit(balance.wallet, s.risk_pct)
            hit = await load_limit_hit_at(day)
            daily = DailyPnl(
                date_kst=day,
                realized_pnl=realized,
                commission=commission,
                funding=funding,
                trade_count=trades,
                r_unit=unit,
                limit_hit_at=hit,
            )
            if hit is None and daily.r_value <= DAILY_LOSS_LIMIT_R:
                daily = DailyPnl(
                    date_kst=day,
                    realized_pnl=realized,
                    commission=commission,
                    funding=funding,
                    trade_count=trades,
                    r_unit=unit,
                    limit_hit_at=at,
                )
                log.warning("일일 손실 한도 도달 (%.2fR) — 오늘은 잠깁니다", daily.r_value)
            await save_daily(daily)
        except Exception as e:
            errors.append(f"당일 손익 조회 실패: {type(e).__name__}")
            log.debug("income 조회 실패", exc_info=True)

    market = MarketStats()
    if with_market:
        try:
            market = await market_stats(symbol)
        except Exception as e:
            log.debug("시장 통계 실패: %s", e)

    snapshot = build_snapshot(
        symbol=symbol,
        position=position,
        balance=balance,
        orders=orders,
        daily=daily,
        underlying=s.underlying_map.get(symbol),
        at=at,
        errors=tuple(errors),
        account_ok=account_ok,
    )
    alerts = build_alerts(snapshot, session, market)

    if own_client:
        await client.close()

    return RiskView(risk=snapshot, alerts=alerts, market=market, session=session,
                    positions=tuple(positions))


# ─────────────────────────────────────────────────────────
# 규율 지표 (docs/15 1-3)
# ─────────────────────────────────────────────────────────

# 세션 게이트의 차단 사유 중 '이벤트' 때문인 것. rules.yaml 의 id 와 events.py 의
# `event:<kind>` 접두어를 함께 본다.
_EVENT_RULE_PREFIXES = ("event", "fomc", "econ", "earnings")


def underlying_close_at(at: datetime) -> datetime | None:
    """오늘 본주 휴장(애프터 끝) 시각. 지금이 🔴 정지 구간이면 None."""
    from ..sessions.market import get_calendar
    from ..sessions.models import Regime

    regime, windows = get_calendar().regime_at(at)
    if windows is None or regime is Regime.CLOSED:
        return None
    return windows.post_end


def build_discipline(
    view: RiskView,
    session: SessionContext | None = None,
    at: datetime | None = None,
    closes_at: datetime | None = None,
) -> Discipline:
    """규율 지표 — **순수 계산.** 조회는 `get_risk_view` 가 이미 끝냈다.

    `carry_forward` 뒤에 불러야 한다. 당일 손익(30초 주기)과 전 종목 포지션은
    보정을 거쳐야 채워지기 때문이다.

    계정 조회가 실패한 턴은 `ok=False` 에 계정 숫자 None — 0 으로 두면 '노출 없음'
    으로 읽힌다. 이벤트·휴장까지는 세션에서 오므로 그대로 채운다.
    """
    at = at or view.risk.at
    r_limit = abs(DAILY_LOSS_LIMIT_R)

    event_blocked = False
    event_name: str | None = None
    event_in_sec: int | None = None
    if session is not None:
        event_blocked = any(
            b.rule_id.startswith(_EVENT_RULE_PREFIXES) for b in session.blocks
        )
        if session.next_event is not None:
            event_name = session.next_event.name
            event_in_sec = int(session.next_event.seconds_from(at))
    closes_in_sec = (
        max(0, int((closes_at - at).total_seconds())) if closes_at else None
    )

    base = dict(
        total_limit_x=EXPOSURE_LIMIT_X, symbol_limit_x=SYMBOL_LIMIT_X,
        drawdown_limit_pct=DRAWDOWN_LIMIT_PCT, r_limit=r_limit,
        event_blocked=event_blocked, event_name=event_name,
        event_in_sec=event_in_sec, closes_in_sec=closes_in_sec,
    )
    if not view.risk.account_ok:
        return Discipline(ok=False, **base)

    wallet = view.risk.balance.wallet
    opens = [p for p in view.positions if p.is_open]
    # 전 종목 조회가 없던 옛 호출(터미널)이라도 이 심볼 포지션만은 센다
    if not opens and view.risk.has_position:
        opens = [view.risk.position]

    total_x = max_x = None
    max_symbol = None
    if wallet > 0:
        total_x = sum(p.notional for p in opens) / wallet
        if opens:
            biggest = max(opens, key=lambda p: p.notional)
            max_symbol = biggest.symbol
            max_x = biggest.notional / wallet
        else:
            max_x = 0.0

    daily = view.risk.daily
    daily_pnl_pct = None
    if daily is not None and wallet > 0:
        unrealized = sum(p.unrealized_pnl for p in opens)
        daily_pnl_pct = (daily.net + unrealized) / wallet * 100

    remaining_r = None
    if daily is not None:
        remaining_r = 0.0 if daily.locked(DAILY_LOSS_LIMIT_R) else max(
            0.0, r_limit - max(0.0, -daily.r_value)
        )

    return Discipline(
        ok=True,
        total_exposure_x=total_x, max_symbol=max_symbol,
        max_symbol_exposure_x=max_x, daily_pnl_pct=daily_pnl_pct,
        remaining_r=remaining_r, **base,
    )
