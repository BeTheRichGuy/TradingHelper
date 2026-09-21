"""HTML 리포트 — 순수 함수. 외부 라이브러리도 CDN도 쓰지 않는다.

차트는 SVG를 직접 그린다. 파일 하나로 끝나야 나중에 열었을 때 깨지지 않는다.
"""

from __future__ import annotations

import html
from datetime import datetime

from .metrics import Gate, Stats, equity_curve, histogram
from .models import Trade

CSS = """
:root{--bg:#fbfaf9;--fg:#1c1a17;--dim:#6b6560;--line:#e3ded8;--card:#fff;
--ok:#0f7a4d;--bad:#b3341f;--accent:#3a5ba0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans KR',sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sub{color:var(--dim);margin:0 0 28px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.card .k{color:var(--dim);font-size:12px}
.card .v{font-size:20px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:8px 12px;text-align:right;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{background:#f4f1ed;font-weight:600;color:var(--dim);font-size:12px}
tr:last-child td{border-bottom:0}
.ok{color:var(--ok)}.bad{color:var(--bad)}.dim{color:var(--dim)}
.scroll{overflow-x:auto}
.note{background:#fff8e6;border:1px solid #f0e0b0;border-radius:8px;padding:12px 16px;margin:16px 0}
svg{background:var(--card);border:1px solid var(--line);border-radius:8px;max-width:100%}
@media (prefers-color-scheme:dark){:root{--bg:#16150f;--fg:#f0ece4;--dim:#a09a90;
--line:#33302a;--card:#1f1e17;--ok:#5fcf9a;--bad:#f08a72}
th{background:#26241d}.note{background:#2a2415;border-color:#4a4028}}
"""


def _e(v) -> str:
    return html.escape(str(v))


def _sign(v: float, digits: int = 2, suffix: str = "") -> str:
    cls = "ok" if v > 0 else ("bad" if v < 0 else "dim")
    return f'<span class="{cls}">{v:+.{digits}f}{suffix}</span>'


def line_chart(values: list[float], width: int = 1000, height: int = 240) -> str:
    if len(values) < 2:
        return '<p class="dim">그릴 만큼의 트레이드가 없습니다.</p>'
    pad = 32
    lo, hi = min(min(values), 0.0), max(max(values), 0.0)
    span = (hi - lo) or 1.0
    w, h = width - pad * 2, height - pad * 2

    def x(i: int) -> float:
        return pad + w * i / (len(values) - 1)

    def y(v: float) -> float:
        return pad + h * (hi - v) / span

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    zero = y(0.0)
    area = f"{pad},{zero} {pts} {x(len(values) - 1):.1f},{zero}"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="누적 손익 곡선">'
        f'<polygon points="{area}" fill="#3a5ba0" opacity="0.12"/>'
        f'<line x1="{pad}" y1="{zero:.1f}" x2="{width - pad}" y2="{zero:.1f}" '
        f'stroke="currentColor" stroke-opacity="0.25" stroke-dasharray="3 3"/>'
        f'<polyline points="{pts}" fill="none" stroke="#3a5ba0" stroke-width="2"/>'
        f'<text x="{pad}" y="{pad - 10}" font-size="11" fill="currentColor" '
        f'opacity="0.6">{hi:+.1f}R</text>'
        f'<text x="{pad}" y="{height - 8}" font-size="11" fill="currentColor" '
        f'opacity="0.6">{lo:+.1f}R</text></svg>'
    )


def bar_chart(bins: list[tuple[float, float, int]], width: int = 1000, height: int = 220) -> str:
    if not bins:
        return '<p class="dim">표본이 없습니다.</p>'
    pad = 32
    w, h = width - pad * 2, height - pad * 2
    top = max(c for _, _, c in bins) or 1
    bw = w / len(bins)
    out = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="R 분포">'
    ]
    for i, (lo, _hi, c) in enumerate(bins):
        bh = h * c / top
        colour = "#b3341f" if lo < 0 else "#0f7a4d"
        out.append(
            f'<rect x="{pad + i * bw + 1:.1f}" y="{pad + h - bh:.1f}" '
            f'width="{bw - 2:.1f}" height="{bh:.1f}" fill="{colour}" opacity="0.75"/>'
        )
    out.append(
        f'<text x="{pad}" y="{height - 8}" font-size="11" fill="currentColor" '
        f'opacity="0.6">{bins[0][0]:+.1f}R</text>'
        f'<text x="{width - pad}" y="{height - 8}" font-size="11" text-anchor="end" '
        f'fill="currentColor" opacity="0.6">{bins[-1][1]:+.1f}R</text></svg>'
    )
    return "".join(out)


def _stats_row(label: str, s: Stats) -> str:
    lo, hi = s.win_rate_ci
    return (
        f"<tr><td>{_e(label)}</td><td>{s.n}</td>"
        f"<td>{s.win_rate:.0%} <span class='dim'>({lo:.0%}~{hi:.0%})</span></td>"
        f"<td>{_sign(s.expectancy_r, 3, 'R')}</td>"
        f"<td>{_sign(s.total_r, 1, 'R')}</td>"
        f"<td>{s.max_drawdown_r:.1f}R</td>"
        f"<td>{s.profit_factor:.2f}</td></tr>"
        if s.profit_factor is not None
        else (
            f"<tr><td>{_e(label)}</td><td>{s.n}</td>"
            f"<td>{s.win_rate:.0%} <span class='dim'>({lo:.0%}~{hi:.0%})</span></td>"
            f"<td>{_sign(s.expectancy_r, 3, 'R')}</td>"
            f"<td>{_sign(s.total_r, 1, 'R')}</td>"
            f"<td>{s.max_drawdown_r:.1f}R</td><td class='dim'>—</td></tr>"
        )
    )


def _stats_table(rows: dict[str, Stats], first_col: str) -> str:
    head = (
        f"<thead><tr><th>{_e(first_col)}</th><th>표본</th><th>승률 (95% CI)</th>"
        "<th>기대값</th><th>누적</th><th>최대낙폭</th><th>PF</th></tr></thead>"
    )
    body = "".join(_stats_row(k, v) for k, v in rows.items())
    return f'<div class="scroll"><table>{head}<tbody>{body}</tbody></table></div>'


def build_report(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    generated_at: datetime,
    overall: Stats,
    in_sample: Stats,
    out_sample: Stats,
    gates: list[Gate],
    by_regime: dict[str, Stats],
    by_setup: dict[str, Stats],
    folds: list[tuple[int, Stats, Stats]],
    trades: list[Trade],
    purged: int,
    counters: dict[str, int],
    basis_coverage: float,
    skip_reasons: dict[str, int],
) -> str:
    passed = all(g.passed for g in gates)
    verdict = (
        '<span class="ok">통과</span>' if passed else '<span class="bad">불합격</span>'
    )

    cards = "".join(
        f'<div class="card"><div class="k">{_e(k)}</div><div class="v">{v}</div></div>'
        for k, v in [
            ("판정 (OOS 기준)", verdict),
            ("OOS 기대값", f"{out_sample.expectancy_r:+.3f}R"),
            ("OOS 표본", f"{out_sample.n}"),
            ("전체 트레이드", f"{overall.n}"),
            ("전체 누적", f"{overall.total_r:+.1f}R"),
            ("비용이 먹은 몫", f"{overall.cost_r:.1f}R"),
        ]
    )

    gate_rows = "".join(
        f"<tr><td>{_e(g.name)}</td>"
        f"<td>{'<span class=ok>✓</span>' if g.passed else '<span class=bad>✗</span>'}</td>"
        f"<td>{_e(g.detail)}</td></tr>"
        for g in gates
    )

    fold_rows = "".join(
        f"<tr><td>구간 {i}</td><td>{tr.n}</td><td>{_sign(tr.expectancy_r, 3, 'R')}</td>"
        f"<td>{te.n}</td><td>{_sign(te.expectancy_r, 3, 'R')}</td></tr>"
        for i, tr, te in folds
    )

    curve = equity_curve(trades)
    rs = [t.net_r for t in trades]
    bins = histogram(rs, -1.5, 3.5, 20) if rs else []

    q = overall.tp1_bars_quartiles
    tp1_line = (
        f"TP1 도달까지 중앙값 <b>{q[1]:.0f}분</b> (사분위 {q[0]:.0f}~{q[2]:.0f}분)"
        if q
        else "TP1 에 도달한 트레이드가 없습니다"
    )
    hq = overall.hold_minutes_quartiles
    hold_line = (
        f"보유시간 중앙값 <b>{hq[1]:.0f}분</b> (사분위 {hq[0]:.0f}~{hq[2]:.0f}분)"
        if hq
        else ""
    )

    reason_rows = "".join(
        f"<tr><td>{_e(k)}</td><td>{v}</td></tr>" for k, v in sorted(overall.exit_reasons.items())
    )
    skip_rows = "".join(
        f"<tr><td>{_e(k)}</td><td>{v}</td></tr>" for k, v in sorted(skip_reasons.items())
    )

    recent = trades[-150:]
    trade_rows = "".join(
        f"<tr><td>{t.entry_at:%Y-%m-%d %H:%M}</td><td>{_e(t.setup_id)}</td>"
        f"<td>{'롱' if t.direction == 'long' else '숏'}</td><td>{t.score}</td>"
        f"<td>{t.fill_price:,.4g}</td><td>{t.stop_loss:,.4g}</td>"
        f"<td>{_e(t.exit_reason)}</td><td>{t.hold_minutes}분</td>"
        f"<td>{_sign(t.net_r, 2, 'R')}</td></tr>"
        for t in recent
    )

    basis_note = (
        f'<div class="note"><b>basis 커버리지 {basis_coverage:.0%}</b> — '
        "본주(SOXL) 가격이 없는 구간에서는 S1-8·괴리 필터가 '데이터 없음 = 미충족'으로 "
        "처리됐습니다. 커버리지가 낮으면 이 백테스트는 basis 조건을 사실상 끄고 돌린 "
        "결과입니다. Alpaca 어댑터를 붙이면 이 값이 올라갑니다.</div>"
        if basis_coverage < 0.9
        else ""
    )

    counter_line = " · ".join(f"{_e(k)} {v:,}" for k, v in counters.items())

    return f"""<title>{_e(symbol)} 백테스트 리포트</title>
<style>{CSS}</style>
<div class="wrap">
<h1>{_e(symbol)} 백테스트</h1>
<p class="sub">{start:%Y-%m-%d} ~ {end:%Y-%m-%d} · 생성 {generated_at:%Y-%m-%d %H:%M} ·
{counter_line}</p>

<div class="cards">{cards}</div>

{basis_note}

<h2>통과 기준 — out-of-sample 성적으로만 판정</h2>
<div class="scroll"><table>
<thead><tr><th>항목</th><th></th><th>결과</th></tr></thead>
<tbody>{gate_rows}</tbody></table></div>
<p class="dim">in-sample 성적은 참고용입니다. 같은 데이터로 만들고 같은 데이터로
채점하면 언제나 좋아 보입니다.</p>

<h2>구간별 성적</h2>
{_stats_table({"전체": overall, "in-sample": in_sample, "out-of-sample": out_sample}, "구간")}
<p class="dim">경계에 물린 {purged}건은 양쪽 어디에도 넣지 않고 버렸습니다(purge).
진입~청산 구간이 경계를 넘으면 검증 구간의 가격이 학습 쪽으로 새어 들어갑니다.</p>

<h2>누적 손익 (R)</h2>
{line_chart(curve)}
<p class="dim">{tp1_line}{' · ' + hold_line if hold_line else ''}</p>

<h2>R 분포</h2>
{bar_chart(bins)}

<h2>세션 레짐별</h2>
{_stats_table(by_regime, "레짐")}

<h2>셋업별</h2>
{_stats_table(by_setup, "셋업")}

<h2>워크포워드</h2>
<div class="scroll"><table>
<thead><tr><th>구간</th><th>학습 표본</th><th>학습 기대값</th>
<th>검증 표본</th><th>검증 기대값</th></tr></thead>
<tbody>{fold_rows or '<tr><td colspan="5" class="dim">표본이 부족해 분할하지 않았습니다</td></tr>'}</tbody>
</table></div>

<h2>청산 사유 · 거래 무산 사유</h2>
<div class="cards">
<div class="scroll"><table><thead><tr><th>청산 사유</th><th>건수</th></tr></thead>
<tbody>{reason_rows or '<tr><td colspan="2" class="dim">없음</td></tr>'}</tbody></table></div>
<div class="scroll"><table><thead><tr><th>거래 무산</th><th>건수</th></tr></thead>
<tbody>{skip_rows or '<tr><td colspan="2" class="dim">없음</td></tr>'}</tbody></table></div>
</div>
<p class="dim"><b>no_fill</b> = 신호는 났지만 진입 트리거에 3봉 안에 안 닿음.
<b>overlap</b> = 앞 포지션 보유 중이라 건너뜀. 둘 다 실전에서 실제로 일어나는 일이라
성과에서 빼지 않고 따로 셉니다.</p>

<h2>최근 트레이드 {len(recent)}건</h2>
<div class="scroll"><table>
<thead><tr><th>진입</th><th>셋업</th><th>방향</th><th>점수</th><th>체결가</th>
<th>손절</th><th>청산</th><th>보유</th><th>손익</th></tr></thead>
<tbody>{trade_rows or '<tr><td colspan="9" class="dim">트레이드 없음</td></tr>'}</tbody>
</table></div>

<p class="dim" style="margin-top:40px">이 숫자는 과거에 대한 것입니다. 미래 수익을
보장하지 않으며, 통과했더라도 최소 4주는 신호를 보되 그대로 따라 하지 않는 기간을
둡니다 (04 문서).</p>
</div>"""
