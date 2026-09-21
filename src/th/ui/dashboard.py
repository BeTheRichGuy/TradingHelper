"""터미널 실시간 대시보드 렌더링.

화면 순서가 곧 우선순위다. **경고가 포지션보다 위에, 포지션이 시세보다 위에** 온다.
10배 레버리지에서 먼저 보아야 하는 것은 수익이 아니라 "지금 위험한가"이기 때문이다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from rich.align import Align
from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..account.alerts import VOL_SPIKE_PERCENTILE, Alert
from ..account.risk import DAILY_LOSS_LIMIT_R, LIQ_CRITICAL_PCT, LIQ_WARNING_PCT
from ..sessions.models import Regime
from ..strategy.engine import MAX_SPREAD_BPS

if TYPE_CHECKING:                       # 화면 코드가 httpx·tenacity 를 끌고 오면 안 된다
    from ..account.service import RiskView

# 화면이 쓰는 임계값은 **전략 엔진과 같은 상수**를 그대로 가져온다.
# 여기서 숫자를 따로 적으면 화면과 엔진이 다른 말을 하게 된다.
MAX_ATR_PERCENTILE = VOL_SPIKE_PERCENTILE
DAILY_LIMIT_R = DAILY_LOSS_LIMIT_R

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

REGIME_COLOR = {
    Regime.PRE: "yellow",
    Regime.RTH: "green",
    Regime.POST: "dark_orange",
    Regime.CLOSED: "red",
}


def _dur(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    m = max(0, seconds) // 60
    if m >= 60:
        return f"{m // 60}시간 {m % 60}분"
    return f"{m}분"


def _pct(v: float | None, digits: int = 2, sign: bool = False) -> str:
    if v is None:
        return "-"
    fmt = f"{{:{'+' if sign else ''}.{digits}f}}%"
    return fmt.format(v)


# ─────────────────────────────────────────────────────────
# 블록
# ─────────────────────────────────────────────────────────

def session_banner(view: RiskView) -> RenderableType:
    ctx = view.session
    now = view.risk.at
    if ctx is None:
        return Panel(
            Text(f"{now.astimezone(KST):%m-%d %H:%M:%S} KST", style="dim"),
            border_style="grey37",
        )

    color = REGIME_COLOR.get(ctx.regime, "white")
    line = Text()
    line.append(f"{ctx.regime.emoji} {ctx.regime.label_ko}", style=f"bold {color}")
    line.append(f"   남은시간 {_dur(ctx.regime_ends_in_sec)}", style="white")
    line.append(
        f"   {now.astimezone(KST):%H:%M:%S} KST / {now.astimezone(NY):%H:%M} ET",
        style="dim",
    )
    if ctx.blocked:
        line.append("   신호 차단", style="bold red")

    body = [line]
    if ctx.next_event:
        ev = ctx.next_event
        body.append(
            Text(
                f"다음 이벤트  {ev.name} · {_dur(ctx.next_event_in_sec)} 뒤 "
                f"({ev.ts_utc.astimezone(KST):%m-%d %H:%M} KST)",
                style="cyan" if ev.importance == "high" else "dim",
            )
        )
    for w in ctx.warnings[:3]:
        body.append(Text(f"· {w}", style="yellow"))

    return Panel(Group(*body), border_style=color, padding=(0, 1))


def alerts_panel(alerts: list[Alert]) -> RenderableType | None:
    if not alerts:
        return Panel(
            Text("경고 없음", style="green"), title="경고", border_style="green",
            padding=(0, 1),
        )
    style = {"critical": "red", "warning": "yellow", "info": "cyan"}
    lines = []
    for a in alerts:
        t = Text()
        t.append(f"{a.icon} ", style=style[a.level])
        t.append(a.message_ko, style=f"bold {style[a.level]}")
        if a.detail_ko:
            t.append(f"\n   {a.detail_ko}", style="dim")
        lines.append(t)
    worst = alerts[0].level
    return Panel(
        Group(*lines), title=f"경고 {len(alerts)}건", border_style=style[worst],
        padding=(0, 1),
    )


# ─────────────────────────────────────────────────────────
# v0.3 — 신호 카드가 있던 자리
#
# Phase 4 에서 셋업에 방향성 우위가 없다는 것이 확인돼 신호 카드를 뺐다.
# 그 자리를 "지금 매매하면 안 되는 이유"가 대신한다. 이 앱의 주된 출력은
# 원래부터 진입 권유가 아니라 판단 근거였다 (01 문서 7절).
# ─────────────────────────────────────────────────────────

def no_trade_reasons(view: RiskView) -> list[str]:
    """지금 손대면 안 되는 이유를 모은다. 없으면 빈 목록.

    **비어 있다고 '사도 된다'는 뜻이 아니다.** 막을 이유를 못 찾았다는 뜻일 뿐이고,
    화면에도 그렇게 쓴다.
    """
    out: list[str] = []

    s = view.session
    if s is not None:
        out.extend(b.reason_ko for b in s.blocks)
        # 게이트가 이미 'underlying_closed' 로 같은 말을 했으면 두 번 적지 않는다
        if s.regime.value == "CLOSED" and not any(
            b.rule_id == "underlying_closed" for b in s.blocks
        ):
            out.append("본주 거래가 멈춘 시간대 — perp 단독 가격이라 기술적 분석이 무의미합니다")
        ev = s.next_event
        if ev is not None and ev.importance == "high":
            secs = s.next_event_in_sec
            if secs is not None and 0 <= secs <= 60 * 60:
                out.append(f"{ev.name}까지 {secs // 60}분 — 발표 순간엔 손절가를 건너뜁니다")
        out.extend(s.warnings)

    m = view.market
    if m is not None:
        if m.spread_bps is not None and m.spread_bps > MAX_SPREAD_BPS:
            out.append(
                f"호가 스프레드 {m.spread_bps:.1f}bp — 10배에서 진입·청산 비용이 손절폭을 잠식합니다"
            )
        if m.atr_percentile is not None and m.atr_percentile > MAX_ATR_PERCENTILE:
            out.append(
                f"변동성이 최근 200봉 중 상위 {100 - m.atr_percentile:.0f}% — "
                "정상 손절폭이 노이즈에 걸립니다"
            )

    d = view.risk.daily
    if d is not None and d.locked(DAILY_LIMIT_R):
        out.append(f"오늘 손실 한도에 닿았습니다 ({d.r_value:.1f}R) — 오늘은 종료하세요")

    return out


def no_trade_panel(view: RiskView) -> RenderableType:
    reasons = no_trade_reasons(view)
    if not reasons:
        body = Group(
            Text("지금 막을 이유는 찾지 못했습니다", style="green"),
            Text(
                "매매해도 된다는 뜻이 아닙니다. 방향 판단은 이 앱이 하지 않습니다.",
                style="dim",
            ),
        )
        border = "green"
    else:
        body = Group(*[Text(f"· {r}", style="yellow") for r in reasons[:6]])
        border = "yellow"
    return Panel(body, title="지금 매매하면 안 되는 이유", border_style=border, padding=(0, 1))


def critical_banner(alerts: list[Alert]) -> RenderableType | None:
    """치명적 경고만 **세션 배너보다 위에** 띄운다.

    01 문서 원칙 3: 청산 경고가 수익 신호보다 위에 있다. 화면 배치도 그렇게 한다.
    """
    crit = [a for a in alerts if a.level == "critical"]
    if not crit:
        return None
    lines = []
    for a in crit:
        t = Text()
        t.append(f"{a.icon} {a.message_ko}", style="bold white on red")
        if a.detail_ko:
            t.append(f"  {a.detail_ko}", style="red")
        lines.append(t)
    return Group(*lines)


def position_panel(view: RiskView) -> RenderableType:
    r = view.risk
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()

    if r.errors:
        for e in r.errors:
            t.add_row("", Text(e, style="red"))
        return Panel(t, title="포지션", border_style="red", padding=(0, 1))

    p = r.position
    if p is None or not p.is_open:
        t.add_row("포지션", Text("없음", style="dim"))
        t.add_row("지갑잔고", f"{r.balance.wallet:,.2f} USDT")
        t.add_row("사용가능", f"{r.balance.available:,.2f} USDT")
        return Panel(t, title="포지션", border_style="grey37", padding=(0, 1))

    side_style = "green" if p.side == "LONG" else "red"
    t.add_row(
        "방향",
        Text(f"{'롱' if p.side == 'LONG' else '숏'}  {p.qty:g}계약", style=f"bold {side_style}"),
    )
    t.add_row("진입 / 현재", f"{p.entry_price:,.4g}  →  {p.mark_price:,.4g}")

    move = p.price_move_pct
    pnl_style = "green" if p.unrealized_pnl >= 0 else "red"
    on_margin = p.unrealized_pct_on_margin
    t.add_row(
        "미실현",
        Text(
            f"{p.unrealized_pnl:+,.2f} USDT"
            + (f"  ({on_margin:+.1f}% 증거금)" if on_margin is not None else "")
            + (f"  · 본주 {move:+.2f}%" if move is not None else ""),
            style=pnl_style,
        ),
    )
    t.add_row("명목가", f"{p.notional:,.0f} USDT  ·  {p.leverage}배 {p.margin_type}")
    t.add_row("지갑잔고", f"{r.balance.wallet:,.2f} USDT")
    return Panel(t, title="포지션", border_style=side_style, padding=(0, 1))


def risk_panel(view: RiskView) -> RenderableType:
    r = view.risk
    m = view.market                     # DB 조회가 실패하면 None 일 수 있다
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()

    if not r.has_position:
        t.add_row("청산가 거리", Text("포지션 없음", style="dim"))
        if m is not None and m.atr_pct is not None:
            t.add_row("ATR(1m)", _pct(m.atr_pct))
        if m is not None and m.spread_bps is not None:
            t.add_row("스프레드", f"{m.spread_bps:.2f} bp")
        return Panel(t, title="리스크", border_style="grey37", padding=(0, 1))

    d = r.liq_distance_pct
    liq_style = (
        "bold red" if d is not None and d < LIQ_CRITICAL_PCT
        else "yellow" if d is not None and d < LIQ_WARNING_PCT
        else "green"
    )
    liq_txt = _pct(d)
    if r.position.liquidation_price:
        liq_txt += f"  (청산가 {r.position.liquidation_price:,.4g})"
    t.add_row("청산가 거리", Text(liq_txt, style=liq_style))

    if r.underlying_move_to_liq_pct is not None and abs(r.underlying_multiplier) > 1:
        t.add_row(
            "기초지수 기준",
            Text(
                f"{r.underlying_move_to_liq_pct:.2f}% 움직이면 청산",
                style=liq_style,
            ),
        )

    if r.has_stop:
        t.add_row(
            "손절가 거리",
            Text(
                f"{_pct(r.stop_distance_pct)}  ({len(r.stop_orders)}건)", style="green"
            ),
        )
    else:
        t.add_row("손절 주문", Text("없음 ⚠", style="bold red"))

    lev = f"{r.account_leverage:.2f}배 (계좌 대비)"
    if r.underlying_leverage and abs(r.underlying_multiplier) > 1:
        lev += f"  ·  기초지수 {r.underlying_leverage:.2f}배"
    t.add_row("실효 레버리지", lev)

    m = view.market
    extras = []
    if m.atr_pct is not None:
        pctl = f" ({m.atr_percentile:.0f}%)" if m.atr_percentile is not None else ""
        extras.append(f"ATR {m.atr_pct:.2f}%{pctl}")
    if m.spread_bps is not None:
        extras.append(f"스프레드 {m.spread_bps:.2f}bp")
    if extras:
        t.add_row("시장", "  ·  ".join(extras))

    return Panel(t, title="리스크", border_style=liq_style.split()[-1], padding=(0, 1))


def daily_bar(view: RiskView) -> RenderableType:
    d = view.risk.daily
    if d is None:
        return Panel(
            Text("당일 손익: API 키가 없어 조회하지 않음", style="dim"),
            border_style="grey37", padding=(0, 1),
        )

    locked = d.locked()
    style = "bold red" if locked else ("red" if d.r_value < 0 else "green")
    t = Text()
    t.append("오늘  ", style="dim")
    t.append(f"{d.r_value:+.2f}R", style=style)
    t.append(f"  ({d.net:+,.2f} USDT · {d.trade_count}회)", style="dim")
    t.append(f"   한도 −3.00R · 1R = {d.r_unit:,.2f} USDT", style="dim")
    if locked:
        t.append("   오늘 종료", style="bold white on red")
    return Panel(t, border_style="red" if locked else "grey37", padding=(0, 1))


def render(view: RiskView) -> RenderableType:
    """01 문서 v0.3 배치 — 치명적 경고 → 세션 → 경고 → 포지션·리스크 → 사유 → 일일."""
    blocks: list[RenderableType] = []

    banner = critical_banner(view.alerts)
    if banner is not None:
        blocks.append(banner)

    blocks.append(session_banner(view))
    a = alerts_panel(view.alerts)
    if a is not None:
        blocks.append(a)
    blocks.append(
        Columns([position_panel(view), risk_panel(view)], expand=True, equal=True)
    )
    blocks.append(no_trade_panel(view))
    blocks.append(daily_bar(view))
    return Group(*blocks)


def footer(last_update: datetime, interval: int) -> RenderableType:
    return Align.right(
        Text(
            f"갱신 {last_update.astimezone(KST):%H:%M:%S} · {interval}초마다 · Ctrl+C 종료",
            style="dim",
        )
    )
