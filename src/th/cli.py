"""명령줄 도구.

  th doctor              환경 점검 (제일 먼저 실행)
  th init                심볼 메타 동기화
  th backfill            최근 봉 채우기  (--full 이면 상장일부터 전부)
  th underlying          본주·지수 수집
  th stream              실시간 수집 시작 (Ctrl+C 로 종료)
  th gaps                봉 누락 검사    (--repair 로 복구)
  th status              적재 현황

  th paper ...           페이퍼 트레이딩 (돈이 나가지 않는 자동매매)
  th journal ...         손으로 한 매매 기록 — 나중에 이 판단을 검정한다
  th serve               웹 대시보드 (브라우저·폰에서 본다)
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import db
from .config import get_settings
from .logging_setup import get_logger, setup_logging

app = typer.Typer(add_completion=False, help="Trading Helper — 데이터 수집 도구")
console = Console()
log = get_logger("th.cli")


def _boot() -> None:
    """모든 명령의 첫 줄. 설정이 깨져 있으면 여기서 친절하게 멈춘다."""
    from .config import ConfigError

    try:
        setup_logging(get_settings().log_level)
    except ConfigError as e:
        console.print(f"[bold red]{e}[/bold red]")
        raise typer.Exit(1) from None


def _run(coro):
    async def wrapper():
        try:
            return await coro
        finally:
            await db.close_pool()

    return asyncio.run(wrapper())


# ─────────────────────────────────────────────────────────
@app.command()
def doctor() -> None:
    """환경이 제대로 잡혔는지 점검한다. 문제가 있으면 무엇을 고칠지 알려준다."""
    _boot()
    s = get_settings()

    async def check():
        t = Table(title="환경 점검", show_lines=False)
        t.add_column("항목"); t.add_column("상태"); t.add_column("비고")

        t.add_row("실행 환경", "✓", f"{s.env}")
        t.add_row("수집 심볼", "✓", ", ".join(s.symbols))
        t.add_row("본주 매핑", "✓", str(s.underlying_map))
        t.add_row(
            "주문 모드",
            "✓" if s.execution_mode == "none" else "⚠",
            f"{s.execution_mode}" + ("  (안전)" if s.execution_mode == "none" else "  주의!"),
        )

        # DB
        try:
            v = await db.fetchval("SELECT version()")
            ts = await db.fetchval("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")
            t.add_row("PostgreSQL", "✓", str(v).split(",")[0])
            t.add_row("TimescaleDB", "✓" if ts else "✗",
                      str(ts) if ts else "확장이 없습니다 — docker compose 로 띄웠는지 확인")
            tables = await db.fetchval(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            )
            t.add_row("테이블", "✓" if tables else "✗", f"{tables}개")
        except Exception as e:
            t.add_row("PostgreSQL", "✗", f"{type(e).__name__}: {e}")
            t.add_row("", "", "→ docker compose up -d 를 먼저 실행하고 .env 의 비밀번호를 확인하세요")

        # Redis
        try:
            import redis.asyncio as aioredis

            r = aioredis.from_url(s.redis_url)
            await r.ping()
            await r.aclose()
            t.add_row("Redis", "✓", s.redis_url)
        except Exception as e:
            t.add_row("Redis", "✗", f"{type(e).__name__}: {e}")

        # Binance
        try:
            from .exchange.binance_rest import BinanceFutures

            async with BinanceFutures() as api:
                st = await api.server_time()
                drift = abs((datetime.now(UTC) - st).total_seconds())
                t.add_row("바이낸스 API", "✓", f"서버시각 {st:%H:%M:%S} UTC")
                t.add_row(
                    "시각 오차",
                    "✓" if drift < 5 else "⚠",
                    f"{drift:.1f}초" + ("" if drift < 5 else "  — PC 시간을 동기화하세요"),
                )
                for sym in s.symbols:
                    f = await api.symbol_filters(sym)
                    if f:
                        t.add_row(f"심볼 {sym}", "✓", f"{f.get('status')} tick={f.get('tickSize')}")
                    else:
                        t.add_row(f"심볼 {sym}", "✗", "바이낸스에 없는 심볼입니다")
        except Exception as e:
            t.add_row("바이낸스 API", "✗", f"{type(e).__name__}: {e}")

        # 계정 연동 (읽기 전용) — Phase 2
        try:
            from th.account.client import AccountClient

            async with AccountClient() as ac:
                ok, msg = await ac.check()
            t.add_row("계정 연동", "✓" if ok else "-", msg)
        except Exception as e:
            t.add_row("계정 연동", "-", f"{type(e).__name__}: {e}")

        # 이벤트 자동 수집 신선도 — Phase 1.5
        try:
            from th.sessions import get_store
            from th.sessions.events import freshness_warnings

            store = get_store()
            warns = freshness_warnings(datetime.now(UTC), store)
            t.add_row(
                "이벤트 캘린더",
                "✓" if not warns else "⚠",
                f"{len(store.events)}건" + (f" · {warns[0]}" if warns else ""),
            )
        except Exception as e:
            t.add_row("이벤트 캘린더", "✗", f"{type(e).__name__}: {e}")

        console.print(t)

        # ── 데이터 커버리지 — 이번 프로젝트 최대 버그였다 ──────
        try:
            from th.ingest.coverage import (
                backtest_span_days,
                collect_coverage,
                warnings_for,
            )

            cov = await collect_coverage(s.symbols[0])
            span = backtest_span_days(cov)
            c = Table(title=f"데이터 커버리지 · {s.symbols[0]}")
            for col in ("소스", "행", "보유 구간", "상태", "없으면 죽는 것"):
                c.add_column(col, justify="right" if col in ("행",) else "left")
            for x in cov:
                span_txt = (
                    f"{x.first:%Y-%m-%d} ~ {x.last:%Y-%m-%d} ({x.days:.0f}일)"
                    if x.first else "[red]없음[/red]"
                )
                st = x.status(span)
                mark = {"충분": "[green]충분[/green]", "부분": "[yellow]부분[/yellow]"}.get(
                    st, f"[red]{st}[/red]"
                )
                c.add_row(x.name, f"{x.rows:,}", span_txt, mark, x.depends)
            console.print(c)

            for w in warnings_for(cov):
                console.print(f"  [yellow]⚠ {w}[/yellow]")
            console.print(
                "[dim]데이터가 없는 조건은 '미충족'이 아니라 '판단 불가'로 처리됩니다. "
                "다만 커버리지가 60% 미만이면 신호를 발행하지 않습니다.[/dim]"
            )
        except Exception as e:
            console.print(f"[dim]커버리지 점검 실패: {type(e).__name__}: {e}[/dim]")

    _run(check())


@app.command()
def init() -> None:
    """심볼 메타(틱사이즈·상장일)를 DB에 저장한다."""
    _boot()
    from .ingest.backfill import sync_symbol_meta

    _run(sync_symbol_meta())
    console.print("[green]심볼 메타 저장 완료[/green]")


@app.command()
def backfill(
    symbol: str = typer.Option(None, "--symbol", "-s", help="특정 심볼만"),
    full: bool = typer.Option(False, "--full", help="상장일부터 전부 (최초 1회)"),
    minutes: int = typer.Option(None, "--minutes", "-m", help="최근 N분만 다시 받기"),
) -> None:
    """과거 1분봉을 채운다."""
    _boot()
    s = get_settings()
    targets = [symbol.upper()] if symbol else s.symbols

    async def go():
        from .ingest.backfill import backfill_full, backfill_symbol

        for sym in targets:
            if full:
                n = await backfill_full(sym)
            else:
                n = await backfill_symbol(sym, lookback_minutes=minutes)
            console.print(f"[green]{sym}[/green]  {n}봉 저장")

    _run(go())


@app.command()
def underlying(
    days: int = typer.Option(7, "--days", "-d", help="1분봉 며칠치 (최대 30)"),
    daily: bool = typer.Option(True, "--daily/--no-daily", help="일봉도 함께"),
) -> None:
    """본주(SOXL 등)와 지수(SOXX, VIX) 가격 수집."""
    _boot()
    from .underlying.yahoo import sync_all

    res = _run(sync_all(days=days, with_daily=daily))
    for ticker, n in res.items():
        console.print(f"[green]{ticker}[/green]  1분봉 {n}행")


@app.command()
def stream() -> None:
    """실시간 수집 시작. 재연결·백필까지 자동 처리한다. Ctrl+C 로 종료."""
    _boot()
    s = get_settings()

    async def go():
        from .exchange.binance_ws import StreamCollector
        from .ingest.oi_funding import run_poller

        collector = StreamCollector(s.symbols)
        loop = asyncio.get_running_loop()
        stop_evt = asyncio.Event()

        def _on_sigint(_sig, _frame):
            # Ctrl+C 를 예외로 터뜨리지 않고 '그만' 신호로만 바꾼다.
            # 그래야 남은 데이터를 저장할 시간이 생긴다.
            loop.call_soon_threadsafe(stop_evt.set)

        try:
            prev_handler = signal.signal(signal.SIGINT, _on_sigint)
        except (ValueError, OSError):
            prev_handler = None       # 메인 스레드가 아니면 기본 동작에 맡긴다

        from .sessions.sync import auto_sync_loop

        collector_task = asyncio.create_task(collector.run(), name="collector")
        poller_task = asyncio.create_task(run_poller(s.symbols), name="poller")
        # 캘린더는 하루 1회 알아서 갱신한다. 손으로 치는 방식은 언젠가 빠뜨리고,
        # 빠뜨린 날이 하필 실적 발표일이다 (auto_sync_loop 주석 참고).
        cal_task = asyncio.create_task(auto_sync_loop(stop_evt), name="calendar")
        stop_task = asyncio.create_task(stop_evt.wait(), name="stop")

        try:
            await asyncio.wait(
                {collector_task, poller_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if prev_handler is not None:
                signal.signal(signal.SIGINT, prev_handler)

            console.print("\n[yellow]종료 중 — 남은 데이터를 저장합니다…[/yellow]")
            stop_task.cancel()
            poller_task.cancel()          # 폴러는 바로 끊어도 된다
            cal_task.cancel()
            collector.stop()              # 수집기는 스스로 마무리하게 둔다

            try:
                await asyncio.wait_for(collector_task, timeout=15)
            except (TimeoutError, asyncio.TimeoutError):
                log.warning("수집기 정리가 15초를 넘겨 강제 종료합니다")
                collector_task.cancel()
            except asyncio.CancelledError:
                pass

            await asyncio.gather(
                collector_task, poller_task, cal_task, stop_task,
                return_exceptions=True,
            )

    _run(go())
    console.print("[green]수집 종료[/green]")


@app.command()
def gaps(
    hours: int = typer.Option(24, "--hours", "-h"),
    repair: bool = typer.Option(False, "--repair", help="찾은 구멍을 백필로 메움"),
) -> None:
    """봉 누락 검사. Phase 0 완료 기준은 24시간 무중단 후 누락 0건이다."""
    _boot()
    s = get_settings()

    async def go():
        from .ingest.gapcheck import find_gaps, repair_gaps

        for sym in s.symbols:
            g = await find_gaps(sym, hours)
            if not g:
                console.print(f"[green]{sym}[/green]  최근 {hours}시간 누락 없음 ✓")
                continue
            miss = sum(x[2] for x in g)
            console.print(f"[red]{sym}[/red]  구간 {len(g)}개 / 봉 {miss}개 누락")
            for start, end, n in g[:10]:
                console.print(f"    {start:%m-%d %H:%M} → {end:%m-%d %H:%M}  ({n}봉)")
            if repair:
                filled = await repair_gaps(sym, hours)
                console.print(f"    [green]{filled}봉 복구[/green]")

    _run(go())


@app.command("refresh-aggregates")
def refresh_aggregates(
    chunk_days: int = typer.Option(30, "--chunk-days", help="한 번에 재계산할 기간(일)"),
) -> None:
    """5분·15분봉 집계를 과거 구간까지 다시 계산한다.

    자동 갱신 정책은 최근 몇 시간만 훑으므로, --full 백필 직후에는
    이걸 한 번 돌려야 과거 5분봉이 채워진다.

    전체를 한 번에 갱신하면 수 분씩 걸려 타임아웃에 걸리므로,
    기간을 잘라 조금씩 진행하고 어디까지 됐는지 출력한다.
    중간에 끊겨도 이미 끝난 구간은 다시 계산하지 않는다.
    """
    _boot()

    async def go():
        conn = await db.open_connection(command_timeout=None)
        try:
            first = await conn.fetchval("SELECT min(ts) FROM klines WHERE tf = '1m'")
            if first is None:
                console.print(
                    "[yellow]klines 가 비어 있습니다. 먼저 th backfill 을 실행하세요[/yellow]"
                )
                return

            now = datetime.now(UTC)
            step = timedelta(days=chunk_days)

            for view in ("klines_5m", "klines_15m"):
                console.print(f"\n[bold]{view}[/bold] 재계산")
                start = first.astimezone(UTC).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                while start < now:
                    end = min(start + step, now)
                    sql = (
                        f"CALL refresh_continuous_aggregate('{view}', "
                        f"TIMESTAMPTZ '{start.isoformat()}', "
                        f"TIMESTAMPTZ '{end.isoformat()}')"
                    )
                    try:
                        await conn.execute(sql)
                    except Exception as e:
                        console.print(
                            f"  [red]{start:%Y-%m-%d} ~ {end:%Y-%m-%d} 실패[/red] "
                            f"{type(e).__name__}: {e}"
                        )
                        raise
                    console.print(f"  {start:%Y-%m-%d} → {end:%Y-%m-%d}  ✓")
                    start = end
                console.print(f"[green]{view} 완료[/green]")
        finally:
            await conn.close()

    _run(go())


@app.command()
def status() -> None:
    """적재 현황 요약."""
    _boot()

    async def go():
        from .ingest.gapcheck import report

        rep = await report(hours=24)
        t = Table(title="적재 현황")
        for c in ("심볼", "봉 수", "첫 봉", "마지막 봉", "지연", "24h 누락"):
            t.add_column(c)
        now = datetime.now(UTC)
        for sym, r in rep.items():
            last = r["last_bar"]
            lag = f"{(now - last).total_seconds() / 60:.0f}분" if last else "-"
            t.add_row(
                sym,
                f"{r['bars_total']:,}",
                f"{r['first_bar']:%Y-%m-%d %H:%M}" if r["first_bar"] else "-",
                f"{last:%m-%d %H:%M}" if last else "-",
                lag,
                str(r["missing_bars"]),
            )
        console.print(t)

        rows = await db.fetch(
            """SELECT ticker, tf, count(*) n, max(ts) last_ts
               FROM underlying_prices GROUP BY ticker, tf ORDER BY ticker, tf"""
        )
        if rows:
            t2 = Table(title="본주 · 지수")
            for c in ("티커", "봉", "행 수", "마지막"):
                t2.add_column(c)
            for r in rows:
                t2.add_row(r["ticker"], r["tf"], f"{r['n']:,}", f"{r['last_ts']:%m-%d %H:%M}")
            console.print(t2)

    _run(go())


# ─────────────────────────────────────────────────────────
# Phase 1 — 세션 · 이벤트 게이트 (DB 불필요)
# ─────────────────────────────────────────────────────────

KST = ZoneInfo("Asia/Seoul")


def _parse_at(s: str | None) -> datetime:
    if not s:
        return datetime.now(UTC)
    txt = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(txt)
    if dt.tzinfo is None:                       # 시각만 주면 KST 로 해석
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(UTC)


def _both_tz(dt: datetime) -> str:
    from th.sessions import NY

    return f"{dt.astimezone(KST):%m-%d %H:%M} KST ({dt.astimezone(NY):%H:%M} ET)"


@app.command()
def session(
    symbol: str = typer.Option(None, "--symbol", "-s", help="기본값은 .env 첫 심볼"),
    at: str = typer.Option(None, "--at", help="ISO 시각. 생략하면 지금 (예: 2026-09-16T14:05 는 KST)"),
) -> None:
    """지금 이 심볼의 세션 상태 — 레짐, 차단 여부와 그 이유, 다음 이벤트."""
    _boot()
    from th.sessions import get_context

    sym = (symbol or get_settings().symbols[0]).upper()
    ctx = get_context(sym, _parse_at(at))

    console.print()
    console.print(f"[bold]{sym}[/bold]  {_both_tz(ctx.at)}")
    console.print(f"  {ctx.summary_ko()}")
    if ctx.regime_ends_at:
        console.print(f"  다음 레짐 전환  {_both_tz(ctx.regime_ends_at)}")
    if ctx.early_close:
        console.print("  [yellow]조기 마감일[/yellow]")

    if ctx.blocked:
        console.print("\n[red bold]■ 신호 차단[/red bold]")
        for b in ctx.blocks:
            console.print(f"  · {b.reason_ko}  [dim]({b.rule_id})[/dim]")
    else:
        console.print("\n[green bold]■ 신호 발행 가능[/green bold]")

    if ctx.penalties or ctx.boosts:
        console.print("\n[bold]■ 신뢰도 가감[/bold]")
        for a in (*ctx.penalties, *ctx.boosts):
            tgt = ", ".join(a.applies_to) if a.applies_to else "전체"
            color = "red" if a.points < 0 else "green"
            console.print(
                f"  [{color}]{a.points:+3d}[/{color}]  {a.reason_ko}  [dim]({tgt})[/dim]"
            )

    if ctx.warnings:
        console.print("\n[yellow bold]■ 주의[/yellow bold]")
        for w in ctx.warnings:
            console.print(f"  · {w}")

    if ctx.next_event:
        e = ctx.next_event
        mins = (ctx.next_event_in_sec or 0) // 60
        when = f"{mins // 60}시간 {mins % 60}분 뒤" if mins < 60 * 48 else f"{mins // 1440}일 뒤"
        console.print(
            f"\n[bold]■ 다음 이벤트[/bold]  {e.name} · {when} · {_both_tz(e.ts_utc)}"
        )
    console.print()


@app.command("session-today")
def session_today(
    day: str = typer.Option(None, "--date", "-d", help="YYYY-MM-DD (미국 동부 기준). 생략하면 오늘"),
    symbol: str = typer.Option(None, "--symbol", "-s"),
    step: int = typer.Option(5, "--step", help="훑는 간격(분)"),
) -> None:
    """하루 전체 타임라인 — 언제 차단되고 언제 열리는지 한눈에."""
    _boot()
    from th.sessions import NY, compress_timeline, day_timeline

    sym = (symbol or get_settings().symbols[0]).upper()
    d = (
        datetime.fromisoformat(day).date()
        if day
        else datetime.now(UTC).astimezone(NY).date()
    )

    segments = compress_timeline(day_timeline(d, sym, step_min=step))

    t = Table(title=f"{sym} · {d} (ET 기준 하루)")
    for c in ("KST", "ET", "레짐", "상태", "사유 / 가감"):
        t.add_column(c)

    for start, end, ctx in segments:
        kst = f"{start.astimezone(KST):%H:%M}–{end.astimezone(KST):%H:%M}"
        et = f"{start.astimezone(NY):%H:%M}–{end.astimezone(NY):%H:%M}"
        if ctx.blocked:
            state = "[red]차단[/red]"
            detail = " / ".join(b.rule_id for b in ctx.blocks)
        else:
            state = "[green]발행 가능[/green]"
            adj = [f"{a.rule_id} {a.points:+d}" for a in (*ctx.penalties, *ctx.boosts)]
            detail = " / ".join(adj) if adj else ""
        t.add_row(kst, et, f"{ctx.regime.emoji} {ctx.regime.label_ko}", state, detail)

    console.print(t)
    console.print(
        "[dim]KST 열이 한국에서 실제로 보게 되는 시각입니다. "
        "미국 서머타임에 따라 1시간씩 밀립니다.[/dim]"
    )


@app.command()
def events(
    days: int = typer.Option(30, "--days", "-d", help="앞으로 며칠까지"),
) -> None:
    """등록된 캘린더 이벤트. 비어 있으면 sessions/data/events.yaml 에 직접 추가하세요."""
    _boot()
    from th.sessions import get_events
    from th.sessions import upcoming as _upcoming

    now = datetime.now(UTC)
    items = _upcoming(now, get_events(), days=days)
    if not items:
        console.print(
            f"[yellow]앞으로 {days}일 안에 등록된 이벤트가 없습니다.[/yellow]\n"
            "src/th/sessions/data/events.yaml 에 실적일·지표 발표일을 추가하세요."
        )
        return

    t = Table(title=f"향후 {days}일 이벤트")
    for c in ("D-", "KST", "ET", "종류", "이름", "중요도"):
        t.add_column(c)
    for e in items:
        d = (e.ts_utc - now).days
        t.add_row(
            f"D-{d}" if d > 0 else "오늘",
            f"{e.ts_utc.astimezone(KST):%m-%d %H:%M}",
            f"{e.ts_utc.astimezone(ZoneInfo('America/New_York')):%m-%d %H:%M}",
            e.kind,
            e.name,
            e.importance,
        )
    console.print(t)


# ─────────────────────────────────────────────────────────
# DB 마이그레이션
# ─────────────────────────────────────────────────────────

@app.command()
def migrate(
    check: bool = typer.Option(False, "--check", help="적용하지 않고 목록만 확인"),
) -> None:
    """db/migrations 의 SQL 을 순서대로 적용한다 (여러 번 돌려도 안전)."""
    _boot()

    async def go():
        from .migrations import migration_files, pending_versions, run_migrations

        files = migration_files()
        if not files:
            console.print("[yellow]마이그레이션 파일이 없습니다[/yellow]")
            return
        if check:
            pending = await pending_versions()
            console.print(f"전체 {len(files)}개 · 미적용 {len(pending)}개")
            for v in pending:
                console.print(f"  · {v}")
            return
        applied = await run_migrations()
        if applied:
            for v in applied:
                console.print(f"[green]적용[/green] {v}")
        else:
            console.print("[green]이미 최신입니다[/green]")

    _run(go())


# ─────────────────────────────────────────────────────────
# Phase 1.5 — 캘린더 이벤트 자동 수집
# ─────────────────────────────────────────────────────────

calendar_app = typer.Typer(
    help="캘린더 이벤트 자동 수집 (경제지표·실적일)", no_args_is_help=True
)
app.add_typer(calendar_app, name="calendar")


@calendar_app.command("sync")
def calendar_sync(
    days: int = typer.Option(None, "--days", "-d", help="앞으로 며칠치 (기본 .env 설정)"),
    save_db: bool = typer.Option(True, "--db/--no-db", help="DB에도 저장할지"),
) -> None:
    """FRED 경제지표 + 실적일을 받아와 스냅샷과 DB에 저장한다."""
    _boot()
    s = get_settings()

    async def go():
        from .sessions.sync import sync_calendar

        rep = await sync_calendar(days=days or s.calendar_sync_days, save_db=save_db)

        t = Table(title="캘린더 동기화")
        for c in ("소스", "결과", "건수", "비고"):
            t.add_column(c)
        for st in rep.statuses:
            t.add_row(
                st.name,
                "[green]✓[/green]" if st.ok else "[red]✗[/red]",
                str(st.count),
                st.detail,
            )
        console.print(t)
        console.print(
            f"병합 결과 [bold]{rep.total_events}건[/bold] "
            f"(손으로 적은 것 {rep.manual_events}건 포함) · {rep.duration_sec:.1f}초"
        )
        if rep.snapshot_path:
            console.print(f"스냅샷 저장: {rep.snapshot_path.name}")
        if rep.db_error:
            console.print(f"[yellow]DB 저장 건너뜀: {rep.db_error}[/yellow]")
        elif rep.db_rows is not None:
            console.print(f"DB 저장: {rep.db_rows}행")

        if not any(st.ok for st in rep.statuses if st.name != "manual"):
            console.print(
                "\n[yellow]자동 소스가 모두 실패했습니다.[/yellow] "
                "FRED 키(.env TH_FRED_API_KEY)와 인터넷 연결을 확인하세요."
            )

    _run(go())


@calendar_app.command("status")
def calendar_status() -> None:
    """자동 수집 상태 — 언제 갱신됐고 무엇이 들어 있는지."""
    _boot()
    from .sessions import get_store
    from .sessions.events import freshness_warnings

    store = get_store()
    now = datetime.now(UTC)

    console.print()
    if store.synced_at:
        age = now - store.synced_at
        hours = age.total_seconds() / 3600
        color = "green" if hours < 24 else "yellow"
        console.print(
            f"마지막 동기화  [{color}]{store.synced_at.astimezone(KST):%m-%d %H:%M} KST "
            f"({hours:.1f}시간 전)[/{color}]"
        )
    else:
        console.print("[yellow]자동 수집을 아직 실행하지 않았습니다[/yellow]")

    if store.sources:
        t = Table(title="소스별 상태")
        for c in ("소스", "결과", "건수", "비고"):
            t.add_column(c)
        for st in store.sources:
            t.add_row(
                st.name,
                "[green]✓[/green]" if st.ok else "[red]✗[/red]",
                str(st.count),
                st.detail,
            )
        console.print(t)

    counts: dict[str, int] = {}
    for e in store.events:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    if counts:
        console.print(
            "보유 이벤트  "
            + " · ".join(f"{k} {v}건" for k, v in sorted(counts.items()))
            + f"  (총 {len(store.events)}건)"
        )

    for w in freshness_warnings(now, store):
        console.print(f"[yellow]⚠ {w}[/yellow]")

    async def db_part():
        from .sessions.sync import db_sync_state

        try:
            rows = await db_sync_state()
        except Exception as e:
            console.print(f"[dim]DB 상태 조회 불가: {type(e).__name__}[/dim]")
            return
        if not rows:
            return
        t = Table(title="DB 기록")
        for c in ("소스", "마지막 성공", "결과", "건수"):
            t.add_column(c)
        for r in rows:
            ok_at = r["last_ok_at"]
            t.add_row(
                r["source"],
                f"{ok_at.astimezone(KST):%m-%d %H:%M}" if ok_at else "-",
                "✓" if r["ok"] else "✗",
                str(r["count"]),
            )
        console.print(t)

    _run(db_part())
    console.print()


@calendar_app.command("releases")
def calendar_releases(
    search: str = typer.Option(None, "--search", "-s", help="이름에 포함된 문자열"),
    limit: int = typer.Option(40, "--limit"),
) -> None:
    """FRED 에 등록된 릴리스 이름 찾기.

    econ_releases.yaml 의 match 에 넣을 정확한 이름을 확인할 때 씁니다.
    """
    _boot()
    s = get_settings()
    if not s.fred_api_key:
        console.print(
            "[yellow]TH_FRED_API_KEY 가 비어 있습니다.[/yellow]\n"
            "무료 키 발급: https://fred.stlouisfed.org/docs/api/api_key.html"
        )
        raise typer.Exit(1)

    async def go():
        from .sessions.sources.fred import FredSource

        try:
            items = await FredSource(api_key=s.fred_api_key).list_releases(search)
        except Exception as e:
            console.print(f"[red]조회 실패:[/red] {type(e).__name__}: {e}")
            return
        if not items:
            console.print("[yellow]일치하는 릴리스가 없습니다[/yellow]")
            return
        t = Table(title=f"FRED 릴리스 ({len(items)}건)")
        t.add_column("id"); t.add_column("이름")
        for r in items[:limit]:
            t.add_row(str(r.get("id")), str(r.get("name")))
        console.print(t)
        if len(items) > limit:
            console.print(f"[dim]… 외 {len(items) - limit}건 (--limit 로 더 보기)[/dim]")

    _run(go())


# ─────────────────────────────────────────────────────────
# Phase 2 — 계정 · 리스크
# ─────────────────────────────────────────────────────────

def _sym(symbol: str | None) -> str:
    return (symbol or get_settings().symbols[0]).upper()


async def _session_ctx(symbol: str):
    from th.sessions import get_context

    return get_context(symbol)


@app.command()
def position(
    symbol: str = typer.Option(None, "--symbol", "-s"),
) -> None:
    """현재 포지션·잔고·미체결 주문을 한 번 조회한다."""
    _boot()
    sym = _sym(symbol)

    async def go():
        from th.account.service import get_risk_view
        from th.ui.dashboard import render

        view = await get_risk_view(sym, await _session_ctx(sym))
        console.print(render(view))

        if view.risk.open_orders:
            t = Table(title="미체결 주문")
            for c in ("종류", "방향", "수량", "트리거", "감소전용"):
                t.add_column(c)
            for o in view.risk.open_orders:
                t.add_row(
                    o.order_type,
                    o.side,
                    f"{o.qty:g}",
                    f"{o.stop_price:g}" if o.stop_price else "-",
                    "✓" if (o.reduce_only or o.close_position) else "",
                )
            console.print(t)

    _run(go())


@app.command()
def risk(
    symbol: str = typer.Option(None, "--symbol", "-s"),
) -> None:
    """리스크 요약과 경고만 간단히."""
    _boot()
    sym = _sym(symbol)

    async def go():
        from th.account.service import get_risk_view
        from th.ui.dashboard import alerts_panel, daily_bar, risk_panel

        view = await get_risk_view(sym, await _session_ctx(sym))
        console.print(risk_panel(view))
        console.print(alerts_panel(view.alerts))
        console.print(daily_bar(view))

    _run(go())


@app.command()
def watch(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    interval: int = typer.Option(3, "--interval", "-i", help="갱신 간격(초)"),
) -> None:
    """실시간 대시보드. 매매 중 옆에 띄워두세요. Ctrl+C 로 종료."""
    _boot()
    sym = _sym(symbol)

    async def go():
        from rich.console import Group
        from rich.live import Live

        from th.account.alerts import build_alerts
        from th.account.client import AccountClient
        from th.account.service import carry_forward, get_risk_view
        from th.ui.dashboard import footer, render

        # 조회 비용이 다른 것들을 각자 주기로 돈다 (레이트리밋 여유 확보)
        income_every = max(1, 30 // interval)     # 당일 손익 30초
        market_every = max(1, 20 // interval)     # 변동성·스프레드 20초
        session_every = max(1, 5 // interval)     # 세션 5초

        loop = asyncio.get_running_loop()
        stop_evt = asyncio.Event()

        def _on_sigint(_s, _f):
            loop.call_soon_threadsafe(stop_evt.set)

        try:
            prev_handler = signal.signal(signal.SIGINT, _on_sigint)
        except (ValueError, OSError):
            prev_handler = None

        client = AccountClient()
        prev = None
        ctx = None
        tick = 0

        try:
            with Live(console=console, refresh_per_second=4, screen=False) as live:
                while not stop_evt.is_set():
                    if tick % session_every == 0 or ctx is None:
                        ctx = await _session_ctx(sym)

                    view = await get_risk_view(
                        sym,
                        ctx,
                        client=client,
                        with_income=(tick % income_every == 0),
                        with_market=(tick % market_every == 0),
                    )
                    # 못 가져온 값은 직전 것을 유지한다 (웹 서버와 같은 함수를 쓴다)
                    view = carry_forward(view, prev)
                    view.alerts = build_alerts(view.risk, ctx, view.market)

                    live.update(Group(render(view), footer(view.risk.at, interval)))
                    prev = view
                    tick += 1

                    try:
                        await asyncio.wait_for(stop_evt.wait(), timeout=interval)
                    except (TimeoutError, asyncio.TimeoutError):
                        pass
        finally:
            if prev_handler is not None:
                signal.signal(signal.SIGINT, prev_handler)
            await client.close()

    _run(go())
    console.print("[green]대시보드 종료[/green]")


# ─────────────────────────────────────────────────────────
# Phase 3 — 전략 엔진
# ─────────────────────────────────────────────────────────

@app.command()
def analyze(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    at: str = typer.Option(
        None, "--at", help="ISO 시각. 생략하면 마지막 확정 봉 (예: 2026-09-11T23:40 은 KST)"
    ),
    wallet: float = typer.Option(
        None, "--wallet", "-w", help="포지션 사이즈 계산용 잔고. 생략하면 계정에서 조회"
    ),
    show_all: bool = typer.Option(False, "--all", help="신호가 없을 때 근접 셋업 근거까지"),
) -> None:
    """신호를 계산한다.

    과거 시각을 지정하면 **그 시점 데이터만으로** 재현한다 (미래 정보 누수 없음).
    """
    _boot()
    sym = _sym(symbol)

    async def go():
        from th.strategy.engine import analyze as run_analyze
        from th.ui.signal import render_analysis

        when = _parse_at(at) if at else None
        a = await run_analyze(sym, when, wallet=wallet)
        console.print()
        console.print(render_analysis(a, show_all=show_all))
        console.print()

    _run(go())


@app.command("replay")
def replay(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    start: str = typer.Option(..., "--start", help="시작 시각 (KST)"),
    minutes: int = typer.Option(120, "--minutes", "-m", help="몇 분 구간"),
    step: int = typer.Option(5, "--step", help="몇 분 간격으로 평가"),
    wallet: float = typer.Option(10000.0, "--wallet", "-w"),
) -> None:
    """구간을 훑으며 신호가 언제 났는지 본다. 백테스트 전 눈으로 확인하는 용도."""
    _boot()
    sym = _sym(symbol)

    async def go():
        from th.strategy.engine import analyze as run_analyze

        begin = _parse_at(start)
        t = Table(title=f"{sym} · {begin.astimezone(KST):%m-%d %H:%M} 부터 {minutes}분")
        for c in ("KST", "레짐", "결과", "점수", "사유 / 셋업"):
            t.add_column(c)

        found = 0
        for i in range(0, minutes, step):
            when = begin + timedelta(minutes=i)
            a = await run_analyze(sym, when, wallet=wallet)
            regime = a.session.regime.label_ko if a.session else "-"
            if a.signal:
                found += 1
                c = a.signal
                t.add_row(
                    f"{when.astimezone(KST):%H:%M}",
                    regime,
                    f"[green]{'롱' if c.result.direction == 'long' else '숏'}[/green]",
                    str(c.score.final),
                    c.result.setup_id,
                )
            else:
                reasons = a.why_not_ko()
                t.add_row(
                    f"{when.astimezone(KST):%H:%M}",
                    regime,
                    "[dim]없음[/dim]",
                    "",
                    Text(reasons[0] if reasons else "", style="dim"),
                )
        console.print(t)
        console.print(f"신호 {found}건 / {len(range(0, minutes, step))}회 평가")

    _run(go())


@app.command()
def backtest(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    start: str = typer.Option(..., "--start", help="시작일 YYYY-MM-DD"),
    end: str = typer.Option(None, "--end", help="종료일 YYYY-MM-DD. 생략하면 어제"),
    step: int = typer.Option(5, "--step", help="몇 분 간격으로 평가할지"),
    regimes: str = typer.Option("RTH", "--regimes", help="쉼표 구분 (RTH,PRE,POST)"),
    wallet: float = typer.Option(10000.0, "--wallet", "-w"),
    risk_pct: float = typer.Option(0.5, "--risk", help="한 트레이드에 거는 계좌 %"),
    oos: float = typer.Option(0.4, "--oos", help="out-of-sample 비율"),
    folds: int = typer.Option(4, "--folds", help="워크포워드 분할 수"),
    out: str = typer.Option("backtest-report.html", "--out", "-o"),
    data: str = typer.Option(
        None, "--data", help="th export 로 만든 CSV 폴더. 주면 DB 대신 이걸 읽는다"
    ),
) -> None:
    """과거 구간을 그대로 돌려 전략을 채점한다. **Phase 4 의 관문.**"""
    _boot()
    sym = _sym(symbol)

    async def go():
        from th.backtest.metrics import evaluate_gates, group_by, summarize
        from th.backtest.models import BacktestConfig
        from th.backtest.report import build_report
        from th.backtest.runner import parse_day, run_backtest
        from th.backtest.walkforward import split_in_out, walk_forward

        begin = parse_day(start)
        finish = (
            parse_day(end) + timedelta(days=1)
            if end
            else datetime.now(UTC) - timedelta(days=1)
        )
        cfg = BacktestConfig(
            wallet=wallet,
            risk_pct=risk_pct,
            step_minutes=step,
            regimes=tuple(r.strip().upper() for r in regimes.split(",") if r.strip()),
        )

        console.print(
            f"[dim]{sym} · {begin:%Y-%m-%d} ~ {finish:%Y-%m-%d} · {step}분 간격 · "
            f"{'/'.join(cfg.regimes)}[/dim]"
        )
        console.print("[dim]캐시를 쓰지 않아 느립니다. 몇 분 걸릴 수 있습니다.[/dim]")

        ds = _load_csv_dataset(data)
        state = {"n": 0}

        def tick(at: datetime, evaluated: int, trades: int) -> None:
            if evaluated - state["n"] >= 500:
                state["n"] = evaluated
                console.print(
                    f"  [dim]{at:%Y-%m-%d %H:%M} · 평가 {evaluated:,} · "
                    f"트레이드 {trades:,}[/dim]"
                )

        r = await run_backtest(sym, begin, finish, cfg, progress=tick, dataset=ds)

        if not r.trades:
            console.print("[yellow]트레이드가 한 건도 없습니다.[/yellow]")
            console.print(
                f"  평가 {r.evaluated:,} · 게이트 차단 {r.blocked:,} · "
                f"필터 탈락 {r.filtered:,} · 신호 {r.signals:,}"
            )
            for k, v in sorted(r.skip_reasons.items()):
                console.print(f"  거래 무산 {k}: {v}")
            return

        trades = sorted(r.trades, key=lambda t: t.signal_at)
        is_, oos_, purged = split_in_out(trades, oos_fraction=oos)
        overall = summarize(trades)
        s_is, s_oos = summarize(is_), summarize(oos_)
        by_regime = group_by(trades, "regime")
        by_setup = group_by(trades, "setup_id")
        gates = evaluate_gates(s_oos, group_by(oos_, "regime"))

        t = Table(title=f"{sym} 백테스트")
        t.add_column("구간")
        for c in ("표본", "승률 (95% CI)", "기대값", "누적", "최대낙폭"):
            t.add_column(c, justify="right")
        for label, s in (("전체", overall), ("in-sample", s_is), ("out-of-sample", s_oos)):
            lo, hi = s.win_rate_ci
            t.add_row(
                label, f"{s.n}", f"{s.win_rate:.0%} ({lo:.0%}~{hi:.0%})",
                f"{s.expectancy_r:+.3f}R", f"{s.total_r:+.1f}R",
                f"{s.max_drawdown_r:.1f}R",
            )
        console.print(t)

        g = Table(title="통과 기준 (out-of-sample)")
        g.add_column(""); g.add_column("항목"); g.add_column("결과")
        for x in gates:
            g.add_row("[green]✓[/green]" if x.passed else "[red]✗[/red]", x.name, x.detail)
        console.print(g)

        ok = all(x.passed for x in gates)
        console.print(
            "[bold green]통과 — 신호 기능을 낼 수 있습니다[/bold green]"
            if ok
            else "[bold red]불합격 — 신호 카드를 빼고 차트·리스크·세션만 출시합니다[/bold red]"
        )

        folds_stats = [
            (f.index, summarize(list(f.train)), summarize(list(f.test)))
            for f in walk_forward(trades, folds=folds)
        ]
        page = build_report(
            symbol=sym, start=begin, end=finish, generated_at=datetime.now(UTC),
            overall=overall, in_sample=s_is, out_sample=s_oos, gates=gates,
            by_regime=by_regime, by_setup=by_setup, folds=folds_stats,
            trades=trades, purged=purged,
            counters={
                "평가": r.evaluated, "게이트 차단": r.blocked,
                "필터 탈락": r.filtered, "신호": r.signals,
            },
            basis_coverage=r.basis_coverage,
            skip_reasons=r.skip_reasons,
        )
        path = Path(out)
        path.write_text(page, encoding="utf-8")
        console.print(f"\n리포트: [bold]{path.resolve()}[/bold]")

    _run(go())


def _load_csv_dataset(folder: str | None):
    """`--data` 로 CSV 폴더를 주면 읽어서 커버리지를 먼저 보여준다.

    DB 조회는 값이 없으면 조용히 None 을 돌려준다. 그게 Phase 4 에서 OI·펀딩·basis 가
    백테스트 내내 '미충족'으로 처리되던 사고의 원인이었으므로, 여기서는 시작 전에
    각 소스가 실제로 언제부터 있는지 찍는다.
    """
    if not folder:
        return None
    from th.backtest.csvdata import load_dataset

    ds = load_dataset(folder)
    t = Table(title=f"데이터 커버리지 · {folder}")
    t.add_column("소스"); t.add_column("행", justify="right"); t.add_column("보유 구간")
    for c in ds.coverage:
        span = f"{c.first:%Y-%m-%d} ~ {c.second:%Y-%m-%d}" if c.first else "[red]없음[/red]"
        t.add_row(c.name, f"{c.rows:,}", span)
    console.print(t)
    return ds


@app.command()
def export(
    out: str = typer.Option("dataset", "--out", "-o", help="내보낼 폴더"),
    symbol: str = typer.Option(None, "--symbol", "-s"),
) -> None:
    """DB 내용을 CSV로 내보낸다. **오프라인 백테스트용.**

    DB에 못 붙는 환경(다른 기계, 클라우드)에서도 `th backtest --data <폴더>` 로
    똑같은 백테스트를 돌리기 위한 것이다. 개인정보는 아무것도 담기지 않는다 —
    계정·주문·API 키 관련 테이블은 내보내지 않는다.
    """
    _boot()
    sym = _sym(symbol)
    target = Path(out)
    target.mkdir(parents=True, exist_ok=True)

    async def go():
        import csv as _csv

        jobs = [
            ("klines_1m.csv",
             "SELECT symbol, ts, open, high, low, close, volume, taker_buy_base "
             "FROM klines WHERE symbol = $1 AND tf = '1m' ORDER BY ts", (sym,)),
            ("open_interest.csv",
             "SELECT symbol, ts, oi, oi_value FROM open_interest "
             "WHERE symbol = $1 ORDER BY ts", (sym,)),
            ("funding.csv",
             "SELECT symbol, ts, rate, mark_price, index_price, next_funding_ts "
             "FROM funding WHERE symbol = $1 ORDER BY ts", (sym,)),
            ("book_ticker.csv",
             "SELECT symbol, ts, bid, ask, spread_bps FROM book_ticker "
             "WHERE symbol = $1 ORDER BY ts", (sym,)),
            ("underlying.csv",
             "SELECT ticker, tf, ts, open, high, low, close, volume "
             "FROM underlying_prices ORDER BY ticker, tf, ts", ()),
            ("calendar_events.csv",
             "SELECT source, kind, name, ts, importance FROM calendar_events "
             "ORDER BY ts", ()),
        ]

        total = 0
        for name, query, args in jobs:
            try:
                rows = await db.fetch(query, *args)
            except Exception as e:                       # 없는 테이블은 건너뛴다
                console.print(f"  [dim]{name} 건너뜀 ({type(e).__name__})[/dim]")
                continue
            path = target / name
            with path.open("w", newline="", encoding="utf-8") as fh:
                if not rows:
                    console.print(f"  [dim]{name} 0행[/dim]")
                    continue
                w = _csv.writer(fh)
                w.writerow(rows[0].keys())
                for r in rows:
                    w.writerow(
                        [v.isoformat() if hasattr(v, "isoformat") else v for v in r.values()]
                    )
            total += len(rows)
            console.print(f"  {name}  {len(rows):,}행  ({path.stat().st_size/1e6:.1f}MB)")

        console.print(f"\n총 {total:,}행 → [bold]{target.resolve()}[/bold]")
        console.print("[dim]계정·주문·키 관련 테이블은 내보내지 않았습니다.[/dim]")

    _run(go())


@app.command()
def funnel(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    start: str = typer.Option(..., "--start", help="시작일 YYYY-MM-DD"),
    end: str = typer.Option(None, "--end", help="종료일 YYYY-MM-DD"),
    step: int = typer.Option(5, "--step"),
    regimes: str = typer.Option("RTH", "--regimes"),
    data: str = typer.Option(None, "--data", help="th export 로 만든 CSV 폴더"),
) -> None:
    """**신호가 어디서 죽는지** 센다. 임계값을 손대기 전에 반드시 먼저 본다."""
    _boot()
    sym = _sym(symbol)

    async def go():
        from th.backtest.funnel import (
            PUBLISHABLE_ATR_MAX,
            PUBLISHABLE_ATR_MIN,
            band_note,
            collect_funnel,
        )
        from th.backtest.models import BacktestConfig
        from th.backtest.runner import parse_day

        begin = parse_day(start)
        finish = (
            parse_day(end) + timedelta(days=1)
            if end
            else datetime.now(UTC) - timedelta(days=1)
        )
        cfg = BacktestConfig(
            step_minutes=step,
            regimes=tuple(r.strip().upper() for r in regimes.split(",") if r.strip()),
        )
        state = {"n": 0}

        def tick(at: datetime, n: int, published: int) -> None:
            if n - state["n"] >= 1000:
                state["n"] = n
                console.print(f"  [dim]{at:%Y-%m-%d} · 평가 {n:,}[/dim]")

        r = await collect_funnel(
            sym, begin, finish, cfg, progress=tick, dataset=_load_csv_dataset(data)
        )
        if not r.evaluated:
            console.print("[yellow]평가된 시각이 없습니다[/yellow]")
            return

        t = Table(title=f"{sym} 신호 퍼널 · {begin:%Y-%m-%d} ~ {finish:%Y-%m-%d}")
        t.add_column("단계")
        for c in ("여기서 죽음", "남음", "비율"):
            t.add_column(c, justify="right")
        t.add_row("평가한 시각", "", f"{r.evaluated:,}", "100%")
        for label, killed, left in r.stage_rows():
            t.add_row(
                label,
                f"{killed:,}" if killed else "[dim]0[/dim]",
                f"{left:,}",
                f"{left / r.evaluated:.1%}",
            )
        console.print(t)

        def top(title: str, counter, limit: int = 8) -> None:
            if not counter:
                return
            x = Table(title=title)
            x.add_column("사유"); x.add_column("건수", justify="right")
            for k, v in counter.most_common(limit):
                x.add_row(str(k), f"{v:,}")
            console.print(x)

        top("세션 게이트 — 무엇이 막았나", r.blocked)
        top("시장 상태 필터 — 무엇이 걸렀나", r.filtered)
        top("셋업 필수 조건 — 어느 조건에서 죽었나", r.required_failed, 10)
        console.print(
            "[dim]위 표는 셋업×방향(롱/숏)마다 세므로 합계가 단계별 표보다 큽니다. "
            "'어느 조건이 병목인가'를 보는 용도입니다.[/dim]"
        )
        top("리스크·점수 — 왜 발행 안 됐나", r.rejected)

        if r.atr_pct:
            vals = sorted(r.atr_pct)
            def q(p):
                return vals[min(len(vals) - 1, int(p * len(vals)))]
            console.print()
            console.print(f"[bold]ATR% 분포[/bold]  중앙값 {q(0.5):.3f}% · "
                          f"사분위 {q(0.25):.3f}~{q(0.75):.3f}% · "
                          f"10~90% {q(0.1):.3f}~{q(0.9):.3f}%")
            share = r.atr_in_band / len(r.atr_pct)
            console.print(
                f"발행 가능 구간({PUBLISHABLE_ATR_MIN:.3f}~{PUBLISHABLE_ATR_MAX:.3f}%)"
                f" 안에 든 비율: [bold]{share:.1%}[/bold]"
            )
            console.print(f"[dim]{band_note()}[/dim]")

        if r.sl_pct:
            s = sorted(r.sl_pct)
            console.print(
                f"[bold]계산된 손절폭[/bold]  중앙값 {s[len(s)//2]:.3f}% · "
                f"최소 {s[0]:.3f}% · 최대 {s[-1]:.3f}%  (n={len(s)})"
            )
        if r.atr_only_sl_pct:
            a2 = sorted(r.atr_only_sl_pct)
            console.print(
                f"[bold]구조를 빼고 ATR 만 썼다면[/bold]  중앙값 "
                f"{a2[len(a2)//2]:.3f}%  → 차이가 크면 손절을 넓히는 범인은 "
                f"변동성이 아니라 [bold]구조[/bold]입니다"
            )

        if r.stop_by_window:
            from th.backtest.funnel import PUBLISHABLE_SL_MAX, PUBLISHABLE_SL_MIN

            w = Table(title="구조 탐색 창을 바꾸면 손절폭이 어떻게 되나 (재보기만 함)")
            w.add_column("창(1분봉)")
            for c in ("손절 중앙값", "발행 가능 구간 안", "비율"):
                w.add_column(c, justify="right")
            for key, vals in r.stop_by_window.items():
                v = sorted(vals)
                ok = sum(1 for x in v if PUBLISHABLE_SL_MIN <= x <= PUBLISHABLE_SL_MAX)
                w.add_row(
                    str(key), f"{v[len(v)//2]:.3f}%", f"{ok:,}", f"{ok/len(v):.1%}"
                )
            console.print(w)
            console.print(
                "[dim]이 표는 '가장 많이 나오는 창'을 고르라는 뜻이 아닙니다. "
                "숫자를 최대화하는 값을 고르면 이 3.5개월에 커브 피팅하는 것입니다.[/dim]"
            )
        if r.scores:
            sc = sorted(r.scores)
            console.print(
                f"[bold]신뢰도 점수[/bold]  중앙값 {sc[len(sc)//2]} · "
                f"최고 {sc[-1]} · 55점 이상 {sum(1 for v in sc if v >= 55)}건 / {len(sc)}"
            )

    _run(go())


@app.command()
def research(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    start: str = typer.Option(..., "--start", help="시작일 YYYY-MM-DD"),
    end: str = typer.Option(None, "--end"),
    step: int = typer.Option(5, "--step"),
    regimes: str = typer.Option("RTH,PRE", "--regimes"),
    data: str = typer.Option(None, "--data", help="th export 로 만든 CSV 폴더"),
) -> None:
    """**우위가 있기는 한가**를 검정한다. 백테스트가 마이너스일 때 먼저 볼 것.

    손익을 최대화하지 않는다. 네 가지만 본다.
    1) (목표,손절) 격자에 양수 칸이 하나라도 있는가
    2) 방향을 뒤집으면 나빠지는가  3) 진입 후 순행>역행 비율이 50%를 넘는가
    4) 종목 자체가 추세형인가 무작위인가
    """
    _boot()
    sym = _sym(symbol)

    async def go():
        from datetime import timedelta as _td

        from th.backtest.models import BacktestConfig
        from th.backtest.research import (
            Path as RPath,
        )
        from th.backtest.research import (
            direction_control,
            edge_signature,
            evaluate,
            random_walk_efficiency,
            series_character,
        )
        from th.backtest.runner import (
            FORWARD_BARS,
            iter_evaluations,
            load_series,
            parse_day,
        )
        from th.backtest.simulate import find_fill

        begin = parse_day(start)
        finish = (
            parse_day(end) + _td(days=1) if end else datetime.now(UTC) - _td(days=1)
        )
        cfg = BacktestConfig(
            step_minutes=step,
            regimes=tuple(r.strip().upper() for r in regimes.split(",") if r.strip()),
        )
        ds = _load_csv_dataset(data)
        pp = vp = None
        if ds is not None:
            series = list(ds.bars)
            pp = lambda at, price: ds.perp_at(at, price)      # noqa: E731
            vp = ds.median_volume_5m
        else:
            series = await load_series(sym, begin, finish)

        console.print("[dim]신호마다 '손절 없이 본 경로'를 뽑습니다. 느립니다.[/dim]")
        paths: list = []
        meta: list = []
        n = 0
        async for ev in iter_evaluations(
            sym, series, begin, finish, cfg, perp_provider=pp, volume_provider=vp
        ):
            n += 1
            if n % 1000 == 0:
                console.print(f"  [dim]{ev.at:%Y-%m-%d} · 평가 {n:,}[/dim]")
            a = ev.analysis
            if a is None or a.signal is None or a.signal.plan is None:
                continue
            plan = a.signal.plan
            signal_at = ev.at - _td(minutes=1)
            fwd = [
                b for b in series[ev.bar_index : ev.bar_index + FORWARD_BARS]
                if b.ts > signal_at
            ]
            hit = find_fill(plan, fwd, cfg.entry_timeout_bars)
            if hit is None:
                continue
            idx, trigger = hit
            long = plan.direction == "long"
            stop_at = fwd[idx].ts + _td(minutes=cfg.max_hold_minutes)
            if ev.session.regime_ends_at is not None:
                stop_at = min(stop_at, ev.session.regime_ends_at)
            # 슬리피지는 outcome_r 안에서 방향별로 적용한다 (뒤집기 컨트롤 대칭 보장)
            paths.append(RPath.from_bars(
                [b for b in fwd[idx:] if b.ts <= stop_at],
                trigger, long, cfg.costs.slippage_pct,
            ))
            meta.append((a.signal.result.setup_id, ev.session.regime.value))

        if not paths:
            console.print("[yellow]관측할 신호가 없습니다[/yellow]")
            return
        console.print(f"\n관측 {len(paths):,}건 (손절 없이 관찰)")

        targets = [0.8, 1.0, 1.5, 2.0]
        stops = [0.5, 0.7, 1.0, 1.5]
        g = Table(title="기대값(R) — 행: 손절% · 열: 목표%")
        g.add_column("손절")
        for t in targets:
            g.add_column(f"{t:.1f}%", justify="right")
        positive = 0
        for s_ in stops:
            row = [f"{s_:.1f}%"]
            for t in targets:
                c = evaluate(paths, t, s_)
                if c.expectancy_r > 0:
                    positive += 1
                    row.append(f"[green]{c.expectancy_r:+.3f}[/green]")
                else:
                    row.append(f"{c.expectancy_r:+.3f}")
            g.add_row(*row)
        console.print(g)
        console.print(
            f"양수 칸 {positive}/{len(targets)*len(stops)}"
            + ("" if positive else "  [red]— 어떤 조합으로도 이익이 나지 않습니다[/red]")
        )

        fwd_c, rev_c = direction_control(paths)
        console.print("\n[bold]대조군 — 방향을 뒤집으면[/bold]")
        console.print(f"  신호 방향   기대값 {fwd_c.expectancy_r:+.3f}R · 승률 {fwd_c.win_rate:.0%}")
        console.print(f"  정반대 방향 기대값 {rev_c.expectancy_r:+.3f}R · 승률 {rev_c.win_rate:.0%}")
        if rev_c.expectancy_r >= fwd_c.expectancy_r - 0.05:
            console.print("  [red]→ 뒤집어도 같거나 낫습니다. 방향성 우위가 없습니다.[/red]")

        sig = edge_signature(paths)
        console.print(
            f"\n[bold]진입 후 순행>역행 비율[/bold] {sig:.1%}  "
            + ("[red](50% 근처 = 우위 없음)[/red]" if 0.45 < sig < 0.55 else "")
        )

        console.print(f"\n[bold]비용만 잃는 선[/bold] {fwd_c.cost_only_r:+.3f}R "
                      f"· 실측 {fwd_c.expectancy_r:+.3f}R"
                      + ("  [red]→ 정확히 비용만큼 잃고 있습니다[/red]"
                         if fwd_c.looks_like_no_edge else ""))

        c = Table(title="종목 성격")
        for col in ("시간대", "봉 수", "자기상관", "효율성비율", "해석"):
            c.add_column(col, justify="right" if col != "해석" else "left")
        for tf in (1, 5, 15, 30):
            ch = series_character(series, tf)
            c.add_row(
                f"{tf}분", f"{ch.n:,}",
                f"{ch.autocorr:+.4f}" if ch.autocorr is not None else "-",
                f"{ch.efficiency_median:.3f}" if ch.efficiency_median else "-",
                ch.reading_ko,
            )
        console.print(c)
        console.print(
            f"[dim]무작위 보행의 효율성비율 기준선 ≈ {random_walk_efficiency(20):.3f}. "
            "관측값이 이 근처면 추세도 회귀도 아니라는 뜻입니다.[/dim]"
        )

        by = Table(title="셋업별 · 레짐별 (손절 1.0% · 목표 1.0%)")
        for col in ("구분", "n", "승률", "기대값"):
            by.add_column(col, justify="right" if col != "구분" else "left")
        for key in sorted({m[0] for m in meta}) + sorted({m[1] for m in meta}):
            sub = [p for p, m in zip(paths, meta) if key in m]
            if not sub:
                continue
            cell = evaluate(sub, 1.0, 1.0)
            by.add_row(key, f"{cell.n}", f"{cell.win_rate:.0%}", f"{cell.expectancy_r:+.3f}R")
        console.print(by)

    _run(go())


@app.command("gate-value")
def gate_value(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    start: str = typer.Option(..., "--start", help="시작일 YYYY-MM-DD"),
    end: str = typer.Option(None, "--end"),
    step: int = typer.Option(5, "--step"),
    regimes: str = typer.Option("RTH,PRE", "--regimes"),
    data: str = typer.Option(None, "--data", help="th export 로 만든 CSV 폴더"),
) -> None:
    """**세션 게이트가 실제로 값을 하는가**를 역검정한다.

    게이트를 끄고 같은 셋업을 돌려, 원래 막혔을 신호와 통과했을 신호의 성적을
    비교한다. 막힌 쪽이 더 나쁘면 게이트가 지켜준 것이고, 같으면 미신이다.

    신호 기능을 빼더라도 게이트는 남는다. 그래서 이 검정이 신호 검정보다 중요하다.
    """
    _boot()
    sym = _sym(symbol)

    async def go():
        from dataclasses import replace as _replace
        from datetime import timedelta as _td

        from th.backtest import runner as _runner
        from th.backtest.metrics import wilson_ci
        from th.backtest.models import BacktestConfig
        from th.backtest.research import Path as RPath
        from th.backtest.research import evaluate
        from th.backtest.runner import FORWARD_BARS, iter_evaluations, load_series, parse_day
        from th.backtest.simulate import find_fill
        from th.sessions.gate import get_context as _real_gate

        begin = parse_day(start)
        finish = parse_day(end) + _td(days=1) if end else datetime.now(UTC) - _td(days=1)
        cfg = BacktestConfig(
            step_minutes=step,
            regimes=tuple(r.strip().upper() for r in regimes.split(",") if r.strip()),
        )
        ds = _load_csv_dataset(data)
        pp = vp = None
        if ds is not None:
            series = list(ds.bars)
            pp = lambda at, price: ds.perp_at(at, price)      # noqa: E731
            vp = ds.median_volume_5m
        else:
            series = await load_series(sym, begin, finish)

        def ungated(s_, at_):
            """판정은 그대로 하되 차단만 푼다 — 어느 규칙이 막았는지는 남긴다."""
            return _replace(_real_gate(s_, at_), blocked=False)

        original = _runner.get_context
        _runner.get_context = ungated
        rows = []
        try:
            console.print("[dim]게이트를 끄고 같은 셋업을 돌립니다. 느립니다.[/dim]")
            async for ev in iter_evaluations(
                sym, series, begin, finish, cfg, perp_provider=pp, volume_provider=vp
            ):
                a = ev.analysis
                if a is None or a.signal is None or a.signal.plan is None:
                    continue
                plan = a.signal.plan
                signal_at = ev.at - _td(minutes=1)
                fwd = [
                    b for b in series[ev.bar_index : ev.bar_index + FORWARD_BARS]
                    if b.ts > signal_at
                ]
                hit = find_fill(plan, fwd, cfg.entry_timeout_bars)
                if hit is None:
                    continue
                idx, trigger = hit
                long = plan.direction == "long"
                stop_at = fwd[idx].ts + _td(minutes=cfg.max_hold_minutes)
                if ev.session.regime_ends_at is not None:
                    stop_at = min(stop_at, ev.session.regime_ends_at)
                rows.append({
                    "path": RPath.from_bars(
                        [b for b in fwd[idx:] if b.ts <= stop_at],
                        trigger, long, cfg.costs.slippage_pct,
                    ),
                    "blocks": tuple(b.reason_ko for b in ev.session.blocks),
                    "penalties": tuple(p.reason_ko for p in ev.session.penalties),
                })
        finally:
            _runner.get_context = original

        if not rows:
            console.print("[yellow]관측할 신호가 없습니다[/yellow]")
            return

        def row_for(tag, sub, table):
            if not sub:
                return None
            c = evaluate([r["path"] for r in sub], 1.0, 1.0)
            lo, hi = wilson_ci(round(c.win_rate * c.n), c.n)
            table.add_row(tag[:40], f"{c.n}", f"{c.win_rate:.0%} ({lo:.0%}~{hi:.0%})",
                          f"{c.expectancy_r:+.3f}R")
            return c

        blocked = [r for r in rows if r["blocks"]]
        allowed = [r for r in rows if not r["blocks"]]

        t = Table(title="게이트가 막았을 신호 vs 통과시킨 신호")
        for col in ("구분", "n", "승률 (95% CI)", "기대값"):
            t.add_column(col, justify="right" if col != "구분" else "left")
        cb = row_for("게이트가 막았을 신호", blocked, t)
        ca = row_for("게이트가 통과시킨 신호", allowed, t)
        console.print(t)
        if cb and ca:
            d = cb.expectancy_r - ca.expectancy_r
            verdict = (
                "[green]막은 쪽이 더 나빴습니다 — 게이트가 지켜줬습니다[/green]" if d < -0.05
                else "[yellow]차이가 없습니다 — 이 규칙들은 근거가 약합니다[/yellow]"
                if abs(d) <= 0.05
                else "[red]막은 쪽이 오히려 나았습니다 — 기회를 버리고 있습니다[/red]"
            )
            console.print(f"차이 {d:+.3f}R  →  {verdict}")

        rules = sorted({b for r in blocked for b in r["blocks"]})
        if rules:
            t2 = Table(title="차단 규칙별")
            for col in ("규칙", "n", "승률 (95% CI)", "기대값"):
                t2.add_column(col, justify="right" if col != "규칙" else "left")
            for rule in rules:
                row_for(rule, [r for r in blocked if rule in r["blocks"]], t2)
            console.print(t2)

        pens = sorted({p for r in rows for p in r["penalties"]})
        if pens:
            t3 = Table(title="페널티 규칙별 (점수를 깎는 규칙 — 실제로 나쁜 구간인가)")
            for col in ("규칙", "n", "승률 (95% CI)", "기대값"):
                t3.add_column(col, justify="right" if col != "규칙" else "left")
            for rule in pens:
                row_for(rule, [r for r in rows if rule in r["penalties"]], t3)
            row_for("페널티 없음 (기준선)", [r for r in rows if not r["penalties"]], t3)
            console.print(t3)
            console.print(
                "[dim]기준선보다 나쁘지 않은 페널티 규칙은 근거가 없다는 뜻입니다. "
                "다만 표본이 작으면 신뢰구간이 겹치니 바로 바꾸지 말고 기록만 하세요.[/dim]"
            )

    _run(go())


@app.command()
def indicators(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    at: str = typer.Option(None, "--at", help="ISO 시각 (KST). 생략하면 지금"),
) -> None:
    """지표값을 그대로 찍는다. **TradingView 와 눈으로 대조하는 용도.**"""
    _boot()
    sym = _sym(symbol)

    async def go():
        from th.sessions.gate import get_context
        from th.strategy import data as sdata
        from th.strategy.features import build_features

        when = _parse_at(at)
        bars = await sdata.load_bars(sym, when)
        if len(bars) < 60:
            console.print(f"[red]봉이 {len(bars)}개뿐입니다[/red]")
            return
        session = get_context(sym, when)
        perp = await sdata.load_perp_context(sym, when)
        f = build_features(sym, bars, when, session, perp)

        t = Table(title=f"{sym} · {f.bars_1m[-1].ts:%Y-%m-%d %H:%M} UTC 마지막 확정봉")
        t.add_column("지표"); t.add_column("값", justify="right")
        rows = [
            ("종가", f"{f.price:,.4f}"),
            ("세션 VWAP", f"{f.vwap.vwap:,.4f}" if f.vwap else "-"),
            ("EMA9 (5m)", f"{f.ema9_5m:,.4f}" if f.ema9_5m else "-"),
            ("EMA21 (5m)", f"{f.ema21_5m:,.4f}" if f.ema21_5m else "-"),
            ("EMA9 (15m)", f"{f.ema9_15m:,.4f}" if f.ema9_15m else "-"),
            ("EMA21 (15m)", f"{f.ema21_15m:,.4f}" if f.ema21_15m else "-"),
            ("RSI14 (5m)", f"{f.rsi14_5m:,.2f}" if f.rsi14_5m else "-"),
            ("RSI9 (1m)", f"{f.rsi9_1m:,.2f}" if f.rsi9_1m else "-"),
            ("ATR14 (1m)", f"{f.atr_1m:,.4f}" if f.atr_1m else "-"),
            ("ATR %", f"{f.atr_pct:,.3f}%" if f.atr_pct else "-"),
            ("거래량 (5m)", f"{f.volume_5m:,.1f}" if f.volume_5m else "-"),
        ]
        for k, v in rows:
            t.add_row(k, v)
        console.print(t)
        console.print(
            "[dim]TradingView 에서 SOXLUSDT.P · 같은 시각 봉으로 비교하세요. "
            "VWAP 은 'Session' 앵커, EMA 는 종가 기준입니다.[/dim]"
        )

    _run(go())


# ─────────────────────────────────────────────────────────
# 자동매매 실행 계층 (Phase 5')
#
# ⚠️ 실거래는 잠겨 있다. `TH_EXECUTION_MODE=live` 로 두면 시작할 때 거부한다.
#    여는 조건은 src/th/execution/live.py 의 GATE_CONDITIONS 참고.

paper_app = typer.Typer(
    help="페이퍼 트레이딩 — 실제 시세로 체결을 흉내낸다. 돈은 나가지 않는다",
    no_args_is_help=True,
)
app.add_typer(paper_app, name="paper")

journal_app = typer.Typer(
    help="손으로 한 매매 기록 — 쌓이면 그 판단이 무작위보다 나은지 검정할 수 있다",
    no_args_is_help=True,
)
app.add_typer(journal_app, name="journal")


def _require_paper_mode(*, allow_none: bool = False) -> None:
    """paper 명령 앞에 반드시 부른다.

    `live` 는 어떤 명령에서도 통과시키지 않는다. 조회만 하는 명령이라도 마찬가지다 —
    실거래 경로를 '조금이라도' 밟는 습관이 생기면 안 된다.

    `allow_none` 은 주문을 만들지 않는 사전 점검(`paper check`)에만 쓴다.
    그 경우 실행 모드 문제는 안전장치(`guard`)가 이유 목록에 넣어 보여준다.
    """
    mode = get_settings().execution_mode
    if mode == "live":
        from .execution.live import GATE_CONDITIONS
        console.print("[bold red]실거래는 아직 잠겨 있습니다.[/bold red]")
        for c in GATE_CONDITIONS:
            console.print(f"  · {c}")
        console.print("\n[dim].env 에서 TH_EXECUTION_MODE=paper 로 바꾸세요.[/dim]")
        raise typer.Exit(1)
    if mode != "paper" and not allow_none:
        console.print(
            f"[yellow]실행 모드가 '{mode}' 입니다.[/yellow] "
            ".env 에 [bold]TH_EXECUTION_MODE=paper[/bold] 를 넣으세요."
        )
        raise typer.Exit(1)


async def _daily_r_for_guard(mode: str | None = None) -> tuple[float, bool, bool]:
    """(오늘 실현손익 R, 모름 여부, 잠금 여부).

    못 읽었으면 '모름' 을 돌려주고, 안전장치가 그걸로 막는다.
    0.0 으로 뭉개면 '한도에 안 걸림'이 되어 잠금이 풀린다.

    페이퍼 모드는 바이낸스 income 이 아니라 **페이퍼 브래킷의 결과**가 오늘 손익이다.
    실계좌 손익으로 페이퍼를 막거나 풀면 둘 다 틀린다.
    """
    mode = mode or get_settings().execution_mode
    if mode == "paper":
        from th.execution.runner import paper_daily_r

        return await paper_daily_r()
    try:
        from th.account.risk import DAILY_LOSS_LIMIT_R
        from th.account.service import get_risk_view

        view = await get_risk_view(with_market=False)
        if view.risk.errors or view.risk.daily is None:
            return 0.0, True, False
        d = view.risk.daily
        # 기록된 잠금(limit_hit_at)은 R 값이 되돌아와도 그날은 유효하다
        return d.r_value, False, d.locked(DAILY_LOSS_LIMIT_R)
    except Exception:
        log.debug("당일 손익 확인 실패", exc_info=True)
        return 0.0, True, False


def _paper_direction(direction: str) -> str:
    d = direction.upper()
    if d not in ("LONG", "SHORT"):
        console.print(f"[red]방향은 LONG 또는 SHORT 여야 합니다:[/red] {direction!r}")
        raise typer.Exit(1)
    return d



@paper_app.command("check")
def paper_check(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    direction: str = typer.Option("LONG", "--dir", help="LONG | SHORT"),
    entry: float = typer.Option(..., "--entry", "-e", help="진입 예상가"),
    stop: float = typer.Option(..., "--stop", help="손절가 (필수)"),
    tp1: float = typer.Option(None, "--tp1"),
    tp2: float = typer.Option(None, "--tp2"),
    equity: float = typer.Option(10_000.0, "--equity", help="계좌 자본 USDT"),
) -> None:
    """주문을 내지 않고 **안전장치만 돌려본다.** 무엇이 막는지 먼저 본다."""
    _boot()
    _require_paper_mode(allow_none=True)   # 주문을 안 내므로 none 에서도 돌려볼 수 있다
    from .execution import Bracket, GuardInput, OrderType, check, new_id
    from .execution import notional_leverage, size_from_risk

    s = get_settings()
    sym = (symbol or s.symbols[0]).upper()
    direction = _paper_direction(direction)
    qty = size_from_risk(equity, s.risk_pct, entry, stop)
    b = Bracket(
        bracket_id=new_id("br"), symbol=sym, direction=direction, qty=qty,
        stop_price=stop, entry_type=OrderType.LIMIT, entry_price=entry,
        tp1_price=tp1, tp2_price=tp2, reason="th paper check",
    )
    daily_r, unknown, locked = _run(_daily_r_for_guard())
    r = check(GuardInput(bracket=b, ref_price=entry, execution_mode=s.execution_mode,
                         equity_usdt=equity, risk_pct=s.risk_pct,
                         daily_realized_r=daily_r, daily_unknown=unknown,
                         daily_locked=locked))

    t = Table(title=f"{sym} {direction.upper()} — 사전 점검", show_header=False)
    t.add_column("", style="dim"); t.add_column("")
    t.add_row("진입", f"{entry:,.4f}")
    t.add_row("손절", f"{stop:,.4f}  ({b.stop_pct(entry):.2f}%)")
    if tp1:
        t.add_row("TP1", f"{tp1:,.4f}  ({abs(tp1-entry)/abs(entry-stop):.2f}R)")
    if tp2:
        t.add_row("TP2", f"{tp2:,.4f}  ({abs(tp2-entry)/abs(entry-stop):.2f}R)")
    t.add_row("수량", f"{qty:,.4f}  (1R = {equity*s.risk_pct/100:,.2f} USDT)")
    t.add_row("명목", f"{qty*entry:,.2f} USDT")
    t.add_row("실효 배율", f"{notional_leverage(qty, entry, equity):.2f}배")
    console.print(t)

    if r.ok:
        console.print("[bold green]통과[/bold green] — 안전장치에 걸리는 것 없음")
    else:
        console.print("[bold red]막힘[/bold red]")
        for x in r.reasons:
            console.print(f"  · {x}")


@paper_app.command("status")
def paper_status() -> None:
    """열려 있는 브래킷과 지금까지 성적."""
    _boot()
    _require_paper_mode()

    async def go():
        from .execution import store
        opens = await store.load_open_brackets("paper")
        hist = await store.history(limit=20, mode="paper")

        if opens:
            t = Table(title="열려 있는 브래킷")
            for c in ("ID", "심볼", "방향", "수량", "진입", "손절", "주문"):
                t.add_column(c)
            for st in opens:
                b = st.bracket
                live = sum(1 for o in st.orders.values() if o.is_open)
                has_stop = "✅" if st.live_stop else "[bold red]❌ 손절 없음[/bold red]"
                t.add_row(b.bracket_id[-8:], b.symbol, b.direction,
                          f"{st.position_qty:+.4f}",
                          f"{st.avg_entry:,.4f}" if st.avg_entry else "-",
                          f"{b.stop_price:,.4f} {has_stop}", str(live))
            console.print(t)
        else:
            console.print("[dim]열려 있는 브래킷 없음[/dim]")

        if not hist:
            console.print("[dim]종료된 매매 없음[/dim]")
            return
        wins = [h for h in hist if (h["realized_r"] or 0) > 0]
        total_r = sum(h["realized_r"] or 0 for h in hist)
        t2 = Table(title=f"최근 {len(hist)}건")
        for c in ("종료", "심볼", "방향", "사유", "R", "손익"):
            t2.add_column(c)
        for h in hist[:10]:
            r = h["realized_r"] or 0
            t2.add_row(
                h["closed_at"].astimezone(ZoneInfo("Asia/Seoul")).strftime("%m-%d %H:%M"),
                h["symbol"], h["direction"], h["close_reason"],
                Text(f"{r:+.3f}", style="green" if r > 0 else "red"),
                f"{h['realized_pnl'] or 0:+,.2f}",
            )
        console.print(t2)
        console.print(
            f"승률 [bold]{len(wins)/len(hist)*100:.0f}%[/bold] ({len(wins)}/{len(hist)}) · "
            f"기대값 [bold]{total_r/len(hist):+.3f}R[/bold] · 누적 [bold]{total_r:+.2f}R[/bold]"
        )
        console.print(
            "[dim]표본이 30건 미만이면 이 숫자로 아무것도 판단하지 마세요.[/dim]"
        )

    _run(go())


@journal_app.command("add")
def journal_add(
    entry: float = typer.Option(..., "--entry", "-e"),
    stop: float = typer.Option(None, "--stop"),
    direction: str = typer.Option("LONG", "--dir"),
    symbol: str = typer.Option(None, "--symbol", "-s"),
    qty: float = typer.Option(None, "--qty"),
    reason: str = typer.Option("", "--reason", "-r", help="왜 잡았는지 (제일 중요)"),
    tag: str = typer.Option("", "--tag", help="ema99_support 같은 짧은 꼬리표"),
) -> None:
    """방금 잡은 매매를 기록한다."""
    _boot()

    async def go():
        from .execution import store
        sym = (symbol or get_settings().symbols[0]).upper()
        i = await store.add_journal(
            symbol=sym, direction=direction.upper(), entry_at=datetime.now(UTC),
            entry_price=entry, stop_price=stop, qty=qty, reason=reason, setup_tag=tag,
        )
        console.print(f"기록됨 [bold]#{i}[/bold] — {sym} {direction.upper()} @ {entry:,.4f}")
        if not stop:
            console.print("[yellow]손절가를 안 적었습니다.[/yellow] "
                          "나중에 R 로 환산이 안 돼서 분석에 못 씁니다.")
        console.print(f"[dim]청산하면: th journal close {i} --exit <가격>[/dim]")

    _run(go())


@journal_app.command("close")
def journal_close(
    entry_id: int = typer.Argument(...),
    exit_price: float = typer.Option(..., "--exit", "-x"),
    note: str = typer.Option("", "--note", "-n"),
) -> None:
    """기록해둔 매매를 청산 처리한다."""
    _boot()

    async def go():
        from .execution import store
        ok = await store.close_journal(
            entry_id, exit_at=datetime.now(UTC), exit_price=exit_price, note=note
        )
        console.print(f"#{entry_id} 청산 기록됨" if ok else f"[red]#{entry_id} 를 못 찾음[/red]")

    _run(go())


@journal_app.command("list")
def journal_list(limit: int = typer.Option(30, "--limit", "-n")) -> None:
    """기록한 매매 목록과 지금까지의 성적."""
    _boot()

    async def go():
        from .execution import store
        rows = await store.journal_entries(limit)
        if not rows:
            console.print("[dim]기록 없음 — th journal add 로 시작하세요[/dim]")
            return
        t = Table(title=f"매매 기록 {len(rows)}건")
        for c in ("#", "진입시각", "심볼", "방향", "진입", "청산", "R", "꼬리표", "이유"):
            t.add_column(c)
        for r in rows:
            rv = r["outcome_r"]
            t.add_row(
                str(r["id"]),
                r["entry_at"].astimezone(ZoneInfo("Asia/Seoul")).strftime("%m-%d %H:%M"),
                r["symbol"], r["direction"], f"{r['entry_price']:,.4f}",
                f"{r['exit_price']:,.4f}" if r["exit_price"] else "[dim]보유중[/dim]",
                Text(f"{rv:+.2f}", style="green" if rv > 0 else "red") if rv is not None else "-",
                r["setup_tag"] or "-", (r["reason"] or "")[:24],
            )
        console.print(t)
        done = [r for r in rows if r["outcome_r"] is not None]
        if len(done) >= 5:
            tot = sum(r["outcome_r"] for r in done)
            wins = sum(1 for r in done if r["outcome_r"] > 0)
            console.print(
                f"청산 {len(done)}건 · 승률 {wins/len(done)*100:.0f}% · "
                f"기대값 [bold]{tot/len(done):+.3f}R[/bold]"
            )
            if len(done) < 30:
                console.print("[dim]30건은 넘어야 의미 있는 숫자가 됩니다.[/dim]")

    _run(go())


@paper_app.command("run")
def paper_run(
    entry: float = typer.Option(None, "--entry", "-e", help="지정가 진입가 (생략하면 시장가)"),
    stop: float = typer.Option(..., "--stop", help="손절가 (필수)"),
    tp1: float = typer.Option(None, "--tp1"),
    tp2: float = typer.Option(None, "--tp2"),
    direction: str = typer.Option("LONG", "--dir", help="LONG | SHORT"),
    symbol: str = typer.Option(None, "--symbol", "-s"),
    equity: float = typer.Option(10_000.0, "--equity", help="계좌 자본 USDT"),
    reason: str = typer.Option("", "--reason", "-r", help="왜 잡았는지 — 나중에 이게 전부다"),
    interval: int = typer.Option(3, "--interval", "-i", help="시세 확인 간격(초)"),
    expire: int = typer.Option(300, "--expire", help="지정가가 안 붙으면 몇 초 뒤 취소"),
) -> None:
    """페이퍼로 한 자리를 잡고 **끝날 때까지 지켜본다.** 돈은 나가지 않는다.

    `th stream` 이 돌고 있어야 시세가 들어옵니다. Ctrl+C 로 중단하면 포지션은
    DB 에 남고, `th paper resume` 으로 이어서 볼 수 있습니다.
    """
    _boot()
    _require_paper_mode()
    sym = _sym(symbol)
    direction = _paper_direction(direction)

    async def go():
        from th.execution import store as exec_store
        from th.execution.manager import BracketManager
        from th.execution.paper import PaperClient
        from th.execution.runner import LeaseHeld, OpenRequest, PaperRunner

        s = get_settings()
        client = PaperClient()
        mgr = BracketManager(client, store=exec_store)
        runner = PaperRunner(mgr, client, s.symbols, interval, holder="cli",
                             execution_mode=s.execution_mode)
        # th serve 가 페이퍼 러너를 내장하고 있으면 같은 브래킷을 둘이 관리하게 된다 — 여기서 멈춘다
        try:
            await runner.acquire()
        except LeaseHeld as e:
            console.print(f"[yellow]{e}[/yellow]")
            raise typer.Exit(1) from None

        # 주문 경로는 웹과 **완전히 같다** — size_from_risk → round → GuardInput → mgr.open()
        out = await runner.open_from_request(OpenRequest(
            mode="paper", symbol=sym, direction=direction, entry=entry, stop=stop,
            tp1=tp1, tp2=tp2, expire_s=expire, reason=reason, equity_usdt=equity,
        ))
        p = out.preview
        for w in (p.warnings if p else []):
            console.print(f"[yellow]· {w}[/yellow]")
        if not out.ok:
            await runner.release()
            console.print("[bold red]막힘[/bold red]")
            for x in out.reasons:
                console.print(f"  · {x}")
            raise typer.Exit(1)

        console.print(
            f"[green]페이퍼 진입[/green] {sym} {direction} "
            f"수량 {p.qty:,.4f} · 손절 {p.guard_input.bracket.stop_price:,.4f} · "
            f"1R = {p.r_unit_usdt:,.2f} USDT · 손절폭 {p.stop_pct:.2f}% · 비용 {p.cost_r:.2f}R\n"
            "[dim]Ctrl+C 로 중단 (포지션은 DB 에 남습니다)[/dim]"
        )
        await _paper_watch_loop(runner)

    _run(go())


@paper_app.command("resume")
def paper_resume(
    symbol: str = typer.Option(None, "--symbol", "-s"),
    interval: int = typer.Option(3, "--interval", "-i"),
) -> None:
    """중단했던 페이퍼 포지션을 DB 에서 되살려 이어서 지켜본다."""
    _boot()
    _require_paper_mode()
    sym = _sym(symbol)

    async def go():
        from th.execution import store as exec_store
        from th.execution.runner import LeaseHeld, build_paper_runner

        s = get_settings()
        opens = await exec_store.load_open_brackets("paper")
        if not opens:
            console.print("[dim]되살릴 브래킷이 없습니다.[/dim]")
            return
        # 저장돼 있던 주문을 체결 엔진에 다시 올리고, 손절이 빠진 포지션은 다시 건다
        runner = await build_paper_runner(s.symbols, focus=sym, interval=interval, holder="cli",
                                          execution_mode=s.execution_mode)
        try:
            await runner.acquire()
        except LeaseHeld as e:
            console.print(f"[yellow]{e}[/yellow]")
            raise typer.Exit(1) from None
        console.print(f"[green]{runner.open_count}개 브래킷 복구[/green] — 이어서 지켜봅니다")
        await _paper_watch_loop(runner)

    _run(go())


async def _paper_watch_loop(runner) -> None:
    """시세를 먹이고 체결을 처리하는 루프 — `th serve` 의 내장 러너와 **같은 코드**(`runner.step`).
    여기서는 화면에 찍는 훅만 건다."""
    mgr = runner.mgr

    def on_quote(q):
        for st in list(mgr.states.values()):
            if st.bracket.symbol == q.symbol and st.has_position and st.avg_entry:
                risk = st.bracket.risk_per_unit(st.avg_entry)
                sign = 1 if st.bracket.direction == "LONG" else -1
                r = sign * (q.mid - st.avg_entry) / risk if risk else 0.0
                console.print(
                    f"  {q.at:%H:%M:%S} {q.symbol} {q.mid:,.4f} · 평가 {r:+.2f}R "
                    f"· 손절 {st.bracket.stop_price:,.4f}",
                    highlight=False,
                )

    def on_stale(symbol: str, stale: bool):
        if stale:
            console.print(f"[yellow]{symbol} 시세가 끊겼습니다 — 체결 판정을 멈춥니다[/yellow]")
        else:
            console.print(f"[green]{symbol} 시세 복구[/green]")

    def on_closed(ids: list[str]):
        for bracket_id in ids:
            console.print(f"[bold]웹 청산 요청[/bold] {bracket_id[-8:]} — 시장가로 정리했습니다")

    runner.on_quote, runner.on_stale, runner.on_closed = on_quote, on_stale, on_closed
    await runner.run_until_flat()

    for st in mgr.closed:
        console.print(
            f"[bold]종료[/bold] {st.bracket.bracket_id[-8:]} — {st.close_reason} · "
            f"[{'green' if st.realized_r > 0 else 'red'}]{st.realized_r:+.3f}R[/] "
            f"({st.realized_pnl:+,.2f} USDT)"
        )
    sm = mgr.summary()
    if sm["closed"]:
        console.print(
            f"이번 세션 {sm['closed']}건 · 승률 {sm['win_rate']*100:.0f}% · "
            f"기대값 [bold]{sm['expectancy_r']:+.3f}R[/bold] · "
            f"누적 {sm['total_r']:+.2f}R ({sm['total_pnl']:+,.2f} USDT)"
        )
        if sm["closed"] < 30:
            console.print("[dim]30건은 넘어야 의미 있는 숫자가 됩니다.[/dim]")


# ─────────────────────────────────────────────────────────
# Phase 5 — 웹 대시보드


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="0.0.0.0 이면 같은 와이파이의 폰에서도 접속"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload", help="개발용 자동 재시작"),
) -> None:
    """웹 대시보드를 띄운다. `th watch` 와 같은 값을 브라우저·폰에서 본다."""
    _boot()
    try:
        import uvicorn
    except ImportError:
        console.print(
            "[red]웹 서버 패키지가 없습니다.[/red]\n"
            '  [bold]pip install -e ".[web]"[/bold] 를 먼저 실행하세요.'
        )
        raise typer.Exit(1) from None

    s = get_settings()
    if s.execution_mode == "paper":
        console.print("[dim]페이퍼 엔진을 내장해서 띄웁니다 — th paper run 은 이 서버가 떠 있는 동안 막힙니다.[/dim]")
    console.print(
        f"[bold green]대시보드 시작[/bold green] — {s.symbols[0]}\n"
        f"  이 컴퓨터  [bold]http://localhost:{port}[/bold]\n"
        f"  같은 와이파이의 폰  http://<이 PC의 IP>:{port}\n"
        "[dim]Ctrl+C 로 종료. 방향 제시는 하지 않습니다 — 금지 사유만 보여줍니다.[/dim]"
    )
    uvicorn.run(
        "th.api.server:create_app", host=host, port=port, reload=reload,
        factory=True, log_level=s.log_level.lower(),
    )


if __name__ == "__main__":
    app()
