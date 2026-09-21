"""주문을 내보내기 **전에** 막는 안전장치. 전부 순수 함수다.

여기 있는 규칙 중 어느 하나라도 걸리면 주문은 만들어지지도 않는다.
`manager` 가 아니라 여기에 두는 이유: 순수 함수라야 테스트로 못 박을 수 있고,
못 박아야 나중에 누가(나 포함) 조용히 풀어버리는 일이 없다.

⚠️ 이 파일의 규칙을 완화하는 변경은 반드시 테스트를 같이 고쳐야 하도록 설계했다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import LONG, SHORT, Bracket

# 손절폭 허용 범위 — strategy/risk.py 와 같은 근거(사용자 목표 TP1 = 본주 1~2%)
STOP_MIN_PCT = 0.20
STOP_MAX_PCT = 3.00

DAILY_LOSS_LIMIT_R = -3.0
MAX_OPEN_BRACKETS = 1          # 동시에 열어둘 브래킷 수
LIQ_BUFFER_PCT = 1.0           # 손절은 청산가보다 최소 이만큼 앞이어야 한다


@dataclass(frozen=True, slots=True)
class GuardInput:
    """판단에 필요한 모든 것. 전부 호출자가 채워 넣는다 — 여기서 조회하지 않는다."""
    bracket: Bracket
    ref_price: float                     # 현재가 (진입 예상가)
    open_bracket_count: int = 0
    daily_realized_r: float = 0.0
    daily_unknown: bool = False
    """오늘 실현손익을 **확인하지 못했다**는 뜻.

    안전장치는 모르면 막아야 한다. DB 나 API 가 실패했을 때 0.0 으로 두면
    '한도에 안 걸렸다'로 읽혀 잠금이 조용히 풀린다 — 가장 나쁜 실패 방향이다.
    """
    daily_locked: bool = False
    """오늘 한도 잠금이 **이미 기록돼 있다**(`daily_pnl.limit_hit_at`).

    손실 뒤에 수익이 나서 R 값이 한도 위로 되돌아와도 그날은 잠긴 채여야 한다.
    R 값만 넘기면 그 잠금이 조용히 풀리므로 따로 받는다.
    """
    liquidation_price: float | None = None
    session_blocked: bool = False        # 🔴 CLOSED 등 게이트 차단
    session_reason: str = ""
    equity_usdt: float = 0.0
    risk_pct: float = 0.5
    max_notional_usdt: float | None = None
    execution_mode: str = "none"
    max_open_brackets: int = MAX_OPEN_BRACKETS
    """동시에 열어둘 브래킷 수. 기본 1. 시그널 엔진이 여러 종목을 볼 때만 TH_MAX_OPEN_BRACKETS 로 올린다."""


@dataclass(frozen=True, slots=True)
class GuardResult:
    ok: bool
    reasons: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return " · ".join(self.reasons) if self.reasons else "통과"


def check(inp: GuardInput) -> GuardResult:
    """막을 이유를 **전부** 모아서 돌려준다. 첫 번째에서 멈추지 않는다.

    하나씩 고치게 하면 사용자가 세 번 시도하게 된다. 한 번에 다 보여준다.
    """
    b = inp.bracket
    bad: list[str] = []

    # ── 0. 실행 모드 · 방향 ────────────────────────────
    if inp.execution_mode == "none":
        bad.append("실행 모드가 none 이다 (TH_EXECUTION_MODE)")
    elif inp.execution_mode not in ("paper", "live"):
        bad.append(f"알 수 없는 실행 모드: {inp.execution_mode}")
    if b.direction not in (LONG, SHORT):
        # 아래 규칙들은 LONG 이 아니면 전부 숏으로 취급한다 — 오타를 숏으로 잡으면 안 된다
        bad.append(f"방향이 LONG/SHORT 가 아니다: {b.direction!r}")

    # ── 1. 손절 자체 ───────────────────────────────────
    if b.stop_price <= 0:
        bad.append("손절가가 없다")
    elif inp.ref_price > 0:
        if b.direction == LONG and b.stop_price >= inp.ref_price:
            bad.append(
                f"롱인데 손절({b.stop_price:g})이 현재가({inp.ref_price:g}) 위에 있다"
            )
        if b.direction == SHORT and b.stop_price <= inp.ref_price:
            bad.append(
                f"숏인데 손절({b.stop_price:g})이 현재가({inp.ref_price:g}) 아래에 있다"
            )
        pct = b.stop_pct(inp.ref_price)
        if pct < STOP_MIN_PCT:
            bad.append(f"손절폭 {pct:.2f}% — 너무 좁다 (최소 {STOP_MIN_PCT}%)")
        elif pct > STOP_MAX_PCT:
            bad.append(f"손절폭 {pct:.2f}% — 너무 넓다 (최대 {STOP_MAX_PCT}%)")

    # ── 2. 손절 vs 청산가 ──────────────────────────────
    if inp.liquidation_price and inp.liquidation_price > 0 and inp.ref_price > 0:
        liq = inp.liquidation_price
        gap = abs(b.stop_price - liq) / inp.ref_price * 100
        beyond = (b.stop_price <= liq) if b.direction == LONG else (b.stop_price >= liq)
        if beyond:
            bad.append(f"손절({b.stop_price:g})이 청산가({liq:g}) 너머다 — 무효인 손절")
        elif gap < LIQ_BUFFER_PCT:
            bad.append(
                f"손절과 청산가 간격 {gap:.2f}% — {LIQ_BUFFER_PCT}% 미만이면 갭에 건너뛰어진다"
            )

    # ── 3. 익절 방향 ───────────────────────────────────
    for name, tp in (("TP1", b.tp1_price), ("TP2", b.tp2_price)):
        if tp is None:
            continue
        if b.direction == LONG and tp <= inp.ref_price:
            bad.append(f"롱인데 {name}({tp:g})이 현재가 아래다")
        if b.direction == SHORT and tp >= inp.ref_price:
            bad.append(f"숏인데 {name}({tp:g})이 현재가 위다")

    # ── 4. 수량 ────────────────────────────────────────
    if b.qty <= 0:
        bad.append("수량이 0 이하다")
    elif inp.max_notional_usdt and inp.ref_price > 0:
        notional = b.qty * inp.ref_price
        if notional > inp.max_notional_usdt:
            bad.append(
                f"명목 {notional:,.0f} USDT — 한도 {inp.max_notional_usdt:,.0f} 초과"
            )

    # ── 5. 계좌 상태 ───────────────────────────────────
    if inp.daily_unknown:
        bad.append("오늘 실현손익을 확인하지 못했습니다 — 한도 확인 전에는 진입하지 않습니다")
    elif inp.daily_locked:
        bad.append("오늘 손실 한도에 이미 도달 — 오늘은 진입하지 않습니다")
    elif inp.daily_realized_r <= DAILY_LOSS_LIMIT_R:
        bad.append(
            f"오늘 실현손익 {inp.daily_realized_r:+.2f}R — 한도 {DAILY_LOSS_LIMIT_R}R 도달"
        )
    limit = max(1, int(inp.max_open_brackets))
    if inp.open_bracket_count >= limit:
        bad.append(f"이미 열린 브래킷 {inp.open_bracket_count}개 (최대 {limit})")

    # ── 6. 세션 게이트 ─────────────────────────────────
    if inp.session_blocked:
        bad.append(f"세션 차단: {inp.session_reason or '거래 금지 구간'}")

    return GuardResult(ok=not bad, reasons=tuple(bad))


def size_from_risk(equity: float, risk_pct: float, entry: float, stop: float) -> float:
    """손절폭에서 수량을 역산한다. **레버리지로 정하지 않는다.**

    1R = 자본 × risk_pct. 그 금액을 손절폭으로 나누면 수량이다.
    레버리지는 결과일 뿐 입력이 아니다 — 이걸 뒤집으면 손절폭이 좁을 때
    수량이 그대로라 위험이 몇 배로 뛴다.
    """
    dist = abs(entry - stop)
    if equity <= 0 or dist <= 0:
        return 0.0
    return (equity * risk_pct / 100) / dist


def notional_leverage(qty: float, entry: float, equity: float) -> float:
    """결과적으로 몇 배가 되는지 — 화면에 보여주기 위한 값."""
    if equity <= 0:
        return 0.0
    return qty * entry / equity
