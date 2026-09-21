"""무한매수법(라오어) — 레버리지 perp 버전 시뮬레이터.

원본과 다른 점을 먼저 못 박는다.

  라오어 원본 : 3배 ETF를 **현금으로** 40분할 매수. 청산이 없다.
                40분할 자체가 레버리지 통제 장치다.
  여기        : perp 에 배율을 **한 번 더** 얹는다. 청산이 생긴다.

크로스 마진 기준으로 매 시간봉마다 청산을 판정한다. 청산은 장중에 나므로
일봉으로 보면 놓친다 — 저가(롱)/고가(숏)로 판정한다.

한 봉 안의 처리 순서는 **청산 → 익절 → 매수**. 어느 쪽이 먼저인지 모를 때는
항상 나쁜 쪽을 택한다.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

FUNDING_INTERVAL = timedelta(hours=8)


@dataclass(frozen=True, slots=True)
class Cfg:
    seed: float = 10_000.0
    splits: int = 40                 # 분할 수
    leverage: float = 10.0           # 1회 매수 시 배율
    tp_pct: float = 10.0             # 평단 대비 +% 익절 (가격 기준)
    buy_mode: str = "daily"          # daily | dip
    dip_pct: float = 0.0             # dip: 평단 대비 이만큼 아래일 때만 산다
    buy_hour: int = 14               # 하루 중 매수 시각(UTC)
    mmr: float = 0.005               # 유지증거금률
    fee_pct: float = 0.05            # 편도 수수료 %
    funding_8h: float = 0.01         # 8시간마다 명목의 % (롱이 낸다)
    add_margin_pct: float = 0.0      # 청산 임박 시 시드의 % 만큼 추가 투입
    add_margin_max: int = 0          # 추가 투입 최대 횟수
    add_trigger: float = 1.30        # equity/유지증거금 이 배수 밑이면 추가


@dataclass(slots=True)
class Result:
    liquidated: bool = False
    liq_at: datetime | None = None
    liq_price: float = 0.0
    cycles: int = 0
    cycle_days: list[float] = field(default_factory=list)
    deposited: float = 0.0           # 총 투입 (시드 + 추가 마진)
    equity_end: float = 0.0
    added: float = 0.0
    max_buys: int = 0
    max_dd: float = 0.0              # equity/투입 최저점
    fees: float = 0.0
    funding: float = 0.0
    days: float = 0.0
    skipped_buys: int = 0            # 증거금이 모자라 못 산 횟수

    @property
    def total_return(self) -> float:
        return (self.equity_end - self.deposited) / self.deposited * 100 if self.deposited else 0.0

    @property
    def cagr(self) -> float:
        if self.days <= 0 or self.deposited <= 0 or self.equity_end <= 0:
            return -100.0
        return ((self.equity_end / self.deposited) ** (365 / self.days) - 1) * 100


def load(path: str) -> list[tuple]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append((
                datetime.fromisoformat(r["ts"]).astimezone(UTC),
                float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
            ))
    out.sort()
    return out


def run(bars: list[tuple], c: Cfg) -> Result:
    res = Result(deposited=c.seed)
    wallet = c.seed                  # 실현 기준 잔고
    qty = 0.0                        # 보유 수량
    avg = 0.0                        # 평단
    used = 0                         # 사용한 분할 수
    last_buy_day = None
    cycle_start = bars[0][0]
    peak_ratio = 1.0
    adds = 0
    unit_margin = c.seed / c.splits
    # 펀딩은 **경과 시간** 8시간마다다. 봉 간격이 뭐든(1시간·4시간·일봉) 같아야 한다.
    # `ts.hour % 8 == 0` 은 시간봉에서만 맞고 일봉이면 하루 한 번(1/3), 4시간봉이면
    # 두 번에 한 번만 걷는다. 첫 봉 시각을 8시간 경계로 내림한 뒤 8시간씩 더해 간다.
    t0 = bars[0][0]
    next_funding = t0.replace(hour=(t0.hour // 8) * 8, minute=0, second=0, microsecond=0)
    next_funding += FUNDING_INTERVAL

    for ts, o, h, lo, cl in bars:
        # ── 펀딩 (경과 8시간마다, 봉이 건너뛴 만큼 여러 번) ──
        while ts >= next_funding:
            if qty > 0:
                f = qty * cl * c.funding_8h / 100
                wallet -= f
                res.funding += f
            next_funding += FUNDING_INTERVAL

        notional = qty * cl
        equity = wallet + qty * (cl - avg) if qty else wallet

        # ── ① 청산 (장중 저가로 판정) ─────────────────
        if qty > 0:
            eq_low = wallet + qty * (lo - avg)
            maint_low = qty * lo * c.mmr
            if eq_low <= maint_low:
                # 추가 증거금으로 버틸 수 있나
                need = maint_low - eq_low
                room = c.add_margin_max - adds
                while room > 0 and need > 0:
                    add = c.seed * c.add_margin_pct / 100
                    wallet += add
                    res.added += add
                    res.deposited += add
                    adds += 1
                    room -= 1
                    need -= add
                if need > 0:
                    res.liquidated = True
                    res.liq_at = ts
                    res.liq_price = lo
                    res.equity_end = 0.0
                    res.max_dd = -100.0
                    res.days = (ts - bars[0][0]).days or 1
                    return res

        # ── 예방적 마진 추가 ──────────────────────────
        if qty > 0 and adds < c.add_margin_max and c.add_margin_pct > 0:
            maint = qty * cl * c.mmr
            if maint > 0 and equity / maint < c.add_trigger:
                add = c.seed * c.add_margin_pct / 100
                wallet += add
                res.added += add
                res.deposited += add
                adds += 1

        # ── ② 익절 (장중 고가로 판정) ─────────────────
        if qty > 0 and avg > 0:
            tp = avg * (1 + c.tp_pct / 100)
            if h >= tp:
                fee = qty * tp * c.fee_pct / 100
                wallet += qty * (tp - avg) - fee
                res.fees += fee
                res.cycles += 1
                res.cycle_days.append((ts - cycle_start).total_seconds() / 86400)
                qty, avg, used = 0.0, 0.0, 0
                cycle_start = ts
                last_buy_day = None

        # ── ③ 매수 ───────────────────────────────────
        if used < c.splits and ts.hour == c.buy_hour:
            day = ts.date()
            if last_buy_day != day:
                ok = True
                if c.buy_mode == "dip" and qty > 0:
                    ok = cl <= avg * (1 - c.dip_pct / 100)
                # 크로스 마진의 가용 증거금 = 자산 − 이미 잡힌 개시증거금.
                # 평가손이 쌓이면 40분할 예산이 남아 있어도 거래소가 주문을 거절한다.
                if ok:
                    equity_now = wallet + qty * (cl - avg) if qty else wallet
                    initial_margin = qty * cl / c.leverage
                    if equity_now - initial_margin < unit_margin:
                        ok = False
                        res.skipped_buys += 1
                if ok:
                    add_notional = unit_margin * c.leverage
                    add_qty = add_notional / cl
                    fee = add_notional * c.fee_pct / 100
                    wallet -= fee
                    res.fees += fee
                    avg = (avg * qty + cl * add_qty) / (qty + add_qty) if qty else cl
                    qty += add_qty
                    used += 1
                    last_buy_day = day
                    res.max_buys = max(res.max_buys, used)

        # ── 낙폭 ─────────────────────────────────────
        eq = wallet + qty * (cl - avg) if qty else wallet
        ratio = eq / res.deposited
        peak_ratio = max(peak_ratio, ratio)
        res.max_dd = min(res.max_dd, (ratio / peak_ratio - 1) * 100)

    res.equity_end = wallet + qty * (bars[-1][4] - avg) if qty else wallet
    res.days = (bars[-1][0] - bars[0][0]).days or 1
    return res
