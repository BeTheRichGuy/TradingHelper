"""백테스트 실행 — DB를 아는 유일한 곳.

**설계 원칙 하나만 지킨다: 백테스트는 실전과 똑같은 입력을 본다.**

그래서 캐시를 쓰지 않는다. 레벨을 하루 한 번만 계산하거나 OI 를 15분마다만 읽으면
분명히 빨라지지만, 그 순간 백테스트가 보는 숫자와 `th analyze` 가 보는 숫자가
갈라진다. 그러면 통과한 백테스트가 아무것도 보증하지 못한다. Phase 3 에서 겪은
'미완성 5분봉' 버그가 정확히 그런 종류였다.

빠르게 만든 곳은 한 군데뿐이다 — 1분봉을 매 스텝 DB에서 다시 읽지 않고 구간 전체를
한 번 읽어 **같은 폭(5일)** 으로 슬라이스한다. 이건 값이 바뀌지 않는 최적화다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..logging_setup import get_logger
from ..sessions.gate import get_context
from ..strategy import data as sdata
from ..strategy.engine import analyze_features
from ..strategy.features import PerpContext, build_features
from ..strategy.indicators import Bar
from .models import BacktestConfig, Skipped, Trade
from .simulate import simulate_trade

log = get_logger(__name__)

LOOKBACK_DAYS = sdata.DEFAULT_LOOKBACK_DAYS
FORWARD_BARS = 600            # 청산 판정용 앞쪽 봉 (최대 보유 4시간 + 여유)
MIN_BARS = 60


@dataclass
class BacktestResult:
    symbol: str
    start: datetime
    end: datetime
    config: BacktestConfig
    trades: list[Trade] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    evaluated: int = 0
    blocked: int = 0
    filtered: int = 0
    signals: int = 0
    basis_bars: int = 0

    @property
    def basis_coverage(self) -> float:
        """basis(본주 괴리)를 실제로 볼 수 있었던 평가 비율."""
        return self.basis_bars / self.evaluated if self.evaluated else 0.0

    @property
    def skip_reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.skipped:
            out[s.reason] = out.get(s.reason, 0) + 1
        return out


async def load_series(symbol: str, start: datetime, end: datetime) -> list[Bar]:
    since = start - timedelta(days=LOOKBACK_DAYS + 1)
    until = end + timedelta(minutes=FORWARD_BARS + 60)
    rows = await sdata.db.fetch(
        """SELECT ts, open, high, low, close, volume, taker_buy_base
           FROM klines
           WHERE symbol = $1 AND tf = '1m' AND ts >= $2 AND ts <= $3
           ORDER BY ts""",
        symbol.upper(), since, until,
    )
    return [
        Bar(
            ts=r["ts"],
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
            taker_buy=float(r["taker_buy_base"]) if r["taker_buy_base"] is not None else None,
        )
        for r in rows
    ]


def bisect_ts(bars: list[Bar], when: datetime) -> int:
    """`ts <= when` 인 마지막 인덱스 + 1."""
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid].ts <= when:
            lo = mid + 1
        else:
            hi = mid
    return lo


def steps(start: datetime, end: datetime, minutes: int):
    t = start
    while t <= end:
        yield t
        t += timedelta(minutes=minutes)


@dataclass(frozen=True, slots=True)
class Evaluation:
    """한 시각을 평가한 결과. 백테스트와 퍼널 진단이 **같은 루프**를 쓴다.

    둘이 각자 루프를 돌면 언젠가 서로 다른 숫자를 말하게 된다.
    """

    at: datetime
    session: object
    analysis: object | None       # 게이트에 막혔으면 None
    features: object | None
    perp: PerpContext | None
    bar_index: int                # `at` 기준 닫힌 봉 다음 인덱스


async def iter_evaluations(
    symbol: str,
    series: list[Bar],
    start: datetime,
    end: datetime,
    config: BacktestConfig,
    *,
    perp_provider=None,
    volume_provider=None,
):
    """레짐이 맞는 매 시각을 하나씩 내놓는다. **여기가 유일한 평가 루프다.**

    `perp_provider` / `volume_provider` 를 넘기면 DB 대신 그걸 쓴다. CSV 로 돌리는
    오프라인 백테스트가 같은 루프를 타게 하려는 것이다 — 루프를 복사해 두면
    언젠가 둘이 다른 숫자를 말한다.
    """
    for at in steps(start, end, config.step_minutes):
        session = get_context(symbol, at)
        if session.regime.value not in config.regimes:
            continue

        if session.blocked:
            yield Evaluation(at, session, None, None, None, 0)
            continue

        # 실전 `load_bars` 와 동일하게 닫힌 봉만, 동일한 5일 폭
        until = at - timedelta(minutes=1)
        hi = bisect_ts(series, until)
        lo = bisect_ts(series, until - timedelta(days=LOOKBACK_DAYS))
        window = series[lo:hi]
        if len(window) < MIN_BARS:
            yield Evaluation(at, session, None, None, None, hi)
            continue

        if perp_provider is not None:
            perp = perp_provider(at, window[-1].close)
        else:
            perp = await _perp_at(symbol, at, window[-1].close)

        f = build_features(symbol, window, at, session, perp)

        if volume_provider is not None:
            median_vol = volume_provider(at)
        else:
            median_vol = await sdata.median_volume_5m(symbol, at)

        a = analyze_features(
            f,
            session,
            wallet=config.wallet,
            risk_pct=config.risk_pct,
            leverage=config.leverage,
            median_volume_5m=median_vol,
        )
        yield Evaluation(at, session, a, f, perp, hi)


async def run_backtest(
    symbol: str,
    start: datetime,
    end: datetime,
    config: BacktestConfig,
    *,
    progress: Callable[[datetime, int, int], None] | None = None,
    dataset=None,
) -> BacktestResult:
    """`dataset` 을 주면 DB 대신 CSV 데이터셋으로 돈다 (같은 루프를 탄다)."""
    symbol = symbol.upper()
    perp_provider = volume_provider = None
    if dataset is not None:
        series = list(dataset.bars)
        perp_provider = lambda at, price: dataset.perp_at(at, price)   # noqa: E731
        volume_provider = dataset.median_volume_5m
    else:
        series = await load_series(symbol, start, end)
    result = BacktestResult(symbol=symbol, start=start, end=end, config=config)
    if len(series) < MIN_BARS:
        log.warning("1분봉이 %d개뿐입니다 — 먼저 th backfill 로 채우세요", len(series))
        return result

    busy_until: datetime | None = None

    async for ev in iter_evaluations(
        symbol, series, start, end, config,
        perp_provider=perp_provider, volume_provider=volume_provider,
    ):
        result.evaluated += 1
        if progress is not None:
            progress(ev.at, result.evaluated, len(result.trades))

        if ev.session.blocked:
            result.blocked += 1
            continue
        a = ev.analysis
        if a is None or ev.perp is None:
            continue
        if ev.perp.basis_pct is not None:
            result.basis_bars += 1
        if a.failed_filters:
            result.filtered += 1
        if a.signal is None or a.signal.plan is None:
            continue

        result.signals += 1

        if busy_until is not None and ev.at < busy_until and not config.allow_overlap:
            result.skipped.append(Skipped(ev.at, a.signal.result.setup_id, "overlap"))
            continue

        hi = ev.bar_index
        outcome = simulate_trade(
            a.signal.plan,
            series[hi : hi + FORWARD_BARS],
            signal_at=ev.at - timedelta(minutes=1),
            setup_id=a.signal.result.setup_id,
            regime=ev.session.regime.value,
            score=a.signal.score.final,
            config=config,
            deadline=ev.session.regime_ends_at,
            funding_rate=ev.perp.funding_rate,
            next_funding_at=ev.perp.next_funding_at,
        )
        if isinstance(outcome, str):
            result.skipped.append(Skipped(ev.at, a.signal.result.setup_id, outcome))
            continue

        result.trades.append(outcome)
        busy_until = outcome.exit_at

    return result


async def _perp_at(symbol: str, at: datetime, price: float) -> PerpContext:
    p = await sdata.load_perp_context(symbol, at)
    return PerpContext(
        funding_rate=p.funding_rate,
        next_funding_at=p.next_funding_at,
        oi_change_5m=p.oi_change_5m,
        oi_change_15m=p.oi_change_15m,
        basis_pct=sdata.compute_basis(price, p.underlying_price),
        spread_bps=p.spread_bps,
        underlying_price=p.underlying_price,
        vix=p.vix,
    )


def parse_day(text: str) -> datetime:
    """YYYY-MM-DD → 그날 00:00 UTC."""
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
