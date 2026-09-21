"""경고 생성 — 03 문서 5.3의 코드 7종.

**청산 경고는 수익 신호보다 위에 있다.** 화면 배치도, 이 목록의 정렬도 그렇게 한다.

여기도 순수 함수다. 입력(스냅샷·세션·시장상태)만 주면 같은 결과가 나오므로
테스트로 고정할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..sessions.models import Regime, SessionContext
from .models import RiskSnapshot
from .risk import DAILY_LOSS_LIMIT_R, LIQ_CRITICAL_PCT, LIQ_WARNING_PCT

LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2}

# 고중요도 이벤트가 이 시간 안이면 경고 (포지션 보유 중일 때)
EVENT_IMMINENT_SEC = 15 * 60
# 본장 마감 이 시간 전이면 알림
REGIME_CLOSING_SEC = 15 * 60
# ATR 퍼센타일이 이 값을 넘으면 변동성 이상
VOL_SPIKE_PERCENTILE = 95.0
# 최근 5분 강제청산 규모가 평소의 이 배수를 넘으면 캐스케이드
CASCADE_RATIO = 5.0


@dataclass(frozen=True, slots=True)
class Alert:
    code: str
    level: str                 # critical | warning | info
    message_ko: str
    detail_ko: str = ""

    @property
    def icon(self) -> str:
        return {"critical": "🔴", "warning": "🟡", "info": "🔵"}[self.level]


@dataclass(frozen=True, slots=True)
class MarketStats:
    """DB에서 계산해 넣어주는 보조 지표. 없으면 해당 경고를 건너뛴다."""

    atr_pct: float | None = None
    atr_percentile: float | None = None
    spread_bps: float | None = None
    liquidation_burst_ratio: float | None = None


def build_alerts(
    risk: RiskSnapshot,
    session: SessionContext | None = None,
    market: MarketStats | None = None,
) -> list[Alert]:
    out: list[Alert] = []
    pos = risk.position
    has_pos = risk.has_position

    # ── 1. 손절 주문 누락 — 10배에서 계좌가 죽는 가장 흔한 경로 ──
    if has_pos and not risk.has_stop:
        detail = f"{pos.side} {pos.qty:g}계약 · 진입 {pos.entry_price:g}"
        if pos.liquidation_price:
            detail += f" · 청산가 {pos.liquidation_price:g}"
        out.append(
            Alert(
                "NO_STOP_ORDER",
                "critical",
                "포지션이 열려 있는데 손절 주문이 없습니다",
                detail,
            )
        )

    # ── 1-b. 손절은 있는데 소용이 없는 경우 ────────────────
    #
    # 손절 주문이 걸려 있다는 사실만으로 안심하면 안 된다. 두 가지 상태는
    # "손절 없음"보다 위험하다 — 화면에 ✓ 가 뜨는데 실제로는 보호가 없다.
    if has_pos and risk.has_stop and risk.stop_distance_pct is not None:
        sd = risk.stop_distance_pct

        if sd <= 0:
            out.append(
                Alert(
                    "STOP_ALREADY_PASSED",
                    "critical",
                    "손절가가 이미 현재가를 지났습니다",
                    f"손절까지 {sd:+.2f}% — 주문이 남아 있어도 보호가 되지 않습니다",
                )
            )
        elif (
            risk.liq_distance_pct is not None
            and risk.liq_distance_pct > 0
            and sd >= risk.liq_distance_pct
        ):
            out.append(
                Alert(
                    "STOP_BEYOND_LIQUIDATION",
                    "critical",
                    "손절가가 청산가보다 멉니다 — 손절 전에 청산됩니다",
                    f"손절까지 {sd:.2f}% · 청산까지 {risk.liq_distance_pct:.2f}%"
                    + (
                        f" (기초지수 {risk.underlying_move_to_liq_pct:.2f}%)"
                        if risk.underlying_move_to_liq_pct is not None
                        and abs(risk.underlying_multiplier) > 1
                        else ""
                    ),
                )
            )

    # ── 2. 청산 임박 ────────────────────────────────────────
    if has_pos and risk.liq_distance_pct is not None:
        d = risk.liq_distance_pct
        under = risk.underlying_move_to_liq_pct
        detail = f"청산가까지 {d:.2f}%"
        if under is not None and abs(risk.underlying_multiplier) > 1:
            detail += f" · 기초지수 기준 {under:.2f}%"
        if d < LIQ_CRITICAL_PCT:
            out.append(Alert("LIQ_PROXIMITY", "critical", "청산가에 근접했습니다", detail))
        elif d < LIQ_WARNING_PCT:
            out.append(Alert("LIQ_PROXIMITY", "warning", "청산가와 거리가 좁습니다", detail))

    # ── 3. 이벤트 임박 ──────────────────────────────────────
    if has_pos and session is not None and session.next_event is not None:
        ev = session.next_event
        secs = session.next_event_in_sec or 0
        if ev.importance == "high" and 0 <= secs <= EVENT_IMMINENT_SEC:
            out.append(
                Alert(
                    "EVENT_IMMINENT",
                    "warning",
                    f"{ev.name}까지 {secs // 60}분 — 포지션 정리를 검토하세요",
                    "발표 순간엔 손절가를 건너뛰고 체결될 수 있습니다",
                )
            )

    # ── 4. 일일 손실 한도 ───────────────────────────────────
    if risk.daily is not None and risk.daily.locked(DAILY_LOSS_LIMIT_R):
        d = risk.daily
        out.append(
            Alert(
                "DAILY_LOSS_LIMIT",
                "warning",
                f"오늘 손실 한도 도달 ({d.r_value:.1f}R) — 오늘은 종료하세요",
                f"실현손익 {d.net:,.2f} USDT · {d.trade_count}회 매매 · 1R={d.r_unit:,.2f}",
            )
        )

    # ── 5. 레짐 전환 임박 ───────────────────────────────────
    if session is not None:
        secs = session.regime_ends_in_sec
        if (
            has_pos
            and session.regime is Regime.RTH
            and secs is not None
            and secs <= REGIME_CLOSING_SEC
        ):
            out.append(
                Alert(
                    "REGIME_CLOSING",
                    "info",
                    f"본장 마감 {secs // 60}분 전",
                    "마감 후에는 본주가 멈춰 perp 단독 가격이 됩니다",
                )
            )
        for w in session.warnings:
            if "주말" in w:
                out.append(Alert("REGIME_CLOSING", "info", w))
                break

    # ── 6. 변동성 이상 ──────────────────────────────────────
    if market is not None and market.atr_percentile is not None:
        if market.atr_percentile >= VOL_SPIKE_PERCENTILE:
            atr = f"{market.atr_pct:.2f}%" if market.atr_pct is not None else "-"
            out.append(
                Alert(
                    "VOL_SPIKE",
                    "warning",
                    "변동성이 평소보다 큽니다 — 정상 손절폭이 노이즈에 걸립니다",
                    f"ATR(1m) {atr} · 상위 {100 - market.atr_percentile:.0f}%",
                )
            )

    # ── 7. 청산 캐스케이드 ──────────────────────────────────
    if market is not None and market.liquidation_burst_ratio is not None:
        if market.liquidation_burst_ratio >= CASCADE_RATIO:
            out.append(
                Alert(
                    "LIQUIDATION_CASCADE",
                    "warning",
                    "강제청산이 몰리고 있습니다",
                    f"최근 5분 청산 규모가 평소의 {market.liquidation_burst_ratio:.1f}배",
                )
            )

    out.sort(key=lambda a: LEVEL_ORDER.get(a.level, 9))
    return out


def worst_level(alerts: list[Alert]) -> str | None:
    if not alerts:
        return None
    return min(alerts, key=lambda a: LEVEL_ORDER.get(a.level, 9)).level
