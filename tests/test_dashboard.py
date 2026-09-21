"""화면(v0.3) — 신호 카드가 빠진 자리를 무엇이 채우는가.

Phase 4 판정으로 방향 제시가 사라졌다. 남은 화면이 하는 일은 두 가지다.
  1. 치명적 경고를 **세션 배너보다 위에** 띄운다 (01 문서 원칙 3)
  2. "지금 매매하면 안 되는 이유"를 빠짐없이 모은다

두 번째가 조용히 비면 사용자는 '막을 게 없구나'로 읽는다. 그래서 각 사유가
실제로 잡히는지 하나씩 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from th.account.alerts import Alert, MarketStats
from th.account.models import Balance, DailyPnl, Position, RiskSnapshot
from th.account.risk import build_snapshot, kst_today
from th.sessions.models import Block, Event, Regime, SessionContext
from th.ui.dashboard import (
    DAILY_LIMIT_R,
    MAX_ATR_PERCENTILE,
    MAX_SPREAD_BPS,
    critical_banner,
    no_trade_panel,
    no_trade_reasons,
    render,
)

T0 = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)


@dataclass
class View:
    """RiskView 와 같은 모양. 화면은 이 세 가지만 본다."""

    risk: RiskSnapshot
    alerts: list[Alert]
    market: MarketStats | None = None
    session: SessionContext | None = None


def position(mark=100.0, liq=91.0) -> Position:
    return Position(symbol="SOXLUSDT", side="LONG", qty=50.0, entry_price=mark,
                    mark_price=mark, liquidation_price=liq, unrealized_pnl=0.0,
                    leverage=10, margin_type="isolated", isolated_margin=500.0,
                    notional=50.0 * mark, updated_at=T0)


def snapshot(pos=None, daily=None) -> RiskSnapshot:
    return build_snapshot(
        symbol="SOXLUSDT", position=pos,
        balance=Balance(wallet=10_000.0, available=9_000.0,
                        unrealized_pnl=0.0, margin_used=1_000.0),
        orders=[], daily=daily, underlying="SOXL", at=T0,
    )


def session(regime=Regime.RTH, blocks=(), warnings=(), event=None,
            event_in_sec=None) -> SessionContext:
    ends = T0 + timedelta(hours=2)
    ctx = SessionContext(
        symbol="SOXLUSDT", at=T0, regime=regime, regime_ends_at=ends,
        is_trading_day=True, early_close=False, blocked=bool(blocks),
        blocks=tuple(blocks), warnings=tuple(warnings), next_event=event,
    )
    return ctx


def view(**kw) -> View:
    kw.setdefault("risk", snapshot())
    kw.setdefault("alerts", [])
    return View(**kw)


# ─────────────────────────────────────────────────────────
# 치명적 경고는 맨 위
# ─────────────────────────────────────────────────────────

def test_critical_alert_gets_its_own_banner():
    banner = critical_banner([Alert("NO_STOP_ORDER", "critical", "손절 주문이 없습니다", "")])
    assert banner is not None


def test_warning_level_does_not_get_the_top_banner():
    """노란 경고까지 최상단에 띄우면 빨간 것이 묻힌다."""
    assert critical_banner([Alert("LIQ_PROXIMITY", "warning", "가깝습니다", "")]) is None
    assert critical_banner([]) is None


def test_render_puts_the_critical_banner_before_everything():
    v = view(alerts=[Alert("NO_STOP_ORDER", "critical", "손절 주문이 없습니다", "")])
    blocks = render(v).renderables
    assert critical_banner(v.alerts) is not None
    assert len(blocks) == 6           # 배너 + 세션 + 경고 + 본문 + 사유 + 일일


def test_render_without_critical_has_one_block_fewer():
    assert len(render(view()).renderables) == 5


# ─────────────────────────────────────────────────────────
# "지금 매매하면 안 되는 이유" — 빠짐없이 모으는가
# ─────────────────────────────────────────────────────────

def test_session_blocks_become_reasons():
    v = view(session=session(blocks=[Block("opening_15m", "개장 직후 15분")]))
    assert "개장 직후 15분" in no_trade_reasons(v)


def test_closed_regime_is_explained_even_without_a_block():
    """본주가 멈춘 시간대는 규칙이 없어도 이유가 된다."""
    reasons = no_trade_reasons(view(session=session(regime=Regime.CLOSED)))
    assert any("본주 거래가 멈춘" in r for r in reasons)


def test_wide_spread_is_a_reason():
    v = view(market=MarketStats(spread_bps=MAX_SPREAD_BPS + 5))
    assert any("스프레드" in r for r in no_trade_reasons(v))


def test_normal_spread_is_not_a_reason():
    v = view(market=MarketStats(spread_bps=MAX_SPREAD_BPS - 1))
    assert not any("스프레드" in r for r in no_trade_reasons(v))


def test_volatility_spike_is_a_reason():
    v = view(market=MarketStats(atr_percentile=MAX_ATR_PERCENTILE + 3))
    assert any("변동성" in r for r in no_trade_reasons(v))


def test_daily_lock_is_a_reason():
    locked = DailyPnl(date_kst=kst_today(T0), realized_pnl=-200.0,
                      trade_count=6, r_unit=50.0)
    assert locked.locked(DAILY_LIMIT_R)
    v = view(risk=snapshot(daily=locked))
    assert any("손실 한도" in r for r in no_trade_reasons(v))


def test_imminent_high_importance_event_is_a_reason():
    ev = Event(name="CPI", ts_utc=T0 + timedelta(minutes=20), kind="econ",
               importance="high")
    v = view(session=session(event=ev))
    assert any("CPI" in r for r in no_trade_reasons(v))


def test_far_event_is_not_a_reason():
    ev = Event(name="CPI", ts_utc=T0 + timedelta(hours=9), kind="econ",
               importance="high")
    assert not any("CPI" in r for r in no_trade_reasons(view(session=session(event=ev))))


def test_low_importance_event_is_not_a_reason():
    ev = Event(name="사소한 지표", ts_utc=T0 + timedelta(minutes=20), kind="econ",
               importance="low")
    assert no_trade_reasons(view(session=session(event=ev))) == []


def test_session_warnings_are_carried_through():
    v = view(session=session(warnings=("이벤트 정보가 오래됐습니다",)))
    assert "이벤트 정보가 오래됐습니다" in no_trade_reasons(v)


def test_reasons_accumulate_rather_than_replace():
    v = view(
        session=session(blocks=[Block("opening_15m", "개장 직후 15분")]),
        market=MarketStats(spread_bps=MAX_SPREAD_BPS + 5,
                           atr_percentile=MAX_ATR_PERCENTILE + 3),
    )
    assert len(no_trade_reasons(v)) >= 3


def test_no_reasons_is_not_a_buy_signal():
    """비어 있을 때 화면이 '사도 된다'로 읽히면 안 된다."""
    v = view(session=session(), market=MarketStats(spread_bps=1.0, atr_percentile=50.0))
    assert no_trade_reasons(v) == []
    panel = no_trade_panel(v)
    text = " ".join(str(x) for x in panel.renderable.renderables)
    assert "매매해도 된다는 뜻이 아닙니다" in text


def test_panel_renders_with_no_session_or_market():
    """데이터가 없을 때 조용히 죽지 않는다."""
    assert no_trade_reasons(view()) == []
    assert no_trade_panel(view()) is not None


def test_thresholds_come_from_the_engine_not_a_copy():
    """화면이 숫자를 따로 적으면 엔진과 다른 말을 하게 된다."""
    from th.account.alerts import VOL_SPIKE_PERCENTILE
    from th.account.risk import DAILY_LOSS_LIMIT_R
    from th.strategy.engine import MAX_SPREAD_BPS as ENGINE_SPREAD

    assert MAX_SPREAD_BPS == ENGINE_SPREAD
    assert MAX_ATR_PERCENTILE == VOL_SPIKE_PERCENTILE
    assert DAILY_LIMIT_R == DAILY_LOSS_LIMIT_R


def test_dashboard_imports_without_network_libraries():
    """화면 코드가 httpx·tenacity 를 끌고 오면 안 된다 (반복해서 겪은 문제)."""
    import subprocess
    import sys

    code = (
        "import sys, th.ui.dashboard;"
        "print(','.join(m for m in ('httpx','tenacity','asyncpg') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip() == "", f"딸려온 모듈: {out.stdout.strip()}"


def test_closed_regime_reason_is_not_duplicated_when_gate_already_said_it():
    """게이트는 CLOSED 면 항상 underlying_closed 블록을 넣는다. 화면이 같은 말을
    한 줄 더 얹으면 사유 목록에 '본주 정지'가 두 번 뜬다."""
    blk = Block("underlying_closed", "미국 본주가 거래되지 않는 시간입니다 — 분석 신뢰 불가")
    reasons = no_trade_reasons(view(session=session(regime=Regime.CLOSED, blocks=[blk])))
    closed = [r for r in reasons if "본주" in r]
    assert len(closed) == 1, reasons
