"""S5 — VWMA 정배열/역배열 눌림 추세추종.

**사용자가 지목한 주 매매 방식이다.** 거래량 가중 이동평균 세 줄이 한 방향으로
정렬(정배열/역배열)되어 있을 때, 가격이 빠른 선 쪽으로 눌렸다가 되돌아 나가는
자리를 잡는다. 프리장·본장 모두에서 평가한다.

S1(VWAP 되돌림)과 무엇이 다른가:

| | S1 | S5 |
|---|---|---|
| 기준선 | 세션 앵커 VWAP (하루 한 줄, 누적) | VWMA 3줄 (이동, 배열을 본다) |
| 추세 판정 | EMA9/21 교차 | VWMA 9/21/50 **정렬 + 지속 기간** |
| 세션 | 본장만 | **프리장 + 본장** |
| 눌림 구간 | VWAP ±0.15% | VWMA9~VWMA21 **띠 전체** |

VWMA 를 쓰는 이유는 거래가 얇은 프리장에서 몇 계약이 만든 가격에 덜 속기
때문이다. EMA 는 종가만 보므로 호가가 비어 튄 가격을 그대로 반영한다.

**갓 만들어진 배열은 받지 않는다.** 한 봉 만에 뒤집히는 자리가 추세추종에서 가장
비싸게 물리는 곳이라, 정렬이 최소 `MIN_ALIGNMENT_BARS` 봉 유지된 것만 본다.

⚠️ **실측 결과: 신호 1,402건 기대값 −0.15R (우위 없음).** 같은 규칙을 SOXL
일봉 10년에 걸어도 +0.001R 이었다. `docs/08-전략-검증-결과.md` 참고.
"""

from __future__ import annotations

from ..features import Features
from .base import Condition, Direction, EntryZone, SetupResult, cond, num, pct

SETUP_ID = "S5_vwma_pullback"
CATEGORY = "trend"
REGIMES = ("RTH", "PRE")

MIN_ALIGNMENT_BARS = 3         # 배열이 몇 봉 이상 유지됐어야 하는가
PULLBACK_LOOKBACK = 3          # 눌림을 찾아볼 5분봉 수
ZONE_TOLERANCE_PCT = 0.05      # 띠에 이만큼 못 미쳐도 '닿았다'로 본다
RSI_LONG = (40.0, 68.0)        # 추세추종이라 S1 보다 넓게 둔다
RSI_SHORT = (32.0, 60.0)
FUNDING_LIMIT = 0.0003
HEADROOM_PCT = 1.5
MAX_STRETCH_ATR = 3.0          # 빠른 선에서 이보다 멀면 이미 다 간 자리


class S5VwmaPullback:
    setup_id = SETUP_ID
    category = CATEGORY
    regimes = REGIMES

    def evaluate(self, f: Features, direction: Direction) -> SetupResult:
        long = direction == "long"
        want = 1 if long else -1
        stack = f.vwma_5m
        cs: list[Condition] = []

        # ── 필수 ─────────────────────────────────────────
        order = stack.order if stack else 0
        fast, mid, slow = stack.values() if stack else (None, None, None)
        cs.append(
            cond(
                "S5-1",
                "VWMA 정배열" if long else "VWMA 역배열",
                None if stack is None else (order == want),
                f"VWMA9 {num(fast)} · VWMA21 {num(mid)} · VWMA50 {num(slow)}"
                + ("  (정배열)" if order == 1 else "  (역배열)" if order == -1 else "  (뒤섞임)"),
                required=True,
            )
        )

        age = stack.alignment_age() if stack else 0
        cs.append(
            cond(
                "S5-2",
                "배열 지속",
                None if stack is None else (order == want and age >= MIN_ALIGNMENT_BARS),
                f"{age}봉째 유지 · 기준 {MIN_ALIGNMENT_BARS}봉"
                if order == want
                else "배열이 아님",
                required=True,
            )
        )

        touched, touch_detail = self._pullback(f, long)
        cs.append(cond("S5-3", "눌림이 띠에 닿음", touched, touch_detail, required=True))

        resumed, resume_detail = self._resumed(f, long)
        cs.append(cond("S5-4", "빠른 선 되찾음", resumed, resume_detail, required=True))

        # ── 선택 ─────────────────────────────────────────
        sl = stack.mid_slope(5) if stack else None
        cs.append(
            cond(
                "S5-5",
                "중간 선 기울기 일치",
                None if sl is None else (sl > 0 if long else sl < 0),
                f"VWMA21 기울기 {num(sl, 4)}",
                weight=20,
            )
        )

        cs.append(
            cond(
                "S5-6",
                "상위 시간대 배열 일치",
                None if f.vwma_15m is None else (f.vwma_15m.order == want),
                f"15분봉 배열 "
                + ("정배열" if f.vwma_15m and f.vwma_15m.order == 1
                   else "역배열" if f.vwma_15m and f.vwma_15m.order == -1 else "뒤섞임"),
                weight=20,
            )
        )

        rv = f.relative_volume_5m
        cs.append(
            cond(
                "S5-7",
                "눌림 구간 거래량 감소",
                None if rv is None else rv < 1.0,
                f"직전 5분봉 거래량이 20봉 평균의 {num(rv, 2)}배",
                weight=15,
            )
        )

        cvd_s = f.cvd_slope
        cs.append(
            cond(
                "S5-8",
                "CVD 방향 일치",
                None if cvd_s is None else (cvd_s > 0 if long else cvd_s < 0),
                f"누적델타 기울기 {num(cvd_s, 1)}",
                weight=15,
            )
        )

        doi = f.perp.oi_change_15m
        cs.append(
            cond(
                "S5-9",
                "미결제약정 증가",
                None if doi is None else doi > 0,
                f"ΔOI(15m) {pct(doi)}",
                weight=15,
            )
        )

        stretch = self._stretch_atr(f)
        cs.append(
            cond(
                "S5-10",
                "과확장 아님",
                None if stretch is None else stretch <= MAX_STRETCH_ATR,
                f"빠른 선에서 {num(stretch, 2)} ATR 떨어짐 · 기준 {MAX_STRETCH_ATR}",
                weight=15,
            )
        )

        lo, hi = RSI_LONG if long else RSI_SHORT
        r = f.rsi14_5m
        cs.append(
            cond(
                "S5-11",
                "RSI 과열 아님",
                None if r is None else (lo <= r <= hi),
                f"RSI(14,5m) {num(r, 1)} · 기준 {lo:.0f}~{hi:.0f}",
                weight=15,
            )
        )

        fr = f.perp.funding_rate
        cs.append(
            cond(
                "S5-12",
                "펀딩 쏠림 없음",
                None if fr is None else (fr < FUNDING_LIMIT if long else fr > -FUNDING_LIMIT),
                f"펀딩률 {fr * 100:.4f}%" if fr is not None else "-",
                weight=10,
            )
        )

        head = (
            f.levels.nearest_resistance_pct(f.price)
            if long
            else self._support_headroom(f)
        )
        cs.append(
            cond(
                "S5-13",
                "목표까지 여유",
                head is None or head >= HEADROOM_PCT,
                f"가장 가까운 {'저항' if long else '지지'}까지 {pct(head)}"
                if head is not None
                else "앞을 막는 레벨 없음",
                weight=15,
            )
        )

        return SetupResult(
            setup_id=SETUP_ID,
            category=CATEGORY,
            direction=direction,
            conditions=tuple(cs),
            entry=self._entry(f, long),
        )

    # ── 내부 ─────────────────────────────────────────────
    def _pullback(self, f: Features, long: bool) -> tuple[bool | None, str]:
        """최근 몇 봉 안에서 가격이 VWMA9~VWMA21 띠에 닿았는가."""
        stack = f.vwma_5m
        zone = stack.zone() if stack else None
        if zone is None or not f.bars_5m:
            return None, "VWMA 띠 없음"
        low, high = zone
        pad = high * ZONE_TOLERANCE_PCT / 100

        recent = f.bars_5m[-PULLBACK_LOOKBACK:]
        edges = [b.low if long else b.high for b in recent]
        edge = min(edges) if long else max(edges)
        ok = edge <= high + pad if long else edge >= low - pad

        side = "저가" if long else "고가"
        return ok, (
            f"최근 {len(recent)}봉 {side} {num(edge)} · "
            f"띠 {num(low)}~{num(high)} (허용 ±{ZONE_TOLERANCE_PCT}%)"
        )

    def _resumed(self, f: Features, long: bool) -> tuple[bool | None, str]:
        """지금 봉이 빠른 선 위(롱)/아래(숏)로 마감했는가 = 되돌림이 끝났다."""
        stack = f.vwma_5m
        fast = stack.values()[0] if stack else None
        if fast is None or not f.bars_5m:
            return None, "VWMA9 없음"
        close = f.bars_5m[-1].close
        ok = close > fast if long else close < fast
        return ok, (
            f"종가 {num(close)} 가 VWMA9({num(fast)}) "
            f"{'위' if close > fast else '아래'} 마감"
        )

    def _stretch_atr(self, f: Features) -> float | None:
        stack = f.vwma_5m
        fast = stack.values()[0] if stack else None
        if fast is None or not f.atr_1m or f.atr_1m <= 0:
            return None
        return abs(f.price - fast) / f.atr_1m

    def _support_headroom(self, f: Features) -> float | None:
        below = f.levels.supports_below(f.price)
        if not below or f.price <= 0:
            return None
        return (f.price - below[0]) / f.price * 100

    def _entry(self, f: Features, long: bool) -> EntryZone | None:
        if not f.bars_1m or f.atr_1m is None:
            return None
        last = f.bars_1m[-1]
        trigger = last.high + 0.05 * f.atr_1m if long else last.low - 0.05 * f.atr_1m
        a, b = f.price, trigger
        return EntryZone(low=min(a, b), high=max(a, b))
