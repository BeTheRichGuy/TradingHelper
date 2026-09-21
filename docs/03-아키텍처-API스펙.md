# 시스템 아키텍처 & API 스펙 (v0.2)

> ## v0.3 (2026-09-08) — `signal` 은 선택적, `alert` 가 1급이 됐습니다
>
> Phase 4 판정으로 신호 카드가 빠졌습니다 (`08-전략-검증-결과.md`). API 구조도
> 그에 맞춥니다.
>
> - **`signal` 메시지는 보내지 않습니다.** 스키마는 남겨두되(재도입 대비) 서버는
>   발행하지 않고, 클라이언트는 없어도 동작해야 합니다
> - **`alert` 와 `no_trade` 가 화면의 중심**입니다
> - `briefing`(AI)도 방향 예측을 전제하므로 함께 보류

작성일: 2026-08-29 · **v0.1(일봉 배치) 폐기하고 재작성**

## 1. 무엇이 달라졌나

v0.1은 하루 한 번 도는 배치 구조였다. 지금은 **1분봉이 마감될 때마다 1초 안에 화면이 바뀌어야 하는 실시간 시스템**이다. 요청/응답이 아니라 **스트리밍이 기본**이고, REST는 초기 로딩과 보조 조회에만 쓴다.

```
   Binance WebSocket (fstream)              Alpaca / yfinance        Finnhub
   kline_1m · aggTrade · markPrice          SOXX·SOXL 실제가         캘린더·뉴스
   bookTicker · forceOrder                        │                     │
            │                                     │                     │
            ▼                                     ▼                     ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │  Ingestion Worker (asyncio)                                            │
   │   · 봉 조립/검증  · OI·펀딩 폴링  · basis 계산  · 캘린더 동기화          │
   └───────────────┬─────────────────────────────┬──────────────────────────┘
                   │ 저장                         │ 봉 마감 이벤트
                   ▼                              ▼
          ┌────────────────┐          ┌──────────────────────────────┐
          │  TimescaleDB   │          │  Analysis Worker             │
          │  1m봉·OI·펀딩  │◀────────▶│  게이트→셋업→리스크→AI       │
          └────────────────┘          └───────────────┬──────────────┘
                                                      │ publish
                   ┌──────────────────────────────────▼──────────┐
                   │            Redis pub/sub + 캐시              │
                   └──────────────────┬───────────────────────────┘
                                      ▼
                   ┌──────────────────────────────────────────────┐
                   │  FastAPI  —  WebSocket /ws + REST /api/v1    │
                   └───────────┬──────────────────┬───────────────┘
                               ▼                  ▼
                        Next.js 웹          Expo RN 앱
                          (같은 WS 페이로드, 같은 TS 타입)
```

**핵심:** 계산은 Analysis Worker 한 곳에서만 일어나고, 웹과 앱은 똑같은 WebSocket 메시지를 받는다. 이게 "연동"의 전부이자 전제다.

## 2. 기술 스택

| 레이어 | 선택 | 이유 |
|---|---|---|
| 백엔드 | Python 3.12 + FastAPI + asyncio | pandas/LightGBM/FAISS가 전부 파이썬. WS도 무난 |
| 시세 수집 | `websockets` 직접 구현 (또는 `python-binance`) | 재연결·시퀀스 갭 처리를 직접 제어해야 함 |
| DB | PostgreSQL + TimescaleDB | 1분봉 하이퍼테이블, 연속 집계로 5m/15m 자동 생성 |
| 캐시/버스 | Redis (pub/sub + 최신 스냅샷) | |
| ML | LightGBM, FAISS, scikit-learn | `06` 문서 |
| LLM | Claude API | 브리핑, 뉴스 구조화 |
| 웹 | Next.js 15 + TypeScript + Tailwind | |
| 앱 | Expo (React Native) + TypeScript | |
| 차트 | `lightweight-charts` (웹) / 동일 라이브러리 WebView (앱) | 실시간 업데이트에 강함 |
| 모노레포 | pnpm + Turborepo | |

### 저장소 구조

```
trading-helper/
├─ apps/
│  ├─ web/                     # Next.js 대시보드
│  └─ mobile/                  # Expo
├─ packages/
│  ├─ api-client/              # OpenAPI→TS 타입 + WS 클라이언트 (웹·앱 공유)
│  └─ core/                    # 포맷터, 색상 임계값 등 표시 로직
└─ services/
   ├─ ingest/                  # WebSocket 수집 워커
   ├─ analysis/                # 봉 마감 → 신호 생성 워커
   │   ├─ calendar/            # 05 문서 (rules.yaml + 게이트)
   │   ├─ strategy/            # 02 문서 (indicators, setups, risk, scoring)
   │   └─ ai/                  # 06 문서 (knn, gbm, agreement, briefing)
   ├─ api/                     # FastAPI (WS + REST)
   └─ jobs/                    # 백테스트, 주간 재학습, 캘린더 동기화
```

> **Phase 0 실제 구현 메모:** 아직 웹·앱이 없으므로 위 모노레포 구조는 Phase 5에서 도입한다.
> 현재는 `src/th/` 아래에 `exchange/`(수집), `ingest/`(백필·폴링·갭검사),
> `underlying/`(본주·지수), `sessions/`(Phase 1 예정) 로 단순화되어 있다.
> 웹을 붙이는 시점에 `services/` 로 옮긴다.

## 3. 데이터 소스 상세

### 3.1 바이낸스 (메인)

WebSocket `wss://fstream.binance.com/stream`

| 스트림 | 용도 |
|---|---|
| `<symbol>@kline_1m` | 1분봉. **`k.x == true`(봉 마감)일 때만 확정 처리** |
| `<symbol>@aggTrade` | CVD(누적 델타) 계산 |
| `<symbol>@markPrice@1s` | 마크가격, **펀딩률·다음 정산 시각** |
| `<symbol>@bookTicker` | 실시간 호가 스프레드 |
| `<symbol>@forceOrder` | **강제청산 스트림** — 청산 캐스케이드 감지에 유용 |

> Phase 0 에서는 `aggTrade` 를 구독하지 않는다. 1분 단위 CVD 는 봉의 `taker_buy_base`
> 로 충분하고, `aggTrade` 는 초당 수백 건이라 로컬 PC 디스크만 먹는다.
> 초 단위 델타가 필요해지면 그때 켠다.

REST `https://fapi.binance.com`

| 엔드포인트 | 용도 | 주기 |
|---|---|---|
| `/fapi/v1/klines` | 과거 봉 백필 (최대 1500봉/요청) | 기동 시 |
| `/futures/data/openInterestHist` | OI 히스토리 (5분 단위) | 5분 |
| `/fapi/v1/fundingRate` | 펀딩 히스토리 | 1시간 |
| `/fapi/v1/leverageBracket` | **유지증거금률(MMR)** — 청산가 계산에 필수 | 하루 1회 |
| `/fapi/v1/exchangeInfo` | 틱사이즈·최소주문 | 하루 1회 |

레이트리밋: IP당 2400 weight/분. WebSocket 중심 설계라 여유롭다.

> **주의:** 주식 perp은 상장 시점이 최근이라 `klines` 히스토리가 짧다. 백테스트·k-NN 코퍼스는 본주(SOXL) 1분봉으로 보강한다 (`06` 문서 A.2).
>
> **`leverageBracket` 은 서명이 필요해 API 키가 있어야 한다.** Phase 0 에서는 호출하지
> 않고, Phase 2(계정 연동)에서 붙인다. 그때까지 청산가는 표시하지 않는다.

### 3.2 읽기 전용 계정 연동

| 엔드포인트 | 표시 항목 |
|---|---|
| `/fapi/v2/positionRisk` | 진입가, 수량, 미실현손익, **청산가**, 레버리지, 마진타입 |
| `/fapi/v2/account` | 지갑잔고, 사용가능 증거금 |
| `/fapi/v1/openOrders` | **손절 주문 존재 여부** ← `02` 5.5의 ①번 방어 |
| `/fapi/v1/userTrades` | 당일 체결 내역 → 일일 손익 한도 계산 |

**보안 규칙**

- 바이낸스에서 API 키 생성 시 **"Enable Reading"만 체크**. 거래·출금 권한은 절대 부여하지 않는다.
- **IP 화이트리스트를 반드시 설정한다.** 서버 고정 IP를 등록하고, 그 외에서는 키가 동작하지 않게 한다.
- 키는 서버 환경변수(또는 시크릿 매니저)에만 둔다. DB에 저장하지 않고, **클라이언트로 절대 내려보내지 않는다.**
- 서명은 서버에서만 (HMAC SHA256), `recvWindow=5000`.

### 3.3 보조 소스

| 데이터 | 소스 | 용도 |
|---|---|---|
| SOXL·SOXX 실제가 | Alpaca (프리·본장·애프터 포함) | basis 계산, 본주 기준 % 환산 |
| VIX | yfinance `^VIX` | 변동성 필터 |
| 거래일·휴장 | `pandas_market_calendars` | 세션 판정 |
| 경제·실적 캘린더, 뉴스 | Finnhub | 이벤트 게이트, 뉴스 레이어 |

> **Phase 0 실제 구현:** 본주·지수는 Alpaca 대신 **yfinance** 로 받고 있다(무료·키 불필요).
> 다만 yfinance 는 **1분봉을 최근 30일치까지만** 준다. 긴 히스토리가 필요해지는
> Phase 4~6 에서 Alpaca 어댑터를 `src/th/underlying/` 에 추가한다. 일봉은 제약이 없어
> 10년치를 이미 받고 있다.

## 4. 실시간 처리 파이프라인

```
1분봉 마감 (k.x = true)
  ├─ 0ms    봉 검증 (시퀀스 갭·중복 확인) → TimescaleDB insert
  ├─ ~20ms  지표 갱신 (증분 계산 — 전체 재계산 금지)
  ├─ ~30ms  리스크 스냅샷 갱신 (청산가 거리, 스프레드, basis, 펀딩)
  └─ Redis publish → WS 브로드캐스트      ◀── 여기까지 항상 1초 이내

5분봉 마감이면 추가로
  ├─ ~10ms  세션·이벤트 게이트 (blocked면 즉시 종료)
  ├─ ~40ms  셋업 평가 + 신뢰도
  ├─ ~50ms  k-NN 검색 (FAISS)
  ├─ ~5ms   LightGBM 추론
  ├─         시나리오 대조 → 신호 확정 → publish
  └─ 비동기  LLM 브리핑 (2~5초) → 완료되면 별도 메시지로 추가 publish
```

**지표는 반드시 증분 갱신**한다. 봉마다 300봉을 pandas로 다시 도는 구조는 심볼이 5개만 돼도 무너진다.

## 5. WebSocket API

```
wss://<host>/ws?symbols=SOXLUSDT,NVDAUSDT
```

접속 직후 `snapshot` 1회, 이후 델타 메시지만 흘린다.

### 5.1 메시지 타입

| type | 발생 | 내용 |
|---|---|---|
| `snapshot` | 접속 시 | 아래 모든 블록의 현재 상태 |
| `bar` | 1분봉 마감 | 봉 1개 + 갱신된 지표값 |
| `risk` | 1분봉 마감 / 포지션 변화 | 청산가 거리, ATR, 스프레드, basis, 펀딩, 일일손익 |
| `session` | 레짐 전환 / 1분마다 | 레짐, 남은시간, 다음이벤트, 활성 페널티 |
| **`alert`** | 즉시 | 청산 임박, 손절주문 없음/무효, 이벤트 임박, 일일한도 |
| **`no_trade`** | 1분마다 / 상태 변화 | **지금 매매하면 안 되는 이유 목록** (신호 카드 자리) |
| ~~`signal`~~ | — | **v0.3 에서 발행 중단.** 스키마는 5.2 에 남겨둠 |
| ~~`briefing`~~ | — | v0.3 에서 보류 (방향 예측 전제) |

### 5.2 `no_trade` 페이로드 — 신호 카드가 있던 자리

```jsonc
{
  "type": "no_trade",
  "symbol": "SOXLUSDT",
  "ts": "2026-09-10T18:00:00Z",
  "reasons": [
    { "source": "session", "code": "opening_15m",
      "text_ko": "개장 직후 15분 — 밤새 쌓인 주문이 쏟아져 VWAP·EMA가 모두 무의미합니다" },
    { "source": "market", "code": "spread",
      "text_ko": "호가 스프레드 13.0bp — 10배에서 진입·청산 비용이 손절폭을 잠식합니다" },
    { "source": "event", "code": "event_imminent",
      "text_ko": "CPI까지 20분 — 발표 순간엔 손절가를 건너뜁니다" }
  ]
}
```

**`reasons` 가 비어 있는 것은 매수 신호가 아닙니다.** 클라이언트는 반드시
"막을 이유를 찾지 못했습니다 / 매매해도 된다는 뜻이 아닙니다" 로 표시해야 합니다.
서버 구현은 `th.ui.dashboard.no_trade_reasons()` 와 같은 규칙을 씁니다.

### 5.2-b `signal` 페이로드 — **v0.3 에서 발행하지 않음**

> 아래 스키마는 Phase 4 통과 기준(out-of-sample 기대값 ≥ +0.20R, 표본 ≥ 300)을
> 넘긴 뒤 재도입할 때를 위해 남겨둡니다. 지금은 서버가 이 메시지를 보내지 않습니다.

계좌 10,000 USDT · 리스크비율 0.5% 가정. 모든 숫자는 `02` 문서 5절 산출식을 그대로 적용한 것이다.

```jsonc
{
  "type": "signal",
  "symbol": "SOXLUSDT",
  "bar_ts": "2026-08-29T18:35:00Z",
  "session": { "regime": "RTH", "regime_ends_in_sec": 8040,
               "next_event": { "name": "FOMC 성명", "in_sec": 7200, "importance": "high" } },
  "signal": {
    "direction": "long",                 // long | short | none
    "setup": "S1_vwap_pullback",
    "confidence": { "base": 72, "session_adj": 0, "ai_adj": +10, "final": 82, "band": "보통" },
    "entry": { "low": 231.00, "high": 231.60 },
    "entry_ref": 231.60,                 // 롱이므로 구간 상단 = 최악 체결 가정
    "stop_loss": 230.21,                 // 231.60 × (1 − 0.60%)
    "risk_pct": 0.60,
    "risk_per_unit": 1.39,               // 1R
    "take_profit": [
      { "level": 234.38, "r_multiple": 2.0, "pct": 1.20, "close_pct": 50 },
      { "level": 237.16, "r_multiple": 4.0, "pct": 2.40, "close_pct": 50 }
    ],
    "risk_reward": 4.0,
    "expected_minutes_to_tp1": { "median": 22, "q1": 11, "q3": 41 },
    "position": { "qty": 35, "notional_usdt": 8106, "risk_usdt": 48.7,
                  "margin_at_10x_usdt": 811 },
    "funding": { "rate": 0.00041, "next_settlement_in_sec": 1080,
                 "within_expected_hold": true, "estimated_cost_usdt": 3.3 }
  },
  // 지면상 3개만 표시 — 실제로는 해당 셋업의 조건 전체가 담긴다
  "evidence": [
    { "id": "S1-3", "label": "VWAP 되돌림 후 복귀", "required": true, "passed": true,
      "detail": "저가 230.9로 VWAP(231.1) 0.09% 이내 접근 후 231.4 마감" },
    { "id": "S1-6", "label": "미결제약정 증가", "required": false, "weight": 15, "passed": true,
      "detail": "ΔOI(15m) +2.1% — 신규 롱 유입" },
    { "id": "S1-9", "label": "펀딩 쏠림 없음", "required": false, "weight": 10, "passed": false,
      "detail": "펀딩률 +0.041%로 기준(0.03%) 초과 — 가중치 10점 미획득" }
  ],
  "ai": {
    "scenario_a": { "...": "06 문서 A.3" },
    "scenario_b": { "...": "06 문서 B.4" },
    "agreement": { "status": "strong", "gap": 0.00, "confidence_adjustment": 10 }
  },
  "win_rate": { "value": 0.57, "sample_size": 186, "ci_low": 0.50, "ci_high": 0.64,
                "expectancy_r": 0.51,
                "basis": "S1 · RTH · 신뢰도 70-84 · out-of-sample",
                "sufficient": true },
  "disclaimer": "정보 제공 목적이며 투자 권유가 아닙니다."
}
```

읽는 법: **손절 0.60%짜리 자리에서 계좌의 0.5%(50 USDT)만 걸려면 명목가는 8,106 USDT**다. 10배 설정에서 실제 묶이는 증거금은 811 USDT. 레버리지 10배가 곧 "계좌 10배어치를 산다"는 뜻이 아니라는 걸 화면에서 계속 상기시키는 게 이 필드들의 목적이다.

`direction: "none"`일 때도 `evidence`와 `session`은 채워 보낸다. **"왜 지금은 아닌가"가 이 앱의 주된 출력이다.**

### 5.3 `alert` — 우선순위 최상

```jsonc
{ "type": "alert", "level": "critical",
  "code": "NO_STOP_ORDER",
  "message_ko": "포지션이 열려 있는데 손절 주문이 없습니다",
  "detail_ko": "롱 35계약 · 진입 231.4 · 청산가 209.8" }
```

| code | level | 조건 |
|---|---|---|
| `NO_STOP_ORDER` | critical | 포지션 있고 STOP류 주문 없음 |
| **`STOP_BEYOND_LIQUIDATION`** | critical | **손절가가 청산가보다 멂 — 손절 전에 청산된다** |
| **`STOP_ALREADY_PASSED`** | critical | **손절가가 이미 현재가를 지남 — 주문이 있어도 보호가 아님** |
| `LIQ_PROXIMITY` | critical | 청산가까지 3% 이내 |
| `EVENT_IMMINENT` | warning | 고중요도 이벤트 15분 이내 + 포지션 보유 |
| `DAILY_LOSS_LIMIT` | warning | 당일 −3R 도달 |
| `REGIME_CLOSING` | info | 본장 마감 15분 전 / 주말 진입 60분 전 |
| `VOL_SPIKE` | warning | ATR이 95퍼센타일 초과 |
| `LIQUIDATION_CASCADE` | warning | `forceOrder` 스트림에서 대량 청산 감지 |

**`STOP_BEYOND_LIQUIDATION` 과 `STOP_ALREADY_PASSED` 는 v0.3 에서 추가됐습니다.**
손절 주문이 *존재하는지*만 보면 화면에 ✓ 가 뜨는데 실제로는 보호가 없는 상태를
놓칩니다. 그 둘은 "손절 없음"보다 위험합니다 — 사용자가 안심하기 때문입니다.
둘은 동시에 뜨지 않습니다 (이미 지난 쪽이 우선).

## 6. REST API (보조)

Base `/api/v1`

| Method | Path | 용도 |
|---|---|---|
| GET | `/bootstrap?symbols=` | 초기 로딩 (봉 히스토리 + 스냅샷) |
| GET | `/ohlcv?symbol=&tf=&limit=` | 차트 백필 |
| GET | `/session/today` | 오늘의 세션·이벤트 타임라인 (차트 마커용) |
| GET | `/account/position` | 포지션 스냅샷 (WS 백업 경로) |
| GET | `/strategy/{setup}/stats?session=` | 셋업별 백테스트 통계 |
| POST | `/backtest` | 파라미터 바꿔 재실행 (개발용) |
| GET | `/health` | WS 연결 상태, 마지막 봉 시각, 모델 버전 |

`/health`는 대시보드 구석에 항상 띄운다. **데이터가 끊긴 줄 모르고 옛날 차트를 보는 것이 이 종류 도구의 가장 흔한 사고다.**

## 7. DB 스키마

> 아래는 최종 목표 스키마다. Phase 0 에서 실제로 만들어진 테이블은
> `db/init/001_schema.sql` 을 보라 (`klines`, `open_interest`, `funding`,
> `book_ticker`, `underlying_prices`, `liquidations`, `symbols`, `users`,
> `ingest_state`, `ingest_gaps` + 연속집계 뷰 `klines_5m` / `klines_15m`).

```sql
-- 시계열 (TimescaleDB 하이퍼테이블)
klines(symbol, tf, ts, open, high, low, close, volume, quote_volume, trades)
open_interest(symbol, ts, oi, oi_value)
funding(symbol, ts, rate, next_funding_ts)
underlying(symbol, ts, price, source)          -- SOXL·SOXX 실제가
book_snapshot(symbol, ts, bid, ask, spread_bps)

-- 이벤트
calendar_events(id, ts_utc, kind, name, importance, symbols, source, payload jsonb)
news_items(id, ts_utc, headline, url, affected jsonb, polarity, magnitude, duration)

-- 신호와 사후검증
signals(id, symbol, tf, bar_ts, regime, direction, setup,
        confidence_base, confidence_final, entry_low, entry_high, entry_ref,
        stop_loss, tp1, tp2, evidence jsonb, ai jsonb,
        model_versions jsonb, created_at)
signal_outcomes(signal_id, exit_ts, exit_reason, realized_r, is_win, mae_pct, mfe_pct)

-- 계정 미러 (읽기전용 조회 결과 캐시)
account_snapshots(ts, balance, positions jsonb, open_orders jsonb)
daily_pnl(date_kst, realized_r, trade_count, limit_hit_at)

-- ML
backtest_runs(id, setup, session, params jsonb, period, trades,
              win_rate, avg_win_r, avg_loss_r, expectancy_r, max_dd)
model_registry(name, version, trained_at, auc, brier, params jsonb, artifact_uri)
```

`signals`에 **모델 버전과 AI 출력까지 통째로 저장**한다. 몇 주 뒤 "이 신호 왜 이렇게 나왔지"를 재현하려면 이게 있어야 한다.

## 8. 배포

개인용 도구라 크게 두 갈래다.

| 방식 | 장점 | 단점 |
|---|---|---|
| **A. 도쿄 리전 VPS** (권장) | 바이낸스 API 서버(AWS 도쿄)와 가까워 지연 최소. IP 고정 → 화이트리스트 가능. 24시간 가동 | 월 $10~20 |
| B. 로컬 PC | 무료, 데이터 완전 로컬 | PC 꺼지면 정지, IP 변동, 발열·소음 |

권장: **A**. 바이낸스 선물 API는 도쿄 리전에 있어 서울/도쿄 VPS면 왕복 수 ms다. 미국이나 유럽 리전에 두면 100ms 이상 손해를 본다.

**현재 결정: B(로컬 PC)로 시작 → Phase 2 진입 시 A로 이전.** 이유와 절차는 `07-확장성-설계-결정` 1절.

```
VPS (Tokyo)  : ingest + analysis + api + postgres + redis  (docker compose)
Vercel       : 웹 (WS는 VPS로 직접 연결)
Expo EAS     : 앱
```

접근 제어: 개인용이므로 단순 토큰 + HTTPS(Caddy 자동 인증서). 공개 인터넷에 열되 **경로 전체를 토큰으로 막는다.**

## 9. 확정할 결정 사항

- [x] VPS vs 로컬 PC → **로컬로 시작, Phase 2에 도쿄 VPS 이전**
- [ ] 앱 차트: WebView(`lightweight-charts` 재사용) vs 네이티브 라이브러리
- [x] TimescaleDB를 쓸지 → **씀** (연속 집계로 5m/15m 자동 생성이 실제로 편함)
- [ ] 심볼 몇 개까지 동시 처리할지 (WS 스트림 수 = 심볼 × 4)
