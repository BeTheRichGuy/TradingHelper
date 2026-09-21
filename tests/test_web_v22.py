"""v2.2 페이지(백테스트 · 매매기록)와 서버 라우트 — 브라우저 없이 못 박을 수 있는 것들."""

from __future__ import annotations

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parent.parent / "src" / "th" / "api" / "web"
SERVER_SRC = (WEB.parent / "server.py").read_text(encoding="utf-8")
PAGES = {name: (WEB / f"{name}.html").read_text(encoding="utf-8")
         for name in ("index", "backtests", "history")}


def test_세_페이지_모두_외부_리소스와_브라우저_저장소를_쓰지_않는다():
    for name, src in PAGES.items():
        ext = re.findall(r'(?:src|href)\s*=\s*"https?://[^"]+', src)
        assert not ext, f"{name}: 외부 리소스 {ext}"
        for bad in ("localStorage", "sessionStorage", "indexedDB"):
            assert bad not in src, f"{name}: {bad}"


def test_세_페이지가_같은_탭_묶음을_가진다():
    for name, src in PAGES.items():
        assert 'id="nav"' in src, name
        for href in ('href="/"', 'href="/backtests"', 'href="/history"'):
            assert href in src, f"{name} 에 {href} 탭이 없다"
    # 현재 페이지 탭만 켜져 있다
    assert '<a class="on" href="/">' in PAGES["index"]
    assert '<a class="on" href="/backtests">' in PAGES["backtests"]
    assert '<a class="on" href="/history">' in PAGES["history"]


def test_페이지와_API_라우트가_서버에_붙어_있다():
    routes = set(re.findall(r'@app\.(?:get|post|delete)\("([^"]+)"', SERVER_SRC))
    for r in ("/backtests", "/history", "/api/engine", "/api/engine/toggle", "/api/engine/log",
              "/api/backtests", "/api/backtests/data", "/api/backtests/{run_id}", "/api/history"):
        assert r in routes, r
    for name in ("backtests.html", "history.html"):
        assert name in SERVER_SRC


def test_페이지는_계산하지_않고_서버_숫자를_그린다():
    """승률·기대값·PF 의 정의는 서버(ma5m.summarize / api.history) 한 곳에만 있어야 한다."""
    b = PAGES["backtests"]
    for key in ("exp_r", "flip", "oos", "verdict", "equity", "monthly"):
        assert key in b, f"백테스트 페이지가 서버의 {key} 를 그려야 한다"
    assert "function ema(" not in b and "function simulate" not in b
    h = PAGES["history"]
    for key in ("summary", "curve", "enough_sample", "min_sample"):
        assert key in h


def test_옛_서버를_알린다():
    for name in ("backtests", "history"):
        assert "ui) >= 3" in PAGES[name], f"{name}: v2.2 서버(ui:3) 확인이 없다"
    assert '"ui": 3' in SERVER_SRC


def test_엔진_토글은_페이퍼_전용이라는_말이_있다():
    assert "돈이 나가지 않" in PAGES["history"]
    assert "페이퍼" in PAGES["backtests"]
    m = re.search(r'@app\.post\("/api/engine/toggle"\)(.*?)@app\.', SERVER_SRC, re.S)
    assert m and "live" not in m.group(1).lower().replace("live_", ""), "토글이 실거래와 엮이면 안 된다"


def test_표본_30건_미만_경고가_있다():
    assert "30건 미만" in PAGES["history"] or "min_sample" in PAGES["history"]


def test_대시보드_브래킷_행이_자동_주문을_표시한다():
    assert "setup_id" in PAGES["index"] and "자동" in PAGES["index"]
