#!/usr/bin/env python3
"""data.binance.vision 벌크 다운로더 — 1분봉을 종목별 CSV 하나로 합친다.

바이낸스는 과거 봉을 zip 파일로 공개한다. API 로 한 번에 1500봉씩 긁는 것보다
수백 배 빠르고 레이트리밋도 없다.

  월별:  data/futures/um/monthly/klines/<심볼>/1m/<심볼>-1m-YYYY-MM.zip
  일별:  data/futures/um/daily/klines/<심볼>/1m/<심볼>-1m-YYYY-MM-DD.zip
         (이번 달처럼 아직 월별 파일이 안 만들어진 구간용)

표준 라이브러리만 쓴다 — pip 설치 필요 없음.

사용법
------
  python scripts/fetch_vision.py --list                 어떤 종목이 있는지 본다
  python scripts/fetch_vision.py --symbols stocks       미국 주식·ETF perp 전부
  python scripts/fetch_vision.py --symbols SOXLUSDT,SOXSUSDT
  python scripts/fetch_vision.py --symbols crypto --since 2022-01

결과: dataset/vision/<심볼>_1m.csv
      기존 dataset/klines_1m.csv 와 완전히 같은 형식이라 그대로 백테스트에 넣을 수 있다.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

BUCKET_LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BUCKET_GET = "https://data.binance.vision"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
UA = {"User-Agent": "Mozilla/5.0 (trading-helper dataset fetcher)"}

ROOT = Path(__file__).resolve().parent.parent
OUTDIR = ROOT / "dataset" / "vision"

# 받을 수 있는 자료 종류. klines 말고도 **가격이 아닌 정보**가 있다.
DATASETS = {
    "klines":  dict(path="klines/{sym}/1m", tag="1m",
                    cols=["symbol", "ts", "open", "high", "low", "close",
                          "volume", "taker_buy_base"],
                    pick=[1, 2, 3, 4, 5, 9], desc="1분봉"),
    # 5분 간격. 미결제약정, 상위 트레이더 롱숏 비율, 테이커 매수/매도 비율.
    # ← 커버리지 버그 때문에 지금까지 백테스트에 한 번도 못 들어간 자료다.
    "metrics": dict(path="metrics/{sym}", tag="metrics",
                    cols=["symbol", "ts", "open_interest", "open_interest_value",
                          "top_trader_account_ls", "top_trader_position_ls",
                          "all_account_ls", "taker_buy_sell_ratio"],
                    pick=[2, 3, 4, 5, 6, 7], desc="미결제약정·롱숏비율", daily_only=True),
    # perp 이 지수 대비 얼마나 비싼가 = 베이시스
    "premium": dict(path="premiumIndexKlines/{sym}/1m", tag="premium",
                    cols=["symbol", "ts", "open", "high", "low", "close",
                          "volume", "taker_buy_base"],
                    pick=[1, 2, 3, 4, 5, 9], desc="프리미엄 지수(베이시스)"),
}
CACHE = ROOT / "dataset" / ".vision_cache"

# 미국 주식·ETF perp 프리셋. 바이낸스가 계속 추가하므로 --list 로 최신 확인 권장.
STOCK_PRESET = [
    # 반도체 — SOXL 과 같은 바구니
    "SOXLUSDT", "SOXSUSDT", "SMHUSDT", "NVDAUSDT", "AMDUSDT", "INTCUSDT",
    "SMCIUSDT", "NXPCUSDT", "PANWUSDT", "SNDKUSDT",
    # 지수 ETF (레버리지 없음)
    "SPYUSDT", "SPXUSDT", "QQQUSDT", "IWMUSDT", "DIAUSDT",
    # 대형 기술주
    "AAPLUSDT", "MSFTUSDT", "GOOGLUSDT", "AMZNUSDT", "METAUSDT", "TSLAUSDT",
    "NFLXUSDT", "IBMUSDT", "SHOPUSDT", "UBERUSDT", "HPEUSDT",
    # 기타
    "MSTRUSDT", "COINUSDT", "GMEUSDT", "PLTRUSDT", "DKNGUSDT", "EBAYUSDT",
    "COSTUSDT", "WMTUSDT", "LLYUSDT", "UNHUSDT",
    # 원자재 ETF
    "GLDUSDT", "GDXUSDT",
]
# 3배(및 고배율) 레버리지 ETF perp. 이 묶음만 다르게 움직인다 — docs/11 참고.
# 매일 종가에 배율을 다시 맞춰야 해서 오르면 사고 내리면 파는 **강제 흐름**이 생긴다.
LEVERAGED_PRESET = [
    "SOXLUSDT", "SOXSUSDT",     # 반도체 3배 롱/숏  ← 이미 검증에 쓴 것
    "TQQQUSDT", "SQQQUSDT",     # 나스닥100 3배 롱/숏 ← 이미 검증에 쓴 것
    "SPXLUSDT", "SPXSUSDT",     # S&P500 3배 롱/숏   ← 새것
    "UPROUSDT", "SPXUUSDT",     # S&P500 3배 롱/숏(다른 운용사) ← 새것
    "TNAUSDT", "TZAUSDT",       # 러셀2000 3배 롱/숏 ← 새것
    "UVXYUSDT",                 # VIX 1.5배        ← 새것
]

CRYPTO_PRESET = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


# ── HTTP ────────────────────────────────────────────────────────────────
def fetch(url: str, tries: int = 4) -> bytes:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"다운로드 실패: {url} ({last})")


def list_keys(prefix: str, delimiter: str = "") -> tuple[list[str], list[str]]:
    """(파일키들, 하위폴더들) 을 돌려준다. 1000개 넘으면 알아서 이어받는다."""
    keys: list[str] = []
    prefixes: list[str] = []
    marker = ""
    while True:
        url = f"{BUCKET_LIST}?prefix={prefix}&max-keys=1000"
        if delimiter:
            url += f"&delimiter={delimiter}"
        if marker:
            url += f"&marker={urllib.request.quote(marker, safe='')}"
        root = ElementTree.fromstring(fetch(url))
        batch = [e.text for e in root.iter(f"{NS}Key") if e.text]
        keys.extend(batch)
        pre = [e.text for e in root.iter(f"{NS}Prefix") if e.text and e.text != prefix]
        prefixes.extend(pre)
        truncated = (root.findtext(f"{NS}IsTruncated") or "false") == "true"
        if not truncated:
            break
        nxt = root.findtext(f"{NS}NextMarker")
        marker = nxt or (pre[-1] if pre else (batch[-1] if batch else ""))
        if not marker:
            break
    return keys, prefixes


# ── 파싱 ────────────────────────────────────────────────────────────────
def to_dt(raw: str) -> datetime:
    """open_time 단위가 파일마다 다르다 — ms 와 μs 가 섞여 있다."""
    v = int(float(raw))
    if v > 1_000_000_000_000_000:      # 마이크로초
        v //= 1000
    return datetime.fromtimestamp(v / 1000, tz=UTC)


def parse_zip(blob: bytes, symbol: str, spec: dict) -> list[tuple]:
    """자료 종류마다 열 구성이 다르다. spec['pick'] 이 쓸 열 번호를 알려준다."""
    rows = []
    pick = spec["pick"]
    need = max(pick) + 1
    is_metrics = spec["tag"] == "metrics"
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            for rec in csv.reader(io.TextIOWrapper(fh, encoding="utf-8")):
                if len(rec) < need:
                    continue
                if is_metrics:
                    # metrics 는 첫 열이 '2026-05-15 00:00:00' 형식, 둘째가 심볼
                    if rec[0].strip().lower().startswith("create"):
                        continue
                    try:
                        ts = datetime.fromisoformat(rec[0].strip()).replace(tzinfo=UTC)
                    except ValueError:
                        continue
                    rows.append((symbol, ts.isoformat(), *[rec[i] for i in pick]))
                    continue
                if not re.match(r"^[\d.]+$", rec[0]):   # 헤더 줄
                    continue
                rows.append((symbol, to_dt(rec[0]).isoformat(),
                             *[rec[i] for i in pick]))
    return rows


# ── 종목 하나 ───────────────────────────────────────────────────────────
def month_range(start: str | None) -> datetime:
    if not start:
        return datetime(2017, 1, 1, tzinfo=UTC)
    y, m = start.split("-")
    return datetime(int(y), int(m), 1, tzinfo=UTC)


def fetch_symbol(symbol: str, since: str | None, force: bool,
                 kind: str = "klines") -> int:
    spec = DATASETS[kind]
    sub = spec["path"].format(sym=symbol)
    out = OUTDIR / f"{symbol}_{spec['tag']}.csv"
    if out.exists() and not force:
        n = sum(1 for _ in open(out, encoding="utf-8")) - 1
        print(f"  {symbol:<12} 이미 있음 ({n:,}봉) — 다시 받으려면 --force")
        return n

    floor = month_range(since)
    months = []
    if not spec.get("daily_only"):
        mkeys, _ = list_keys(f"data/futures/um/monthly/{sub}/")
        months = sorted(
            k for k in mkeys
            if k.endswith(".zip") and month_range(k[-11:-4]) >= floor
        )

    dkeys, _ = list_keys(f"data/futures/um/daily/{sub}/")
    last_month = months[-1][-11:-4] if months else "0000-00"
    days = sorted(
        k for k in dkeys
        if k.endswith(".zip") and k[-14:-7] > last_month
        and month_range(k[-14:-7]) >= floor
    )
    if not months and not days:
        print(f"  {symbol:<12} ❌ 파일 없음 — 심볼/자료 종류 확인 (--list)")
        return 0

    rows: list[tuple] = []
    todo = months + days
    for i, key in enumerate(todo, 1):
        cached = CACHE / key.replace("/", "_")
        try:
            blob = cached.read_bytes() if cached.exists() else fetch(f"{BUCKET_GET}/{key}")
        except urllib.error.HTTPError:
            continue
        if not cached.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(blob)
        rows.extend(parse_zip(blob, symbol, spec))
        print(f"\r  {symbol:<12} {i}/{len(todo)} 파일 · {len(rows):,}봉", end="", flush=True)

    rows.sort(key=lambda r: r[1])
    seen, uniq = set(), []
    for r in rows:                      # 월별·일별이 겹치는 날 중복 제거
        if r[1] in seen:
            continue
        seen.add(r[1])
        uniq.append(r)

    OUTDIR.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(spec["cols"])
        w.writerows(uniq)
    span = f"{uniq[0][1][:10]} ~ {uniq[-1][1][:10]}" if uniq else "-"
    print(f"\r  {symbol:<12} ✅ {len(uniq):,}봉  {span}" + " " * 20)
    return len(uniq)


# ── 진입점 ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="stocks",
                    help="stocks | leveraged | crypto | 쉼표로 구분한 심볼 목록")
    ap.add_argument("--since", default=None, help="YYYY-MM 이후만 (예: 2022-01)")
    ap.add_argument("--list", action="store_true", help="받을 수 있는 심볼 전부 출력")
    ap.add_argument("--force", action="store_true", help="이미 받은 것도 다시 받기")
    ap.add_argument("--type", default="klines", choices=list(DATASETS),
                    help=" | ".join(f"{k}={v['desc']}" for k, v in DATASETS.items()))
    a = ap.parse_args()

    if a.list:
        print("바이낸스 아카이브에 있는 심볼 목록을 가져오는 중...")
        _, pre = list_keys("data/futures/um/monthly/klines/", delimiter="/")
        syms = sorted(p.rstrip("/").split("/")[-1] for p in pre)
        print(f"총 {len(syms)}개\n")
        for i in range(0, len(syms), 8):
            print("  " + "".join(f"{s:<15}" for s in syms[i:i + 8]))
        return 0

    if a.symbols == "stocks":
        syms = STOCK_PRESET
    elif a.symbols in ("lev", "leveraged"):
        syms = LEVERAGED_PRESET
    elif a.symbols == "crypto":
        syms = CRYPTO_PRESET
    else:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    print(f"종목 {len(syms)}개 · 자료 {DATASETS[a.type]['desc']} · 저장 위치 {OUTDIR}")
    print("(같은 파일을 두 번 받지 않도록 zip 을 dataset/.vision_cache 에 보관한다)\n")
    t0, total, ok = time.time(), 0, 0
    for s in syms:
        try:
            n = fetch_symbol(s, a.since, a.force, a.type)
        except Exception as e:  # noqa: BLE001
            print(f"\r  {s:<12} ❌ {e}" + " " * 20)
            continue
        total += n
        ok += bool(n)
    print(f"\n완료 — 종목 {ok}/{len(syms)} · 총 {total:,}봉 · {time.time() - t0:.0f}초")
    print(f"파일: {OUTDIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
