#!/usr/bin/env python3
"""엔진 자체 검사 — 결과를 믿기 전에 먼저 돌린다.

v1 에서 '손절 8452건인데 승률 97%' 라는 말이 안 되는 결과가 나왔다.
손절가가 진입가의 이익 쪽에 놓여서 손절이 이익으로 잡힌 버그였다.
그런 게 다시는 조용히 지나가지 않도록 여기서 막는다.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ema99_engine as E
from ema99_engine import Config, flip_of, load_bars, run

ROOT = Path(__file__).resolve().parent.parent
FAILED = []


def check(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def make(prices, start=None):
    """종가 리스트를 1분봉으로. 고가/저가는 종가 ±0.02%."""
    t0 = start or datetime(2026, 1, 1, tzinfo=UTC)
    out = []
    for k, p in enumerate(prices):
        o = prices[k - 1] if k else p
        out.append((t0 + timedelta(minutes=k), o, max(o, p) * 1.0002,
                    min(o, p) * 0.9998, p, 100.0))
    return out


print("1) 합성 데이터 — 위에서 내려와 터치하면 롱이 되는가")
# 완만한 상승으로 EMA99 를 한참 위에 두고 → 눌러서 터치 → 목표까지 상승
up = [100 + i * 0.05 for i in range(1500)]        # EMA99 를 아래에 깔아둔다
dip = [up[-1] * (1 - 0.001 * i) for i in range(1, 130)]  # EMA99 까지 눌린다
rally = [dip[-1] * (1 + 0.0006 * i) for i in range(1, 400)]
bars = make(up + dip + rally)
cfg = Config(take="support", stop_buf=0.30, target=1.5, touch_tol=0.30)
r = run(bars, cfg)
check("지지 터치가 잡힌다", r.n > 0, f"체결 {r.n}건")
check("전부 지지(롱)로 분류", r.by_kind[1] == 0, f"지지 {r.by_kind[0]} / 저항 {r.by_kind[1]}")
print(f"     (참고: 손절가가 이익 쪽이라 버린 신호 {r.skipped_bad_stop}건 — 버려지는 게 정상 동작)")

print("\n2) 합성 데이터 — 아래에서 올라와 터치하면 숏이 되는가")
down = [200 - i * 0.05 for i in range(1500)]
pop = [down[-1] * (1 + 0.001 * i) for i in range(1, 130)]
fall = [pop[-1] * (1 - 0.0006 * i) for i in range(1, 400)]
bars2 = make(down + pop + fall)
r2 = run(bars2, Config(take="resistance", stop_buf=0.30, target=1.5, touch_tol=0.30))
check("저항 터치가 잡힌다", r2.n > 0, f"체결 {r2.n}건")
check("전부 저항(숏)으로 분류", r2.by_kind[0] == 0, f"지지 {r2.by_kind[0]} / 저항 {r2.by_kind[1]}")

print("\n3) 핵심 불변식 — 손절로 끝난 매매는 절대 이길 수 없다")
print("   (승리 건수 ≤ 익절 건수 + 시간청산 건수)")
path = ROOT / "dataset" / "vision" / "SOXLUSDT_1m.csv"
if not path.exists():
    path = ROOT / "dataset" / "klines_1m.csv"
if path.exists():
    real = load_bars(path, "SOXLUSDT")
    bad = 0
    tested = 0
    for take in ("both", "support", "resistance"):
        for buf in (0.10, 0.20, 0.30, 0.50):
            for tgt in (1.0, 1.5, 2.0):
                for trend in ("none", "with"):
                    rr = run(real, Config(take=take, stop_buf=buf, target=tgt, trend=trend))
                    if rr.n == 0:
                        continue
                    tested += 1
                    tp, sl, tm = rr.by_exit
                    if rr.wins > tp + tm:
                        bad += 1
                    if rr.n != tp + sl + tm:
                        bad += 1
    check(f"{tested}개 설정 전부 불변식 통과", bad == 0, f"위반 {bad}건")

    print("\n4) 독립평가 모드에서 원본과 뒤집기가 **같은 신호**를 쓰는가")
    print("   (이게 깨지면 대조군 비교가 무의미해진다 — 비용이 상쇄되지 않는다)")
    ci = Config(take="both", stop_buf=0.50, target=1.5, trend="with",
                confirm=True, independent=True)
    ia = run(real, ci)
    ib = run(real, flip_of(ci))
    check("신호 수 일치", ia.n == ib.n, f"원본 {ia.n}건 / 뒤집기 {ib.n}건")
    check("평균 손절폭 일치", abs(ia.avg_stop_pct - ib.avg_stop_pct) < 1e-9,
          f"{ia.avg_stop_pct:.4f}% / {ib.avg_stop_pct:.4f}%")

    print("\n4b) 방향 대조군이 실제로 반대로 도는가")
    a = run(real, Config(take="both", stop_buf=0.20, target=1.5))
    b = run(real, Config(take="both", stop_buf=0.20, target=1.5, flip=True))
    check("뒤집기가 원본과 다른 결과", abs(a.exp_r - b.exp_r) > 1e-9,
          f"원본 {a.exp_r:+.3f}R / 뒤집기 {b.exp_r:+.3f}R")

    print("\n5) 비용이 오르면 기대값은 반드시 내려간다")
    E.ROUND_TRIP = 0.00
    lo = run(real, Config(take="both", stop_buf=0.20, target=1.5)).exp_r
    E.ROUND_TRIP = 0.30
    hi = run(real, Config(take="both", stop_buf=0.20, target=1.5)).exp_r
    E.ROUND_TRIP = 0.14
    check("비용 0% > 비용 0.30%", lo > hi, f"{lo:+.3f}R → {hi:+.3f}R")
else:
    print("  ⚠️ SOXLUSDT 데이터가 없어 3~5번은 건너뛴다")

print()
if FAILED:
    print(f"❌ 실패 {len(FAILED)}건: {', '.join(FAILED)}")
    sys.exit(1)
print("✅ 전부 통과 — 이제 백테스트 결과를 믿어도 된다")
