"""CSV 데이터셋으로 백테스트를 돌린다 — **DB 없이.**

`th export` 로 내보낸 폴더를 그대로 읽는다. 목적 두 가지.

1. DB 에 못 붙는 기계(다른 PC, 클라우드, CI)에서도 같은 백테스트를 돌린다.
2. 데이터 커버리지를 **눈에 보이게** 만든다. DB 조회는 값이 없으면 조용히 None 을
   돌려주는데, 그게 Phase 4 에서 OI·펀딩·basis 가 백테스트 내내 '미충족'으로
   처리되던 사고의 원인이었다. 여기서는 각 소스의 실제 보유 구간을 먼저 찍는다.

DB 경로와 **같은 규칙**을 지킨다 — 스냅샷(OI·펀딩·호가)은 `ts <= at`, **봉**(1분봉·본주
1분봉·VIX 1분봉)은 `ts <= at − 1분` (ts 봉은 ts+1분에야 닫힌다), 5일 룩백.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..strategy.features import PerpContext
from ..strategy.indicators import Bar


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def _f(text: str | None) -> float | None:
    if text is None or text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class Coverage:
    name: str
    rows: int
    first: datetime | None
    second: datetime | None = None      # 마지막

    def pct_over(self, start: datetime, end: datetime) -> float:
        if self.first is None or self.second is None or end <= start:
            return 0.0
        lo = max(self.first, start)
        hi = min(self.second, end)
        if hi <= lo:
            return 0.0
        return (hi - lo).total_seconds() / (end - start).total_seconds()


@dataclass
class CsvDataset:
    """`th export` 폴더 하나를 통째로 들고 있는다."""

    bars: list[Bar] = field(default_factory=list)
    oi: list[tuple[datetime, float]] = field(default_factory=list)
    funding: list[tuple[datetime, float, datetime | None]] = field(default_factory=list)
    spread: list[tuple[datetime, float]] = field(default_factory=list)
    underlying: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)
    coverage: list[Coverage] = field(default_factory=list)

    # ── 조회 (스냅샷은 ts <= at, 봉은 ts <= at − 1분) ────────
    def _last_before(self, seq: list, at: datetime):
        if not seq:
            return None
        i = bisect_right([x[0] for x in seq], at)
        return seq[i - 1] if i else None

    def oi_change_pct(self, at: datetime, minutes: int) -> float | None:
        now = self._last_before(self.oi, at)
        past = self._last_before(self.oi, at - timedelta(minutes=minutes))
        if not now or not past or past[1] <= 0:
            return None
        # 너무 오래된 값을 현재값으로 쓰지 않는다
        if (at - now[0]) > timedelta(minutes=30):
            return None
        return (now[1] - past[1]) / past[1] * 100

    def funding_at(self, at: datetime) -> tuple[float | None, datetime | None]:
        hit = self._last_before(self.funding, at)
        if not hit or (at - hit[0]) > timedelta(hours=16):
            return None, None
        return hit[1], hit[2]

    def spread_at(self, at: datetime) -> float | None:
        hit = self._last_before(self.spread, at)
        if not hit or (at - hit[0]) > timedelta(minutes=5):
            return None
        return hit[1]

    def underlying_at(self, ticker: str, at: datetime) -> float | None:
        # 본주·VIX 도 1분봉이다. ts 봉의 종가는 ts+1분에야 확정되므로 `ts <= at` 로 읽으면
        # 아직 형성 중인 봉의 종가를 1분 미리 보는 셈이다 — 1분봉과 같은 규칙을 적용한다.
        hit = self._last_before(self.underlying.get(ticker, []), at - timedelta(minutes=1))
        if not hit or (at - hit[0]) > timedelta(minutes=15):
            return None
        return hit[1]

    def perp_at(self, at: datetime, price: float, underlying_ticker: str = "SOXL") -> PerpContext:
        rate, next_at = self.funding_at(at)
        under = self.underlying_at(underlying_ticker, at)
        basis = ((price - under) / under * 100) if (under and under > 0) else None
        return PerpContext(
            funding_rate=rate,
            next_funding_at=next_at,
            oi_change_5m=self.oi_change_pct(at, 5),
            oi_change_15m=self.oi_change_pct(at, 15),
            basis_pct=basis,
            spread_bps=self.spread_at(at),
            underlying_price=under,
            vix=self.underlying_at("^VIX", at),
        )

    def median_volume_5m(self, at: datetime, buckets: int = 100) -> float | None:
        """완성된 5분봉 거래량의 중앙값. DB 의 연속집계와 같은 것을 손으로 만든다."""
        i = bisect_right([b.ts for b in self.bars], at - timedelta(minutes=1))
        window = self.bars[max(0, i - buckets * 5) : i]
        if len(window) < 100:
            return None
        sums: list[float] = []
        bucket: list[Bar] = []
        key = None
        for b in window:
            k = int(b.ts.timestamp()) // 300
            if key is None:
                key = k
            if k != key:
                if len(bucket) == 5:
                    sums.append(sum(x.volume for x in bucket))
                bucket, key = [], k
            bucket.append(b)
        if len(sums) < 20:
            return None
        sums.sort()
        return sums[len(sums) // 2]


def load_dataset(folder: str | Path, symbol: str = "SOXLUSDT") -> CsvDataset:
    d = Path(folder)
    ds = CsvDataset()
    sym = symbol.upper()

    def rows(name: str):
        path = d / name
        if not path.exists():
            return []
        with path.open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    kl = [r for r in rows("klines_1m.csv") if r.get("symbol", sym).upper() == sym]
    ds.bars = [
        Bar(
            ts=_ts(r["ts"]),
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]),
            volume=float(r["volume"]),
            taker_buy=_f(r.get("taker_buy_base")),
        )
        for r in kl
    ]
    ds.bars.sort(key=lambda b: b.ts)

    ds.oi = sorted(
        (_ts(r["ts"]), float(r["oi"]))
        for r in rows("open_interest.csv")
        if r.get("symbol", sym).upper() == sym
    )
    ds.funding = sorted(
        (
            _ts(r["ts"]),
            float(r["rate"]),
            _ts(r["next_funding_ts"]) if r.get("next_funding_ts") else None,
        )
        for r in rows("funding.csv")
        if r.get("symbol", sym).upper() == sym
    )
    ds.spread = sorted(
        (_ts(r["ts"]), float(r["spread_bps"]))
        for r in rows("book_ticker.csv")
        if r.get("symbol", sym).upper() == sym
    )
    for r in rows("underlying.csv"):
        if r.get("tf") != "1m":
            continue
        ds.underlying.setdefault(r["ticker"], []).append((_ts(r["ts"]), float(r["close"])))
    for v in ds.underlying.values():
        v.sort()

    def cov(name: str, seq: list) -> Coverage:
        if not seq:
            return Coverage(name, 0, None, None)
        first = seq[0].ts if isinstance(seq[0], Bar) else seq[0][0]
        last = seq[-1].ts if isinstance(seq[-1], Bar) else seq[-1][0]
        return Coverage(name, len(seq), first, last)

    ds.coverage = [
        cov("1분봉", ds.bars),
        cov("미결제약정", ds.oi),
        cov("펀딩", ds.funding),
        cov("호가 스프레드", ds.spread),
        cov("본주 1분봉", ds.underlying.get("SOXL", [])),
        cov("VIX 1분봉", ds.underlying.get("^VIX", [])),
    ]
    return ds
