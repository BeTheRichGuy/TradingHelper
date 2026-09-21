"""주식·ETF perp 유니버스 — 워치리스트와 포커스 종목 검증의 기준.

v2.1 (docs/15 9절): **후보 목록은 우리가 적고, 실제 목록은 바이낸스가 정한다.**

  · `STOCK_PERPS`   후보 (정적). 반도체 · 지수ETF · 대형기술주 · 기타 순서.
  · `GROUP_OF`      후보 → 그룹 태그 (워치리스트 칩).
  · `apply_exchange_info(info)`  서버가 시작할 때 한 번 부른다.
        (a) 후보 중 TRADING 상태인 것만 남기고
        (b) exchange_info 에서 주식으로 보이는 심볼을 더 찾아 그룹 `기타` 로 넣는다.
    부르지 않았거나 실패했으면 정적 후보 목록이 그대로 유니버스다.

이 모듈은 설정도 네트워크도 끌어오지 않는다 — exchange_info 를 **받아서** 처리할 뿐이다.
그래야 테스트가 dict 하나로 발견 규칙을 고정할 수 있다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

GROUP_SEMI = "반도체"
GROUP_INDEX = "지수ETF"
GROUP_BIGTECH = "대형기술주"
GROUP_OTHER = "기타"
GROUPS: tuple[str, ...] = (GROUP_SEMI, GROUP_INDEX, GROUP_BIGTECH, GROUP_OTHER)

# 묶음 순서를 그대로 유지한다 (docs/15 1-1: 워치리스트는 이 순서로 그린다)
_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (GROUP_SEMI, (
        # SOXL 과 같은 바구니 — docs/15 9절의 반도체 목록
        "SOXL", "SOXS", "SMH", "NVDA", "AMD", "INTC", "SMCI", "AMAT", "AVGO", "TSM",
        "ASML", "MU", "QCOM", "TXN", "ARM", "LRCX", "KLAC", "MRVL", "ADI", "ON",
        "MCHP", "MPWR", "SNDK", "WDC", "STX",
    )),
    (GROUP_INDEX, (
        # 지수 ETF (레버리지 없음)
        "SPY", "QQQ", "IWM", "DIA",
    )),
    (GROUP_BIGTECH, (
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NFLX", "IBM", "SHOP", "UBER",
        "HPE", "PANW",
    )),
    (GROUP_OTHER, (
        "MSTR", "COIN", "GME", "PLTR", "DKNG", "EBAY", "COST", "WMT", "LLY", "UNH",
        # 원자재 ETF
        "GLD", "GDX",
    )),
)

GROUP_OF: dict[str, str] = {}
_ordered: list[str] = []
for _group, _tickers in _CANDIDATES:
    for _t in _tickers:
        _sym = f"{_t}USDT"
        if _sym in GROUP_OF:          # 중복(MRVL 처럼 목록에 두 번 적힌 것)은 한 번만
            continue
        GROUP_OF[_sym] = _group
        _ordered.append(_sym)
STOCK_PERPS: tuple[str, ...] = tuple(_ordered)
del _group, _tickers, _t, _sym, _ordered

# STOCK_PRESET 에 섞여 있던 코인 — 여기 없다는 것을 테스트가 확인한다
NOT_STOCKS: frozenset[str] = frozenset({"BTCUSDT", "SPXUSDT", "NXPCUSDT"})

# 레버리지 ETF 배율 표시. 목록에 없으면 워치리스트의 lev_tag 는 null 이다.
# (TQQQ·SQQQ·TZA·UVXY 는 지금 유니버스 밖이지만 태그는 미리 둔다 — 종목을 추가할 때
#  태그를 빠뜨리는 실수를 막기 위해서다)
LEVERAGE_TAG: dict[str, str] = {
    "SOXLUSDT": "3x", "SOXSUSDT": "-3x",
    "TQQQUSDT": "3x", "SQQQUSDT": "-3x",
    "TNAUSDT": "3x", "TZAUSDT": "-3x",
    "SPXLUSDT": "3x", "SPXSUSDT": "-3x",
    "UPROUSDT": "3x", "SPXUUSDT": "-3x",
    "UVXYUSDT": "1.5x",
}

_STOCK_SET = frozenset(STOCK_PERPS)

# exchange_info 의 이 필드들에 이런 말이 들어 있으면 주식으로 본다 (대소문자 무시).
# 판별 필드가 확실치 않아 셋을 다 본다 — 그래서 SOXLUSDT 항목을 DEBUG 로 한 번 찍는다.
_STOCK_FIELDS = ("underlyingType", "underlyingSubType", "contractType")
_STOCK_WORDS = ("STOCK", "EQUITY", "ETF")
_PROBE_SYMBOL = "SOXLUSDT"


@dataclass(slots=True)
class UniverseState:
    """`apply_exchange_info` 의 결과. 부른 적이 없으면 `active` 는 정적 후보 그대로."""

    active: tuple[str, ...] = STOCK_PERPS
    discovered: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    applied: bool = False
    _set: frozenset[str] = field(default_factory=lambda: _STOCK_SET)

    def refresh_set(self) -> None:
        self._set = frozenset(self.active) | frozenset(self.discovered)


_state = UniverseState()


def reset_universe() -> None:
    """정적 후보 목록으로 되돌린다 (테스트용)."""
    global _state
    _state = UniverseState()


def universe_state() -> UniverseState:
    return _state


def active_symbols() -> tuple[str, ...]:
    """워치리스트 순서 — 살아 있는 후보 + 새로 발견된 심볼(뒤에, 그룹 `기타`)."""
    return _state.active + _state.discovered


def looks_like_stock(entry: dict[str, Any]) -> bool:
    """exchange_info 심볼 항목이 주식/ETF 로 보이는가."""
    for key in _STOCK_FIELDS:
        v = entry.get(key)
        vals = v if isinstance(v, (list, tuple)) else [v]
        for x in vals:
            if x is None:
                continue
            if any(w in str(x).upper() for w in _STOCK_WORDS):
                return True
    return False


def apply_exchange_info(info: dict[str, Any] | None) -> UniverseState:
    """바이낸스 exchange_info → 유니버스. 서버가 시작할 때 한 번.

    `info` 가 None 이거나 심볼 목록이 비어 있으면 **아무것도 바꾸지 않는다** —
    조회 실패로 워치리스트가 통째로 비면 안 된다.
    """
    global _state
    symbols = (info or {}).get("symbols") or []
    if not symbols:
        log.warning("exchange_info 에 심볼이 없다 — 정적 유니버스를 그대로 쓴다")
        return _state

    by_sym: dict[str, dict[str, Any]] = {}
    for e in symbols:
        sym = str(e.get("symbol") or "").upper()
        if sym:
            by_sym[sym] = e

    probe = by_sym.get(_PROBE_SYMBOL)
    if probe is not None:
        log.debug("exchange_info %s 항목: %s", _PROBE_SYMBOL, probe)

    trading = {s for s, e in by_sym.items() if str(e.get("status") or "").upper() == "TRADING"}

    active = tuple(s for s in STOCK_PERPS if s in trading)
    dropped = tuple(s for s in STOCK_PERPS if s not in trading)
    discovered = tuple(
        s for s in sorted(by_sym)
        if s not in _STOCK_SET and s in trading and s.endswith("USDT")
        and looks_like_stock(by_sym[s])
    )

    if dropped:
        log.info("바이낸스에 없거나 거래 중이 아닌 후보 %d개 제외: %s",
                 len(dropped), ", ".join(dropped))
    if discovered:
        log.info("후보에 없던 주식 perp %d개 발견 (그룹 %s): %s",
                 len(discovered), GROUP_OTHER, ", ".join(discovered))

    _state = UniverseState(active=active, discovered=discovered, dropped=dropped, applied=True)
    _state.refresh_set()
    return _state


def is_stock_perp(symbol: str | None) -> bool:
    return bool(symbol) and str(symbol).upper() in _state._set


def group_of(symbol: str) -> str:
    return GROUP_OF.get(str(symbol).upper(), GROUP_OTHER)


def leverage_tag(symbol: str) -> str | None:
    return LEVERAGE_TAG.get(str(symbol).upper())


def underlying_ticker(symbol: str) -> str:
    """perp 심볼 → 본주 티커. `TH_UNDERLYING_MAP` 에 없을 때의 기본 규칙(USDT 떼기)."""
    s = str(symbol).upper()
    return s[:-4] if s.endswith("USDT") else s
