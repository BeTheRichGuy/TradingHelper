"""중복 키.

같은 기사가 Yahoo·Google·Finnhub·텔레그램에서 각각 온다. 두 단계로 잡는다.

  1. `dedupe_key`  URL 이 있으면 정규화한 URL 의 해시. 없으면 (소스, 정규화 제목).
  2. `title_key`   정규화한 제목만의 해시. URL 이 달라도 제목이 같으면 48시간 안에서는 같은 기사로 본다
                   (저장소가 확인한다).

정규화는 보수적으로 한다 — 다른 기사를 같다고 하는 것이 같은 기사를 두 번 판단하는 것보다 나쁘다
(판단은 돈이 들지만 놓친 뉴스는 기회다).
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import NewsItem

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "yclid", "mc_", "ref", "ref_", "src", ".tsrc", "guccounter", "guce_")
# 링크가 리다이렉트 래퍼라 기사 식별에 못 쓰는 호스트 — 제목으로 잡는다
_OPAQUE_HOSTS = ("news.google.com",)

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SOURCE_SUFFIX = re.compile(r"\s+[-–—|]\s+[A-Za-z][A-Za-z .&']{2,40}$")   # "… - Reuters"


def normalize_url(url: str) -> str:
    """추적 파라미터·조각·대소문자 호스트·끝 슬래시를 없앤다. 비면 빈 문자열."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not k.lower().startswith(_TRACKING_PREFIXES)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, urlencode(sorted(q)), ""))


def is_opaque_url(url: str) -> bool:
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _OPAQUE_HOSTS)


def normalize_title(title: str) -> str:
    t = (title or "").strip()
    t = _SOURCE_SUFFIX.sub("", t)
    t = t.lower()
    t = _PUNCT.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    return t


def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:24]


def title_key(title: str) -> str:
    n = normalize_title(title)
    return "t:" + _h(n) if n else ""


def dedupe_key(item: NewsItem) -> str:
    """항목의 1차 중복 키."""
    u = normalize_url(item.url)
    if u and not is_opaque_url(u):
        return "u:" + _h(u)
    if item.source_id:
        return "i:" + _h(f"{item.source}|{item.channel}|{item.source_id}")
    return "s:" + _h(f"{item.source}|{normalize_title(item.title)}")
