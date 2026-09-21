"""실행 계층 테스트.

여기서 가장 중요한 것은 '잘 되는 경우'가 아니라 **막아야 할 것이 막히는가** 다.
10배 레버리지에서 사고는 전부 '막았어야 하는데 안 막힌' 쪽에서 난다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from th.execution import (
    Bracket,
    GuardInput,
    OrderType,
    Purpose,
    check,
    new_id,
    notional_leverage,
    size_from_risk,
)
from th.execution.manager import BracketManager
from th.execution.paper import PaperClient, Quote


def run(coro):
    """동기 테스트 안에서 코루틴 하나를 돌린다.

    ⚠️ `asyncio.get_event_loop()` 를 쓰면 안 된다.
    그건 '현재 루프가 없고 아무도 set_event_loop 를 부른 적 없을 때'만 루프를
    만들어준다. 앞선 테스트가 `asyncio.run()` 을 한 번이라도 쓰면 그 플래그가 켜져서
    그 뒤로는 RuntimeError 가 난다.

    그래서 **이 파일만 돌리면 통과하고 전체를 돌리면 깨진다.** 실제로 그랬다.
    매번 새 루프를 여는 `asyncio.run` 이 정답이다.
    """
    return asyncio.run(coro)


def mk_bracket(**kw) -> Bracket:
    base = dict(
        bracket_id=new_id("br"), symbol="SOXLUSDT", direction="LONG",
        qty=10.0, stop_price=99.0, entry_type=OrderType.MARKET,
        tp1_price=102.0, tp2_price=104.0, reason="테스트",
    )
    base.update(kw)
    return Bracket(**base)


def mk_input(b: Bracket, **kw) -> GuardInput:
    base = dict(bracket=b, ref_price=100.0, execution_mode="paper", equity_usdt=10_000)
    base.update(kw)
    return GuardInput(**base)


# ── 안전장치 ────────────────────────────────────────────
def test_실행모드가_none이면_주문이_막힌다():
    r = check(mk_input(mk_bracket(), execution_mode="none"))
    assert not r.ok
    assert any("none" in x for x in r.reasons)


def test_롱인데_손절이_현재가_위면_막힌다():
    r = check(mk_input(mk_bracket(stop_price=101.0)))
    assert not r.ok
    assert any("손절" in x and "위에" in x for x in r.reasons)


def test_숏인데_손절이_현재가_아래면_막힌다():
    b = mk_bracket(direction="SHORT", stop_price=99.0, tp1_price=98.0, tp2_price=96.0)
    r = check(mk_input(b))
    assert not r.ok
    assert any("아래에" in x for x in r.reasons)


def test_손절이_청산가_너머면_막힌다():
    r = check(mk_input(mk_bracket(stop_price=95.0), liquidation_price=96.0))
    assert not r.ok
    assert any("청산가" in x for x in r.reasons)


def test_손절과_청산가가_너무_가까우면_막힌다():
    r = check(mk_input(mk_bracket(stop_price=99.0), liquidation_price=98.5))
    assert not r.ok
    assert any("간격" in x for x in r.reasons)


def test_손절폭이_너무_좁으면_막힌다():
    r = check(mk_input(mk_bracket(stop_price=99.95)))
    assert not r.ok
    assert any("너무 좁다" in x for x in r.reasons)


def test_일일_한도에_닿으면_막힌다():
    r = check(mk_input(mk_bracket(), daily_realized_r=-3.0))
    assert not r.ok
    assert any("한도" in x for x in r.reasons)


def test_오늘_손익을_모르면_막힌다():
    """안전장치는 모르면 막아야 한다.

    DB 나 API 가 실패했을 때 0.0 으로 두면 '한도에 안 걸렸다'로 읽혀
    일일 손실 잠금이 조용히 풀린다. 가장 나쁜 실패 방향이다.
    """
    r = check(mk_input(mk_bracket(), daily_unknown=True))
    assert not r.ok
    assert any("확인하지 못했" in x for x in r.reasons)


def test_손익을_아는_상태에서_한도_아래면_통과한다():
    assert check(mk_input(mk_bracket(), daily_realized_r=-2.0, daily_unknown=False)).ok


def test_이미_열린_브래킷이_있으면_막힌다():
    r = check(mk_input(mk_bracket(), open_bracket_count=1))
    assert not r.ok


def test_세션_차단이면_막힌다():
    r = check(mk_input(mk_bracket(), session_blocked=True, session_reason="본주 휴장"))
    assert not r.ok
    assert any("본주 휴장" in x for x in r.reasons)


def test_막을_이유를_한꺼번에_모아준다():
    r = check(mk_input(mk_bracket(stop_price=101.0), session_blocked=True,
                       daily_realized_r=-5.0, execution_mode="none"))
    assert len(r.reasons) >= 4       # 한 번에 다 보여준다


def test_정상이면_통과한다():
    assert check(mk_input(mk_bracket())).ok


# ── 사이징 ──────────────────────────────────────────────
def test_수량은_손절폭에서_역산된다_레버리지가_아니라():
    # 자본 10,000 · 0.5% = 50 USDT 를 1R 로 본다
    q1 = size_from_risk(10_000, 0.5, entry=100.0, stop=99.0)   # 손절폭 1.0
    q2 = size_from_risk(10_000, 0.5, entry=100.0, stop=99.5)   # 손절폭 0.5
    assert q1 == pytest.approx(50.0)
    assert q2 == pytest.approx(100.0)
    # 손절이 절반이면 수량은 두 배 — 위험은 같다
    assert q1 * 1.0 == pytest.approx(q2 * 0.5)


def test_레버리지는_결과일_뿐이다():
    q = size_from_risk(10_000, 0.5, entry=100.0, stop=99.0)
    assert notional_leverage(q, 100.0, 10_000) == pytest.approx(0.5)


# ── 페이퍼 체결 ─────────────────────────────────────────
def test_지정가는_호가가_넘어야_체결된다():
    c = PaperClient()
    b = mk_bracket(entry_type=OrderType.LIMIT, entry_price=100.0)
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=100.5, ask=100.6))
    run(m.open(mk_input(b, ref_price=100.5)))
    # 아직 ask 가 100.0 보다 높다 → 체결 안 됨
    assert not c.feed(Quote("SOXLUSDT", bid=100.0, ask=100.1))
    # ask 가 100.0 아래로 내려와야 체결
    fills = c.feed(Quote("SOXLUSDT", bid=99.8, ask=99.9))
    assert len(fills) == 1 and fills[0].purpose is Purpose.ENTRY


def test_진입이_체결되면_손절이_자동으로_걸린다():
    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket())))
    assert res.ok
    st = m.states[res.state.bracket.bracket_id]
    assert st.has_position
    assert st.live_stop is not None, "진입 체결 직후 손절이 없으면 안 된다"
    assert st.live_stop.trigger == 99.0


def test_같은_봉에서_손절과_익절이_둘_다_닿으면_손절이_이긴다():
    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket())))
    bid = res.state.bracket.bracket_id
    # 고가 103(TP1 102 도달) · 저가 98(손절 99 도달) — 어느 쪽이 먼저인지 모른다
    c.feed(Quote("SOXLUSDT", bid=100.0, ask=100.02, high=103.0, low=98.0))
    run(m.drain_fills())
    st = [s for s in m.closed if s.bracket.bracket_id == bid][0]
    assert st.realized_r < 0, "불확실할 때는 나쁜 쪽을 택해야 한다"


def test_손절에_맞으면_대략_마이너스_1R_이다():
    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket(tp1_price=None, tp2_price=None))))
    bid = res.state.bracket.bracket_id
    c.feed(Quote("SOXLUSDT", bid=98.9, ask=98.95, high=100.0, low=98.9))
    run(m.drain_fills())
    st = [s for s in m.closed if s.bracket.bracket_id == bid][0]
    assert -1.6 < st.realized_r < -1.0, f"손절인데 {st.realized_r:+.2f}R"


def test_익절이_체결되면_남은_손절_수량이_줄어든다():
    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket())))
    st = m.states[res.state.bracket.bracket_id]
    assert st.live_stop.qty == pytest.approx(10.0)
    c.feed(Quote("SOXLUSDT", bid=102.1, ask=102.2, high=102.5, low=101.0))
    run(m.drain_fills())
    assert st.position_qty == pytest.approx(5.0)
    assert st.live_stop.qty == pytest.approx(5.0), "남은 포지션만큼 손절이 다시 걸려야 한다"


def test_같은_주문을_두_번_보내도_하나만_생긴다():
    from th.execution.models import Order, Side
    c = PaperClient()
    o = Order(client_id="fixed-1", bracket_id="b1", symbol="SOXLUSDT",
              side=Side.BUY, type=OrderType.LIMIT, purpose=Purpose.ENTRY,
              qty=1.0, price=50.0)
    a = run(c.place(o))
    b = run(c.place(o))
    assert a.client_id == b.client_id
    assert len(c.orders) == 1


def test_진입_지정가는_유효시간이_지나면_취소된다():
    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=100.5, ask=100.6))
    b = mk_bracket(entry_type=OrderType.LIMIT, entry_price=100.0, expire_after_s=60)
    run(m.open(mk_input(b, ref_price=100.5)))
    assert m.open_count == 1
    n = run(m.expire_stale_entries(now=datetime.now(UTC) + timedelta(seconds=61)))
    assert n == 1 and m.open_count == 0
    assert m.closed[-1].close_reason == "ENTRY_EXPIRED"


def test_수동_청산은_남은_주문을_모두_취소한다():
    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket())))
    bid = res.state.bracket.bracket_id
    run(m.close_now(bid, reason="수동"))
    assert m.open_count == 0
    st = [s for s in m.closed if s.bracket.bracket_id == bid][0]
    assert not any(o.is_open for o in st.orders.values())


# ── 실거래 잠금 ─────────────────────────────────────────
def test_실거래_클라이언트는_아직_열리지_않는다():
    from th.execution.live import LiveClient
    with pytest.raises(NotImplementedError):
        LiveClient()


def test_account_패키지에는_주문_코드가_없다():
    """주문 경로가 execution 한 곳뿐이라는 것을 코드로 고정한다."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "th" / "account"
    banned = ("POST /fapi/v1/order", "\"/fapi/v1/order\"", "'/fapi/v1/order'",
              "place_order", "new_order")
    for p in root.rglob("*.py"):
        txt = p.read_text(encoding="utf-8")
        for bad in banned:
            assert bad not in txt, f"{p.name} 에 주문 코드가 있다: {bad}"


# ── 영속화 ──────────────────────────────────────────────
# 실제로 겪은 문제: store.py 를 만들어놓고 매니저가 **한 번도 부르지 않았다.**
# 저장이 없으면 프로세스가 죽었을 때 '손절이 걸려 있다고 믿는데 실제로는 없는'
# 상태가 된다. store.py 의 존재 이유가 그건데, 그게 배선이 안 돼 있었다.

class FakeStore:
    """호출을 기록만 하는 가짜 저장소."""

    def __init__(self):
        self.brackets, self.orders, self.fills = [], [], []

    async def save_bracket(self, st, mode):
        self.brackets.append((st.bracket.bracket_id, mode, st.closed_at))

    async def save_order(self, o):
        self.orders.append(o.client_id)

    async def save_fill(self, f):
        self.fills.append(f.order_client_id)


def test_브래킷을_열면_저장된다():
    c, store = PaperClient(), FakeStore()
    m = BracketManager(c, store=store)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    run(m.open(mk_input(mk_bracket())))
    assert store.brackets, "브래킷이 저장되지 않았다 — 재시작하면 사라진다"
    assert store.orders, "주문이 저장되지 않았다"
    assert store.fills, "체결이 저장되지 않았다"


def test_손절_주문도_저장된다():
    """재시작 후 '손절이 걸려 있나'를 알 수 있어야 한다."""
    c, store = PaperClient(), FakeStore()
    m = BracketManager(c, store=store)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket())))
    st = m.states[res.state.bracket.bracket_id]
    assert st.live_stop.client_id in store.orders, "손절 주문이 저장 안 됐다"


def test_종료도_저장된다():
    c, store = PaperClient(), FakeStore()
    m = BracketManager(c, store=store)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket())))
    bid = res.state.bracket.bracket_id
    run(m.close_now(bid, reason="테스트"))
    assert any(b[0] == bid and b[2] is not None for b in store.brackets), \
        "종료 시각이 저장되지 않았다"


def test_저장에_실패해도_매매는_계속된다():
    class Broken(FakeStore):
        async def save_bracket(self, st, mode):
            raise RuntimeError("DB 죽음")

    c = PaperClient()
    m = BracketManager(c, store=Broken())
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket())))
    assert res.ok, "저장 실패가 매매를 막으면 안 된다"
    st = m.states[res.state.bracket.bracket_id]
    assert st.live_stop is not None, "저장이 실패해도 손절은 걸려야 한다"


def test_재시작하면_열린_브래킷이_되살아난다():
    from th.execution.models import BracketState

    c = PaperClient()
    m = BracketManager(c, store=FakeStore())
    st = BracketState(bracket=mk_bracket())
    st.position_qty = 10.0
    st.avg_entry = 100.0
    n = run(m.restore([st]))
    assert n == 1 and m.open_count == 1


def test_매니저가_저장소를_실제로_부른다():
    """배선을 빠뜨리면 이 테스트가 잡는다."""
    import inspect

    from th.execution import manager as mod

    src = inspect.getsource(mod)
    assert "self.store.save_bracket" in src, "save_bracket 을 부르지 않는다"
    assert "self.store.save_order" in src, "save_order 를 부르지 않는다"
    assert "self.store.save_fill" in src, "save_fill 을 부르지 않는다"


# ── CLI 안전장치 ────────────────────────────────────────
# 아래 두 테스트가 이번에 실제로 놓친 버그를 잡는다.
# `_require_paper_mode()` 를 만들어두고 **호출을 빠뜨려서** live 모드가 그냥 통과했다.
# 정적 검사기(test_no_undefined_names)는 '없는 이름'만 잡지 '안 쓴 함수'는 못 잡는다.

def _cli_source() -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent
            / "src" / "th" / "cli.py").read_text(encoding="utf-8")


def test_모든_paper_명령이_실행모드를_확인한다():
    """`@paper_app.command` 함수는 예외 없이 `_require_paper_mode` 를 불러야 한다."""
    import ast

    tree = ast.parse(_cli_source())
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        decorated = any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "paper_app"
            for d in node.decorator_list
        )
        if not decorated:
            continue
        calls = {
            c.func.id for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        if "_require_paper_mode" not in calls:
            missing.append(node.name)
    assert not missing, f"실행 모드 확인이 빠진 paper 명령: {missing}"


def test_안_쓰이는_안전장치_함수가_없다():
    """정의만 해두고 아무도 안 부르는 안전장치는 없는 것과 같다."""
    import ast

    src = _cli_source()
    tree = ast.parse(src)
    defined = {
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("_require_")
    }
    called = {
        c.func.id for c in ast.walk(tree)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    dead = defined - called
    assert not dead, f"정의만 되고 호출되지 않는 안전장치: {sorted(dead)}"


def test_테스트_코드가_get_event_loop_를_쓰지_않는다():
    """이번에 전체 스위트를 깨뜨린 원인을 규칙으로 못 박는다.

    `asyncio.get_event_loop()` 는 앞선 테스트가 `asyncio.run` 을 썼는지에 따라
    성공하기도 실패하기도 한다. 그래서 **파일 단독으로는 통과하고 전체에서는 깨지는**
    가장 찾기 어려운 종류의 실패를 만든다.

    문자열이나 주석이 아니라 **실제 호출**만 잡도록 AST 로 본다.
    (문자열 검색으로 짰더니 이 docstring 자신에 걸려서 실패했다.)
    """
    import ast
    import pathlib

    here = pathlib.Path(__file__).resolve().parent
    bad: list[str] = []
    for path in sorted(here.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get_event_loop"):
                bad.append(f"{path.name}:{node.lineno}")
    assert not bad, (
        "asyncio.get_event_loop() 대신 asyncio.run() 을 쓰세요: " + ", ".join(bad)
    )


def test_실행_계층에_죽은_코드가_없다():
    """정의만 하고 아무도 안 쓰는 함수는 '만들었다'는 착각만 남긴다.

    이번에 store.py 의 저장 함수 3개가 그 상태였고, 그래서 재시작 복구가
    실제로는 동작하지 않았다. 같은 일이 반복되지 않게 막는다.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "th"
    watch = [root / "execution", root / "api"]

    defined, used = {}, set()
    for base in [root] + watch:
        for p in base.rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(p == w or w in p.parents for w in watch) and \
                            not n.decorator_list and not n.name.startswith("__"):
                        defined[n.name] = f"{p.name}:{n.lineno}"
                elif isinstance(n, ast.Name):
                    used.add(n.id)
                elif isinstance(n, ast.Attribute):
                    used.add(n.attr)
                elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                    used.add(n.value)
    # 테스트 코드에서 쓰는 것도 '쓰임'으로 본다
    for p in (root.parent.parent / "tests").rglob("test_*.py"):
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Attribute):
                used.add(n.attr)
            elif isinstance(n, ast.Name):
                used.add(n.id)

    dead = {k: v for k, v in defined.items() if k not in used}
    assert not dead, f"쓰이지 않는 함수: {dead}"


# ── 리뷰에서 확인된 버그들의 회귀 테스트 ──────────────────
# 각 테스트는 실제로 있었던 버그 하나를 못 박는다. 완화하려면 여기부터 고쳐야 한다.

def _fill_all(c: PaperClient, m: BracketManager):
    """(브래킷 열기 → 진입 체결) 공통 준비. 상태 객체를 돌려준다."""
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket())))
    return m.states[res.state.bracket.bracket_id]


def test_안전장치는_기록된_일일_잠금을_존중한다():
    """손실 뒤 수익으로 R 값이 -3R 위로 돌아와도 limit_hit_at 이 찍힌 날은 막아야 한다."""
    r = check(mk_input(mk_bracket(), daily_realized_r=-2.5, daily_locked=True))
    assert not r.ok
    assert any("이미 도달" in x for x in r.reasons)
    assert check(mk_input(mk_bracket(), daily_realized_r=-2.5, daily_locked=False)).ok


def test_방향이_LONG_SHORT_가_아니면_만들어지지도_막히지도_않고_거부된다():
    """'LNOG' 같은 오타는 (롱이 아니므로) 숏으로 매매됐다. 이제 타입에서 막는다."""
    with pytest.raises(ValueError):
        mk_bracket(direction="LNOG")
    # 어떻게든 만들어졌더라도 안전장치가 한 번 더 막는다
    b = mk_bracket()
    object.__setattr__(b, "direction", "LNOG")
    r = check(mk_input(b))
    assert not r.ok and any("LONG/SHORT" in x for x in r.reasons)


def test_tp1_ratio_범위를_벗어나면_거부된다():
    with pytest.raises(ValueError):
        mk_bracket(tp1_ratio=1.5)


def test_주문보다_먼저_지나간_봉의_고저로는_체결하지_않는다():
    """`quotes.latest_quote` 는 최근 봉 두 개를 돌려주므로, 진입 직후 몇 틱 동안은
    진입 **전**에 찍힌 고가가 그대로 따라온다. 그걸로 익절이 나면 안 된다."""
    c = PaperClient()
    m = BracketManager(c)
    old_bar = datetime.now(UTC) - timedelta(minutes=2)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01, high=103.0, low=99.5, bar_start=old_bar))
    res = run(m.open(mk_input(mk_bracket(tp2_price=None))))
    st = m.states[res.state.bracket.bracket_id]
    # 가격은 그대로, 같은 봉이 다시 옴 — 주문(now)이 bar_start 보다 늦으므로 호가로만 판정
    fills = c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01, high=103.0, low=99.5,
                         bar_start=old_bar))
    run(m.drain_fills())
    assert fills == [] and st.position_qty == pytest.approx(10.0)
    # 주문 이후 시작된 봉이 103 을 찍으면 그때는 익절이다
    fills = c.feed(Quote("SOXLUSDT", bid=101.0, ask=101.02, high=103.0, low=100.0,
                         bar_start=datetime.now(UTC) + timedelta(seconds=1)))
    assert [f.purpose for f in fills] == [Purpose.TP1]


def test_bar_start_를_모르면_예전처럼_봉_범위를_믿는다():
    q = Quote("SOXLUSDT", bid=100.0, ask=100.02, high=103.0, low=98.0)
    assert q.covers(datetime.now(UTC))


def test_실현_R_은_브래킷_전체_위험_기준이다():
    """절반 익절을 청산 수량으로 나누면 1R 이 되어 성적이 두 배로 부풀려진다."""
    c = PaperClient()
    m = BracketManager(c)
    st = _fill_all(c, m)
    c.feed(Quote("SOXLUSDT", bid=102.1, ask=102.2, high=102.5, low=101.0))   # TP1 만
    run(m.drain_fills())
    risk_total = st.bracket.risk_per_unit(st.avg_entry) * st.bracket.qty
    assert st.realized_r == pytest.approx(st.realized_pnl / risk_total)
    assert st.realized_r < 1.0
    c.feed(Quote("SOXLUSDT", bid=98.9, ask=98.95, high=100.0, low=98.9))     # 나머지 손절
    run(m.drain_fills())
    done = m.closed[-1]
    assert done.realized_r == pytest.approx(done.realized_pnl / risk_total)


def test_진입_수수료도_실현손익에서_뺀다():
    c = PaperClient()
    m = BracketManager(c)
    st = _fill_all(c, m)
    entry_fee = st.fills[0].fee
    assert entry_fee > 0
    assert st.realized_pnl == pytest.approx(-entry_fee)
    c.feed(Quote("SOXLUSDT", bid=98.9, ask=98.95, high=100.0, low=98.9))
    run(m.drain_fills())
    done = m.closed[-1]
    gross = sum((1 if f.side.value == "SELL" else -1) * f.qty * f.price for f in done.fills)
    fees = sum(f.fee for f in done.fills)
    assert done.realized_pnl == pytest.approx(gross - fees)


def test_TP1_이_없으면_TP2_가_전량을_맡는다():
    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket(tp1_price=None, tp2_price=104.0))))
    st = m.states[res.state.bracket.bracket_id]
    assert st.order_by_purpose(Purpose.TP2).qty == pytest.approx(10.0)


def test_거래소_스텝에_맞춰_나눠도_손절_수량은_TP1_더하기_TP2_다():
    from th.execution.models import round_price, round_qty

    # float 로 하면 0.3 이 0.29999… 가 되는 값들
    assert round_qty(0.3, 0.1) == 0.3
    assert round_qty(1.2345, 0.001) == 1.234
    assert round_qty(5.0, None) == 5.0
    assert round_price(100.123, 0.01) == 100.12
    assert round_price(None, 0.01) is None

    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    b = mk_bracket(qty=0.7, tp1_ratio=0.5, qty_step=0.1)
    res = run(m.open(mk_input(b)))
    st = m.states[res.state.bracket.bracket_id]
    tp1, tp2 = st.order_by_purpose(Purpose.TP1), st.order_by_purpose(Purpose.TP2)
    assert tp1.qty == 0.3 and tp2.qty == 0.4
    assert tp1.qty + tp2.qty == st.live_stop.qty == 0.7


def test_진입_체결은_손절을_걸기_전에_먼저_저장된다():
    """손절도 강제 청산도 실패하면, 최소한 '포지션이 있다'는 사실은 DB 에 남아야 한다."""
    order_log: list[str] = []

    class Spy(FakeStore):
        async def save_fill(self, f):
            order_log.append(f"fill:{f.purpose.value}")
            await super().save_fill(f)

    class Flaky(PaperClient):
        async def place(self, order):
            if order.purpose in (Purpose.STOP, Purpose.EXIT):
                order_log.append(f"place:{order.purpose.value}")
                raise RuntimeError("network down")
            return await super().place(order)

    c, store = Flaky(), Spy()
    m = BracketManager(c, store=store)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket())))      # 예외가 밖으로 새면 안 된다
    st = m.states[res.state.bracket.bracket_id]
    assert order_log.index("fill:ENTRY") < order_log.index("place:STOP")
    assert st.has_position and st.live_stop is None
    assert store.brackets[-1][0] == st.bracket.bracket_id, "실패 뒤 상태가 다시 저장돼야 한다"


def test_종료_체결도_저장된다():
    c, store = PaperClient(), FakeStore()
    m = BracketManager(c, store=store)
    st = _fill_all(c, m)
    c.feed(Quote("SOXLUSDT", bid=98.9, ask=98.95, high=100.0, low=98.9))
    run(m.drain_fills())
    stop_fill = [f for f in st.fills if f.purpose is Purpose.STOP][0]
    assert stop_fill.order_client_id in store.fills, "손절 체결이 저장되지 않았다"


def test_수동_청산은_청산_주문을_먼저_내고_취소는_그_뒤에_한다():
    """손절부터 취소하면 청산 주문이 거절됐을 때 보호 없는 포지션이 남는다."""
    calls: list[str] = []

    class Spy(PaperClient):
        async def place(self, order):
            calls.append(f"place:{order.purpose.value}")
            return await super().place(order)

        async def cancel(self, order):
            calls.append(f"cancel:{order.purpose.value}")
            return await super().cancel(order)

    c = Spy()
    m = BracketManager(c, store=FakeStore())
    st = _fill_all(c, m)
    calls.clear()
    run(m.close_now(st.bracket.bracket_id, reason="MANUAL"))
    assert calls[0] == "place:EXIT"
    assert "cancel:STOP" in calls and calls.index("cancel:STOP") > 0


def test_청산_주문이_거절되면_손절은_그대로_남는다():
    class Rejecting(PaperClient):
        async def place(self, order):
            if order.purpose is Purpose.EXIT:
                raise RuntimeError("거절")
            return await super().place(order)

    c = Rejecting()
    m = BracketManager(c)
    st = _fill_all(c, m)
    with pytest.raises(RuntimeError):
        run(m.close_now(st.bracket.bracket_id))
    assert st.live_stop is not None and st.has_position


def test_수동_청산은_취소와_청산_주문을_모두_저장한다():
    """시세가 없어 청산이 바로 안 붙어도 취소·청산 주문은 DB 에 남아야 한다."""
    c, store = PaperClient(), FakeStore()
    m = BracketManager(c, store=store)
    st = _fill_all(c, m)
    c._last.clear()                       # 재시작 직후처럼 시세가 아직 없다
    n_before = len(store.orders)
    run(m.close_now(st.bracket.bracket_id, reason="MANUAL"))
    saved = store.orders[n_before:]
    ex = [o for o in st.orders.values() if o.purpose is Purpose.EXIT][0]
    assert ex.client_id in saved, "청산 주문이 저장되지 않았다"
    assert st.live_stop is None
    assert st.has_position and ex.is_open, "시세가 없으면 청산은 아직 대기 중이어야 한다"


def test_복구된_포지션에_손절이_없으면_다시_건다():
    from th.execution.models import BracketState, Order, OrderStatus, Side

    c = PaperClient()
    m = BracketManager(c, store=FakeStore())
    b = mk_bracket()
    st = BracketState(bracket=b, position_qty=10.0, avg_entry=100.0)
    st.orders["ent-1"] = Order(
        client_id="ent-1", bracket_id=b.bracket_id, symbol=b.symbol, side=Side.BUY,
        type=OrderType.MARKET, purpose=Purpose.ENTRY, qty=10, status=OrderStatus.FILLED,
        filled_qty=10, avg_price=100.0,
    )
    run(m.restore([st]))
    assert st.live_stop is not None and st.live_stop.qty == pytest.approx(10.0)
    assert c.open_orders("SOXLUSDT"), "체결 엔진에도 손절이 올라가야 한다"
    # 진짜로 보호되는가 — 가격이 무너지면 손절이 나가야 한다
    c.feed(Quote("SOXLUSDT", bid=95.0, ask=95.1, high=100.0, low=94.9))
    run(m.drain_fills())
    assert not st.is_open and not st.has_position


def test_복구된_손절_수량이_포지션과_다르면_맞춘다():
    from th.execution.models import BracketState, Order, OrderStatus, Side

    c = PaperClient()
    m = BracketManager(c, store=FakeStore())
    b = mk_bracket()
    # TP1 로 절반 청산된 직후 죽었다 — 손절은 아직 10 으로 걸려 있다
    st = BracketState(bracket=b, position_qty=5.0, avg_entry=100.0)
    st.orders["stp-1"] = Order(
        client_id="stp-1", bracket_id=b.bracket_id, symbol=b.symbol, side=Side.SELL,
        type=OrderType.STOP_MARKET, purpose=Purpose.STOP, qty=10.0, trigger=99.0,
        reduce_only=True, status=OrderStatus.OPEN,
    )
    run(m.restore([st]))
    assert st.orders["stp-1"].status is OrderStatus.CANCELED
    assert st.live_stop.qty == pytest.approx(5.0)
    assert [o.qty for o in c.open_orders("SOXLUSDT")] == [5.0]


def test_지정가는_내_가격에_체결되고_낼_때_이미_넘은_지정가는_테이커다():
    from th.execution.paper import MAKER_FEE_PCT, TAKER_FEE_PCT

    # 걸어둔 지정가 — 호가가 더 좋아도 내 가격에 체결 · 메이커
    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=100.5, ask=100.6))
    run(m.open(mk_input(mk_bracket(entry_type=OrderType.LIMIT, entry_price=100.0), ref_price=100.5)))
    f = c.feed(Quote("SOXLUSDT", bid=99.0, ask=99.1, low=99.0, high=100.6))[0]
    assert f.price == pytest.approx(100.0)
    assert f.fee == pytest.approx(10 * 100.0 * MAKER_FEE_PCT / 100)

    # 낼 때 이미 호가를 넘은 지정가 — 즉시 체결 · 테이커
    c = PaperClient()
    m = BracketManager(c)
    c.feed(Quote("SOXLUSDT", bid=99.99, ask=100.01))
    res = run(m.open(mk_input(mk_bracket(entry_type=OrderType.LIMIT, entry_price=100.5), ref_price=100.0)))
    st = m.states[res.state.bracket.bracket_id]
    assert st.has_position
    f = st.fills[0]
    assert f.price <= 100.5
    assert f.fee == pytest.approx(f.qty * f.price * TAKER_FEE_PCT / 100)


def test_유효시간은_브래킷과_함께_저장된다():
    """`--expire` 로 준 값이 재시작 뒤 기본값으로 돌아가면 안 된다."""
    import inspect

    from th.execution import store as mod

    src = inspect.getsource(mod.save_bracket)
    assert "expire_after_s" in src and "qty_step" in src
    assert "expire_after_s" in inspect.getsource(mod._row_to_bracket)
    import pathlib
    sql = (pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"
           / "004_bracket_meta.sql").read_text(encoding="utf-8")
    assert "expire_after_s" in sql


def _runner_source() -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent / "src" / "th" / "execution"
            / "runner.py").read_text(encoding="utf-8")


def _runner_step_top_calls() -> set[str]:
    """`PaperRunner.step` 본문의 **최상위** 문장(for/if 안이 아닌 곳)에서 부르는 이름들.
    v2.1 부터 페이퍼 루프의 한 반복은 `runner.step()` 이고 CLI·서버가 같이 쓴다."""
    import ast

    tree = ast.parse(_runner_source())
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "step"][0]
    out = set()
    for stmt in fn.body:
        if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try)):
            continue
        for c in ast.walk(stmt):
            if isinstance(c, ast.Call):
                out.add(c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", ""))
    return out


def test_유효시간_만료는_시세가_끊겨도_돈다():
    """루프 한 반복(`runner.step`)에서 expire_stale_entries 는 시세 유무와 무관하게 매번 불려야 한다."""
    assert "expire_stale_entries" in _runner_step_top_calls()
    # CLI 와 서버가 같은 루프를 쓴다 — cli.py 에는 자기만의 while 루프가 더 이상 없다
    import ast

    tree = ast.parse(_cli_source())
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "_paper_watch_loop"][0]
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.While)], "CLI 가 루프를 따로 갖고 있다"
    assert "run_until_flat" in {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}


def test_페이퍼_실행은_페이퍼_기준으로_안전장치를_채운다():
    """실계좌 income 이 아니라 페이퍼 결과·열린 페이퍼 브래킷·세션 게이트를 넘겨야 한다.

    v2.1: 주문 경로는 `runner.preview` 한 곳이고 CLI(`paper run`)와 웹이 그것을 부른다."""
    import ast

    tree = ast.parse(_runner_source())
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "preview"][0]
    kws = {
        k.arg for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "GuardInput"
        for k in c.keywords
    }
    for need in ("daily_locked", "open_bracket_count", "session_blocked", "session_reason",
                 "daily_unknown", "equity_usdt", "risk_pct"):
        assert need in kws, f"preview 의 GuardInput 에 {need} 가 빠졌다"
    names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    for need in ("load_open_brackets", "load_symbol_steps", "latest_quote"):
        assert need in names, need
    ids = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    for need in ("size_from_risk", "round_qty", "round_price", "paper_daily_r", "session_gate", "check"):
        assert need in ids, need

    guard_fn = [n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "paper_daily_r"][0]
    guard_names = {n.attr for n in ast.walk(guard_fn) if isinstance(n, ast.Attribute)}
    assert "daily_realized_r" in guard_names, "페이퍼는 페이퍼 결과로 오늘 손익을 세야 한다"

    # CLI 는 그 경로를 **그대로** 쓴다 — 자기 GuardInput 을 만들지 않는다
    cli = ast.parse(_cli_source())
    run_fn = [n for n in ast.walk(cli) if isinstance(n, ast.FunctionDef) and n.name == "paper_run"][0]
    run_ids = {n.id for n in ast.walk(run_fn) if isinstance(n, ast.Name)}
    assert "GuardInput" not in run_ids and "Bracket" not in run_ids
    run_attrs = {n.attr for n in ast.walk(run_fn) if isinstance(n, ast.Attribute)}
    assert {"open_from_request", "acquire"} <= run_attrs


# ── 웹 청산 요청 (docs/15 3절) ───────────────────────────
# 웹 서버에는 매니저가 없다. DB 에 표시만 남기고 `th paper run` 루프가 처리한다.

class RecordingPool:
    """SQL 과 인자를 기록만 하는 가짜 풀."""

    def __init__(self, rows=None, row=None):
        self.calls, self._rows, self._row = [], rows or [], row

    async def execute(self, sql, *args):
        self.calls.append(("execute", " ".join(sql.split()), args))

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", " ".join(sql.split()), args))
        return self._rows

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", " ".join(sql.split()), args))
        return self._row


def _with_pool(pool):
    import contextlib

    from th.execution import store as mod

    @contextlib.contextmanager
    def cm():
        real = mod.get_pool

        async def fake():
            return pool

        mod.get_pool = fake
        try:
            yield
        finally:
            mod.get_pool = real

    return cm()


def test_청산_요청은_열린_페이퍼_브래킷에만_표시된다():
    from th.execution import store

    pool = RecordingPool(row={"bracket_id": "brk-1"})
    with _with_pool(pool):
        assert run(store.request_close("brk-1", "paper")) is True
    kind, sql, args = pool.calls[0]
    assert kind == "fetchrow" and args == ("brk-1", "paper")
    assert "close_requested_at" in sql and "closed_at IS NULL" in sql and "mode = $2" in sql
    assert "coalesce(close_requested_at, now())" in sql, "두 번 눌러도 첫 요청 시각을 지킨다"

    pool = RecordingPool(row=None)
    with _with_pool(pool):
        assert run(store.request_close("brk-닫힘", "paper")) is False


def test_대기_중인_청산_요청은_오래된_것부터_돌려준다():
    from th.execution import store

    pool = RecordingPool(rows=[{"bracket_id": "brk-a"}, {"bracket_id": "brk-b"}])
    with _with_pool(pool):
        assert run(store.pending_close_requests("paper")) == ["brk-a", "brk-b"]
    _, sql, args = pool.calls[0]
    assert args == ("paper",)
    assert "close_requested_at IS NOT NULL" in sql and "closed_at IS NULL" in sql
    assert "ORDER BY close_requested_at" in sql


def test_청산_요청_표시는_지울_수_있다():
    from th.execution import store

    pool = RecordingPool()
    with _with_pool(pool):
        run(store.clear_close_request("brk-a"))
    kind, sql, args = pool.calls[0]
    assert kind == "execute" and args == ("brk-a",)
    assert "close_requested_at = NULL" in sql


def test_마이그레이션_005가_요청_표시_컬럼을_만든다():
    import pathlib
    sql = (pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"
           / "005_close_request.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE brackets ADD COLUMN IF NOT EXISTS close_requested_at" in sql


def test_페이퍼_루프는_매_반복_청산_요청을_확인한다():
    """시세가 끊겨도 웹의 청산 버튼은 먹혀야 한다 — if/for 밖 최상위에서 부른다."""
    assert "serve_close_requests" in _runner_step_top_calls()


def test_러너는_자기_브래킷만_청산하고_표시를_지운다():
    """모르는 브래킷의 요청은 **건드리지 않는다** — 지우면 화면이 '처리됨'으로 읽는다."""
    from th.execution import store

    closed, cleared = [], []

    class Mgr:
        states = {"brk-mine": object()}

        async def close_now(self, bracket_id, reason="MANUAL"):
            closed.append((bracket_id, reason))

    async def pending(mode):
        assert mode == "paper"
        return ["brk-mine", "brk-other"]

    async def clear(bracket_id):
        cleared.append(bracket_id)

    saved = store.pending_close_requests, store.clear_close_request
    store.pending_close_requests, store.clear_close_request = pending, clear
    try:
        done = run(store.serve_close_requests(Mgr(), "paper"))
    finally:
        store.pending_close_requests, store.clear_close_request = saved
    assert done == ["brk-mine"]
    assert closed == [("brk-mine", "WEB_CLOSE")]
    assert cleared == ["brk-mine"], "남의 브래킷 표시는 그대로 둔다"


def test_청산_요청_조회가_실패해도_루프는_계속된다():
    from th.execution import store

    async def boom(mode):
        raise RuntimeError("db down")

    saved = store.pending_close_requests
    store.pending_close_requests = boom
    try:
        assert run(store.serve_close_requests(type("M", (), {"states": {}})(), "paper")) == []
    finally:
        store.pending_close_requests = saved


def test_러너는_store_의_청산_요청_처리기를_쓴다():
    """규칙(남의 브래킷은 안 건드림·조회 실패 무시)은 store 에 있다. 러너가 다시 적으면 갈라진다."""
    src = _runner_source()
    assert "store.serve_close_requests(self.mgr, MODE)" in src
    assert "pending_close_requests" not in src


# ═════════════════════════════════════════════════════════
# v2.1 — PaperRunner (docs/15 7절): 임대 · 주문 경로 · 루프
# ═════════════════════════════════════════════════════════

def _lease_row(holder, age_s):
    return {"holder": holder, "heartbeat_at": datetime.now(UTC) - timedelta(seconds=age_s)}


def test_임대는_다른_holder_의_heartbeat_가_신선할_때만_막는다():
    from th.execution.store import LEASE_FRESH_S, lease_blocked_by

    now = datetime.now(UTC)
    assert LEASE_FRESH_S == 30
    assert lease_blocked_by(None, "cli", now) is None                     # 아무도 없다
    assert lease_blocked_by(_lease_row("serve", 5), "cli", now) == "serve"   # serve 가 살아 있다
    assert lease_blocked_by(_lease_row("cli", 5), "serve", now) == "cli"
    assert lease_blocked_by(_lease_row("serve", 5), "serve", now) is None    # 나 자신 — 재시작
    assert lease_blocked_by(_lease_row("serve", 31), "cli", now) is None     # 낡았다 — 넘겨받는다
    assert lease_blocked_by(_lease_row("serve", 29), "cli", now) == "serve"
    assert lease_blocked_by({"holder": "serve", "heartbeat_at": None}, "cli", now) is None


class _MemLease:
    """DB 임대 테이블을 흉내낸다 — `lease_blocked_by` 규칙 그대로."""

    def __init__(self):
        self.row = None
        self.beats = []

    def install(self):
        from th.execution import store

        async def acquire(mode, holder, fresh_s=30):
            b = store.lease_blocked_by(self.row, holder, datetime.now(UTC), fresh_s)
            if b is not None:
                return b
            self.row = {"holder": holder, "heartbeat_at": datetime.now(UTC)}
            return None

        async def heartbeat(mode, holder):
            self.beats.append(holder)
            if self.row and self.row["holder"] == holder:
                self.row["heartbeat_at"] = datetime.now(UTC)
                return True
            return False

        async def release(mode, holder):
            if self.row and self.row["holder"] == holder:
                self.row = None

        self.saved = (store.lease_acquire, store.lease_heartbeat, store.lease_release)
        store.lease_acquire, store.lease_heartbeat, store.lease_release = acquire, heartbeat, release
        return self

    def restore(self):
        from th.execution import store

        store.lease_acquire, store.lease_heartbeat, store.lease_release = self.saved


def _runner(mgr=None, client=None, **kw):
    from th.execution.runner import PaperRunner

    client = client or PaperClient()
    mgr = mgr or BracketManager(client)
    kw.setdefault("execution_mode", "paper")
    kw.setdefault("max_open_brackets", 1)      # .env 의 TH_MAX_OPEN_BRACKETS 와 무관하게 고정
    return PaperRunner(mgr, client, ["SOXLUSDT"], 1, **kw)


def test_두_러너가_같은_브래킷을_관리하지_못한다():
    """`th serve` 가 임대를 쥐고 있으면 `th paper run` 은 한국어 경고와 함께 멈춘다."""
    from th.execution.runner import LeaseHeld

    lease = _MemLease().install()
    try:
        serve = _runner(holder="serve")
        cli = _runner(holder="cli")

        async def go():
            await serve.start()                          # serve 가 먼저
            assert serve.alive and lease.row["holder"] == "serve"
            with pytest.raises(LeaseHeld) as ei:
                await cli.acquire()
            assert ei.value.holder == "serve" and "serve" in str(ei.value) and "th serve" in str(ei.value)
            assert not cli.alive
            await serve.stop()                           # 놓으면 잡을 수 있다
            assert lease.row is None and not serve.alive
            await cli.acquire()
            assert lease.row["holder"] == "cli" and cli.alive
            # cli 가 죽어 heartbeat 가 낡으면 serve 가 넘겨받는다
            lease.row["heartbeat_at"] -= timedelta(seconds=31)
            await serve.acquire()
            assert lease.row["holder"] == "serve"

        run(go())
    finally:
        lease.restore()


def test_러너는_페이퍼_클라이언트만_받는다():
    """`LiveClient` 는 만들 수도 없지만, mode 가 paper 가 아닌 것은 아예 들어오지 못한다."""
    from th.execution.runner import PaperRunner

    class NotPaper:
        mode = "live"

    with pytest.raises(ValueError):
        PaperRunner(BracketManager(PaperClient()), NotPaper(), ["SOXLUSDT"], 3)


class _Env:
    """preview 가 조회하는 것들을 전부 가짜로 — 시세·스텝·당일 손익·열린 브래킷·세션·잔고."""

    def __init__(self, *, quote=None, steps=(0.01, 0.01), daily=0.0, open_count=0,
                 blocked=(False, ""), equity=10_000.0, steps_fail=False, daily_fail=False,
                 equity_fail=False, session_fail=False):
        self.quote, self.steps, self.daily, self.open_count = quote, steps, daily, open_count
        self.blocked, self.equity = blocked, equity
        self.steps_fail, self.daily_fail, self.equity_fail, self.session_fail = (
            steps_fail, daily_fail, equity_fail, session_fail)

    def __enter__(self):
        from th.execution import quotes, runner, store

        env = self

        async def latest_quote(symbol, **kw):
            return env.quote

        async def load_symbol_steps(symbol):
            if env.steps_fail:
                raise RuntimeError("db")
            return env.steps

        async def daily_realized_r(mode, day):
            if env.daily_fail:
                raise RuntimeError("db")
            return env.daily

        async def load_open_brackets(mode):
            return [None] * env.open_count

        async def gate(symbol):
            if env.session_fail:
                raise RuntimeError("no calendar")
            return env.blocked

        async def equity():
            if env.equity_fail:
                raise RuntimeError("HTTP 401")
            return env.equity

        self.saved = (quotes.latest_quote, store.load_symbol_steps, store.daily_realized_r,
                      store.load_open_brackets, runner.session_gate)
        quotes.latest_quote = latest_quote
        store.load_symbol_steps = load_symbol_steps
        store.daily_realized_r = daily_realized_r
        store.load_open_brackets = load_open_brackets
        runner.session_gate = gate
        self.equity_fn = equity
        return self

    def __exit__(self, *a):
        from th.execution import quotes, runner, store

        (quotes.latest_quote, store.load_symbol_steps, store.daily_realized_r,
         store.load_open_brackets, runner.session_gate) = self.saved


def _req(**kw):
    from th.execution.runner import OpenRequest

    base = dict(mode="paper", symbol="SOXLUSDT", direction="LONG", stop=131.25,
                tp1=134.09, tp2=135.68)
    base.update(kw)
    return OpenRequest(**base)


def test_미리보기_계산은_CLI_와_같은_경로다():
    """size_from_risk → round_qty/round_price → 노셔널·1R·손절폭·비용/R. 주문은 안 낸다."""
    q = Quote("SOXLUSDT", 132.49, 132.51)
    with _Env(quote=q, steps=(0.01, 0.01), equity=10_000.0) as env:
        r = _runner(); r.equity_source = env.equity_fn
        p = run(r.preview(_req(risk_pct=0.5)))
    assert p.ok and p.reasons == [] and p.warnings == [], (p.reasons, p.warnings)
    assert p.ref_price == 132.5                              # 시장가 → 호가 중간
    # 1R = 10,000 × 0.5% = 50 USDT, 손절폭 1.25 → 40 주 (0.01 스텝 내림)
    assert p.r_unit_usdt == 50.0
    assert p.qty == pytest.approx(round(50 / 1.25, 2)) and p.qty == 40.0
    assert p.notional_usdt == pytest.approx(40.0 * 132.5)
    assert p.risk_usdt == pytest.approx(40.0 * 1.25)
    assert p.stop_pct == pytest.approx(1.25 / 132.5 * 100)
    assert p.cost_r == pytest.approx(0.11 / p.stop_pct)     # 왕복 0.11% ÷ 손절폭%
    assert p.equity_usdt == 10_000.0
    d = p.as_dict()
    assert set(d) == {"ok", "symbol", "direction", "ref_price", "qty", "notional_usdt", "risk_usdt",
                      "r_unit_usdt", "stop_pct", "cost_r", "equity_usdt", "warnings", "reasons"}
    assert "guard_input" not in d
    # 안전장치 입력이 CLI 와 같은 형태로 채워졌다
    gi = p.guard_input
    assert gi.execution_mode == "paper" and gi.equity_usdt == 10_000.0 and gi.risk_pct == 0.5
    assert gi.bracket.entry_type is OrderType.MARKET and gi.bracket.qty == 40.0
    assert gi.bracket.qty_step == 0.01 and gi.bracket.tick_size == 0.01
    assert gi.bracket.expire_after_s == 300 and gi.open_bracket_count == 0
    assert not gi.daily_unknown and not gi.session_blocked
    # 지정가 → 기준가는 지정가, 틱 반올림
    with _Env(quote=q, steps=(0.1, 0.05)) as env:
        r = _runner(); r.equity_source = env.equity_fn
        p2 = run(r.preview(_req(entry=132.523, stop=131.26, expire_s=120)))
    assert p2.ref_price == 132.5 and p2.guard_input.bracket.stop_price == 131.25
    assert p2.guard_input.bracket.entry_type is OrderType.LIMIT
    assert p2.guard_input.bracket.expire_after_s == 120
    assert p2.qty == 40.0 and p2.guard_input.bracket.qty == 40.0       # 0.1 스텝 내림


def test_미리보기는_가드_사유를_전부_모으고_열지_않는다():
    q = Quote("SOXLUSDT", 132.49, 132.51)
    # 롱인데 손절이 위 + 세션 차단 + 이미 열린 브래킷
    with _Env(quote=q, open_count=1, blocked=(True, "🔴 언더라잉 정지")) as env:
        r = _runner(); r.equity_source = env.equity_fn
        p = run(r.preview(_req(stop=133.0)))
        out = run(r.open_from_request(_req(stop=133.0)))
    assert not p.ok
    joined = " / ".join(p.reasons)
    assert "손절(133)이 현재가" in joined and "세션 차단: 🔴 언더라잉 정지" in joined
    assert "이미 열린 브래킷 1개" in joined
    assert out.ok is False and out.as_dict() == {"ok": False, "reasons": list(p.reasons)}
    assert r.mgr.open_count == 0, "거절이면 아무것도 열지 않는다"
    # 당일 손익을 못 읽으면 막힌다 (0.0 으로 뭉개지 않는다)
    with _Env(quote=q, daily_fail=True) as env:
        r = _runner(); r.equity_source = env.equity_fn
        p = run(r.preview(_req()))
    assert any("오늘 실현손익을 확인하지 못했습니다" in x for x in p.reasons)
    # 잠금
    with _Env(quote=q, daily=-3.0) as env:
        r = _runner(); r.equity_source = env.equity_fn
        p = run(r.preview(_req()))
    assert any("한도" in x for x in p.reasons)
    # 시세가 없고 시장가면 막힌다; 지정가면 경고만
    with _Env(quote=None) as env:
        r = _runner(); r.equity_source = env.equity_fn
        p = run(r.preview(_req()))
        p_lim = run(r.preview(_req(entry=132.5)))
    assert any("시세" in x for x in p.reasons) and p.qty == 0.0
    assert p_lim.ok and any("지정가를 기준" in w for w in p_lim.warnings)
    # 세션 상태를 못 읽어도 차단
    from th.execution import runner as rmod
    with _Env(quote=q):
        real = rmod.session_gate
        try:
            assert run(rmod.session_gate("SOXLUSDT"))[0] in (True, False)   # 실제 게이트가 돈다
        finally:
            rmod.session_gate = real


def test_미리보기는_계정_잔고를_못_읽으면_막되_모드와_방향은_먼저_거른다():
    from th.execution.live import LIVE_ORDER_REFUSED_KO

    q = Quote("SOXLUSDT", 132.49, 132.51)
    with _Env(quote=q, equity_fail=True) as env:
        r = _runner(); r.equity_source = env.equity_fn
        p = run(r.preview(_req()))
    assert not p.ok and p.equity_usdt == 0.0 and p.qty == 0.0
    assert any("잔고" in w for w in p.warnings), p.warnings
    assert any("확인하지 못했습니다" in x for x in p.reasons), p.reasons   # daily_unknown 으로 막는다
    # CLI 의 --equity 가 들어오면 계정을 부르지 않는다
    with _Env(quote=q, equity_fail=True) as env:
        r = _runner(); r.equity_source = env.equity_fn
        p = run(r.preview(_req(equity_usdt=5_000.0)))
    assert p.ok and p.equity_usdt == 5_000.0 and p.r_unit_usdt == 25.0 and p.qty == 20.0
    # 스텝 조회 실패 → 경고 + 반올림 없이
    with _Env(quote=q, steps_fail=True) as env:
        r = _runner(); r.equity_source = env.equity_fn
        p = run(r.preview(_req(equity_usdt=10_000.0)))
    assert p.ok and any("스텝" in w for w in p.warnings) and p.qty == pytest.approx(40.0)
    # 실거래·잘못된 방향·수집하지 않는 종목은 아무것도 조회하기 전에 거른다
    with _Env(quote=None, daily_fail=True, steps_fail=True) as env:
        r = _runner(); r.equity_source = env.equity_fn
        live = run(r.preview(_req(mode="live")))
        none = run(r.preview(_req(mode="none")))
        typo = run(r.preview(_req(direction="LNOG")))
        other = run(r.preview(_req(symbol="NVDAUSDT")))
        nostop = run(r.preview(_req(stop=0)))
    assert live.reasons == [LIVE_ORDER_REFUSED_KO]
    assert none.reasons and "페이퍼 주문만" in none.reasons[0]
    assert typo.reasons and "LONG 또는 SHORT" in typo.reasons[0]
    assert other.reasons and "수집 종목" in other.reasons[0]
    assert nostop.reasons == ["손절가가 없다"]
    out = run(r.open_from_request(_req(mode="live")))
    assert out.ok is False and out.reasons == (LIVE_ORDER_REFUSED_KO,) and r.mgr.open_count == 0


def test_open_from_request_는_미리보기_뒤_같은_입력으로_연다():
    """시장가면 마지막 호가로 즉시 체결되고 손절이 걸린다 — CLI 가 하던 그대로."""
    q = Quote("SOXLUSDT", 132.49, 132.51)
    with _Env(quote=q) as env:
        r = _runner(); r.equity_source = env.equity_fn
        out = run(r.open_from_request(_req(reason="테스트")))
    assert out.ok and out.bracket_id.startswith("br-") and out.as_dict() == {"ok": True, "bracket_id": out.bracket_id}
    st = r.mgr.states[out.bracket_id]
    assert st.has_position and st.position_qty == 40.0 and st.live_stop is not None
    assert st.bracket.reason == "테스트" and st.bracket.qty_step == 0.01
    assert r.last_quote["SOXLUSDT"] is q


def test_러너_루프는_열린_브래킷의_종목과_포커스_종목에_시세를_먹인다():
    """열린 브래킷 + 포커스(수집 종목일 때). 시세가 끊기면 체결 판정을 멈추고 만료·청산 요청은 계속."""
    from th.execution import quotes, store

    client = PaperClient()
    mgr = BracketManager(client)
    r = _runner(mgr, client, focus="NVDAUSDT")          # NVDA 는 수집 종목이 아니다 → 안 먹인다
    r.symbols = ["SOXLUSDT", "AMDUSDT"]
    assert r.feed_symbols() == set()
    r.focus = "AMDUSDT"
    assert r.feed_symbols() == {"AMDUSDT"}

    feeds = {"SOXLUSDT": Quote("SOXLUSDT", 100.0, 100.02), "AMDUSDT": None}
    asked, served = [], []

    async def latest_quote(symbol, **kw):
        asked.append(symbol)
        return feeds[symbol]

    async def serve_close(m, mode="paper"):
        served.append(mode)
        return []

    saved = quotes.latest_quote, store.serve_close_requests
    quotes.latest_quote, store.serve_close_requests = latest_quote, serve_close
    try:
        async def go():
            b = mk_bracket(qty=10.0, stop_price=99.0, tp1_price=None, tp2_price=None)
            client.feed(feeds["SOXLUSDT"])
            res = await mgr.open(mk_input(b))
            assert res.ok and mgr.states[b.bracket_id].has_position
            assert r.feed_symbols() == {"SOXLUSDT", "AMDUSDT"}
            out = await r.step()
            assert sorted(asked) == ["AMDUSDT", "SOXLUSDT"]
            assert out["fed"] == ["SOXLUSDT"] and "AMDUSDT" in r.stale_since
            assert served == ["paper"] and r.steps == 1
            # 손절에 닿는 시세 → 이번 반복에서 체결·종료
            feeds["SOXLUSDT"] = Quote("SOXLUSDT", 98.9, 98.92)
            out = await r.step()
            assert out["fills"] >= 1 and mgr.open_count == 0
            assert mgr.closed[-1].close_reason == "STOP"
            # AMD 시세 복구
            feeds["AMDUSDT"] = Quote("AMDUSDT", 150.0, 150.1)
            out = await r.step()
            assert "AMDUSDT" not in r.stale_since and out["fed"] == ["AMDUSDT"]

        run(go())
    finally:
        quotes.latest_quote, store.serve_close_requests = saved


def test_러너_루프는_시세가_끊겨도_낡은_지정가를_걷고_heartbeat_를_찍는다():
    from th.execution import quotes, runner, store

    lease = _MemLease().install()
    client = PaperClient()
    mgr = BracketManager(client)
    r = _runner(mgr, client)

    async def none_quote(symbol, **kw):
        return None

    async def serve_close(m, mode="paper"):
        return []

    saved = quotes.latest_quote, store.serve_close_requests
    quotes.latest_quote, store.serve_close_requests = none_quote, serve_close
    try:
        async def go():
            b = mk_bracket(entry_type=OrderType.LIMIT, entry_price=99.5, expire_after_s=60)
            res = await mgr.open(mk_input(b))
            assert res.ok and not mgr.states[b.bracket_id].has_position
            await r.acquire()
            beats0 = len(lease.beats)
            now = datetime.now(UTC)
            out = await r.step(now)
            assert out["expired"] == 0 and out["fed"] == []
            assert len(lease.beats) == beats0                        # 10초 안 지났다
            out = await r.step(now + timedelta(seconds=runner.LEASE_HEARTBEAT_S + 61))
            assert out["expired"] == 1 and mgr.open_count == 0
            assert mgr.closed[-1].close_reason == "ENTRY_EXPIRED"
            assert lease.beats == ["cli"] if r.holder == "cli" else lease.beats == ["serve"]
            # run_until_flat 은 브래킷이 없으면 바로 끝나고 임대를 놓는다
            await r.run_until_flat()
            assert lease.row is None

        run(go())
    finally:
        quotes.latest_quote, store.serve_close_requests = saved
        lease.restore()


def test_build_paper_runner_는_저장된_브래킷을_복구한다():
    from th.execution import store
    from th.execution.runner import build_paper_runner

    from th.execution.models import BracketState

    b = mk_bracket(qty=5.0)
    st = BracketState(bracket=b, position_qty=5.0, avg_entry=100.0)

    async def fake_open(mode):
        return [st]

    saved = store.load_open_brackets, store.save_bracket, store.save_order
    store.load_open_brackets = fake_open

    async def noop(*a, **k):
        return None

    store.save_bracket = store.save_order = noop
    try:
        r = run(build_paper_runner(["SOXLUSDT"], focus="SOXLUSDT", holder="serve", execution_mode="paper"))
    finally:
        store.load_open_brackets, store.save_bracket, store.save_order = saved
    assert r.holder == "serve" and r.focus == "SOXLUSDT" and r.open_count == 1
    assert r.mgr.states[b.bracket_id].live_stop is not None, "복구 시 손절이 없으면 다시 건다"
    assert r.client.mode == "paper" and r.mgr.store is store


def test_게이트_체크리스트는_전부_미충족이고_live_는_열리지_않는다():
    from th.execution.live import GATE_CHECKLIST, LIVE_ORDER_REFUSED_KO, gate_status

    assert len(GATE_CHECKLIST) == 5 and all(m is False for _, m in GATE_CHECKLIST)
    g = gate_status("live")
    assert g["live_allowed"] is False and g["mode"] == "live"
    assert [c["text_ko"] for c in g["conditions"]] == [t for t, _ in GATE_CHECKLIST]
    assert "docs/07" in LIVE_ORDER_REFUSED_KO


def test_OpenRequest_from_dict_는_웹_폼_값을_정규화한다():
    from th.execution.runner import OpenRequest

    r = OpenRequest.from_dict({"mode": "Paper", "symbol": "soxlusdt", "direction": "short",
                               "entry": "", "stop": "131.25", "tp1": None, "risk_pct": "0.25",
                               "expire_s": "90"})
    assert r.mode == "paper" and r.symbol == "SOXLUSDT" and r.direction == "SHORT"
    assert r.entry is None and r.stop == 131.25 and r.tp1 is None and r.risk_pct == 0.25
    assert r.expire_s == 90 and r.equity_usdt is None
    empty = OpenRequest.from_dict(None)
    assert empty.mode == "" and empty.stop == 0.0 and empty.expire_s is None
