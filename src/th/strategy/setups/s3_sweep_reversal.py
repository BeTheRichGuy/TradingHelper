"""S3 — 유동성 스윕 반전 (02 문서 4절).

전저(전일 저가·프리장 저가)를 살짝 깨고 바로 되돌아오는 자리.
손절 물량을 먹고 반전하는 구조로, 호가가 얕은 perp 에서 특히 자주 나온다.

핵심은 "살짝"이다. 너무 깊게 깨면 그건 스윕이 아니라 추세 이탈이다.

⚠️ **실측 결과: 체결 65건 기대값 −0.34R (셋 중 가장 나쁨).** `docs/08` 참고.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..features import Features
from .base import Condition, Direction, EntryZone, SetupResult, cond, num, pct

SETUP_ID = "S3_sweep_reversal"
CATEGORY = "reversal"
REGIMES = ("RTH", "PRE")

SWEEP_MIN_PCT = 0.10           # 이보다 얕으면 그냥 지지 테스트
SWEEP_MAX_PCT = 0.40           # 이보다 깊으면 추세 이탈
RECOVER_BARS = 3               # 5분봉 3개 안에 복귀해야 한다
VOLUME_SPIKE = 2.0
VWAP_TARGET_PCT = 1.2          # 목표(VWAP)까지 필요한 거리


@dataclass(frozen=True, slots=True)
class Sweep:
    level_name: str
    level: float
    bar_index: int              # bars_5m 안에서의 위치
    depth_pct: float
    recovered: bool


class S3SweepReversal:
    setup_id = SETUP_ID
    category = CATEGORY
    regimes = REGIMES

    def evaluate(self, f: Features, direction: Direction) -> SetupResult:
        long = direction == "long"
        sweep = self._find_sweep(f, long)
        cs: list[Condition] = []

        # ── 필수 ─────────────────────────────────────────
        cs.append(
            cond(
                "S3-1",
                "주요 레벨 스윕",
                sweep is not None,
                (
                    f"{sweep.level_name}({num(sweep.level)}) 을 {sweep.depth_pct:.2f}% 이탈"
                    if sweep
                    else f"최근 {RECOVER_BARS}봉에 {SWEEP_MIN_PCT}~{SWEEP_MAX_PCT}% 이탈한 레벨 없음"
                ),
                required=True,
            )
        )
        cs.append(
            cond(
                "S3-2",
                "레벨 위로 종가 복귀",
                sweep.recovered if sweep else None,
                (
                    f"현재 종가 {num(f.price)} 가 {num(sweep.level)} "
                    f"{'위' if sweep.recovered else '아래'}"
                    if sweep
                    else "-"
                ),
                required=True,
            )
        )

        # ── 선택 ─────────────────────────────────────────
        spike = self._volume_spike(f, sweep)
        cs.append(
            cond(
                "S3-3",
                "스윕봉 거래량 급증",
                None if spike is None else spike >= VOLUME_SPIKE,
                f"스윕봉 거래량이 평균의 {num(spike, 2) if spike else '-'}배 "
                f"(기준 {VOLUME_SPIKE}배)",
                weight=25,
            )
        )

        doi = f.perp.oi_change_15m
        cs.append(
            cond(
                "S3-4",
                "스윕 직후 OI 감소",
                None if doi is None else doi < 0,
                f"ΔOI(15m) {pct(doi)} — {'반대 포지션 청산 소화' if doi and doi < 0 else '청산 흔적 없음'}",
                weight=20,
            )
        )

        div, div_detail = self._divergence(f, sweep, long)
        cs.append(cond("S3-5", "RSI 다이버전스", div, div_detail, weight=20))

        cvd_s = f.cvd_slope
        cs.append(
            cond(
                "S3-6",
                "복귀봉 CVD 전환",
                None if cvd_s is None else (cvd_s > 0 if long else cvd_s < 0),
                f"누적델타 기울기 {num(cvd_s, 1)}",
                weight=20,
            )
        )

        vd = f.vwap_distance_pct
        room = abs(vd) if vd is not None else None
        cs.append(
            cond(
                "S3-7",
                "VWAP 까지 목표 여유",
                None if room is None else room >= VWAP_TARGET_PCT,
                f"VWAP 까지 {pct(room)} (기준 {VWAP_TARGET_PCT}%)",
                weight=15,
            )
        )

        return SetupResult(
            setup_id=SETUP_ID,
            category=CATEGORY,
            direction=direction,
            conditions=tuple(cs),
            entry=self._entry(f, long, sweep),
            note=f"{sweep.level_name} 스윕" if sweep else "",
        )

    # ── 내부 ─────────────────────────────────────────────
    def _find_sweep(self, f: Features, long: bool) -> Sweep | None:
        bars = f.bars_5m[-RECOVER_BARS:]
        if not bars:
            return None
        levels = f.levels.sweep_levels() if long else self._upper_levels(f)
        if not levels:
            return None

        best: Sweep | None = None
        for name, level in levels:
            if level <= 0:
                continue
            for i, b in enumerate(bars):
                depth = (
                    (level - b.low) / level * 100 if long else (b.high - level) / level * 100
                )
                if not (SWEEP_MIN_PCT <= depth <= SWEEP_MAX_PCT):
                    continue
                recovered = f.price > level if long else f.price < level
                cand = Sweep(name, level, len(f.bars_5m) - len(bars) + i, depth, recovered)
                # 더 깊게 찌른 쪽이 진짜 스윕에 가깝다
                if best is None or cand.depth_pct > best.depth_pct:
                    best = cand
        return best

    def _upper_levels(self, f: Features) -> list[tuple[str, float]]:
        out = []
        if f.levels.prev_day_high is not None:
            out.append(("전일 고가", f.levels.prev_day_high))
        if f.levels.premarket_high is not None:
            out.append(("프리장 고가", f.levels.premarket_high))
        return out

    def _volume_spike(self, f: Features, sweep: Sweep | None) -> float | None:
        if sweep is None or not f.volume_ma20_5m:
            return None
        if not (0 <= sweep.bar_index < len(f.bars_5m)):
            return None
        return f.bars_5m[sweep.bar_index].volume / f.volume_ma20_5m

    def _divergence(
        self, f: Features, sweep: Sweep | None, long: bool
    ) -> tuple[bool | None, str]:
        """가격은 저점을 갱신했는데 RSI 는 안 갱신했는가."""
        series = [v for v in f.rsi9_1m_series if v is not None]
        if sweep is None or len(series) < 30:
            return None, "판단할 데이터 부족"
        recent = series[-10:]
        earlier = series[-30:-10]
        if not recent or not earlier:
            return None, "판단할 데이터 부족"
        if long:
            ok = min(recent) > min(earlier)
            return ok, (
                f"RSI(9,1m) 최근 저점 {min(recent):.1f} vs 직전 {min(earlier):.1f} — "
                f"{'가격은 낮아졌으나 RSI 는 안 낮아짐' if ok else '다이버전스 없음'}"
            )
        ok = max(recent) < max(earlier)
        return ok, (
            f"RSI(9,1m) 최근 고점 {max(recent):.1f} vs 직전 {max(earlier):.1f} — "
            f"{'가격은 높아졌으나 RSI 는 안 높아짐' if ok else '다이버전스 없음'}"
        )

    def _entry(self, f: Features, long: bool, sweep: Sweep | None) -> EntryZone | None:
        if sweep is None or f.atr_1m is None:
            return None
        a = f.price
        b = sweep.level + (0.05 * f.atr_1m if long else -0.05 * f.atr_1m)
        return EntryZone(low=min(a, b), high=max(a, b))
