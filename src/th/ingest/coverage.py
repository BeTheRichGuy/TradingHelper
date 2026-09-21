"""데이터 커버리지 점검 — 이번 프로젝트 최대 버그의 재발 방지.

`cond()` 는 데이터가 없으면 조건을 '미충족'으로 본다. 실전에서는 그게 맞다 —
지금 OI 를 못 읽으면 OI 조건을 통과로 쳐줄 수 없다. 그런데 **과거 구간에 그 데이터가
애초에 존재한 적이 없으면** 이야기가 다르다. 그때는 조건이 통째로 죽어 점수가
조용히 0에 수렴한다.

실제로 그랬다. 백테스트 구간(110일)의 커버리지가 이랬다.

    1분봉        100%
    미결제약정      0%   ← 하루치만 있었다
    호가 스프레드    0%
    펀딩            5%
    본주 1분봉      4%

S1 은 선택 가중치 115점 중 35점(30%)이 자동 소멸했고, 발행 기준 55점을 넘으려면
남은 조건을 거의 다 맞춰야 했다. 셋업이 약해서가 아니라 없는 것을 채점하고 있었다.

**그래서 `th doctor` 가 매번 이걸 보여준다.** 조용히 비어 있는 것이 가장 위험하다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .. import db
from ..logging_setup import get_logger

log = get_logger(__name__)

# (표시 이름, 테이블, 시각 컬럼, 심볼 컬럼 or None, 이 데이터가 없으면 죽는 조건)
SOURCES = [
    ("1분봉", "klines", "ts", "symbol", "모든 지표"),
    ("미결제약정", "open_interest", "ts", "symbol", "ΔOI 조건 (S1-6·S3-4·S5-9)"),
    ("펀딩", "funding", "ts", "symbol", "펀딩 쏠림 조건 (S1-9·S5-12)"),
    ("호가 스프레드", "book_ticker", "ts", "symbol", "스프레드 필터"),
    ("본주·지수 가격", "underlying_prices", "ts", None, "basis 조건·필터 (S1-8)"),
]


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    name: str
    rows: int
    first: datetime | None
    last: datetime | None
    depends: str

    @property
    def days(self) -> float:
        if self.first is None or self.last is None:
            return 0.0
        return (self.last - self.first).total_seconds() / 86400

    def fraction_of(self, days: float) -> float:
        return min(1.0, self.days / days) if days > 0 else 0.0

    def status(self, days: float) -> str:
        f = self.fraction_of(days)
        if self.rows == 0:
            return "없음"
        if f >= 0.9:
            return "충분"
        if f >= 0.3:
            return "부분"
        return "거의 없음"

    def stale_minutes(self, now: datetime | None = None) -> float | None:
        if self.last is None:
            return None
        return ((now or datetime.now(UTC)) - self.last).total_seconds() / 60


async def collect_coverage(symbol: str) -> list[SourceCoverage]:
    out: list[SourceCoverage] = []
    for name, table, ts_col, sym_col, depends in SOURCES:
        try:
            where = f"WHERE {sym_col} = $1" if sym_col else ""
            args = (symbol.upper(),) if sym_col else ()
            row = await db.fetch(
                f"SELECT count(*) AS n, min({ts_col}) AS first, max({ts_col}) AS last "
                f"FROM {table} {where}",
                *args,
            )
            r = row[0] if row else {}
            out.append(
                SourceCoverage(
                    name=name, rows=int(r.get("n") or 0),
                    first=r.get("first"), last=r.get("last"), depends=depends,
                )
            )
        except Exception as e:                       # 테이블이 없을 수도 있다
            log.debug("%s 커버리지 조회 실패: %s", table, e)
            out.append(SourceCoverage(name, 0, None, None, depends))
    return out


def backtest_span_days(coverage: list[SourceCoverage]) -> float:
    """1분봉이 있는 기간 = 백테스트가 가능한 기간."""
    bars = next((c for c in coverage if c.name == "1분봉"), None)
    return bars.days if bars else 0.0


def warnings_for(coverage: list[SourceCoverage], now: datetime | None = None) -> list[str]:
    span = backtest_span_days(coverage)
    out: list[str] = []
    for c in coverage:
        if c.name == "1분봉":
            continue
        if c.rows == 0:
            out.append(f"{c.name} 이력이 전혀 없습니다 — {c.depends} 이 항상 미충족 처리됩니다")
        elif c.fraction_of(span) < 0.3:
            out.append(
                f"{c.name} 이 최근 {c.days:.0f}일치뿐입니다 (봉은 {span:.0f}일) — "
                f"그 이전 구간 백테스트에서 {c.depends} 이 죽습니다"
            )
    return out
