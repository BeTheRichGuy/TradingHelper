"""웹 대시보드 테스트.

브라우저를 띄우지 않고도 못 박을 수 있는 것들을 고정한다. 특히
**빈 사유 목록을 초록불로 그리지 않는다**는 규칙 — 이건 스키마
(`is_permission_to_trade`) 로 한 번, 화면 코드로 한 번 더 막아야 한다.
사람은 빈 경고창을 초록불로 읽기 때문이다.
"""

from __future__ import annotations

import pathlib
import re

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "src" / "th" / "api" / "web"
HTML = WEB / "index.html"
SERVER = (pathlib.Path(__file__).resolve().parent.parent
          / "src" / "th" / "api" / "server.py")


# fixture 를 쓰지 않고 모듈 상수로 읽는다.
# (fixture 를 쓰면 테스트 러너에 따라 못 도는 경우가 있어, 검증 환경을 좁힌다)
HTML_SRC = HTML.read_text(encoding="utf-8")
SERVER_SRC = SERVER.read_text(encoding="utf-8")


# ── 화면 규칙 ───────────────────────────────────────────
def test_외부_리소스를_하나도_쓰지_않는다():
    """인터넷이 끊겨도, CDN 이 죽어도 화면은 떠야 한다."""
    ext = re.findall(r'(?:src|href)\s*=\s*"https?://[^"]+', HTML_SRC)
    assert not ext, f"외부 리소스: {ext}"


def test_브라우저_저장소를_쓰지_않는다():
    for bad in ("localStorage", "sessionStorage", "indexedDB"):
        assert bad not in HTML_SRC, f"{bad} 를 쓰면 안 된다"


def test_빈_사유_목록을_초록불로_그리지_않는다():
    """`reasons` 가 비었을 때 타는 분기에 초록 계열이 들어가면 안 된다."""
    m = re.search(r"function paintReasons\(\)\{(.*?)\n\}", HTML_SRC, re.S)
    assert m, "paintReasons 를 찾지 못했다 (이름을 바꿨다면 이 테스트도 같이 고칠 것)"
    body = m.group(1)
    empty_branch = body.split("return;")[-1]         # reasons 가 있을 때는 위에서 return
    for bad in ("--up", "green", "허가", "매매 가능", "진입 가능"):
        assert bad not in empty_branch, f"빈 사유 분기에 '{bad}' 가 있다"


def test_사유가_비면_서버가_준_문장을_그대로_쓴다():
    assert "note_ko" in HTML_SRC, "빈 목록일 때 서버 문장(note_ko)을 표시해야 한다"


def test_방향을_제시하는_말이_화면에_없다():
    """Phase 4 판정으로 신호 카드가 빠졌다. 화면에도 남아 있으면 안 된다."""
    for bad in ("매수 신호", "매도 신호", "롱 진입", "숏 진입", "승률 예상", "signal_card"):
        assert bad not in HTML_SRC, f"방향 제시 문구가 남아 있다: {bad}"


def test_오래된_값을_오래됐다고_표시한다():
    assert "오래" in HTML_SRC, "갱신이 끊겼을 때 그 사실을 화면에 알려야 한다"
    assert "onclose" in HTML_SRC, "연결이 끊기면 재접속해야 한다"


def test_차트는_서버가_계산한_값을_그리기만_한다():
    """지표 계산이 브라우저로 새어나가면 웹과 앱이 갈라진다."""
    for bad in ("function ema(", "function calcEma", "function vwap(", "function sma("):
        assert bad not in HTML_SRC, f"브라우저에서 지표를 계산하고 있다: {bad}"
    for key in ("ema9", "ema21", "ema99", "vwap"):
        assert key in HTML_SRC, f"서버가 준 {key} 를 그려야 한다"


# ── 서버 규칙 ───────────────────────────────────────────
def test_방향을_주는_엔드포인트가_없다():
    routes = re.findall(r'@app\.(?:get|post|websocket)\("([^"]+)"', SERVER_SRC)
    assert routes, "라우트를 못 찾았다"
    for r in routes:
        assert not any(k in r for k in ("signal", "predict", "recommend", "advice")), \
            f"방향을 제시하는 엔드포인트: {r}"


def test_사유_계산을_다시_적지_않고_공유한다():
    """터미널과 웹이 갈라지지 않게 같은 함수를 쓴다."""
    from th.api import messages
    assert "no_trade_reasons" in pathlib.Path(messages.__file__).read_text(encoding="utf-8")
    assert "build_snapshot" in SERVER_SRC


def test_fastapi_를_최상위에서_임포트하지_않는다():
    """웹은 선택 의존성이다. CLI 만 쓰는 사람에게 강요하면 안 된다."""
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "src" / "th" / "cli.py").read_text(encoding="utf-8")
    head = src.split("app = typer.Typer")[0]
    for bad in ("import fastapi", "import uvicorn", "from fastapi"):
        assert bad not in head, f"cli.py 최상위에서 {bad} — serve 안에서만 임포트할 것"


def test_한_번_조회해서_여러_접속자에게_뿌린다():
    """브라우저를 두 개 열었다고 바이낸스 조회가 두 배가 되면 안 된다."""
    assert "class Hub" in SERVER_SRC
    assert "_send" in SERVER_SRC


# ── 서버가 실제로 뜨는가 ────────────────────────────────
# fastapi 는 선택 의존성이라 없을 수도 있다. 그때는 건너뛴다.
#   pip install -e ".[web]"
try:
    import fastapi as _fastapi                       # noqa: F401
    HAS_WEB = True
except ImportError:                                   # pragma: no cover
    HAS_WEB = False


import contextlib
import os


@contextlib.contextmanager
def clean_settings():
    """테스트는 사용자의 `.env` 를 읽으면 안 된다.

    실제로 겪은 일: `.env` 의 TH_EXECUTION_MODE 가 잘못된 값이라
    **제품이 아니라 테스트가** 깨졌다. 테스트가 개발 기계의 설정에 좌우되면
    통과 여부를 믿을 수 없다.
    """
    from th.config import Settings

    import th.api.server as srv

    saved = {k: v for k, v in os.environ.items()
             if k.startswith(("TH_", "POSTGRES_"))}
    for k in saved:
        del os.environ[k]
    real = srv.get_settings
    srv.get_settings = lambda: Settings(_env_file=None)
    try:
        yield srv
    finally:
        srv.get_settings = real
        os.environ.update(saved)


def test_앱이_만들어지고_라우트가_붙는다():
    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    with clean_settings() as srv:
        app = srv.create_app()
    paths = {r.path for r in app.routes}
    for p in ("/", "/api/health", "/api/candles", "/api/paper", "/ws"):
        assert p in paths, f"{p} 라우트가 없다"


def test_json_변환이_datetime_을_문자열로_만든다():
    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    from datetime import UTC, datetime

    from th.api.server import _json

    out = _json({"a": datetime(2026, 9, 8, tzinfo=UTC), "b": [1, 2]})
    assert isinstance(out["a"], str) and out["b"] == [1, 2]


# ── 조회 실패를 '포지션 없음'으로 그리지 않는다 ──────────
# 실제로 겪은 버그다. 계정 조회가 간헐적으로 실패하는 동안 화면이
# "열린 포지션이 없습니다" 와 정상 표시 사이를 오갔다.

def test_조회_실패를_포지션_없음으로_그리지_않는다():
    m = re.search(r"function paintRisk\(\)\{(.*?)\n\}", HTML_SRC, re.S)
    assert m, "paintRisk 를 찾지 못했다 (이름을 바꿨다면 이 테스트도 같이 고칠 것)"
    body = m.group(1)
    assert "r.errors" in body, "서버가 준 errors 를 화면이 무시하고 있다"
    assert "마지막으로 확인된 값" in body or "마지막 확인" in body, \
        "실패 시 '지금 값이 아니다' 라는 사실을 알려야 한다"


def test_risk_메시지가_errors_를_담는다():
    from th.api.schemas import RiskMessage
    assert "errors" in RiskMessage.model_fields, "실패 사실을 클라이언트로 보내야 한다"
    assert "stale" in RiskMessage.model_fields


def test_터미널과_웹이_같은_보정_함수를_쓴다():
    """규칙을 두 곳에 적으면 언젠가 갈라진다."""
    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "th"
    cli = (root / "cli.py").read_text(encoding="utf-8")
    assert "carry_forward" in cli, "th watch 가 보정 함수를 쓰지 않는다"
    assert "carry_forward" in SERVER_SRC, "웹 서버가 보정 함수를 쓰지 않는다"


# ── 한 화면 · 1초 갱신 (사용자 요구) ────────────────────
def test_화면이_스크롤되지_않는다():
    """트레이딩 터미널처럼 한 화면에 다 떠 있어야 한다."""
    assert "overflow:hidden" in HTML_SRC.replace(" ", ""), "본문 스크롤을 막아야 한다"
    assert "100dvh" in HTML_SRC, "뷰포트 높이에 맞춰야 한다"
    assert "grid-template-areas" in HTML_SRC, "격자 배치여야 한다"


def test_매초_갱신한다():
    assert re.search(r"setInterval\(\s*everySecond\s*,\s*1000\s*\)", HTML_SRC), \
        "1초마다 화면을 갱신해야 한다"


def test_평가손익은_브라우저가_매초_계산한다():
    """스냅샷(3초)을 기다리면 화면이 뚝뚝 끊긴다.
    가격이 올 때마다 브라우저가 직접 R 을 계산해야 매초 살아 있다."""
    m = re.search(r"function paintRisk\(\)\{(.*?)\n\}", HTML_SRC, re.S)
    body = m.group(1)
    for need in ("entry_price", "stop_price", "S.price"):
        assert need in body, f"평가손익 계산에 {need} 가 필요하다"


def test_서버가_가격과_계정을_다른_주기로_보낸다():
    """가격은 DB 만 읽으므로 1초가 싸다. 계정은 바이낸스 API 라 1초면 레이트리밋에 걸린다."""
    assert "TICK_S = 1" in SERVER_SRC, "가격은 매초 보내야 한다"
    m = re.search(r"ACCOUNT_EVERY\s*=\s*(\d+)", SERVER_SRC)
    assert m and int(m.group(1)) >= 2, "계정 조회를 매초 하면 안 된다"
    assert '"type": "tick"' in SERVER_SRC, "가격 전용 메시지가 있어야 한다"


# ── 갱신 주기 — 항목마다 자기 주기로 ────────────────────
def test_세션_시장_손익_갱신이_계정_주기에_묶이지_않는다():
    """SESSION(5초)·MARKET(20초)·INCOME(30초)이 `tick % ACCOUNT_EVERY == 0` 안에
    있으면 3의 배수와 겹치는 15초·60초·30초에만 돈다. 각자 자기 나머지로 판정해야 한다."""
    m = re.search(r"async def _loop\(self\)(.*?)\n        finally:", SERVER_SRC, re.S)
    assert m, "_loop 를 찾지 못했다"
    body = m.group(1)
    guard = re.search(r"if tick % ACCOUNT_EVERY == 0([^\n]*):", body)
    assert guard, "계정 주기 분기를 찾지 못했다"
    for name in ("due_session", "due_income", "due_market"):
        assert name in guard.group(1), f"{name} 이 계정 주기(3초) 안에 갇혀 있다"
    # 그리고 실제 스케줄: 0~60초 동안 각 항목이 자기 주기마다 한 번씩
    from th.api import server as srv
    ticks = range(0, 61)
    session_ticks = [t for t in ticks if t % srv.SESSION_EVERY == 0]
    assert 5 in session_ticks and 10 in session_ticks     # 15 의 배수에만 오면 안 된다
    assert 20 in [t for t in ticks if t % srv.MARKET_EVERY == 0]


# ── 차트 VWAP — 묶음의 마지막 1분봉 값 ──────────────────
def test_리샘플_봉의_vwap은_묶음_마지막_1분봉_값이다():
    """5분봉의 종가는 마지막 1분에서 오는데 VWAP 만 첫 1분 것을 쓰면 4분 뒤처진다."""
    from datetime import UTC, datetime, timedelta

    from th.api.server import bucket_key, vwap_by_bucket
    from th.strategy.indicators import Bar, VwapPoint

    t0 = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
    bars1 = [Bar(ts=t0 + timedelta(minutes=i), open=1, high=1, low=1, close=1, volume=1)
             for i in range(10)]
    pts = [VwapPoint(vwap=float(i), sigma=0.0) for i in range(10)]
    vmap = vwap_by_bucket(bars1, pts, 5)
    assert vmap[bucket_key(t0, 5)] == 4.0, vmap            # 13:30 묶음 → 13:34 의 값
    assert vmap[bucket_key(t0 + timedelta(minutes=5), 5)] == 9.0
    # 1분봉이면 그대로
    one = vwap_by_bucket(bars1, pts, 1)
    assert one[bucket_key(t0 + timedelta(minutes=3), 1)] == 3.0
    # 앵커 전(None)은 건너뛴다
    assert bucket_key(t0, 5) not in vwap_by_bucket(bars1[:2], [None, None], 5)


# ── 0.0.0.0 바인드 경고 ───────────────────────────────────
def test_루프백_밖_접속이면_경고를_한_번만_남긴다():
    import logging

    from th.api import server as srv

    class Catch(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, r):
            self.msgs.append(r.getMessage())

    h = Catch()
    logger = logging.getLogger(srv.__name__)
    logger.addHandler(h)
    saved = srv._exposure_warned
    srv._exposure_warned = False
    try:
        assert srv._warn_if_exposed({"server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 5)}) is False
        assert not h.msgs
        assert srv._warn_if_exposed({"server": ("192.168.0.7", 8000), "client": ("192.168.0.20", 5)}) is True
        assert srv._warn_if_exposed({"server": ("192.168.0.7", 8000)}) is False   # 두 번 안 한다
        assert len(h.msgs) == 1 and "127.0.0.1" in h.msgs[0], h.msgs
    finally:
        srv._exposure_warned = saved
        logger.removeHandler(h)


# ── 살아있는 봉 ───────────────────────────────────────────
def test_서버가_미완성_묶음을_live_로_따로_준다():
    """지표는 닫힌 봉으로만, 만들어지는 중인 봉은 브라우저가 매초 호가로 늘린다."""
    assert '"live": True' in SERVER_SRC, "candles 응답 마지막에 미완성 봉을 live 로 붙여야 한다"
    assert "drop_incomplete=True" in SERVER_SRC, "지표 계산은 닫힌 봉으로만 해야 한다"
    assert "function applyTick" in HTML_SRC, "브라우저가 호가로 살아있는 봉을 갱신해야 한다"
    assert "applyTick(m.price" in HTML_SRC, "tick 메시지마다 살아있는 봉에 반영해야 한다"


def test_미완성_묶음은_닫힌_봉과_다른_시각이다():
    from datetime import UTC, datetime, timedelta

    from th.strategy.indicators import Bar, resample

    t0 = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
    bars1 = [Bar(ts=t0 + timedelta(minutes=i), open=1, high=2, low=0.5, close=1.5, volume=1)
             for i in range(7)]                                   # 13:30~13:36 → 5분 묶음 1개 + 2분짜리
    closed = resample(bars1, 5, drop_incomplete=True)
    full = resample(bars1, 5, drop_incomplete=False)
    assert len(closed) == 1 and len(full) == 2
    assert full[-1].ts != closed[-1].ts, "미완성 묶음은 새 시각이어야 live 로 붙는다"
    assert full[-1].ts == t0 + timedelta(minutes=5)


# ═════════════════════════════════════════════════════════
# v2 (docs/15) — 워치리스트 · 포커스 · 시장 · 브래킷 · 청산 요청
# ═════════════════════════════════════════════════════════

def test_유니버스_후보는_주식뿐이고_그룹이_전부_붙어_있다():
    """docs/15 9절 — 후보는 넓히되 코인은 없고, 모든 후보에 그룹 태그가 있다."""
    from th.universe import (
        GROUP_OF,
        GROUPS,
        LEVERAGE_TAG,
        NOT_STOCKS,
        STOCK_PERPS,
        group_of,
        is_stock_perp,
        reset_universe,
    )

    reset_universe()
    assert len(STOCK_PERPS) == len(set(STOCK_PERPS)), "중복 심볼"
    for bad in ("BTCUSDT", "ETHUSDT", "SPXUSDT", "NXPCUSDT", "SOLUSDT"):
        assert bad not in STOCK_PERPS, f"코인이 유니버스에 있다: {bad}"
    assert NOT_STOCKS.isdisjoint(STOCK_PERPS)
    assert all(s.endswith("USDT") for s in STOCK_PERPS)
    # 반도체 목록(docs/15 9절)이 들어 있고 맨 앞이다 — 워치리스트가 이 순서로 그려진다
    for t in ("SOXL", "SOXS", "SMH", "NVDA", "AMD", "AMAT", "AVGO", "TSM", "ASML", "MU",
              "QCOM", "ARM", "LRCX", "KLAC", "MRVL", "ADI", "ON", "MCHP", "MPWR", "WDC", "STX"):
        assert f"{t}USDT" in STOCK_PERPS, f"반도체 후보 누락: {t}"
        assert group_of(f"{t}USDT") == "반도체"
    assert STOCK_PERPS[0] == "SOXLUSDT" and STOCK_PERPS[-1] == "GDXUSDT"
    assert set(GROUP_OF) == set(STOCK_PERPS), "그룹 태그가 없는 후보가 있다"
    assert set(GROUP_OF.values()) <= set(GROUPS)
    assert group_of("SPYUSDT") == "지수ETF" and group_of("AAPLUSDT") == "대형기술주"
    assert group_of("GLDUSDT") == "기타" and group_of("XYZUSDT") == "기타"
    assert is_stock_perp("soxlusdt") and not is_stock_perp("BTCUSDT") and not is_stock_perp(None)
    assert LEVERAGE_TAG["SOXLUSDT"] == "3x" and LEVERAGE_TAG["SOXSUSDT"] == "-3x"
    assert LEVERAGE_TAG["UVXYUSDT"] == "1.5x"


def _exchange_info(*entries):
    return {"symbols": [
        {"symbol": sym, "status": status, "contractType": "PERPETUAL",
         "underlyingType": ut, "underlyingSubType": sub, "filters": []}
        for sym, status, ut, sub in entries
    ]}


def test_유니버스는_바이낸스_exchange_info_가_정한다():
    """(a) 후보 중 TRADING 만 남고 (b) 주식으로 보이는 낯선 심볼은 `기타` 로 발견된다."""
    import logging

    from th import universe as u

    class Catch(logging.Handler):
        def __init__(self):
            super().__init__(); self.records = []

        def emit(self, r):
            self.records.append(r)

    h = Catch(); h.setLevel(logging.DEBUG)
    logger = logging.getLogger(u.__name__)
    old_level = logger.level
    logger.setLevel(logging.DEBUG); logger.addHandler(h)
    try:
        u.reset_universe()
        st = u.apply_exchange_info(_exchange_info(
            ("SOXLUSDT", "TRADING", "STOCK", ["Stocks"]),
            ("NVDAUSDT", "TRADING", "STOCK", []),
            ("GDXUSDT", "SETTLING", "STOCK", []),              # 후보지만 거래 중이 아님 → 제외
            ("BTCUSDT", "TRADING", "COIN", ["Layer-1"]),        # 코인 → 무시
            ("HOODUSDT", "TRADING", "coin", ["us-equity"]),     # 후보에 없는 주식 → 발견 (대소문자 무시)
            ("RIVNUSDT", "TRADING", "Equity", []),              # 발견
            ("ARKKUSDT", "TRADING", "COIN", ["ETF"]),           # ETF 도 발견
            ("TSLABUSD", "TRADING", "STOCK", []),               # USDT 아님 → 무시
            ("PLTRUSDT", "BREAK", "STOCK", []),                 # 발견 후보지만 거래 중 아님
        ))
        assert st.applied
        assert st.active == ("SOXLUSDT", "NVDAUSDT"), st.active
        assert "GDXUSDT" in st.dropped and "AMDUSDT" in st.dropped
        assert st.discovered == ("ARKKUSDT", "HOODUSDT", "RIVNUSDT"), st.discovered
        assert u.active_symbols() == ("SOXLUSDT", "NVDAUSDT", "ARKKUSDT", "HOODUSDT", "RIVNUSDT")
        # 발견된 심볼은 포커스로 쓸 수 있고 그룹은 `기타`
        assert u.is_stock_perp("HOODUSDT") and u.group_of("HOODUSDT") == "기타"
        assert not u.is_stock_perp("BTCUSDT") and not u.is_stock_perp("TSLABUSD")
        # SOXLUSDT 항목을 DEBUG 로 찍는다 (판별 필드 확인용)
        dbg = [r.getMessage() for r in h.records if r.levelno == logging.DEBUG]
        assert any("SOXLUSDT" in m and "Stocks" in m for m in dbg), dbg
        # 워치리스트도 같은 순서 + 그룹
        from th.api.messages import build_watchlist
        m = build_watchlist(_tickers(("HOODUSDT", 30.0, 1.0, 1e5), ("SOXLUSDT", 131.8, 0.4, 1e7),
                                     ("GDXUSDT", 40.0, 0.0, 1e5)), "SOXLUSDT", ["SOXLUSDT"], None)
        assert [(i.symbol, i.group) for i in m.items] == [("SOXLUSDT", "반도체"), ("HOODUSDT", "기타")]
        # 빈 응답·실패는 아무것도 바꾸지 않는다
        u.reset_universe()
        assert not u.apply_exchange_info({"symbols": []}).applied
        assert not u.apply_exchange_info(None).applied
        assert u.active_symbols() == u.STOCK_PERPS and u.is_stock_perp("GDXUSDT")
    finally:
        u.reset_universe()
        logger.removeHandler(h); logger.setLevel(old_level)


def test_허브가_시작할_때_exchange_info_를_한_번_받고_실패해도_뜬다():
    import asyncio

    from th import universe as u
    from th.api.server import Hub

    calls = []

    class Api:
        async def exchange_info(self, symbol=None):
            calls.append(symbol)
            return _exchange_info(("SOXLUSDT", "TRADING", "STOCK", []),
                                  ("HOODUSDT", "TRADING", "STOCK", []))

    class Dead:
        async def exchange_info(self, symbol=None):
            raise RuntimeError("timeout")

    try:
        hub = Hub("SOXLUSDT", ["SOXLUSDT"]); hub._api = Api()
        asyncio.run(hub.load_universe())
        assert calls == [None] and u.active_symbols() == ("SOXLUSDT", "HOODUSDT")
        u.reset_universe()
        hub._api = Dead()
        asyncio.run(hub.load_universe())                  # 예외가 새어 나오면 안 된다
        assert u.active_symbols() == u.STOCK_PERPS
    finally:
        u.reset_universe()


def _tickers(*rows):
    return [{"symbol": s, "last_price": p, "change_pct": c, "quote_volume": v,
             "close_time_ms": 0} for s, p, c, v in rows]


def test_워치리스트_메시지_형태와_순서():
    """`STOCK_PERPS` 순서, 바이낸스에 없는 심볼은 빠지고, 코인은 애초에 안 들어온다."""
    import json
    from datetime import UTC, datetime

    from th.api.messages import build_watchlist

    tk = _tickers(("BTCUSDT", 65000.0, 1.0, 9e9),          # 유니버스 밖 → 빠진다
                  ("NVDAUSDT", 118.2, -0.8, 3.4e6),
                  ("SOXLUSDT", 131.8, 0.47, 1.2e7),
                  ("GLDUSDT", 210.0, 0.1, 5e5))
    at = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
    m = build_watchlist(tk, "soxlusdt", ["SOXLUSDT"], 1.9, at)
    payload = json.loads(m.model_dump_json())
    assert payload["type"] == "watchlist" and payload["focus"] == "SOXLUSDT"
    assert [i["symbol"] for i in payload["items"]] == ["SOXLUSDT", "NVDAUSDT", "GLDUSDT"]
    soxl, nvda, gld = payload["items"]
    assert soxl == {"symbol": "SOXLUSDT", "price": 131.8, "change_pct": 0.47,
                    "quote_volume": 1.2e7, "lev_tag": "3x", "collected": True,
                    "group": "반도체"}
    assert nvda["lev_tag"] is None and nvda["collected"] is False
    assert gld["collected"] is False
    assert payload["basket_exposure_x"] == 1.9
    # 계정 조회 실패 → null (0 이 아니다)
    assert build_watchlist(tk, "SOXLUSDT", [], None, at).basket_exposure_x is None


def test_시장_메시지는_마크가_기준_베이시스를_계산한다():
    import json
    from datetime import UTC, datetime

    from th.api.messages import build_market

    at = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
    premium = {"mark_price": 131.81, "last_funding_rate": 0.000172,
               "next_funding_ts": datetime(2026, 9, 10, 16, 0, tzinfo=UTC)}
    m = build_market("SOXLUSDT", premium, 131.76, "SOXL", at)
    payload = json.loads(m.model_dump_json())
    assert payload["type"] == "market" and payload["symbol"] == "SOXLUSDT"
    assert payload["funding_rate"] == 0.000172
    assert payload["next_funding_at"].startswith("2026-09-10T16:00:00")
    assert abs(payload["basis_pct"] - (131.81 - 131.76) / 131.76 * 100) < 1e-9
    assert payload["underlying_ticker"] == "SOXL"
    # 본주 값이 없으면 둘 다 null. premiumIndex 가 죽어도 본주는 남는다.
    none = build_market("SOXLUSDT", premium, None, "SOXL", at)
    assert none.underlying_price is None and none.basis_pct is None
    dead = build_market("SOXLUSDT", None, 43.9, "SOXL", at)
    assert dead.mark_price is None and dead.funding_rate is None and dead.underlying_price == 43.9


# ── 브래킷 패널 ─────────────────────────────────────────
def _bracket_state(*, position_qty=0.0, avg_entry=None, entry_type="LIMIT",
                   stop_trigger=None, tp1_filled=False, entry_age_s=0, bracket_id="brk-a1b2c3d4e5f6"):
    from datetime import UTC, datetime, timedelta

    from th.execution.models import (
        Bracket, BracketState, Fill, Order, OrderStatus, OrderType, Purpose, Side,
    )

    b = Bracket(bracket_id=bracket_id, symbol="SOXLUSDT", direction="LONG", qty=41.67,
                stop_price=131.25, entry_type=OrderType(entry_type), entry_price=132.5,
                tp1_price=134.09, tp2_price=135.68, expire_after_s=300)
    st = BracketState(bracket=b, position_qty=position_qty, avg_entry=avg_entry)
    created = datetime.now(UTC) - timedelta(seconds=entry_age_s)
    if position_qty == 0:
        st.orders["e"] = Order(client_id="e", bracket_id=b.bracket_id, symbol=b.symbol,
                               side=Side.BUY, type=OrderType(entry_type), purpose=Purpose.ENTRY,
                               qty=b.qty, price=132.5, status=OrderStatus.OPEN, created_at=created)
    if stop_trigger is not None:
        st.orders["s"] = Order(client_id="s", bracket_id=b.bracket_id, symbol=b.symbol,
                               side=Side.SELL, type=OrderType.STOP_MARKET, purpose=Purpose.STOP,
                               qty=abs(position_qty), trigger=stop_trigger, reduce_only=True,
                               status=OrderStatus.OPEN)
    if tp1_filled:
        st.fills.append(Fill(order_client_id="t1", bracket_id=b.bracket_id, symbol=b.symbol,
                             side=Side.SELL, qty=20.8, price=134.09, fee=0.1, purpose=Purpose.TP1))
    return st


def test_브래킷_status_ko_규칙():
    """docs/15 1-4 — 상태 문장은 서버가 만든다. 브라우저는 그대로 보여준다."""
    from th.api.messages import bracket_status_ko

    # 진입 대기 · 지정가 · 남은 유효시간
    s = bracket_status_ko(_bracket_state(entry_age_s=100), False)
    assert s.startswith("진입 대기 · 지정가 · ") and s.endswith(" 후 만료"), s
    left = s.split(" · ")[2].split(" ")[0]
    m, sec = left.split(":")
    assert 195 <= int(m) * 60 + int(sec) <= 200, s
    assert bracket_status_ko(_bracket_state(entry_type="MARKET"), False) == "진입 대기 · 시장가"
    # 진입 후 손절 대기 · 잔여 %
    full = _bracket_state(position_qty=41.67, avg_entry=132.5, stop_trigger=131.25)
    assert bracket_status_ko(full, False) == "손절 대기 · 잔여 100%"
    half = _bracket_state(position_qty=20.8, avg_entry=132.5, stop_trigger=131.25, tp1_filled=True)
    assert bracket_status_ko(half, False) == "TP1 체결 · 손절 대기 · 잔여 50%"
    # 손절이 실제로 본절까지 올라왔을 때만 '본절 이동'이라고 말한다
    be = _bracket_state(position_qty=20.8, avg_entry=132.5, stop_trigger=132.5, tp1_filled=True)
    assert bracket_status_ko(be, False) == "TP1 체결 → 손절 본절 이동"
    # 손절 없음은 숨기지 않는다
    naked = _bracket_state(position_qty=41.67, avg_entry=132.5)
    assert "손절 없음" in bracket_status_ko(naked, False)
    # 청산 요청은 다른 모든 상태보다 우선
    assert bracket_status_ko(full, True) == "청산 요청됨 · 러너 대기"


def test_브래킷_메시지_형태():
    import json
    from datetime import UTC, datetime, timedelta

    from th.api.messages import build_brackets

    at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)          # KST 9/10 21:00
    half = _bracket_state(position_qty=20.8, avg_entry=132.5, stop_trigger=131.25, tp1_filled=True)
    half.realized_r = 0.55
    hist = [
        {"realized_r": 1.2, "closed_at": at - timedelta(minutes=30)},      # 오늘(KST)
        {"realized_r": -1.0, "closed_at": at - timedelta(days=2)},        # 그제
        {"realized_r": None, "closed_at": at - timedelta(minutes=5)},     # R 없음 → 표본 아님
    ]
    m = build_brackets([half], hist, 1.2, "paper", {"brk-a1b2c3d4e5f6"}, at=at)
    payload = json.loads(m.model_dump_json())
    assert payload["type"] == "brackets" and payload["available"] is True
    assert payload["mode"] == "paper"
    (item,) = payload["open"]
    assert item == {
        "id": "a1b2c3d4e5f6"[-8:], "symbol": "SOXLUSDT", "direction": "LONG",
        "entry": 132.5, "stop": 131.25, "tp1": 134.09, "tp2": 135.68,
        "qty": 41.67, "position_qty": 20.8, "has_stop": True,
        "status_ko": "청산 요청됨 · 러너 대기", "realized_r": 0.55,
        "risk_per_unit": 1.25, "close_requested": True, "setup_id": "",
    }
    assert payload["today_realized_r"] == 1.2
    assert payload["today_count"] == 1
    assert abs(payload["expectancy_r"] - 0.1) < 1e-9
    # 진입 전 브래킷은 지정가 기준 risk_per_unit, 요청 없음
    waiting = build_brackets([_bracket_state()], [], 0.0, "paper", set(), at=at).open[0]
    assert waiting.entry == 132.5 and waiting.risk_per_unit == 1.25
    assert waiting.close_requested is False and waiting.position_qty == 0.0


def test_스냅샷에_규율_지표가_실린다():
    from th.api.schemas import DisciplineMessage, SnapshotMessage

    assert "discipline" in SnapshotMessage.model_fields
    d = DisciplineMessage(ok=False)
    assert d.total_exposure_x is None and d.remaining_r is None, "실패 턴은 0 이 아니라 null"
    assert d.total_limit_x == 3.0 and d.symbol_limit_x == 1.0 and d.drawdown_limit_pct == 15.0


# ── 라우트 · 청산 요청 ──────────────────────────────────
def _payload(resp):
    """JSONResponse 본문 — 진짜 starlette 는 `.body`(bytes), 컨테이너 스텁은 `.content`(dict)."""
    import json as _json

    if isinstance(resp, dict):
        return resp
    if hasattr(resp, "body"):
        return _json.loads(resp.body)
    return resp.content


def _route(app, path, kind=None):
    for r in app.routes:
        if r.path == path and (kind is None or getattr(r, "kind", kind) == kind):
            return r.endpoint
    raise AssertionError(f"{path} 라우트가 없다")


def test_v2_라우트가_붙는다():
    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    with clean_settings() as srv:
        app = srv.create_app()
    paths = {r.path for r in app.routes}
    for p in ("/api/focus", "/api/brackets", "/api/brackets/{id}/close"):
        assert p in paths, f"{p} 라우트가 없다"


def test_청산_요청은_페이퍼_모드가_아니면_403():
    """실거래에서는 이 경로가 **존재하지 않는다**. 기록조차 남기지 않는다."""
    import asyncio

    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    from th.execution import store

    with clean_settings() as srv:
        app = srv.create_app()             # 기본 실행모드 = none
    called = []

    async def boom(*a, **k):
        called.append(a)
        raise AssertionError("페이퍼가 아니면 DB 를 만지면 안 된다")

    saved = store.load_open_brackets, store.request_close
    store.load_open_brackets = boom
    store.request_close = boom
    try:
        resp = asyncio.run(_route(app, "/api/brackets/{id}/close")("a1b2c3d4"))
    finally:
        store.load_open_brackets, store.request_close = saved
    assert resp.status_code == 403, resp
    assert not called


def test_청산_요청은_열린_페이퍼_브래킷에만_남는다():
    import asyncio
    import os

    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    from th.config import Settings

    import th.api.server as srv
    from th.execution import store

    real = srv.get_settings
    saved_env = {k: v for k, v in os.environ.items() if k.startswith(("TH_", "POSTGRES_"))}
    for k in saved_env:
        del os.environ[k]
    srv.get_settings = lambda: Settings(_env_file=None, execution_mode="paper")
    try:
        app = srv.create_app()
    finally:
        srv.get_settings = real
        os.environ.update(saved_env)

    st = _bracket_state(position_qty=41.67, avg_entry=132.5, stop_trigger=131.25,
                        bracket_id="brk-0123456789ab")
    requested = []

    async def fake_open(mode):
        return [st]

    async def fake_request(bracket_id, mode):
        requested.append((bracket_id, mode))
        return True

    saved = store.load_open_brackets, store.request_close
    store.load_open_brackets, store.request_close = fake_open, fake_request
    try:
        close = _route(app, "/api/brackets/{id}/close")
        ok = asyncio.run(close("456789ab"))                 # 8자리 접미어로 찾는다
        missing = asyncio.run(close("deadbeef"))
    finally:
        store.load_open_brackets, store.request_close = saved
    assert ok["ok"] is True and ok["status_ko"] == "청산 요청됨 · 러너 대기"
    assert requested == [("brk-0123456789ab", "paper")], "전체 id 로 표시해야 한다"
    assert missing.status_code == 404


def test_포커스_변경은_유니버스_밖이면_400이고_바뀌면_focus_를_뿌린다():
    import asyncio

    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    with clean_settings() as srv:
        app = srv.create_app()
    hub = getattr(app, "state", app).hub
    sent = []

    class Sock:
        async def send_json(self, p):
            sent.append(p)

    hub.clients.add(Sock())
    focus = _route(app, "/api/focus")
    bad = asyncio.run(focus({"symbol": "BTCUSDT"}))
    assert bad.status_code == 400
    out = asyncio.run(focus({"symbol": "nvdausdt"}))
    assert out["ok"] and out["symbol"] == "NVDAUSDT" and out["collected"] is False
    assert out["changed"] is True
    assert sent[-1] == {"type": "focus", "symbol": "NVDAUSDT", "collected": False}
    assert hub.symbol == "NVDAUSDT" and hub._focus_dirty


def test_수집하지_않는_종목의_tick_은_워치리스트_가격으로_대신한다():
    from datetime import UTC, datetime, timedelta

    from th.api.server import WATCHLIST_FRESH_S, Hub

    hub = Hub("NVDAUSDT", ["SOXLUSDT"])
    assert not hub.is_collected
    now = datetime.now(UTC)
    assert hub._watch_tick(now)["fresh"] is False              # 아직 티커가 없다
    hub._watch["NVDAUSDT"] = (118.2, now - timedelta(seconds=3))
    t = hub._watch_tick(now)
    assert t["type"] == "tick" and t["price"] == 118.2 and t["fresh"] is True
    assert t["bid"] is None and t["spread_bps"] is None
    hub._watch["NVDAUSDT"] = (118.2, now - timedelta(seconds=WATCHLIST_FRESH_S + 1))
    assert hub._watch_tick(now)["fresh"] is False, "15초 넘은 티커는 오래됨"


def test_공개_klines_폴백은_살아있는_봉을_따로_준다():
    """수집하지 않는 종목도 지표는 서버가 닫힌 봉으로 계산하고 마지막 봉은 live 다."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from th.api.server import candle_rows, public_bars
    from th.exchange.base import Kline

    t0 = datetime(2026, 9, 10, 13, 0, tzinfo=UTC)
    seen = {}

    class Api:
        async def klines(self, symbol, interval, limit, include_open=False, **kw):
            seen.update(symbol=symbol, interval=interval, limit=limit, include_open=include_open)
            ks = [Kline(symbol, interval, t0 + timedelta(minutes=5 * i), 1, 2, 0.5, 1.5, 10)
                  for i in range(120)]
            ks.append(Kline(symbol, interval, t0 + timedelta(minutes=600), 1, 2, 0.5, 1.2, 3,
                            closed=False))
            return ks

    bars, live = asyncio.run(public_bars(Api(), "NVDAUSDT", 5, 100))
    assert seen == {"symbol": "NVDAUSDT", "interval": "5m", "limit": 300, "include_open": True}
    assert len(bars) == 120 and live is not None and live.close == 1.2
    rows = candle_rows(bars, live, {}, 5, 100)
    assert len(rows) == 101 and rows[-1]["live"] is True and rows[-1]["ema9"] is None
    assert rows[-2].get("live") is None and rows[-2]["ema99"] is not None
    assert rows[0]["t"] == (t0 + timedelta(minutes=100)).isoformat()


def test_브래킷_조회_실패는_available_false():
    """DB 를 못 읽었을 때 '브래킷 없음'으로 그리면 안 된다."""
    import asyncio

    from th.api.server import brackets_payload
    from th.execution import store

    async def boom(*a, **k):
        raise RuntimeError("db down")

    saved = store.load_open_brackets
    store.load_open_brackets = boom
    try:
        p = asyncio.run(brackets_payload("paper"))
    finally:
        store.load_open_brackets = saved
    assert p["type"] == "brackets" and p["available"] is False and p["open"] == []
    assert "RuntimeError" in p["message_ko"]


def test_서버는_주문_코드를_runner_를_통해서만_부른다():
    """v2.1: 페이퍼 엔진이 서버 안에서 돌지만, 주문 코드는 여전히 `th.execution` 에만 있다.

    서버가 스스로 주문을 만들거나(`Bracket(`·`place(`), 매니저·클라이언트를 직접 조립하면
    (`BracketManager`·`PaperClient`·`LiveClient`) 주문 경로가 둘이 된다. 서버는
    `th.execution.runner` 의 `build_paper_runner`/`OpenRequest` 에 **요청을 넘길 뿐**이고,
    `th.execution` 에서 그 밖에 임포트하는 것은 조회 전용 `store`·`quotes` 와 게이트 상수(`live`)뿐이다.
    """
    for bad in ("/fapi/v1/order", "place(", "close_now", "BracketManager", "PaperClient",
                "LiveClient", "Bracket(", "GuardInput", "size_from_risk", "mgr.open"):
        assert bad not in SERVER_SRC, f"웹 서버에 주문 코드가 있다: {bad}"
    imports = re.findall(r"from \.\.execution(?:\.(\w+))? import ([^\n]+)", SERVER_SRC)
    assert imports, "execution 임포트를 못 찾았다"
    for mod, names in imports:
        names = {n.strip() for n in names.split(",")}
        if mod == "runner":
            assert names <= {"LeaseHeld", "build_paper_runner", "OpenRequest"}, names
        elif mod == "live":
            assert names <= {"LIVE_ORDER_REFUSED_KO", "gate_status"}, names
        elif mod == "quotes":
            assert names == {"latest_quote"}, names          # 호가 읽기 — 조회 전용
        elif mod == "signals":
            # v2.2: 시그널 엔진도 러너의 open_from_request 로만 주문한다. 서버는 조립·상태 조회뿐.
            assert names == {"build_signal_engine"}, names
        elif not mod:
            assert names == {"store"}, names
        else:
            raise AssertionError(f"서버가 th.execution.{mod} 를 직접 쓴다: {names}")
    assert "request_close" in SERVER_SRC
    assert 'execution_mode != "paper"' in SERVER_SRC, "페이퍼가 아니면 403 이어야 한다"
    # 실거래 주문은 무조건 403 — 러너 유무보다 먼저 판정한다
    m = re.search(r"def _refuse_mode\(req\):(.*?)return None", SERVER_SRC, re.S)
    assert m and m.group(1).index('"live"') < m.group(1).index("runner_alive")


# ═════════════════════════════════════════════════════════
# v2.1 (docs/15 6~9절) — 주문 패널 · 실매매 포지션 · 내장 러너
# ═════════════════════════════════════════════════════════

def _position(symbol="SOXLUSDT", side="LONG", qty=12.61, entry=125.62, mark=117.21, liq=109.88):
    from datetime import UTC, datetime

    from th.account.models import Position

    return Position(symbol=symbol, side=side, qty=qty, entry_price=entry, mark_price=mark,
                    liquidation_price=liq, unrealized_pnl=(mark - entry) * qty * (1 if side == "LONG" else -1),
                    leverage=10, margin_type="cross", isolated_margin=0.0, notional=qty * mark,
                    updated_at=datetime(2026, 9, 11, 1, 0, tzinfo=UTC))


def _order(symbol, side, kind="STOP_MARKET", stop_price=0.0, reduce_only=True):
    from th.account.models import OpenOrder

    return OpenOrder(symbol=symbol, order_id="9", side=side, order_type=kind,
                     stop_price=stop_price, price=0.0, qty=1.0, reduce_only=reduce_only,
                     close_position=False)


def test_positions_메시지_형태와_손절_매핑():
    """docs/15 8절 — 전 종목, 종목별 STOP 주문 매핑, 레버리지 ETF 배수, 실효배수."""
    import json
    from datetime import UTC, datetime

    from th.api.messages import build_positions

    at = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
    positions = [
        _position(),                                                      # SOXL 롱, 손절 없음
        _position("NVDAUSDT", "SHORT", qty=10.0, entry=120.0, mark=118.0, liq=130.0),
        _position("AMDUSDT", "FLAT", qty=0.0, entry=0.0, mark=150.0, liq=None),  # 빠진다
    ]
    orders = [
        _order("NVDAUSDT", "BUY", stop_price=121.0),                      # 숏의 손절 (BUY, reduce-only)
        _order("NVDAUSDT", "BUY", stop_price=125.0),                      # 더 먼 손절 — 가까운 121 이 이긴다
        _order("NVDAUSDT", "BUY", kind="TAKE_PROFIT_MARKET", stop_price=110.0),   # 익절은 손절 아님
        _order("SOXLUSDT", "BUY", stop_price=100.0),                      # 롱인데 BUY — 손절 아님
        _order("SOXLUSDT", "SELL", stop_price=110.0, reduce_only=False),  # reduce-only 아님 — 손절 아님
    ]
    m = build_positions(positions, orders, 2890.5, True, at=at)
    payload = json.loads(m.model_dump_json())
    assert payload["type"] == "positions" and payload["ok"] is True
    assert payload["wallet_usdt"] == 2890.5 and payload["ts"].startswith("2026-09-11T01:00:00")
    assert [i["symbol"] for i in payload["items"]] == ["SOXLUSDT", "NVDAUSDT"], "FLAT 은 뺀다"
    soxl, nvda = payload["items"]
    assert soxl["side"] == "LONG" and soxl["qty"] == 12.61 and soxl["entry_price"] == 125.62
    assert soxl["mark_price"] == 117.21 and abs(soxl["notional_usdt"] - 12.61 * 117.21) < 1e-6
    assert abs(soxl["unrealized_pnl"] - (117.21 - 125.62) * 12.61) < 1e-6
    assert abs(soxl["unrealized_pct"] - (117.21 - 125.62) / 125.62 * 100) < 1e-9
    assert soxl["liquidation_price"] == 109.88
    liq = (117.21 - 109.88) / 117.21 * 100
    assert abs(soxl["liq_distance_pct"] - liq) < 1e-9
    assert abs(soxl["underlying_move_to_liq_pct"] - liq / 3) < 1e-9, "SOXL 은 3배 ETF"
    assert abs(soxl["leverage_eff"] - 12.61 * 117.21 / 2890.5) < 1e-9
    assert soxl["has_stop"] is False and soxl["stop_price"] is None and soxl["stop_distance_pct"] is None
    # NVDA 숏: 가장 가까운 reduce-only STOP(121) 이 손절, 배수 1
    assert nvda["has_stop"] is True and nvda["stop_price"] == 121.0
    assert abs(nvda["stop_distance_pct"] - (121.0 - 118.0) / 118.0 * 100) < 1e-9
    assert abs(nvda["underlying_move_to_liq_pct"] - nvda["liq_distance_pct"]) < 1e-9
    assert set(soxl) == {"symbol", "side", "qty", "entry_price", "mark_price", "notional_usdt",
                         "unrealized_pnl", "unrealized_pct", "liquidation_price", "liq_distance_pct",
                         "underlying_move_to_liq_pct", "leverage_eff", "has_stop", "stop_price",
                         "stop_distance_pct"}


def test_positions_는_계정_실패면_ok_false_에_마지막_항목을_유지한다():
    """빈 목록으로 보내면 '포지션 없음'으로 읽힌다 — 10배에서 그건 사고다."""
    from th.api.messages import build_positions

    good = build_positions([_position()], [], 2890.5, True)
    bad = build_positions([], [], 0.0, False, last_items=list(good.items))
    assert bad.ok is False and bad.wallet_usdt is None
    assert [i.symbol for i in bad.items] == ["SOXLUSDT"], "마지막 값을 그대로 싣는다"
    empty = build_positions([], [], 0.0, False)
    assert empty.ok is False and empty.items == []
    from th.api.schemas import OUTBOUND_TYPES
    assert "positions" in OUTBOUND_TYPES


def test_허브가_positions_를_계정_뷰에서_만들고_실패_턴에_마지막_값을_유지한다():
    """positions 는 규율 지표와 **같은 계정 조회**에서 나온다 — 추가 API 호출이 없다."""
    from dataclasses import replace

    from th.account.models import Balance
    from th.account.risk import build_snapshot as risk_snapshot
    from th.account.service import RiskView
    from th.api.server import Hub

    hub = Hub("SOXLUSDT", ["SOXLUSDT"], "paper")
    p = _position()
    snap = risk_snapshot("SOXLUSDT", p, Balance(2890.5, 2000.0, -106.0, 500.0),
                         [_order("SOXLUSDT", "SELL", stop_price=112.0)], None, "SOXL")
    view = RiskView(risk=snap, alerts=[], market=None, positions=(p,))
    out = hub._positions(view)
    assert out["type"] == "positions" and out["ok"] is True and len(out["items"]) == 1
    assert out["items"][0]["has_stop"] is True and out["items"][0]["stop_price"] == 112.0
    # 실패 턴
    view.risk = replace(snap, account_ok=False, errors=("계정 조회 실패",))
    view.positions = ()
    fail = hub._positions(view)
    assert fail["ok"] is False and fail["wallet_usdt"] is None
    assert [i["symbol"] for i in fail["items"]] == ["SOXLUSDT"]
    assert "positions" in SERVER_SRC.split("async def join")[1].split("def leave")[0], \
        "새 접속자에게 positions 도 다시 보내야 한다"


def _paper_app():
    """실행모드 paper 인 앱 (러너는 붙이지 않는다 — 테스트가 직접 넣는다)."""
    import os

    from th.config import Settings

    import th.api.server as srv

    real = srv.get_settings
    saved_env = {k: v for k, v in os.environ.items() if k.startswith(("TH_", "POSTGRES_"))}
    for k in saved_env:
        del os.environ[k]
    srv.get_settings = lambda: Settings(_env_file=None, execution_mode="paper")
    try:
        return srv.create_app()
    finally:
        srv.get_settings = real
        os.environ.update(saved_env)


class _FakeRunner:
    """서버가 러너에 무엇을 넘기는지만 본다. 주문 계산은 test_execution 이 검증한다."""

    alive = True

    def __init__(self):
        self.previews, self.opens, self.closes = [], [], []
        self.focus = None

    async def preview(self, req):
        from th.execution.runner import Preview

        self.previews.append(req)
        return Preview(ok=True, symbol=req.symbol, direction=req.direction, qty=41.67,
                       notional_usdt=5521.3, risk_usdt=52.1, r_unit_usdt=50.0,
                       stop_pct=0.94, cost_r=0.117, equity_usdt=10_000.0)

    async def open_from_request(self, req):
        from th.execution.runner import OrderOutcome

        self.opens.append(req)
        if req.stop >= 999:
            return OrderOutcome(False, reasons=("롱인데 손절이 현재가 위에 있다",))
        return OrderOutcome(True, bracket_id="br-0123456789ab")

    async def serve_close_requests(self):
        self.closes.append(True)
        return ["brk-0123456789ab"]


def test_orders_라우트와_게이트():
    import asyncio

    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    with clean_settings() as srv:
        app = srv.create_app()
    paths = {r.path for r in app.routes}
    for p in ("/api/orders/preview", "/api/orders", "/api/orders/gate"):
        assert p in paths, f"{p} 라우트가 없다"
    gate = asyncio.run(_route(app, "/api/orders/gate")())
    assert gate["mode"] == "none" and gate["live_allowed"] is False
    assert len(gate["conditions"]) == 5
    assert all(c["met"] is False for c in gate["conditions"])
    assert gate["conditions"][0]["text_ko"].startswith("①") and "docs/14" in gate["conditions"][0]["text_ko"]
    assert gate["conditions"][-1]["text_ko"] == "⑤ 수동 승인 스위치"
    from th.api.schemas import GateResponse
    assert GateResponse(**gate).live_allowed is False
    try:
        GateResponse(mode="paper", live_allowed=True)
        raise AssertionError("live_allowed=True 가 스키마를 통과했다")
    except Exception:                                   # noqa: BLE001
        pass


def test_live_주문은_403_이고_러너가_없으면_503():
    import asyncio

    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    app = _paper_app()
    body = {"symbol": "SOXLUSDT", "direction": "LONG", "stop": 131.25, "tp1": 134.09}
    for path in ("/api/orders/preview", "/api/orders"):
        ep = _route(app, path)
        live = asyncio.run(ep({**body, "mode": "live"}))
        assert live.status_code == 403, path
        assert _payload(live) == {"ok": False, "reasons": ["실거래 주문 경로는 아직 없습니다 (docs/07 게이트)"]}
        none = asyncio.run(ep({**body, "mode": "none"}))
        assert none.status_code == 403
        # 페이퍼인데 러너가 없다 (lifespan 이 안 돌았거나 시작 실패) → 503
        no_runner = asyncio.run(ep({**body, "mode": "paper"}))
        assert no_runner.status_code == 503 and _payload(no_runner)["ok"] is False
        assert "페이퍼 엔진" in _payload(no_runner)["reasons"][0]
    # live 는 러너가 있어도 403 — 러너를 건드리지도 않는다
    hub = getattr(app, "state", app).hub
    hub.runner = _FakeRunner()
    live = asyncio.run(_route(app, "/api/orders")({**body, "mode": "live"}))
    assert live.status_code == 403 and not hub.runner.opens


def test_주문_성공이면_brackets_를_즉시_뿌리고_거절이면_아무것도_열지_않는다():
    import asyncio

    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    import th.api.server as srv

    app = _paper_app()
    hub = getattr(app, "state", app).hub
    hub.runner = _FakeRunner()
    sent = []

    class Sock:
        async def send_json(self, p):
            sent.append(p)

    hub.clients.add(Sock())

    async def fake_brackets(mode, at=None):
        return {"type": "brackets", "mode": mode, "available": True, "open": [{"id": "6789ab"}]}

    real = srv.brackets_payload
    srv.brackets_payload = fake_brackets
    try:
        body = {"mode": "paper", "symbol": "soxlusdt", "direction": "long", "entry": "132.5",
                "stop": 131.25, "tp1": 134.09, "tp2": None, "risk_pct": "0.5", "expire_s": "120"}
        pv = asyncio.run(_route(app, "/api/orders/preview")(body))
        assert pv["ok"] is True and pv["qty"] == 41.67 and pv["reasons"] == [] and pv["warnings"] == []
        for k in ("notional_usdt", "risk_usdt", "r_unit_usdt", "stop_pct", "cost_r", "equity_usdt"):
            assert k in pv, k
        req = hub.runner.previews[-1]
        assert req.symbol == "SOXLUSDT" and req.direction == "LONG" and req.entry == 132.5
        assert req.risk_pct == 0.5 and req.expire_s == 120 and req.tp2 is None
        assert not sent, "미리보기는 아무것도 뿌리지 않는다"

        ok = asyncio.run(_route(app, "/api/orders")(body))
        assert ok == {"ok": True, "bracket_id": "br-0123456789ab"}
        assert sent and sent[-1]["type"] == "brackets" and sent[-1]["open"][0]["id"] == "6789ab"
        assert hub.latest["brackets"]["open"][0]["id"] == "6789ab", "새 접속자도 바로 본다"

        sent.clear()
        bad = asyncio.run(_route(app, "/api/orders")({**body, "stop": 999}))
        assert bad == {"ok": False, "reasons": ["롱인데 손절이 현재가 위에 있다"]}
        assert not sent, "거절이면 뿌릴 것이 없다"
    finally:
        srv.brackets_payload = real


def test_청산_요청은_러너가_살아_있으면_그_자리에서_처리한다():
    import asyncio

    if not HAS_WEB:
        pytest.skip('fastapi 없음 — pip install -e ".[web]"')
    import th.api.server as srv
    from th.execution import store

    app = _paper_app()
    hub = getattr(app, "state", app).hub
    st = _bracket_state(position_qty=41.67, avg_entry=132.5, stop_trigger=131.25,
                        bracket_id="brk-0123456789ab")
    requested = []

    async def fake_open(mode):
        return [st]

    async def fake_request(bracket_id, mode):
        requested.append(bracket_id)
        return True

    async def fake_brackets(mode, at=None):
        return {"type": "brackets", "mode": mode, "available": True, "open": []}

    saved = store.load_open_brackets, store.request_close, srv.brackets_payload
    store.load_open_brackets, store.request_close, srv.brackets_payload = fake_open, fake_request, fake_brackets
    try:
        close = _route(app, "/api/brackets/{id}/close")
        hub.runner = _FakeRunner()
        out = asyncio.run(close("456789ab"))
        assert out["status_ko"] == "청산됨" and hub.runner.closes == [True]
        assert requested == ["brk-0123456789ab"], "요청 표시는 여전히 DB 에 남긴다"
        # 러너가 죽어 있으면 예전처럼 요청만 남는다
        hub.runner.alive = False
        out = asyncio.run(close("456789ab"))
        assert out["status_ko"] == "청산 요청됨 · 러너 대기" and len(hub.runner.closes) == 1
    finally:
        store.load_open_brackets, store.request_close, srv.brackets_payload = saved
        _FakeRunner.alive = True


def test_포커스가_바뀌면_러너의_포커스도_바뀐다():
    import asyncio

    from th.api.server import Hub

    hub = Hub("SOXLUSDT", ["SOXLUSDT"], "paper")
    hub.runner = _FakeRunner()
    asyncio.run(hub.set_focus("nvdausdt"))
    assert hub.runner.focus == "NVDAUSDT"
    assert hub.runner_alive
    hub.runner = None
    assert not hub.runner_alive


def test_앱_시작은_paper_모드에서만_러너를_붙이고_임대가_잡혀_있으면_없이_뜬다():
    """lifespan 은 (a) 유니버스 (b) 러너 순서. 러너 실패는 서버를 죽이지 않는다."""
    import asyncio

    from th.api.server import Hub
    from th.execution import runner as rmod

    built = []

    async def fake_build(symbols, *, focus=None, interval=3, holder="serve", execution_mode=None):
        built.append((list(symbols), focus, holder, execution_mode))
        r = _FakeRunner()

        async def start():
            raise rmod.LeaseHeld("cli")

        r.start = start
        return r

    real = rmod.build_paper_runner
    rmod.build_paper_runner = fake_build
    try:
        none = Hub("SOXLUSDT", ["SOXLUSDT"], "none")
        asyncio.run(none.start_runner())
        assert built == [] and none.runner is None, "paper 가 아니면 러너를 만들지 않는다"
        paper = Hub("SOXLUSDT", ["SOXLUSDT", "NVDAUSDT"], "paper")
        asyncio.run(paper.start_runner())
        assert built == [(["SOXLUSDT", "NVDAUSDT"], "SOXLUSDT", "serve", "paper")]
        assert paper.runner is None, "CLI 러너가 임대를 쥐고 있으면 내장 러너 없이 뜬다"
    finally:
        rmod.build_paper_runner = real
    assert "lifespan=lifespan" in SERVER_SRC and "await hub.load_universe()" in SERVER_SRC
    assert "await hub.stop_runner()" in SERVER_SRC
