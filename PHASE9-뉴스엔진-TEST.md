# PHASE 9 — 뉴스·이벤트 매매 엔진 (v3.0) 확인 절차

텔레그램·RSS·SEC 공시를 모아 Claude 가 종목·방향·분할 계획을 판단하고, 페이퍼 브래킷을 자동으로 여닫는 엔진이 붙었다 (docs/17).
**돈은 나가지 않는다.** 실거래 경로는 여전히 없다. 단, **Claude API 호출은 실제 비용이 든다** — §5 를 먼저 읽고 시작하자.

## 0. 결과부터 알고 시작하자

- 이 엔진은 백테스트로 우위를 잴 수 없다 (뉴스는 과거 데이터가 없다). 그래서 **페이퍼로 60거래일·30건을 모아 매매기록 페이지에서 판정**한다 (docs/17 §10). 지금 확인하는 건 "돈이 되는가" 가 아니라 **"배선이 맞는가"** 다.
- 판단이 없으면 주문이 없다. Claude 가 거절하거나 파싱이 실패하거나 계획이 범위를 벗어나면 그 뉴스는 `error` 로 기록되고 넘어간다. 조용히 기본값으로 진입하는 경로는 없다.
- 지금 단계(M1·M2)에는 텔레그램 **알림·승인 봇이 아직 없다** (M3). `approve` 모드는 작전이 `pending_approval` 로 쌓이기만 한다.

## 1. 준비

### 1-1. 재설치 · 마이그레이션 (노트북에서는 필수)

```powershell
cd C:\Users\nemo0\source\TradingHelper
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,web,news]"     # 새 하위 패키지 th.news 는 편집 가능 설치를 다시 해야 잡힌다
th migrate                            # 008_news 적용
python -m pytest -q                   # 0 failed (뉴스 테스트 56개 포함)
```

`pip install` 이 `th.exe` 를 못 바꾼다고 하면 켜 둔 `th stream`/`th serve` 때문이다. 패키지 등록은 됐으니 무시해도 된다.

### 1-2. Claude API 키 — **Max 플랜과 별개다** (§5)

1. https://console.anthropic.com 에서 로그인 (claude.ai 계정과 같은 이메일이어도 **결제는 따로**다)
2. Billing → 크레딧 충전 (선불). **Spending limit 을 월 $30~50 로 걸어 둔다** — 코드의 일일 상한과 이중 안전장치
3. API Keys → 키 발급 → `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

### 1-3. `.env` 에 추가 (메모장으로 열어서 맨 아래에)

```
# ── v3.0 뉴스 엔진 ──
TH_NEWS_ENABLED=true
TH_NEWS_EXEC=paper
TH_NEWS_SOURCES=telegram,edgar,yahoo      # 처음엔 가볍게. gnews·finnhub 는 나중에 (§5)
TH_NEWS_MODEL=claude-opus-5
TH_NEWS_TRIAGE_MODEL=claude-haiku-4-5
TH_NEWS_EFFORT=medium                     # 사고 깊이. 비용의 절반이 여기서 갈린다
TH_NEWS_DAILY_USD_CAP=3                   # 하루 판단 비용 상한 (USD). 넘으면 판단만 멈춘다
TH_MAX_OPEN_BRACKETS=9                    # 작전 3개 × 다리 3개. 실제 상한은 노출 3.0배·종목 1.0배
TH_EDGAR_USER_AGENT=TradingHelper/0.1 (본인이메일@example.com)
```

| 항목 | 뜻 |
|---|---|
| `TH_NEWS_EXEC` | `observe` 판단만 · `paper` 바로 페이퍼 주문 · `approve` 승인된 것만 (봇은 M3) |
| `TH_NEWS_SOURCES` | 켤 소스. **판단 건수 = 비용** 이고 건수는 소스가 정한다 |
| `TH_NEWS_EFFORT` | `low`/`medium`/`high`. Opus 의 사고 토큰 양. 판단 품질을 보고 올린다 |
| `TH_NEWS_DAILY_USD_CAP` | 한국 자정 기준 하루 합계. 넘으면 그날은 판단을 건너뛰고(수집은 계속) 로그에 경고 |
| `TH_EDGAR_USER_AGENT` | SEC 가 요구한다. 이메일이 없으면 차단당할 수 있다 |

### 1-4. 텔레그램 채널 읽기 (형 계정 세션 · 1회)

1. https://my.telegram.org → API development tools → `api_id`, `api_hash` 발급
2. `.env` 에 `TH_TELEGRAM_API_ID=...`, `TH_TELEGRAM_API_HASH=...`
3. 터미널에서 **직접** (전화번호 → 문자 인증코드 → 2단계 비번이 있으면 그것도):

```powershell
th news telegram-login
```

끝나면 구독 중인 채널 목록이 뜬다:

```
로그인 완료. 구독 중인 채널 (TH_TELEGRAM_CHANNELS 에 쓸 이름):
  @some_alerts                 Some Alerts
  -1001234567890               이름만 있는 비공개 채널
```

4. 읽을 채널을 골라 `.env` 에 `TH_TELEGRAM_CHANNELS=@some_alerts,-1001234567890`
5. 세션 파일은 `data\telegram.session` — git 에 안 올라간다. **노트북에서도 쓰려면 이 파일을 복사**하거나 거기서 다시 로그인한다.

## 2. 네트워크·DB 없이 되는 것부터

```powershell
th news extract "BREAKING: $NVDA and Super Micro surge; onsemi falls; markets ON edge"
```

```
NVDA(Nvidia) ON(onsemi) SMCI(Super Micro)
```

`ON edge` 의 대문자 `ON` 은 안 붙고 `onsemi` 만 붙어야 맞다. `$MU` 는 붙고 `MU` 만 쓰면 안 붙는다 (두 글자). 이게 틀리면 판단 비용이 폭발한다.

## 3. 판단 1건 — 비용 몇 센트 (`ANTHROPIC_API_KEY` 필요)

```powershell
th news test "Nvidia raises full-year guidance after record data center revenue; shares up 6% after hours"
```

기대하는 모양 (숫자는 그때그때 다르다):

```
현재 시각(UTC): 2026-09-22T15:40
종목 상태:
  - NVDA · 현재가 182.4 · 24h +1.12% · 뉴스 이후 +0.05% · 세션 POST
트리아지 (claude-haiku-4-5, $0.0021): 관련=True 크기=high 종목=['NVDAUSDT'] — 가이던스 상향
본판단 (claude-opus-5, $0.0312): LONG 확신 0.78 크기 high 종목 ['NVDAUSDT'] 반영됨=False 지평 48h
  논지: …
  위험: …
  계획: 손절 3% · 목표 6% / 9% · 최대 48h
    1: 마진 4% market · 만료 0.1h
    2: 마진 3% limit -1.5% · 만료 24h
    3: 마진 3% limit -2.5% · 만료 24h
  무효화: …
  → 매매 가능
```

| 보이는 것 | 뜻 |
|---|---|
| `트리아지 … 크기=low` 뒤에 `본판단으로 가지 않음` | 정상. 싼 모델이 걸러서 Opus 를 안 불렀다 |
| `본판단 실패: ANTHROPIC_API_KEY 가 없거나 잘못됐습니다` | 1-2 |
| `본판단 실패: API 오류 400 …` | 모델 이름 오타 (`TH_NEWS_MODEL`) 또는 크레딧 없음 |
| `계획 범위 위반: …` 노란 줄 + `매매 안 함: 계획 없음` | 정상. 모델이 손절 1~5%·마진 10% 밖을 냈고 **버렸다**. 자주 나오면 알려줘 |
| 뉴스 이후 변동이 이미 크면 `반영됨=True` | 정상. 늦었으면 안 들어간다 |
| 두 줄의 `$` 합계 | 이 뉴스 한 건의 실제 비용. §5 표와 비교해 보자 |

문장을 서너 개 바꿔 가며 (숏 뉴스, 무관한 뉴스, 소문) 방향·확신도가 상식적인지 본다. **여기서 이상하면 엔진을 켜지 않는다.**

## 4. 수집 → 엔진 → 페이퍼 브래킷

### 4-1. 수집 (`th stream` 재시작)

`th stream` 창 Ctrl+C → 다시 `th stream`. 시작 로그에:

```
뉴스 수집 시작 — 폴링 yahoo,edgar · 푸시 telegram
텔레그램 채널 2개 수신 중: Some Alerts, …
EDGAR CIK 맵 57개 (유니버스 61)
뉴스 [yahoo] NVDA · Nvidia unveils …
```

| 증상 | 원인 · 해결 |
|---|---|
| `텔레그램 세션이 로그인돼 있지 않습니다` | 1-4 를 안 했거나 `data\telegram.session` 이 없다 |
| `텔레그램 채널 @xxx 를 못 찾았습니다` | 이름 오타이거나 그 계정이 구독하지 않은 채널 |
| `뉴스 소스 edgar 실패 — …403` | `TH_EDGAR_USER_AGENT` 에 이메일이 없다 |
| `뉴스 소스 yahoo 실패 — 전부 실패` | 인터넷·방화벽. 한 종목만 실패하면 `ok` 로 계속 돈다 |

2~3분 뒤 다른 터미널에서:

```powershell
th news status
th news recent -n 20
th news recent --symbol NVDA
```

`뉴스 적재 현황 · 오늘 판단 비용 $0.000` 표에 소스별 건수가 늘고 있으면 통과. `미판단` 열은 엔진(`th serve`)이 안 떠 있으면 계속 쌓인다 — 정상.

### 4-2. 엔진 (`th serve` 재시작)

`th serve` 창 Ctrl+C → 다시 `th serve`. 시작 로그에 세 줄이 순서대로:

```
페이퍼 러너 시작 (holder=serve, 3초, 브래킷 0개)
시그널 엔진 시작 — …
뉴스 엔진 시작 — 모드 paper · 확신도 ≥ 0.70 · 손절 ≤ 5% · 배율 10 · 일일 API 상한 $3
```

`뉴스 엔진 시작` 이 없으면: `TH_NEWS_ENABLED=true` 인지, `TH_EXECUTION_MODE=paper` 인지 (러너 없이는 뜨지 않는다).

20초마다 미판단 뉴스를 5건씩 처리한다. 로그에:

```
뉴스 작전 camp-1a2b3c4d NVDAUSDT LONG 다리 1 열림 → br-… (손절 176.9 · 위험 1.20%)
뉴스 작전 camp-1a2b3c4d 다리 2 건너뜀 — 가드: 세션 차단: …
```

### 4-3. 확인할 곳

| 어디 | 기대 |
|---|---|
| `th news campaigns` | 작전 목록. 상태 `open`/`rejected`/`closed`, 다리별 `1:open 2:skip 3:open`, 손절/목표, R |
| `th news campaigns --status rejected` | 가드가 막은 것. 사유가 `close_reason`. **`시세 없음`** 이면 그 종목 공개 호가 폴백이 실패한 것 (드묾) |
| 대시보드 `페이퍼 브래킷` | 엔진이 연 다리마다 한 줄, `자동` 칩. reason 에 `뉴스 1/3 · …` |
| `매매기록` 탭 `자동` | 청산되면 여기 쌓인다. setup_id 는 `news_event` |
| `th paper status` | 열린 브래킷 수가 다리 수와 맞는지 |
| `th news status` | 오늘 판단 비용. **하루 지나면 §5 예상치와 비교** |

### 4-4. 모드 바꿔 보기

- `TH_NEWS_EXEC=observe` + `th serve` 재시작 → 작전이 `proposed` 로만 쌓이고 브래킷은 안 생긴다
- `TH_NEWS_EXEC=approve` → `pending_approval` 로 쌓이고 30분 뒤 `expired`. 승인 버튼은 M3 봇이 붙어야 눌 수 있다
- 수동으로 미판단 건을 지금 판단만 해 보려면 (주문 없음): `th news judge -n 3`

### 4-5. 안전장치가 실제로 막는지

대시보드 주문 패널에서 같은 종목에 손주문을 크게 넣어 본다:
- 노셔널이 시드의 1.0배를 넘으면 `NVDAUSDT 노출 1.xx배 — 한 종목 한도 1배 초과` 로 거절돼야 한다
- 손절폭 4% 손주문은 `너무 넓다 (최대 3.0%)` — 5% 상한은 뉴스 엔진 요청에만 열린다

## 5. 비용 — Max 플랜은 여기 안 쓰인다

**Claude Max x5 는 claude.ai 와 Claude Code 용이고, 이 엔진이 쓰는 API 는 console.anthropic.com 에서 따로 선불 크레딧으로 낸다.** 구독에 포함되지 않는다.

### 단가 (2026-06 기준, 1M 토큰당 입력/출력)

| 모델 | 입력 | 출력 | 이 엔진의 판단 1건 (추정) |
|---|---|---|---|
| claude-opus-5 | $5 | $25 | effort high ≈ $0.05 · medium ≈ $0.035 · low ≈ $0.025 |
| claude-sonnet-5 | $2 | $10 | high ≈ $0.02 · low ≈ $0.01 |
| claude-haiku-4-5 | $1 | $5 | 트리아지 ≈ $0.002 · 본판단으로 써도 ≈ $0.004 |

Opus 비용의 3분의 2가 **사고 토큰**(출력 단가)이다. 그래서 `TH_NEWS_EFFORT` 가 모델 교체만큼 크다.

### 하루 판단 건수 (추정 — 소스가 정한다)

| 소스 구성 | 트리아지/일 | 본판단/일 (트리아지의 ~15%) |
|---|---|---|
| 전체 (`telegram,edgar,yahoo,gnews,finnhub`) | ~800 | ~120 |
| 가벼움 (`telegram,edgar,yahoo`) | ~350 | ~50 |
| 최소 (`telegram,edgar`) | ~100 | ~15 |

### 월 비용 (30일)

| 구성 | 일 | **월** |
|---|---|---|
| 전체 소스 · Opus high | $7.6 | **≈ $230** |
| 전체 소스 · Opus low | $4.6 | ≈ $140 |
| 전체 소스 · Sonnet low | $2.8 | ≈ $85 |
| **가벼움 · Opus medium** ← 권장 시작점 | $2.5 | **≈ $75** |
| 가벼움 · Sonnet low | $1.2 | ≈ $36 |
| 최소 · Opus high | $1.0 | ≈ $30 |

오차 ±2배. 텔레그램 채널이 하루 수백 건 쏟아지면 위로, 조용하면 아래로 간다. **이틀 돌리고 `th news status` 의 실측으로 갈아 끼우자.**

### 시드 대비로 보면

월 비용 ÷ 시드 = 매달 먼저 깎이고 시작하는 비율이다. 시드 $3,000 에 월 $75 면 **2.5%/월** — 작전 몇 개의 이익을 통째로 먹는다. docs/17 §10 의 조건 3 "판단 비용 < 기대 수익의 10%" 가 이래서 있다.

### 그래서 권장

1. `TH_NEWS_SOURCES=telegram,edgar,yahoo` · `TH_NEWS_EFFORT=medium` · `TH_NEWS_DAILY_USD_CAP=3` 으로 시작 (월 최대 $90 에서 하드캡)
2. 콘솔 Spending limit 도 $50 로 (코드 상한이 뚫려도 여기서 멈춘다)
3. 이틀 뒤 실측: 본판단이 하루 50건을 넘으면 `TH_NEWS_MODEL=claude-sonnet-5` 로. 판단이 이상하면 Opus 로 되돌리고 소스를 줄인다
4. 텔레그램 채널은 **신호가 진한 것만** 넣는다. 잡담 채널 하나가 트리아지 수백 건이다
5. `th news test` 로 같은 뉴스를 Opus / Sonnet 에 넣어 판단이 갈리는지 직접 비교해 본다 (`TH_NEWS_MODEL` 만 바꿔서)

## 6. 안 되면

| 증상 | 원인 · 해결 |
|---|---|
| `th news …` 가 `No such command` | 재설치 안 함 (1-1) |
| `relation "news_items" does not exist` | `th migrate` |
| 엔진 로그 `뉴스 판단 일일 비용 상한 도달` | 정상. 내일 자정(한국) 에 풀린다. 급하면 `TH_NEWS_DAILY_USD_CAP` 올리고 `th serve` 재시작 |
| 작전이 전부 `rejected` · `가드: 이미 열린 브래킷 N개 (최대 1)` | `TH_MAX_OPEN_BRACKETS` 를 안 올렸다 |
| 작전 `rejected` · `세션 차단` | 본주 정지 시간(20:00~04:00 ET·주말). 게이트가 막는 게 맞다 |
| 판단은 되는데 작전이 `skip · 확신도 0.6x < 0.70` 만 | 정상. 임계값을 내리기 전에 며칠 판단 분포를 본다 |
| `error · 계획 범위 위반` 이 자주 | 프롬프트 손질 필요 — 예시 몇 건 보내줘 |
| `th news test` 가 `연결 실패` | 인터넷·프록시. `ant auth status` 나 키 재확인 |

문제 나면 `th serve` 창 마지막 30줄 + `th news campaigns` 출력. 🔑 **API 키·텔레그램 해시는 절대 보내지 마.**

## 7. git

```powershell
git add -A
git commit -m "v3.0 M1+M2: news engine (collect, Claude judge, campaigns, paper exec)"
```
