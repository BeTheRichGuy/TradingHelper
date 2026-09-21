"""S1 — VWAP 되돌림 지속 (02 문서 4절).

그날 방향이 정해진 뒤 VWAP 으로 눌렸다 가는 자리. 가장 자주 나오고 가장 안정적이다.
본장(RTH)에서만 평가한다.

⚠️ **실측 결과: 체결 92건 기대값 −0.05R (우위 없음).** `docs/08` 참고.
"""

from __future__ import annotations

from ..features import Features
from .base import Condition, Direction, EntryZone, SetupResult, cond, num, pct

SETUP_ID = "S1_vwap_pullback"
CATEGORY = "trend"
REGIMES = ("RTH",)

VWAP_TOUCH_PCT = 0.15          # 저가가 VWAP 에서 이 % 이내로 접근했는가
TOUCH_LOOKBACK_BARS = 3        # 되돌림을 찾아볼 5분봉 개수 (S3-2 의 '3봉 이내'와 동일)
RSI_LONG = (45.0, 60.0)
RSI_SHORT = (40.0, 55.0)
BASIS_LIMIT = 0.2              # % — 넘으면 perp 과열
FUNDING_LIMIT = 0.0003         # 0.03%
HEADROOM_PCT = 1.5             # 상위 저항까지 필요한 여유


class S1VwapPullback:
    setup_id = SETUP_ID
    category = CATEGORY
    regimes = REGIMES

    def evaluate(self, f: Features, direction: Direction) -> SetupResult:
        long = direction == "long"
        cs: list[Condition] = []

        # ── 필수 ─────────────────────────────────────────
        above = f.above_vwap
        trend5 = f.trend_5m_up
        s1_1 = None
        if above is not None and trend5 is not None:
            s1_1 = (above and trend5) if long else ((not above) and (not trend5))
        cs.append(
            cond(
                "S1-1",
                "VWAP·EMA 방향 일치",
                s1_1,
                f"종가 {num(f.price)} / VWAP {num(f.vwap.vwap if f.vwap else None)} · "
                f"EMA9 {num(f.ema9_5m)} {'>' if trend5 else '<'} EMA21 {num(f.ema21_5m)}",
                required=True,
            )
        )

        trend15 = f.trend_15m_up
        s1_2 = None if trend15 is None else (trend15 if long else not trend15)
        cs.append(
            cond(
                "S1-2",
                "15분봉 추세 일치",
                s1_2,
                f"15m EMA9 {num(f.ema9_15m)} / EMA21 {num(f.ema21_15m)}",
                required=True,
            )
        )

        touched, touch_detail = self._touched_vwap(f, long)
        cs.append(cond("S1-3", "VWAP 되돌림 후 복귀", touched, touch_detail, required=True))

        # ── 선택 ─────────────────────────────────────────
        lo, hi = RSI_LONG if long else RSI_SHORT
        r = f.rsi14_5m
        cs.append(
            cond(
                "S1-4",
                "RSI 과열 아님",
                None if r is None else (lo <= r <= hi),
                f"RSI(14,5m) {num(r, 1)} · 기준 {lo:.0f}~{hi:.0f}",
                weight=20,
            )
        )

        rv = f.relative_volume_5m
        cs.append(
            cond(
                "S1-5",
                "되돌림 구간 거래량 감소",
                None if rv is None else rv < 1.0,
                f"직전 5분봉 거래량이 20봉 평균의 {num(rv, 2) if rv else '-'}배",
                weight=15,
            )
        )

        doi = f.perp.oi_change_15m
        cs.append(
            cond(
                "S1-6",
                "미결제약정 증가",
                None if doi is None else (doi > 0 if long else doi > 0),
                f"ΔOI(15m) {pct(doi)} — {'신규 진입 유입' if doi and doi > 0 else '유입 없음'}",
                weight=15,
            )
        )

        cvd_s = f.cvd_slope
        cs.append(
            cond(
                "S1-7",
                "CVD 가격과 동행",
                None if cvd_s is None else (cvd_s > 0 if long else cvd_s < 0),
                f"누적델타 기울기 {num(cvd_s, 1)}",
                weight=15,
            )
        )

        basis = f.perp.basis_pct
        cs.append(
            cond(
                "S1-8",
                "본주 대비 과열 아님",
                None if basis is None else (basis < BASIS_LIMIT if long else basis > -BASIS_LIMIT),
                f"괴리 {pct(basis)} · 기준 ±{BASIS_LIMIT}%",
                weight=10,
            )
        )

        fr = f.perp.funding_rate
        cs.append(
            cond(
                "S1-9",
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
                "S1-10",
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
    def _touched_vwap(self, f: Features, long: bool) -> tuple[bool | None, str]:
        """되돌림 **후** 복귀 — 최근 3봉 안에서 VWAP 을 건드렸고 지금은 제 방향 마감.

        마지막 봉 하나만 보면 '건드린 봉'과 '복귀한 봉'이 같아야만 통과한다.
        조건 이름이 '되돌림 후 복귀'인데 그건 그 중 아주 좁은 경우뿐이다.
        """
        if f.vwap is None or not f.bars_5m:
            return None, "VWAP 없음"
        v = f.vwap.vwap
        last = f.bars_5m[-1]
        closed_right = (last.close > v) if long else (last.close < v)

        recent = f.bars_5m[-TOUCH_LOOKBACK_BARS:]
        dists = [abs((b.low if long else b.high) - v) / v * 100 for b in recent]
        nearest = min(dists)
        ok = nearest <= VWAP_TOUCH_PCT and closed_right

        side = "저가" if long else "고가"
        return ok, (
            f"최근 {len(recent)}봉 중 {side}가 VWAP({num(v)}) 에 최대 {nearest:.2f}% 까지 "
            f"접근 (기준 {VWAP_TOUCH_PCT}%) · 종가 {num(last.close)} "
            f"{'위' if last.close > v else '아래'} 마감"
        )

    def _support_headroom(self, f: Features) -> float | None:
        below = f.levels.supports_below(f.price)
        if not below or f.price <= 0:
            return None
        return (f.price - below[0]) / f.price * 100

    def _entry(self, f: Features, long: bool) -> EntryZone | None:
        if not f.bars_1m or f.atr_1m is None:
            return None
        last = f.bars_1m[-1]
        trigger = (
            last.high + 0.05 * f.atr_1m if long else last.low - 0.05 * f.atr_1m
        )
        a, b = f.price, trigger
        return EntryZone(low=min(a, b), high=max(a, b))
