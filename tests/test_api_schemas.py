"""WebSocket 스키마 (v0.3) — **빈 목록이 초록불로 읽히면 안 된다.**

이 파일의 절반은 한 가지를 막으려는 것이다. "지금 매매하면 안 되는 이유"가
비어 있을 때 사람은 그걸 매수 허가로 읽는다. Phase 4 판정으로 방향 제시를 뺐는데,
빈 패널이 그 자리를 대신 채우면 아무것도 뺀 게 아니다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from th.account.alerts import Alert, MarketStats
from th.account.models import Balance, DailyPnl, Position, RiskSnapshot
from th.account.risk import build_snapshot as build_risk_snapshot
from th.account.risk import kst_today
from th.api.messages import (
    build_alerts,
    build_no_trade,
    build_risk,
    build_session,
    build_snapshot,
    classify_reason,
)
from th.api.schemas import (
    ALERT_CODES,
    FORBIDDEN_TYPES,
    NO_PERMISSION_NOTE,
    OUTBOUND_TYPES,
    NoTradeMessage,
    NoTradeReason,
)
from th.sessions.models import Block, Event, Regime, SessionContext

T0 = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)


@dataclass
class View:
    risk: RiskSnapshot
    alerts: list[Alert]
    market: MarketStats | None = None
    session: SessionContext | None = None


def position(mark=100.0, liq=91.0) -> Position:
    return Position(symbol="SOXLUSDT", side="LONG", qty=50.0, entry_price=mark,
                    mark_price=mark, liquidation_price=liq, unrealized_pnl=0.0,
                    leverage=10, margin_type="isolated", isolated_margin=500.0,
                    notional=5000.0, updated_at=T0)


def view(pos=None, alerts=(), market=None, session=None, daily=None) -> View:
    snap = build_risk_snapshot(
        symbol="SOXLUSDT", position=pos,
        balance=Balance(wallet=10_000.0, available=9_000.0,
                        unrealized_pnl=0.0, margin_used=1_000.0),
        orders=[], daily=daily, underlying="SOXL", at=T0,
    )
    return View(risk=snap, alerts=list(alerts), market=market, session=session)


def session_ctx(blocks=(), warnings=(), event=None) -> SessionContext:
    return SessionContext(
        symbol="SOXLUSDT", at=T0, regime=Regime.RTH,
        regime_ends_at=T0 + timedelta(hours=2), is_trading_day=True,
        early_close=False, blocked=bool(blocks), blocks=tuple(blocks),
        warnings=tuple(warnings), next_event=event,
    )


# ─────────────────────────────────────────────────────────
# 빈 목록이 허가로 읽히지 않게
# ─────────────────────────────────────────────────────────

def test_empty_reasons_is_never_permission():
    m = NoTradeMessage(symbol="SOXLUSDT", ts=T0)
    assert m.reasons == []
    assert m.is_permission_to_trade is False
    assert m.blocked is False


def test_empty_reasons_always_carries_the_disclaimer():
    m = NoTradeMessage(symbol="SOXLUSDT", ts=T0)
    assert m.note_ko == NO_PERMISSION_NOTE
    assert "매매해도 된다는 뜻이 아닙니다" in m.note_ko


def test_permission_flag_cannot_be_set_to_true():
    """타입 수준에서 막는다. 서버 코드가 실수로도 True 를 못 넣는다."""
    with pytest.raises(ValidationError):
        NoTradeMessage(symbol="SOXLUSDT", ts=T0, is_permission_to_trade=True)


def test_permission_flag_survives_serialisation():
    m = NoTradeMessage(symbol="SOXLUSDT", ts=T0)
    payload = json.loads(m.model_dump_json())
    assert payload["is_permission_to_trade"] is False
    assert payload["note_ko"]


def test_reasons_present_means_blocked():
    m = NoTradeMessage(
        symbol="SOXLUSDT", ts=T0,
        reasons=[NoTradeReason(source="session", code="opening_15m",
                               text_ko="개장 직후 15분")],
    )
    assert m.blocked
    assert m.is_permission_to_trade is False       # 있어도 없어도 항상 False


# ─────────────────────────────────────────────────────────
# 서버가 방향을 제시하지 않는다
# ─────────────────────────────────────────────────────────

def test_signal_and_briefing_are_not_outbound_types():
    assert "signal" not in OUTBOUND_TYPES
    assert "briefing" not in OUTBOUND_TYPES
    assert FORBIDDEN_TYPES == {"signal", "briefing"}


def test_outbound_and_forbidden_do_not_overlap():
    assert not (OUTBOUND_TYPES & FORBIDDEN_TYPES)


def test_no_schema_class_emits_a_forbidden_type():
    """v0.3 스키마에 방향을 담는 메시지가 남아 있으면 안 된다."""
    import th.api.schemas as sc

    for name in dir(sc):
        obj = getattr(sc, name)
        default = getattr(getattr(obj, "model_fields", {}).get("type", None), "default", None)
        assert default not in FORBIDDEN_TYPES, f"{name} 이 {default} 을 냅니다"


def test_new_stop_alert_codes_are_declared():
    assert "STOP_BEYOND_LIQUIDATION" in ALERT_CODES
    assert "STOP_ALREADY_PASSED" in ALERT_CODES


# ─────────────────────────────────────────────────────────
# 터미널과 웹이 같은 답을 내는가
# ─────────────────────────────────────────────────────────

def test_no_trade_uses_the_same_rules_as_the_terminal():
    """규칙을 두 군데 적으면 언젠가 갈라진다."""
    from th.ui.dashboard import no_trade_reasons

    v = view(session=session_ctx(blocks=[Block("opening_15m", "개장 직후 15분")]),
             market=MarketStats(spread_bps=20.0))
    terminal = no_trade_reasons(v)
    web = [r.text_ko for r in build_no_trade(v, T0).reasons]
    assert terminal == web


def test_reason_sources_are_classified():
    assert classify_reason("호가 스프레드 13.0bp") == ("market", "spread")
    assert classify_reason("변동성이 최근 200봉 중 상위 2%") == ("market", "volatility")
    assert classify_reason("오늘 손실 한도에 닿았습니다 (-3.1R)")[0] == "account"
    assert classify_reason("CPI까지 20분")[0] == "event"
    assert classify_reason("개장 직후 15분")[0] == "session"


def test_risk_message_carries_the_underlying_conversion():
    """이 앱의 대표 숫자 — SOXL 9% = SOXX 3%."""
    m = build_risk(view(pos=position(mark=100.0, liq=91.0)), T0)
    assert m.has_position
    assert m.liq_distance_pct == pytest.approx(9.0)
    assert m.underlying_multiplier == pytest.approx(3.0)
    assert m.underlying_move_to_liq_pct == pytest.approx(3.0)


def test_risk_message_without_position_is_quiet():
    m = build_risk(view(), T0)
    assert not m.has_position and m.side is None and m.liq_distance_pct is None


def test_daily_lock_reaches_the_client():
    locked = DailyPnl(date_kst=kst_today(T0), realized_pnl=-200.0,
                      trade_count=6, r_unit=50.0)
    m = build_risk(view(daily=locked), T0)
    assert m.daily_locked and m.daily_r < -3


def test_session_message_is_none_without_a_session():
    assert build_session(view(), T0) is None


def test_session_message_carries_event_countdown():
    ev = Event(name="CPI", ts_utc=T0 + timedelta(minutes=20), kind="econ",
               importance="high")
    m = build_session(view(session=session_ctx(event=ev)), T0)
    assert m.regime == "RTH" and m.next_event_name == "CPI"
    assert m.next_event_in_sec == pytest.approx(1200, abs=2)


def test_alerts_are_carried_through_with_codes():
    v = view(pos=position(), alerts=[
        Alert("NO_STOP_ORDER", "critical", "손절 주문이 없습니다", "롱 50계약"),
    ])
    msgs = build_alerts(v, T0)
    assert [m.code for m in msgs] == ["NO_STOP_ORDER"]
    assert msgs[0].level == "critical"


def test_snapshot_contains_every_block_and_no_signal():
    v = view(pos=position(), session=session_ctx(), market=MarketStats(spread_bps=1.0))
    snap = build_snapshot(v, T0)
    payload = json.loads(snap.model_dump_json())
    assert payload["type"] == "snapshot"
    assert {"risk", "session", "alerts", "no_trade"} <= set(payload)
    assert "signal" not in payload and "briefing" not in payload
    assert payload["no_trade"]["is_permission_to_trade"] is False


def test_snapshot_survives_missing_session_and_market():
    snap = build_snapshot(view(), T0)
    assert snap.session is None
    assert snap.no_trade is not None and snap.no_trade.reasons == []


def test_api_package_stays_free_of_server_dependencies():
    """`th.api` 임포트만으로 fastapi·asyncpg 가 딸려오면 안 된다."""
    import subprocess
    import sys

    code = (
        "import sys, th.api;"
        "print(','.join(m for m in ('fastapi','uvicorn','asyncpg','httpx') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip() == "", f"딸려온 모듈: {out.stdout.strip()}"
