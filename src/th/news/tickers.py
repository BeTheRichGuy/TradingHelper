"""텍스트 → 유니버스 심볼.

뉴스 한 줄에서 어느 종목 얘기인지 뽑는다. 이게 LLM 앞의 **무료 프리필터**다 — 심볼이 하나도
안 붙은 뉴스는 판단으로 보내지 않는다 (docs/17 §3). 그래서 **놓치는 것보다 잘못 붙이는 것이 낫다**:
잘못 붙으면 트리아지(저가 모델)가 걸러 주지만, 놓치면 기회가 사라진다. 다만 `ON`·`COST` 같은
영어 단어 티커는 반대다 — 그건 이름이나 `$` 가 있어야 붙인다. 안 그러면 모든 뉴스가 통과한다.

유니버스는 `th.universe` 가 정한다. 여기서는 그 심볼들의 **이름 사전**만 가진다.
"""

from __future__ import annotations

import re

from ..universe import STOCK_PERPS, active_symbols

# 티커 → (표시 이름, 별칭들). 별칭은 소문자·단어 경계로 찾는다.
# 일반 단어와 겹치는 티커(ON·ARM·COST·COIN·SHOP·DIA)는 별칭을 회사명 전체로만 둔다.
_NAMES: dict[str, tuple[str, tuple[str, ...]]] = {
    # 반도체
    "SOXL": ("Direxion 반도체 3x 롱", ("direxion semiconductor bull", "soxl")),
    "SOXS": ("Direxion 반도체 3x 숏", ("direxion semiconductor bear", "soxs")),
    "SMH": ("VanEck 반도체 ETF", ("vaneck semiconductor", "semiconductor etf")),
    "NVDA": ("Nvidia", ("nvidia", "nvda")),
    "AMD": ("AMD", ("advanced micro devices", "amd")),
    "INTC": ("Intel", ("intel", "intc")),
    "SMCI": ("Super Micro", ("super micro computer", "supermicro", "super micro", "smci")),
    "AMAT": ("Applied Materials", ("applied materials", "amat")),
    "AVGO": ("Broadcom", ("broadcom", "avgo")),
    "TSM": ("TSMC", ("tsmc", "taiwan semiconductor", "taiwan semi")),
    "ASML": ("ASML", ("asml",)),
    "MU": ("Micron", ("micron technology", "micron")),
    "QCOM": ("Qualcomm", ("qualcomm", "qcom")),
    "TXN": ("Texas Instruments", ("texas instruments", "txn")),
    "ARM": ("Arm Holdings", ("arm holdings",)),
    "LRCX": ("Lam Research", ("lam research", "lrcx")),
    "KLAC": ("KLA", ("kla corp", "kla corporation", "kla-tencor", "klac")),
    "MRVL": ("Marvell", ("marvell", "mrvl")),
    "ADI": ("Analog Devices", ("analog devices",)),
    "ON": ("onsemi", ("onsemi", "on semiconductor")),
    "MCHP": ("Microchip", ("microchip technology", "microchip", "mchp")),
    "MPWR": ("Monolithic Power", ("monolithic power", "mpwr")),
    "SNDK": ("Sandisk", ("sandisk", "sndk")),
    "WDC": ("Western Digital", ("western digital", "wdc")),
    "STX": ("Seagate", ("seagate", "stx")),
    # 지수 ETF
    "SPY": ("SPDR S&P 500", ("spdr s&p 500", "s&p 500 etf", "spy etf")),
    "QQQ": ("Invesco QQQ", ("invesco qqq", "nasdaq 100 etf", "qqq")),
    "IWM": ("iShares Russell 2000", ("russell 2000 etf", "iwm")),
    "DIA": ("SPDR Dow Jones", ("spdr dow jones", "dow jones etf")),
    # 대형 기술주
    "AAPL": ("Apple", ("apple", "aapl")),
    "MSFT": ("Microsoft", ("microsoft", "msft")),
    "GOOGL": ("Alphabet", ("alphabet inc", "alphabet", "google", "googl", "goog")),
    "AMZN": ("Amazon", ("amazon", "amzn")),
    "META": ("Meta", ("meta platforms", "facebook", "meta")),
    "TSLA": ("Tesla", ("tesla", "tsla")),
    "NFLX": ("Netflix", ("netflix", "nflx")),
    "IBM": ("IBM", ("ibm", "international business machines")),
    "SHOP": ("Shopify", ("shopify",)),
    "UBER": ("Uber", ("uber",)),
    "HPE": ("HPE", ("hewlett packard enterprise", "hpe")),
    "PANW": ("Palo Alto Networks", ("palo alto networks", "panw")),
    # 기타
    "MSTR": ("MicroStrategy", ("microstrategy", "strategy inc", "mstr")),
    "COIN": ("Coinbase", ("coinbase",)),
    "GME": ("GameStop", ("gamestop", "gme")),
    "PLTR": ("Palantir", ("palantir", "pltr")),
    "DKNG": ("DraftKings", ("draftkings", "dkng")),
    "EBAY": ("eBay", ("ebay",)),
    "COST": ("Costco", ("costco",)),
    "WMT": ("Walmart", ("walmart", "wmt")),
    "LLY": ("Eli Lilly", ("eli lilly", "lilly", "lly")),
    "UNH": ("UnitedHealth", ("unitedhealth", "united health", "unh")),
    "GLD": ("SPDR Gold", ("spdr gold", "gold etf", "gld")),
    "GDX": ("VanEck Gold Miners", ("gold miners etf", "gdx")),
}

# 대문자 그대로 나와도 회사로 볼 수 없는 티커 — 이름이나 $ 가 있어야 한다
AMBIGUOUS_BARE: frozenset[str] = frozenset({"ON", "ARM", "COST", "COIN", "SHOP", "DIA", "META", "SPY", "IBM"})
# 2글자 티커는 $ 가 있어야 한다 (MU)
_MIN_BARE_LEN = 3

_CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")
_BARE = re.compile(r"(?<![A-Za-z$])([A-Z]{2,5})(?![A-Za-z])")


def ticker_of(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def symbol_of(ticker: str) -> str:
    return f"{ticker.upper()}USDT"


def display_name(symbol: str) -> str:
    t = ticker_of(symbol)
    return _NAMES.get(t, (t, ()))[0]


def aliases(symbol: str) -> tuple[str, ...]:
    return _NAMES.get(ticker_of(symbol), ("", ()))[1]


def _universe_tickers() -> dict[str, str]:
    """{티커: 심볼} — 정적 후보 + 바이낸스가 찾아 준 것."""
    out: dict[str, str] = {}
    for sym in tuple(STOCK_PERPS) + tuple(active_symbols()):
        if sym.endswith("USDT"):
            out[ticker_of(sym)] = sym
    return out


_alias_cache: dict[str, re.Pattern[str]] = {}


def _alias_pattern(alias: str) -> re.Pattern[str]:
    p = _alias_cache.get(alias)
    if p is None:
        p = re.compile(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", re.IGNORECASE)
        _alias_cache[alias] = p
    return p


def extract(text: str) -> tuple[str, ...]:
    """텍스트에서 유니버스 심볼을 뽑는다. 정렬된 튜플, 중복 없음."""
    if not text:
        return ()
    universe = _universe_tickers()
    found: set[str] = set()

    for m in _CASHTAG.finditer(text):
        t = m.group(1).upper()
        if t in universe:
            found.add(t)

    for m in _BARE.finditer(text):
        t = m.group(1)
        if t in universe and len(t) >= _MIN_BARE_LEN and t not in AMBIGUOUS_BARE:
            found.add(t)

    for t in universe:
        if t in found:
            continue
        for a in _NAMES.get(t, ("", ()))[1]:
            # 별칭이 티커 자체이고 그 티커가 모호하면 이름으로는 안 잡는다 (예: "meta" 소문자 단어)
            if a.upper() == t and t in AMBIGUOUS_BARE:
                continue
            if _alias_pattern(a).search(text):
                found.add(t)
                break

    return tuple(sorted(universe[t] for t in found))


def known_symbols() -> tuple[str, ...]:
    """이름 사전이 있는 심볼 전부 (프롬프트·RSS 검색어용)."""
    return tuple(symbol_of(t) for t in _NAMES)
