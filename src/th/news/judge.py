"""Claude 판단 계층 (docs/17 §4).

세 단계: 프리필터(무료, 수집기가 함) → 트리아지(저가 모델) → 본판단(구조화 출력 `Judgment`).
**판단이 없으면 매매도 없다.** 거절·파싱 실패·예외는 전부 `error` 로 기록되고 그 뉴스는 건너뛴다.

`anthropic` 은 함수 안에서만 임포트한다. 테스트는 가짜 클라이언트를 넣는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from ..logging_setup import get_logger
from . import tickers
from .models import Judgment, NewsItem, Triage

log = get_logger(__name__)

# USD / 1M 토큰: (입력, 출력, 캐시 쓰기, 캐시 읽기). 2026-06 기준 1st-party 요금.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (5.0, 25.0, 6.25, 0.5),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 0.5),
    "claude-opus-4-7": (5.0, 25.0, 6.25, 0.5),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 0.2),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.3),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.1),
}
_DEFAULT_PRICE = (5.0, 25.0, 6.25, 0.5)     # 모르는 모델은 비싸게 센다 — 상한이 조용히 풀리면 안 된다

TRIAGE_MAX_TOKENS = 1024
FULL_MAX_TOKENS = 4096


def usage_dict(u: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = getattr(u, k, None) if not isinstance(u, dict) else u.get(k)
        if v:
            out[k] = int(v)
    return out


def cost_of(model: str, usage: dict[str, int]) -> float:
    pi, po, pw, pr = PRICES.get(model, _DEFAULT_PRICE)
    return (usage.get("input_tokens", 0) * pi + usage.get("output_tokens", 0) * po
            + usage.get("cache_creation_input_tokens", 0) * pw
            + usage.get("cache_read_input_tokens", 0) * pr) / 1_000_000


# ─────────────────────────────────────────────────────────
# 문맥 — 판단에 같이 넣는 시장 상태 (원시 캔들은 넣지 않는다, docs/06 E.1)
# ─────────────────────────────────────────────────────────

@dataclass(slots=True)
class SymbolSnapshot:
    symbol: str
    price: float | None = None
    change_24h_pct: float | None = None
    move_since_news_pct: float | None = None
    regime_ko: str = ""
    blocked: bool = False
    block_reason: str = ""
    next_event_ko: str = ""
    note: str = ""


@dataclass(slots=True)
class JudgeContext:
    now: datetime
    snapshots: list[SymbolSnapshot] = field(default_factory=list)
    recent_titles: list[str] = field(default_factory=list)
    open_campaigns: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"현재 시각(UTC): {self.now.isoformat(timespec='minutes')}"]
        if self.snapshots:
            lines.append("종목 상태:")
            for s in self.snapshots:
                bits = [tickers.ticker_of(s.symbol)]
                if s.price is not None:
                    bits.append(f"현재가 {s.price:g}")
                if s.change_24h_pct is not None:
                    bits.append(f"24h {s.change_24h_pct:+.2f}%")
                if s.move_since_news_pct is not None:
                    bits.append(f"뉴스 이후 {s.move_since_news_pct:+.2f}%")
                if s.regime_ko:
                    bits.append(f"세션 {s.regime_ko}")
                if s.blocked:
                    bits.append(f"진입 차단: {s.block_reason}")
                if s.next_event_ko:
                    bits.append(f"다음 이벤트 {s.next_event_ko}")
                if s.note:
                    bits.append(s.note)
                lines.append("  - " + " · ".join(bits))
        if self.open_campaigns:
            lines.append("열린 작전: " + "; ".join(self.open_campaigns))
        if self.recent_titles:
            lines.append("같은 종목 최근 24h 뉴스:")
            lines.extend(f"  - {t}" for t in self.recent_titles[:8])
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# 프롬프트
# ─────────────────────────────────────────────────────────

def system_rules(*, universe: list[str], margin_max: float, leverage: float, stop_min: float,
                 stop_max: float, hold_max: int) -> str:
    names = ", ".join(f"{tickers.ticker_of(s)}({tickers.display_name(s)})" for s in universe)
    return f"""당신은 바이낸스 선물의 미국 주식 토큰(perp)을 뉴스로 매매하는 데스크의 판단 엔진이다.
입력은 뉴스 한 건과 시장 상태다. 출력은 주어진 JSON 스키마 하나뿐이다. 설명문을 따로 쓰지 않는다.

## 유니버스 (이 밖의 종목은 절대 넣지 않는다 — 심볼은 반드시 …USDT)
{names}

## 돈 규칙 (사용자가 정한 것 — 바꾸지 않는다)
- 작전 하나의 마진 합계 ≤ 시드의 {margin_max:g}%. 배율 {leverage:g}배. 즉 노셔널 ≤ 시드 × {margin_max * leverage / 100:g}.
- 분할 진입은 1~3 다리. 예: 4% 시장가 + 3% 지정가(-1.5%) + 3% 지정가(-3%). 확신이 낮을수록 첫 다리를 작게.
- 손절은 **가격 기준** {stop_min:g}~{stop_max:g}%. 지정가 다리의 오프셋은 손절폭보다 안쪽이어야 한다.
- 목표는 가격 기준 %. 마진 +20%~+100% 가 목표이므로 {leverage:g}배면 가격 +{20 / leverage:g}%~+{100 / leverage:g}%.
  손익비(target_pct / stop_pct) 는 1 이상.
- 보유는 몇 시간에서 최대 {hold_max}시간. 뉴스의 유효 기간(horizon_hours)에 맞춘다.

## 판단 규칙
- 뉴스가 **가격에 아직 반영되지 않았고 방향이 분명할 때만** direction 을 LONG/SHORT 로 둔다. 아니면 NONE.
- "뉴스 이후 변동" 이 이미 목표의 절반 이상 움직였으면 already_priced_in=true, direction=NONE.
- 확신도(confidence) 는 정직하게. 근거가 소문·요약·출처 불명이면 0.5 이하.
- 실적: 헤드라인 숫자보다 가이던스와 시장 기대 대비를 본다. 사전 발표(프리·애프터) 는 반응이 크다.
- 규제·소송·리콜·경영진 교체는 크기(magnitude) 를 보수적으로.
- 세션이 '진입 차단' 이면 계획을 만들어도 진입은 나중이다 — 그래도 판단은 정확히 남긴다.
- 반대 방향의 열린 작전이 있고 이 뉴스가 그 논리를 깨면, 그 사실을 thesis_ko 에 적고 magnitude=high 로 둔다.
- 다른 사람의 매수·매도 추천은 근거가 아니다. 사실(공시·실적·계약·규제)만 근거다.
- 3배 레버리지 ETF(SOXL/SOXS 등)는 기초지수 방향의 3배로 움직인다. 반도체 업종 뉴스는 SOXL/SOXS/SMH 로도 표현할 수 있다.

## 출력
- thesis_ko, risks_ko, invalidation_ko 는 한국어. 3문장 이내.
- plan 은 direction 이 LONG/SHORT 이고 매매할 만할 때만. 그 외 null.
"""


def news_block(item: NewsItem, ctx: JudgeContext) -> str:
    age_min = int((ctx.now - item.ts).total_seconds() // 60)
    hdr = (f"[뉴스] 소스={item.source} 채널={item.channel or '-'} 발행={item.ts.isoformat(timespec='minutes')} "
           f"({age_min}분 전) 매핑종목={','.join(tickers.ticker_of(s) for s in item.symbols) or '-'}")
    body = item.body.strip()
    if len(body) > 6000:
        body = body[:6000] + " …(잘림)"
    parts = [hdr, f"제목: {item.title}"]
    if body:
        parts.append(f"본문: {body}")
    if item.url:
        parts.append(f"링크: {item.url}")
    parts.append("")
    parts.append("[시장 상태]")
    parts.append(ctx.render())
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────
# Judge
# ─────────────────────────────────────────────────────────

@dataclass(slots=True)
class CallRecord:
    """호출 한 번의 기록 — DB 의 news_judgments 한 행이 된다."""

    stage: str
    model: str
    result: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str = ""


class Judge:
    def __init__(self, *, model: str, triage_model: str, universe: list[str], margin_max: float,
                 leverage: float, stop_min: float, stop_max: float, hold_max: int,
                 client: Any = None, api_key: str = "", effort: str | None = None) -> None:
        self.model = model
        self.triage_model = triage_model
        self.effort = effort            # 본판단 사고 깊이 (low|medium|high). Haiku 는 effort 를 받지 않는다
        self.universe = [s.upper() for s in universe]
        self.limits = dict(margin_max=margin_max, stop_min=stop_min, stop_max=stop_max, hold_max=hold_max)
        self.leverage = leverage
        self._client = client
        self._api_key = api_key
        self._system = system_rules(universe=self.universe, margin_max=margin_max, leverage=leverage,
                                    stop_min=stop_min, stop_max=stop_max, hold_max=hold_max)

    # ── 클라이언트 ─────────────────────────────────────────
    def client(self) -> Any:
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key or None)
        return self._client

    def system_blocks(self) -> list[dict[str, Any]]:
        # 규칙 + 유니버스는 고정이라 캐시한다. 뉴스 본문은 그 뒤(user)에 온다.
        return [{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}]

    # ── 호출 ─────────────────────────────────────────────
    async def _parse(self, *, stage: str, model: str, schema: type[BaseModel], text: str,
                     max_tokens: int) -> tuple[BaseModel | None, CallRecord]:
        rec = CallRecord(stage=stage, model=model)
        try:
            import anthropic
        except ImportError:
            rec.error = "anthropic 패키지가 없습니다: pip install -e \".[news]\""
            return None, rec
        kw: dict[str, Any] = dict(model=model, max_tokens=max_tokens, system=self.system_blocks(),
                                  messages=[{"role": "user", "content": text}], output_format=schema)
        if self.effort and stage == "full" and not model.startswith("claude-haiku"):
            kw["output_config"] = {"effort": self.effort}
        try:
            resp = await self.client().messages.parse(**kw)
        except anthropic.RateLimitError as e:
            rec.error = f"레이트리밋: {e}"
            return None, rec
        except anthropic.AuthenticationError:
            rec.error = "ANTHROPIC_API_KEY 가 없거나 잘못됐습니다"
            return None, rec
        except anthropic.APIStatusError as e:
            rec.error = f"API 오류 {e.status_code}: {getattr(e, 'message', e)}"
            return None, rec
        except anthropic.APIConnectionError as e:
            rec.error = f"연결 실패: {e}"
            return None, rec
        except Exception as e:  # noqa: BLE001
            rec.error = f"예외 {type(e).__name__}: {e}"
            return None, rec
        rec.usage = usage_dict(getattr(resp, "usage", None))
        rec.cost_usd = cost_of(model, rec.usage)
        if getattr(resp, "stop_reason", None) == "refusal":
            det = getattr(resp, "stop_details", None)
            rec.error = "모델이 거절함" + (f" ({getattr(det, 'category', '')})" if det else "")
            return None, rec
        obj = getattr(resp, "parsed_output", None)
        if obj is None:
            rec.error = "구조화 출력 파싱 실패" + (" (max_tokens)" if getattr(resp, "stop_reason", "") == "max_tokens" else "")
            return None, rec
        return obj, rec

    def _filter_symbols(self, syms: list[str]) -> list[str]:
        out: list[str] = []
        for s in syms:
            s = str(s).upper().strip()
            if not s.endswith("USDT"):
                s = tickers.symbol_of(s)
            if s in self.universe and s not in out:
                out.append(s)
        return out

    async def triage(self, item: NewsItem, ctx: JudgeContext) -> tuple[Triage | None, CallRecord]:
        text = ("아래 뉴스가 유니버스 종목 가격에 영향을 줄 만한지, 크기가 어느 정도인지만 판정하라.\n\n"
                + news_block(item, ctx))
        obj, rec = await self._parse(stage="triage", model=self.triage_model, schema=Triage, text=text,
                                     max_tokens=TRIAGE_MAX_TOKENS)
        if obj is None:
            return None, rec
        t: Triage = obj  # type: ignore[assignment]
        t.symbols = self._filter_symbols(t.symbols)
        rec.result = t.model_dump()
        return t, rec

    async def full(self, item: NewsItem, ctx: JudgeContext) -> tuple[Judgment | None, CallRecord]:
        text = ("아래 뉴스로 매매 판단을 내려라. 스키마대로만 답한다.\n\n" + news_block(item, ctx))
        obj, rec = await self._parse(stage="full", model=self.model, schema=Judgment, text=text,
                                     max_tokens=FULL_MAX_TOKENS)
        if obj is None:
            return None, rec
        j: Judgment = obj  # type: ignore[assignment]
        j.symbols = self._filter_symbols(j.symbols)
        j.confidence = max(0.0, min(1.0, float(j.confidence)))
        if j.plan is not None:
            problems = j.plan.problems(**self.limits)
            if problems:
                # 범위를 어긴 계획은 버린다 — 조용히 잘라 쓰지 않는다 (models.py 머리말)
                rec.error = "계획 범위 위반: " + " · ".join(problems)
                j.plan = None
        rec.result = j.model_dump()
        return j, rec


# ─────────────────────────────────────────────────────────
# 문맥 만들기 (네트워크 · DB)
# ─────────────────────────────────────────────────────────

def make_context_builder(api: Any, *, max_symbols: int = 5):
    """`build(item, now) -> JudgeContext`. 실패한 조각은 note 로 남기고 나머지로 계속한다."""

    async def snapshot(sym: str, news_ts: datetime, now: datetime) -> SymbolSnapshot:
        snap = SymbolSnapshot(symbol=sym)
        try:
            start = min(news_ts, now - timedelta(hours=24)) - timedelta(minutes=10)
            bars = await api.klines(sym, "5m", start_ms=int(start.timestamp() * 1000), limit=400,
                                    include_open=True)
            if bars:
                last = bars[-1]
                snap.price = float(last.close)
                day_ago = [b for b in bars if b.ts <= now - timedelta(hours=24)]
                base = day_ago[-1].close if day_ago else bars[0].close
                snap.change_24h_pct = (snap.price / base - 1) * 100 if base else None
                after = [b for b in bars if b.ts >= news_ts]
                if after:
                    ref = float(after[0].open)
                    snap.move_since_news_pct = (snap.price / ref - 1) * 100 if ref else None
        except Exception as e:  # noqa: BLE001
            snap.note = f"시세 조회 실패({type(e).__name__})"
        try:
            from ..sessions import get_context
            c = get_context(sym)
            snap.regime_ko = getattr(c.regime, "value", str(c.regime))
            snap.blocked = bool(c.blocked)
            snap.block_reason = " · ".join(b.reason_ko for b in c.blocks)
            if c.next_event is not None:
                snap.next_event_ko = f"{c.next_event.name} {c.next_event.ts_utc.isoformat(timespec='minutes')}"
        except Exception as e:  # noqa: BLE001
            snap.note = (snap.note + " " if snap.note else "") + f"세션 조회 실패({type(e).__name__})"
        return snap

    async def build(item: NewsItem, now: datetime | None = None) -> JudgeContext:
        from . import store

        now = now or datetime.now(UTC)
        ctx = JudgeContext(now=now)
        for sym in item.symbols[:max_symbols]:
            ctx.snapshots.append(await snapshot(sym, item.ts, now))
        try:
            for camp in await store.open_campaigns():
                ctx.open_campaigns.append(f"{tickers.ticker_of(camp['symbol'])} {camp['direction']} ({camp['status']})")
        except Exception:  # noqa: BLE001
            log.debug("열린 작전 조회 실패", exc_info=True)
        try:
            seen: set[str] = set()
            for sym in item.symbols[:max_symbols]:
                for r in await store.recent(8, symbol=sym, since=now - timedelta(hours=24)):
                    t = str(r["title"])
                    if t != item.title and t not in seen:
                        seen.add(t)
                        ctx.recent_titles.append(f"[{r['source']}] {t}")
        except Exception:  # noqa: BLE001
            log.debug("최근 뉴스 조회 실패", exc_info=True)
        return ctx

    return build
