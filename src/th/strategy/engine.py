"""전략 엔진 — 02 문서의 평가 순서를 그대로 구현한다.

    1. 세션·이벤트 게이트   ← blocked 면 여기서 끝. 셋업 계산을 시작하지 않는다
    2. 시장 상태 필터       (스프레드·변동성·괴리·유동성)
    3. 셋업 조건 평가
    4. 리스크 계산
    5. 신뢰도 점수 + 세션 가감
    6. 최종 발행 여부

순서가 중요하다. 계산해놓고 나중에 가리는 구조면 언젠가 화면 어딘가로 새어 나간다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..logging_setup import get_logger
from ..sessions.models import Regime, SessionContext
from .features import Features, PerpContext
from .risk import TradePlan, build_plan
from .scoring import MIN_COVERAGE, MIN_PUBLISH_SCORE, Score, oi_reading, score_setup
from .setups.base import Direction, SetupResult
from .setups.s1_vwap_pullback import S1VwapPullback
from .setups.s3_sweep_reversal import S3SweepReversal
from .setups.s5_vwma_pullback import S5VwmaPullback

log = get_logger(__name__)

# 02 문서 3절 — 셋업 평가 전 통과해야 하는 시장 상태
MAX_SPREAD_BPS = 8.0            # 0.08%
MAX_ATR_PERCENTILE = 95.0
MAX_BASIS_PCT = 0.5
MIN_RELATIVE_VOLUME = 0.4
VIX_WARN = 30.0

# ⚠️ 이 셋업들은 **실측에서 우위가 확인되지 않았다** (docs/08). 화면은 이걸로
# 방향을 제시하지 않으며, 백테스트·연구용으로만 남아 있다.
# S5 가 사용자가 지목한 방식이고 S1·S3 는 비교군이다.
SETUPS = (S5VwmaPullback(), S1VwapPullback(), S3SweepReversal())


@dataclass(frozen=True, slots=True)
class FilterResult:
    name: str
    passed: bool
    reason_ko: str


@dataclass(frozen=True, slots=True)
class Candidate:
    result: SetupResult
    score: Score
    plan: TradePlan | None
    published: bool
    reject_reason: str = ""

    @property
    def key(self) -> tuple:
        return (self.published, self.score.final)


@dataclass(slots=True)
class Analysis:
    symbol: str
    at: datetime
    session: SessionContext | None = None
    features: Features | None = None
    filters: tuple[FilterResult, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    near_misses: tuple[SetupResult, ...] = ()   # 필수 조건에서 걸린 셋업
    signal: Candidate | None = None
    blocked_reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def direction(self) -> str:
        return self.signal.result.direction if self.signal else "none"

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reasons)

    @property
    def failed_filters(self) -> tuple[FilterResult, ...]:
        return tuple(f for f in self.filters if not f.passed)

    def why_not_ko(self) -> list[str]:
        """'왜 지금은 아닌가' — 이 앱의 주된 출력."""
        if self.blocked_reasons:
            return list(self.blocked_reasons)
        out = [f.reason_ko for f in self.failed_filters]
        if out:
            return out
        for c in self.candidates:
            if c.reject_reason:
                out.append(f"{c.result.setup_id}: {c.reject_reason}")
        if out:
            return out

        # 필수 조건에서 걸린 경우 — **어느 조건인지** 말해준다.
        # "평가할 셋업이 없다"로 끝내면 사용자가 판단할 근거가 없다 (01 문서 7절).
        seen: set[str] = set()
        for r in self.near_misses:
            failed = r.failed_required
            if not failed:
                continue
            key = f"{r.setup_id}:{failed[0].label}"
            if key in seen:
                continue
            seen.add(key)
            out.append(f"{r.setup_id}: {failed[0].label} — {failed[0].detail}")

        if not out:
            out.append("현재 레짐에서 평가할 셋업이 없습니다")
        return out


# ─────────────────────────────────────────────────────────
# 필터
# ─────────────────────────────────────────────────────────

def market_filters(f: Features, median_volume_5m: float | None = None) -> list[FilterResult]:
    out: list[FilterResult] = []

    sp = f.perp.spread_bps
    out.append(
        FilterResult(
            "spread",
            sp is None or sp <= MAX_SPREAD_BPS,
            f"호가 스프레드 {sp:.2f}bp — 10배에서 진입·청산 비용이 손절폭을 잠식합니다"
            if sp is not None and sp > MAX_SPREAD_BPS
            else f"스프레드 {sp:.2f}bp" if sp is not None else "스프레드 데이터 없음",
        )
    )

    pctl = f.atr_percentile
    out.append(
        FilterResult(
            "volatility",
            pctl is None or pctl <= MAX_ATR_PERCENTILE,
            f"변동성 이상 (ATR 상위 {100 - pctl:.0f}%) — 정상 손절폭이 노이즈에 걸립니다"
            if pctl is not None and pctl > MAX_ATR_PERCENTILE
            else f"ATR {f.atr_pct:.2f}%" if f.atr_pct is not None else "ATR 데이터 없음",
        )
    )

    basis = f.perp.basis_pct
    out.append(
        FilterResult(
            "basis",
            basis is None or abs(basis) <= MAX_BASIS_PCT,
            f"본주 대비 괴리 {basis:+.2f}% 로 과대합니다"
            if basis is not None and abs(basis) > MAX_BASIS_PCT
            else f"본주 괴리 {basis:+.2f}%" if basis is not None else "본주 가격 없음",
        )
    )

    rel = None
    if median_volume_5m and f.volume_5m:
        rel = f.volume_5m / median_volume_5m
    out.append(
        FilterResult(
            "liquidity",
            rel is None or rel >= MIN_RELATIVE_VOLUME,
            f"거래량이 평소의 {rel:.0%} 로 부족합니다"
            if rel is not None and rel < MIN_RELATIVE_VOLUME
            else f"거래량 평소의 {rel:.0%}" if rel is not None else "거래량 기준 없음",
        )
    )
    return out


def volatility_notes(f: Features) -> list[str]:
    out = []
    if f.perp.vix is not None and f.perp.vix > VIX_WARN:
        out.append(
            f"VIX {f.perp.vix:.1f} — 권장 레버리지 5배 이하 (정상 손절폭이 노이즈에 걸립니다)"
        )
    return out


# ─────────────────────────────────────────────────────────
# 엔진
# ─────────────────────────────────────────────────────────

def active_setups(regime: Regime):
    return [s for s in SETUPS if regime.value in s.regimes]


def analyze_features(
    f: Features,
    session: SessionContext | None,
    *,
    wallet: float = 0.0,
    risk_pct: float = 0.5,
    leverage: int = 10,
    qty_step: float | None = None,
    median_volume_5m: float | None = None,
) -> Analysis:
    """DB 없이 Features 만으로 도는 순수 진입점 (백테스트·테스트에서 그대로 쓴다)."""
    analysis = Analysis(symbol=f.symbol, at=f.at, session=session, features=f)

    # 1. 게이트
    if session is not None and session.blocked:
        analysis.blocked_reasons = tuple(b.reason_ko for b in session.blocks)
        return analysis

    # 2. 시장 상태 필터
    analysis.filters = tuple(market_filters(f, median_volume_5m))
    notes = volatility_notes(f)
    reading = None

    if analysis.failed_filters:
        analysis.notes = tuple(notes)
        return analysis

    # 3~5. 셋업 → 리스크 → 점수
    regime = session.regime if session else Regime.RTH
    candidates: list[Candidate] = []
    near_misses: list[SetupResult] = []

    for setup in active_setups(regime):
        for direction in ("long", "short"):
            result = setup.evaluate(f, direction)  # type: ignore[arg-type]
            if not result.required_ok:
                near_misses.append(result)
                continue
            if reading is None:
                reading = oi_reading(f, direction)

            score = score_setup(result, f, session)
            plan = None
            reject = ""

            if result.entry is None or f.atr_1m is None:
                reject = "진입 구간을 계산할 수 없습니다"
            else:
                plan = _plan_for(
                    result, f, direction, wallet, risk_pct, leverage, qty_step
                )
                if not plan.publishable:
                    reject = plan.reject_reason

            if not reject and not score.coverage_ok:
                reject = (
                    f"조건 데이터가 {score.coverage:.0%}만 있습니다 "
                    f"(기준 {MIN_COVERAGE:.0%}) — 점수를 신뢰할 수 없습니다"
                )
            if not reject and not score.publishable:
                reject = f"신뢰도 {score.final}점이 기준({MIN_PUBLISH_SCORE}) 미만입니다"

            candidates.append(
                Candidate(
                    result=result,
                    score=score,
                    plan=plan,
                    published=not reject,
                    reject_reason=reject,
                )
            )

    candidates.sort(key=lambda c: c.key, reverse=True)
    analysis.candidates = tuple(candidates)
    analysis.near_misses = tuple(near_misses)
    analysis.signal = candidates[0] if candidates and candidates[0].published else None
    if reading:
        notes.append(reading)
    analysis.notes = tuple(notes)
    return analysis


def _plan_for(
    result: SetupResult,
    f: Features,
    direction: Direction,
    wallet: float,
    risk_pct: float,
    leverage: int,
    qty_step: float | None,
) -> TradePlan:
    long = direction == "long"
    swing = f.swing_low_1m if long else f.swing_high_1m
    levels = (
        f.levels.resistances_above(f.price) if long else f.levels.supports_below(f.price)
    )
    resistance = levels[0] if levels else None

    within_hold = False
    if f.perp.next_funding_at is not None:
        within_hold = (f.perp.next_funding_at - f.at).total_seconds() <= 4 * 3600

    return build_plan(
        direction,
        result.entry,
        atr=f.atr_1m or 0.0,
        price=f.price,
        swing=swing,
        resistance=resistance,
        wallet=wallet,
        risk_pct=risk_pct,
        leverage=leverage,
        qty_step=qty_step,
        funding_rate=f.perp.funding_rate,
        funding_within_hold=within_hold,
    )


# ─────────────────────────────────────────────────────────
# DB 를 쓰는 진입점
# ─────────────────────────────────────────────────────────

async def analyze(
    symbol: str | None = None,
    at: datetime | None = None,
    *,
    wallet: float | None = None,
) -> Analysis:
    from ..config import get_settings
    from ..sessions import get_context
    from . import data
    from .features import build_features

    s = get_settings()
    symbol = (symbol or s.symbols[0]).upper()
    at = (at or data.now_floor_minute()).astimezone(UTC).replace(second=0, microsecond=0)

    session = get_context(symbol, at)
    bars = await data.load_bars(symbol, at)
    if len(bars) < 60:
        a = Analysis(symbol=symbol, at=at, session=session)
        a.blocked_reasons = (
            f"봉이 {len(bars)}개뿐입니다 — `th backfill` 로 데이터를 채우세요",
        )
        return a

    perp = await data.load_perp_context(symbol, at)
    basis = data.compute_basis(bars[-1].close, perp.underlying_price)
    perp = PerpContext(
        funding_rate=perp.funding_rate,
        next_funding_at=perp.next_funding_at,
        oi_change_5m=perp.oi_change_5m,
        oi_change_15m=perp.oi_change_15m,
        basis_pct=basis if session.regime in (Regime.RTH, Regime.PRE, Regime.POST) else None,
        spread_bps=perp.spread_bps,
        underlying_price=perp.underlying_price,
        vix=perp.vix,
    )

    f = build_features(symbol, bars, at, session, perp)
    median_vol = await data.median_volume_5m(symbol, at)

    if wallet is None:
        wallet = await _wallet_or_zero()

    return analyze_features(
        f,
        session,
        wallet=wallet,
        risk_pct=s.risk_pct,
        median_volume_5m=median_vol,
    )


async def _wallet_or_zero() -> float:
    try:
        from ..account.client import AccountClient

        async with AccountClient() as ac:
            if not ac.has_key:
                return 0.0
            bal = await ac.balance()
            return bal.wallet
    except Exception as e:
        log.debug("잔고 조회 실패 (포지션 사이즈 생략): %s", e)
        return 0.0
