#!/usr/bin/env python3
"""바이낸스 웹소켓 스트림 이름별로 뭐가 오는지 60초 동안 세어 본다.

실측(2026-09-10): SOXLUSDT 는 bookTicker 만 오고 kline_1m / markPrice@1s / forceOrder 는 0건.
이 스크립트는 후보 스트림을 한꺼번에 구독해 어떤 이름이 살아 있는지 보여준다.
비교용으로 BTCUSDT kline 도 같이 구독한다 — 그건 오는데 SOXL 만 안 오면 상품 특성이다.

사용:  python scripts\\ws_probe.py            (기본 SOXLUSDT, 60초)
       python scripts\\ws_probe.py NVDAUSDT 90
"""
import asyncio
import json
import sys
import time
from datetime import UTC, datetime

try:
    import websockets
except ImportError:
    sys.exit("websockets 패키지가 없습니다 — 가상환경(.venv)을 켰는지 확인하세요")

SYM = (sys.argv[1] if len(sys.argv) > 1 else "SOXLUSDT").lower()
SECS = int(sys.argv[2]) if len(sys.argv) > 2 else 60
CANDIDATES = [
    f"{SYM}@kline_1m", f"{SYM}@kline_5m", f"{SYM}@markPrice@1s", f"{SYM}@markPrice",
    f"{SYM}@bookTicker", f"{SYM}@aggTrade", f"{SYM}@trade", f"{SYM}@miniTicker",
    f"{SYM}@ticker", f"{SYM}@depth5@500ms", f"{SYM}@forceOrder",
    "btcusdt@kline_1m", "btcusdt@markPrice@1s",          # 비교용
]
URL = "wss://fstream.binance.com/stream?streams=" + "/".join(CANDIDATES)


async def main() -> None:
    counts: dict[str, int] = {c: 0 for c in CANDIDATES}
    closed: dict[str, int] = {}
    t0 = last = time.time()
    print(f"구독 {len(CANDIDATES)}개 · {SECS}초")
    async with websockets.connect(URL, ping_interval=20, ping_timeout=20) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            stream = msg.get("stream", "?")
            counts[stream] = counts.get(stream, 0) + 1
            if "@kline" in stream:
                k = msg["data"]["k"]
                if k.get("x"):
                    closed[stream] = closed.get(stream, 0) + 1
                    ts = datetime.fromtimestamp(k["t"] / 1000, tz=UTC)
                    print(f"  ✅ {stream} 마감 봉 {ts:%H:%M} UTC  C {k['c']}")
            now = time.time()
            if now - last >= 15:
                last = now
                print(f"[{int(now - t0):3d}s] " + " · ".join(f"{s.split('@',1)[1]}={n}" for s, n in counts.items() if s.startswith(SYM)))
            if now - t0 >= SECS:
                break
    print("\n결과:")
    for s, n in counts.items():
        mark = "✅" if n else "❌"
        extra = f" (마감 {closed[s]})" if s in closed else ""
        print(f"  {mark} {s:<28} {n:6d}{extra}")
    print("\n해석: kline_1m 이 ❌ 인데 btcusdt@kline_1m 이 ✅ 면, 주식 perp 은 봉 스트림을 안 주는 것 —")
    print("      th stream 은 이제 REST 로 20초마다 봉을 받아오니 그대로 쓰면 된다.")
    alive = [s for s, n in counts.items() if n and s.startswith(SYM) and "@kline" not in s and "bookTicker" not in s]
    if alive:
        print(f"      {SYM} 에서 살아 있는 다른 스트림: {', '.join(alive)}")


asyncio.run(main())
