"""신호 카드 렌더링.

**"왜 지금은 아닌가"를 신호만큼 크게 보여준다.** 이 앱의 주된 출력은 진입 권유가
아니라 판단 근거이기 때문이다 (01 문서 7절).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..strategy.engine import Analysis, Candidate

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

BAND_STYLE = {"강함": "bold green", "보통": "green", "약함": "yellow", "미달": "dim"}


def header(a: Analysis) -> RenderableType:
    t = Text()
    t.append(f"{a.symbol}  ", style="bold")
    t.append(
        f"{a.at.astimezone(KST):%m-%d %H:%M} KST ({a.at.astimezone(NY):%H:%M} ET)",
        style="dim",
    )
    if a.session:
        t.append(f"   {a.session.regime.emoji} {a.session.regime.label_ko}")
        if a.features:
            t.append(f"   {a.features.price:,.4g}", style="bold")
    return t


def signal_panel(c: Candidate) -> RenderableType:
    r, s, p = c.result, c.score, c.plan
    long = r.direction == "long"
    color = "green" if long else "red"

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()

    t.add_row("방향", Text("롱" if long else "숏", style=f"bold {color}"))
    t.add_row("셋업", f"{r.setup_id}" + (f"  ({r.note})" if r.note else ""))
    t.add_row(
        "신뢰도",
        Text(
            f"{s.final}점 · {s.band}"
            + (f"  (기본 {s.base}" + (f" {s.session_adj:+d} 세션" if s.session_adj else "")
               + (f" {s.other_adj:+d}" if s.other_adj else "") + ")"
               if s.final != s.base else ""),
            style=BAND_STYLE.get(s.band, "white"),
        ),
    )

    if p is not None:
        t.add_row("진입", f"{p.entry.low:,.4g} ~ {p.entry.high:,.4g}   (기준 {p.entry_ref:,.4g})")
        t.add_row("손절", Text(f"{p.stop_loss:,.4g}   −{p.risk_pct:.2f}%", style="red"))
        for i, tp in enumerate(p.take_profit, start=1):
            src = "" if tp.source == "r_multiple" else "  ← 구조적 저항"
            t.add_row(
                f"익절 {i}",
                Text(
                    f"{tp.level:,.4g}   +{tp.pct:.2f}%  ({tp.r_multiple:.1f}R, "
                    f"{tp.close_pct}% 청산){src}",
                    style="green",
                ),
            )
        t.add_row("손익비", f"{p.risk_reward:.2f}")
        if p.position:
            pos = p.position
            t.add_row(
                "수량",
                f"{pos.qty:g}  ·  명목 {pos.notional:,.0f} USDT  ·  "
                f"리스크 {pos.risk_usdt:,.2f} USDT  ·  증거금 {pos.margin_usdt:,.0f}",
            )
        if p.funding_cost_usdt:
            t.add_row("펀딩 예상", f"{p.funding_cost_usdt:,.2f} USDT")

    return Panel(t, title="신호", border_style=color, padding=(0, 1))


def _conditions_table(conditions) -> RenderableType:
    t = Table(title="근거", show_lines=False)
    for col in ("", "조건", "가중치", "내용"):
        t.add_column(col)
    for cond in conditions:
        mark = "[green]✓[/green]" if cond.passed else "[red]✗[/red]"
        weight = "필수" if cond.required else (f"{cond.weight}" if cond.weight else "")
        style = "" if cond.passed else "dim"
        t.add_row(mark, Text(cond.label, style=style), weight, Text(cond.detail, style=style))
    return t


def evidence_table(c: Candidate) -> RenderableType:
    return _conditions_table(c.result.conditions)


def why_not_panel(a: Analysis) -> RenderableType:
    reasons = a.why_not_ko()
    body = [Text(f"· {r}", style="yellow") for r in reasons] or [
        Text("사유를 특정하지 못했습니다", style="dim")
    ]
    return Panel(
        Group(*body),
        title="지금은 신호가 없는 이유",
        border_style="yellow",
        padding=(0, 1),
    )


def filters_line(a: Analysis) -> RenderableType:
    parts = []
    for f in a.filters:
        mark = "✓" if f.passed else "✗"
        color = "green" if f.passed else "red"
        parts.append(f"[{color}]{mark}[/{color}] {f.reason_ko}")
    return Text.from_markup("   ".join(parts), style="dim") if parts else Text("")


def render_analysis(a: Analysis, show_all: bool = False) -> RenderableType:
    blocks: list[RenderableType] = [header(a)]

    if a.session and a.session.warnings:
        blocks.append(
            Panel(
                Group(*[Text(f"· {w}", style="yellow") for w in a.session.warnings[:4]]),
                title="주의",
                border_style="yellow",
                padding=(0, 1),
            )
        )

    if a.blocked:
        blocks.append(why_not_panel(a))
        return Group(*blocks)

    blocks.append(filters_line(a))

    if a.signal is not None:
        blocks.append(signal_panel(a.signal))
        blocks.append(evidence_table(a.signal))
    else:
        blocks.append(why_not_panel(a))
        if show_all and a.candidates:
            best = a.candidates[0]
            blocks.append(
                Text(
                    f"\n가장 근접했던 셋업: {best.result.setup_id} "
                    f"({best.result.direction}, {best.score.final}점)",
                    style="dim",
                )
            )
            blocks.append(evidence_table(best))
        elif show_all and a.near_misses:
            r = a.near_misses[0]
            blocks.append(
                Text(f"\n필수 조건에서 걸린 셋업: {r.setup_id} ({r.direction})", style="dim")
            )
            blocks.append(_conditions_table(r.conditions))

    for n in a.notes:
        blocks.append(Text(f"· {n}", style="cyan"))

    return Group(*blocks)
