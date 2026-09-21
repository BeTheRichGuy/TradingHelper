# 여기서부터 시작 — Phase 0 첫 실행 가이드

> ## 📍 이미 설치를 마치셨다면 이 파일이 아닙니다
>
> **현재 상태: Phase 0~4 완료.** 다음에 볼 것은 아래 둘입니다.
>
> | 파일 | 언제 |
> |---|---|
> | [**PHASE5-준비.md**](PHASE5-준비.md) | **지금 여기부터.** 밤새 있었던 일과 다음 선택지 |
> | [docs/08-전략-검증-결과.md](docs/08-전략-검증-결과.md) | 왜 신호 카드가 빠졌는지 (판정 근거) |
>
> **신호 기능은 없습니다.** Phase 4 백테스트에서 셋업에 방향성 우위가 확인되지
> 않아 뺐습니다. 지금 이 앱이 하는 일은 **10배 레버리지에서 계좌가 죽는 경로를
> 막는 것** — 손절 누락 경고, 청산가 거리(SOXX 환산), 일일 −3R 한도, 세션 게이트입니다.
>
> 아래는 **처음부터 새로 설치할 때** 보는 문서입니다.

> VS Code로 이 폴더를 연 상태에서 시작합니다.
> **VS Code에서 이 파일을 예쁘게 보려면** `Ctrl` + `Shift` + `V` 를 누르세요.
>
> 각 단계마다 **무엇을 왜 하는지**, **어떤 화면이 나와야 정상인지**를 같이 적어뒀습니다.
> 화면이 다르면 그 단계에서 멈추고 맨 아래 "자주 나는 에러"를 보세요.

**총 소요 40~60분 · 비용 0원**

---

## 먼저 안심하고 시작하세요

- 이 단계에서는 **주문이 나가지 않습니다.** 주문 기능 자체가 코드에서 꺼져 있습니다.
- 바이낸스 **API 키가 필요 없습니다.** 공개된 시세만 읽습니다.
- 돈이 나가지 않습니다. 쓰는 것은 전부 무료입니다.
- 망가뜨릴 수 없습니다. 잘못되면 `docker compose down -v` 로 전부 지우고 다시 시작하면 됩니다.

---

## 0단계 — VS Code에서 터미널 열기 · 1분

터미널은 "명령어를 타이핑해서 컴퓨터에 시키는 창"입니다. 앞으로 모든 작업을 여기서 합니다.

상단 메뉴 **Terminal → New Terminal**.
단축키는 `Ctrl` + `` ` `` (백틱 — 숫자 1 왼쪽, Tab 위에 있는 키).

창 아래쪽에 이런 줄이 뜨면 성공입니다:

```
PS C:\Users\nemo0\source\trading-helper>
```

**경로가 다르다면** 터미널에 이걸 치고 Enter:

```powershell
cd C:\Users\nemo0\source\trading-helper
```

> 앞으로 나오는 `PS>` 는 **치지 마세요.** 그 뒤에 오는 명령만 입력합니다.

**✅ 확인** — 터미널이 열렸고 경로 끝이 `trading-helper` 다

---

## 1단계 — 준비물 두 개 확인 · 2분

파이썬(프로그램을 돌리는 것)과 도커(데이터베이스를 담는 상자)가 있는지 봅니다.

```powershell
python --version
docker --version
```

**이렇게 나오면 정상:**

```
Python 3.12.4
Docker version 27.3.1, build ce12230
```

**파이썬 버전이 3.11 미만이거나 안 나오면**
python.org 에서 최신 버전을 설치하고, 설치 첫 화면 맨 아래 **"Add python.exe to PATH"를 꼭 체크**하세요. 설치 후 VS Code를 껐다 켭니다.

**도커가 안 나오면**
Docker Desktop을 설치합니다(무료). 설치 후 **재부팅**하고, 시작 메뉴에서 Docker Desktop을 실행해 좌측 하단이 **초록색 "Engine running"** 이 될 때까지 기다립니다. 그다음 위 명령을 다시 칩니다.

> ⚠️ **앞으로 작업할 때 Docker Desktop은 항상 켜져 있어야 합니다.** 창은 닫아도 되지만 트레이(시계 옆)에 고래 아이콘이 살아 있어야 합니다.

**✅ 확인** — 두 명령 모두 버전 숫자를 출력한다

---

## 2단계 — 설정 파일 만들기 · 3분

비밀번호 같은 값을 코드가 아니라 `.env` 파일에 둡니다. 나중에 서버로 옮길 때 이 파일 하나만 챙기면 됩니다.

```powershell
copy .env.example .env
```

아무 메시지도 안 나오면 성공입니다. (터미널은 조용한 게 정상인 경우가 많습니다.)

이제 VS Code 왼쪽 파일 목록에서 **`.env`** 를 클릭해 엽니다. 위에서 다섯째 줄쯤에 있는 이 줄을 찾아

```
POSTGRES_PASSWORD=change_me_please
```

비밀번호를 아무 값으로나 바꿉니다. **영어와 숫자만** 쓰세요(특수문자는 문제를 일으킬 수 있습니다).

```
POSTGRES_PASSWORD=soxl2026helper
```

`Ctrl` + `S` 로 저장합니다. **나머지 줄은 건드리지 마세요.**

> **왜 굳이?** 이 파일은 `.gitignore` 에 등록돼 있어 깃에 절대 올라가지 않습니다. 비밀번호를 코드에 직접 쓰면 나중에 코드를 공유하는 순간 같이 새어 나갑니다.

**✅ 확인** — `.env` 파일이 생겼고 비밀번호를 바꿔 저장했다

---

## 3단계 — 데이터베이스 띄우기 · 5~10분

시세를 저장할 데이터베이스와 캐시를 "상자(컨테이너)" 형태로 내 PC에 올립니다. 직접 설치하는 게 아니라서, 나중에 통째로 서버로 옮길 수 있습니다.

```powershell
docker compose up -d
```

처음 한 번은 프로그램을 내려받느라 **2~10분** 걸립니다. 진행 막대가 잔뜩 지나가는데 정상입니다. 마지막에 이렇게 끝나야 합니다:

```
 ✔ Network trading-helper_default  Created
 ✔ Container th-db                 Healthy
 ✔ Container th-redis              Healthy
```

30초쯤 기다렸다가 상태를 확인합니다:

```powershell
docker compose ps
```

```
NAME       IMAGE                              STATUS
th-db      timescale/timescaledb:latest-pg16  Up 45 seconds (healthy)
th-redis   redis:7-alpine                     Up 45 seconds (healthy)
```

> **핵심은 `(healthy)` 두 글자입니다.**
> `(starting)` 이면 30초 더 기다렸다 다시 확인하세요.
> `(unhealthy)` 나 `Exited` 면 맨 아래 에러표를 봅니다.

**✅ 확인** — `th-db`, `th-redis` 둘 다 **(healthy)**

---

## 4단계 — 파이썬 환경 만들고 프로그램 설치 · 5분

이 프로젝트 전용 서랍(가상환경)을 만들고 거기에만 필요한 부품을 넣습니다. PC 전체를 어지럽히지 않기 위해서입니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

성공하면 프롬프트 **맨 앞에 `(.venv)`** 가 붙습니다. 이게 "서랍이 열렸다"는 표시입니다:

```
(.venv) PS C:\Users\nemo0\source\trading-helper>
```

> **"이 시스템에서 스크립트를 실행할 수 없으므로" 라는 빨간 에러가 나면**
> 윈도우 보안 설정 때문입니다. 아래를 한 번 치고 다시 시도하세요.
> 이 터미널 창에만 적용되는 임시 허용이라 안전합니다.
>
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> ```

`(.venv)` 가 보이는 상태에서 프로그램을 설치합니다. 2~4분 걸립니다.

```powershell
pip install -e ".[dev]"
```

마지막 줄이 이렇게 나오면 정상:

```
Successfully installed asyncpg-0.30.0 httpx-0.28.1 pandas-2.2.3
  pydantic-2.10.4 rich-13.9.4 trading-helper-0.1.0 typer-0.15.1 ...
```

> 노란색 **WARNING** 은 무시해도 됩니다. 빨간 **ERROR** 만 문제입니다.

**✅ 확인** — 프롬프트에 `(.venv)` 가 있고 **Successfully installed** 가 떴다

---

## 5단계 — 전체 점검 `th doctor` · 1분

**여기가 가장 중요한 단계입니다.** 지금까지 한 게 전부 맞물렸는지 한 번에 확인합니다.

```powershell
th doctor
```

**전부 ✓ 여야 다음으로 갑니다:**

```
                        환경 점검
┏━━━━━━━━━━━━━━━━┳━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 항목           ┃상태┃ 비고                            ┃
┡━━━━━━━━━━━━━━━━╇━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 실행 환경      │ ✓  │ local                           │
│ 수집 심볼      │ ✓  │ SOXLUSDT                        │
│ 주문 모드      │ ✓  │ none  (안전)                    │
│ PostgreSQL     │ ✓  │ PostgreSQL 16.4                 │
│ TimescaleDB    │ ✓  │ 2.17.2                          │
│ 테이블         │ ✓  │ 11개                            │
│ Redis          │ ✓  │ redis://localhost:6379/0        │
│ 바이낸스 API   │ ✓  │ 서버시각 09:41:22 UTC           │
│ 시각 오차      │ ✓  │ 0.4초                           │
│ 심볼 SOXLUSDT  │ ✓  │ TRADING tick=0.001              │
└────────────────┴────┴─────────────────────────────────┘
```

### ✗ 나 ⚠ 가 나왔을 때

| 항목 | 해결 |
|---|---|
| **주문 모드** | `none` 이 아니면 즉시 `.env` 를 `TH_EXECUTION_MODE=none` 으로 되돌리세요 |
| **PostgreSQL** | 3단계로 돌아가 컨테이너가 healthy 인지, `.env` 비밀번호가 맞는지 확인 |
| **TimescaleDB / 테이블** | DB는 떴는데 초기화가 안 된 것. `docker compose down -v` 후 `docker compose up -d` 재실행 |
| **바이낸스 API** | 인터넷·방화벽·VPN 문제. 브라우저에서 binance.com 이 열리는지 먼저 확인 |
| **시각 오차** | 5초 넘으면 봉 시각이 어긋납니다. 윈도우 설정 → 시간 및 언어 → **지금 동기화** |
| **심볼** | 바이낸스에 없는 이름입니다. 바이낸스 선물 화면에서 정확한 심볼명을 확인해 `.env` 의 `TH_SYMBOLS` 수정 |

**✅ 확인** — 모든 줄이 ✓ 다. **✗ 가 하나라도 있으면 다음으로 넘어가지 마세요.**

---

## 6단계 — 코드 자체 테스트 · 1분

인터넷이나 DB와 무관하게, 코드 로직이 멀쩡한지 자동으로 검사합니다.
(제가 작업 환경에서 실행해보지 못한 부분이라 여기서 처음 확인됩니다.)

```powershell
pytest -q
```

```
............                                        [100%]
12 passed in 0.38s
```

점 하나가 테스트 하나입니다.

**`F` 나 `E` 가 섞여 나오면** 그 아래 빨간 글씨가 무엇이 왜 틀렸는지 알려줍니다.
**그 부분을 통째로 복사해서 저에게 보내주세요.** 제 코드 문제일 가능성이 높습니다.

**✅ 확인** — **12 passed** (숫자는 달라도 됩니다. `failed` 만 없으면 통과)

---

## 7단계 — 과거 데이터 받기 · 10~30분

드디어 실제 데이터입니다. 네 개를 **순서대로** 실행합니다. 각각 끝날 때까지 기다렸다가 다음 것을 치세요.

### ① 심볼 정보 저장 — 몇 초

```powershell
th init
```

```
SOXLUSDT 메타 저장 (tick=0.001 step=0.1 status=TRADING)
심볼 메타 저장 완료
```

### ② 1분봉 전부 — 여기가 오래 걸립니다

```powershell
th backfill --full
```

```
SOXLUSDT 상장일 2026-03-14 부터 백필합니다
SOXLUSDT 2026-03-14  누적 1440봉
SOXLUSDT 2026-03-15  누적 2880봉
SOXLUSDT 2026-03-16  누적 4320봉
                  ⋮  (하루씩 올라갑니다)
SOXLUSDT  241920봉 저장
```

> **날짜가 하루씩 올라가고 있으면 정상입니다.**
> 중간에 끊겨도 괜찮습니다 — 다시 실행하면 저장된 지점부터 이어서 받습니다.

### ③ 5분·15분봉 집계 — 1~3분

```powershell
th refresh-aggregates
```

### ④ 본주·지수 — 2~3분

```powershell
th underlying --days 7
```

```
SOXL  1분봉 2730행
SOXX  1분봉 2730행
^VIX  1분봉 2730행
SPY   1분봉 2730행
```

**✅ 확인** — 네 명령 모두 에러 없이 끝났다

---

## 8단계 — 실시간 수집 켜두기 · 24시간

먼저 결과를 확인합니다:

```powershell
th status
```

```
                          적재 현황
┏━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━┓
┃ 심볼      ┃ 봉 수   ┃ 첫 봉            ┃ 마지막 봉  ┃ 지연 ┃ 24h누락 ┃
┡━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━┩
│ SOXLUSDT  │ 241,920 │ 2026-03-14 00:00 │ 08-29 09:52│ 1분  │ 0       │
└───────────┴─────────┴──────────────────┴────────────┴──────┴─────────┘
```

**읽는 법** — `지연`이 1~2분이면 최신입니다. `24h누락`이 0이면 구멍이 없다는 뜻입니다.

이제 실시간 수집을 켭니다:

```powershell
th stream
```

```
09:53:10 INFO    수집 시작: SOXLUSDT
09:53:11 INFO    WebSocket 연결됨 (스트림 4개)
09:53:12 INFO    [연결 직후] SOXLUSDT 백필 2봉
09:53:14 INFO    OI/펀딩 폴러 시작: SOXLUSDT
                  ⋮  (조용해지면 정상입니다)
```

> **로그가 멈춘 것처럼 보여도 정상입니다.** 1분마다 조용히 저장 중이고, 문제가 있을 때만 새 줄이 뜹니다. 노란색 `WARNING` 은 재연결 중이라는 뜻이고 이것도 자동 복구됩니다.

**이 창은 닫지 마세요.** 확인용 명령을 치려면 **새 터미널**을 엽니다 — 터미널 패널 오른쪽 위 **+** 버튼. 새 창에서는 `.\.venv\Scripts\Activate.ps1` 을 다시 한 번 쳐야 `(.venv)` 가 붙습니다.

- **중단하려면** `Ctrl` + `C`
- **PC를 껐다 켠 뒤에는** Docker Desktop이 켜졌는지 확인하고 `th stream` 을 다시 실행 → 끊긴 구간을 자동으로 메웁니다

**✅ 확인** — **WebSocket 연결됨** 이 떴고 창을 켜둔 상태다

---

## 9단계 — 24시간 뒤, Phase 0 합격 판정 · 1분

데이터에 구멍이 없어야 그 위에 쌓는 지표·백테스트가 의미를 가집니다. **이게 유일한 합격 기준입니다.**

```powershell
th gaps --hours 24
```

**합격:**

```
SOXLUSDT  최근 24시간 누락 없음 ✓
```

**불합격:**

```
SOXLUSDT  구간 2개 / 봉 37개 누락
    08-29 03:12 → 08-29 03:31  (18봉)
    08-29 07:40 → 08-29 07:59  (18봉)
```

메우는 명령:

```powershell
th gaps --hours 24 --repair
```

왜 생겼는지는 `logs/th.log` 파일에서 그 시각을 찾아보면 나옵니다. 대개 PC 절전 모드나 인터넷 끊김입니다.
**반복해서 생긴다면 서버(도쿄 VPS)로 옮길 때가 된 것**입니다.

**✅ 확인** — **누락 없음 ✓** → Phase 0 완료, Phase 1로 넘어갑니다

---

## 쌓인 데이터를 눈으로 보고 싶을 때

`th status` 로도 충분하지만, 실제 숫자를 보면 감이 훨씬 빨리 옵니다.

```powershell
docker exec -it th-db psql -U th -d trading_helper
```

프롬프트가 `trading_helper=#` 로 바뀌면 접속된 것입니다.
이제 아래를 쳐보세요. **끝에 세미콜론(`;`)이 꼭 필요합니다.**

### 가장 최근 1분봉 5개

```sql
SELECT ts, open, high, low, close, volume
FROM klines ORDER BY ts DESC LIMIT 5;
```

```
          ts           |  open  |  high  |  low   | close  | volume
-----------------------+--------+--------+--------+--------+---------
 2026-08-29 09:52:00+00| 231.41 | 231.68 | 231.30 | 231.55 |  8412.3
 2026-08-29 09:51:00+00| 231.22 | 231.49 | 231.18 | 231.41 |  6109.7
(5 rows)
```

### 호가 스프레드 — 지금 진입 비용이 얼마나 되는지

```sql
SELECT ts, bid, ask, round(spread_bps::numeric,2) AS spread_bps
FROM book_ticker ORDER BY ts DESC LIMIT 5;
```

`spread_bps` 는 만분율입니다. **1.30 이면 0.013%** — 설계 문서의 차단 기준 0.08%(=8 bps)보다 훨씬 좁으니 정상입니다.

### 펀딩률과 다음 정산 시각

```sql
SELECT ts, rate, mark_price, next_funding_ts
FROM funding ORDER BY ts DESC LIMIT 3;
```

### 테이블별로 얼마나 쌓였나

```sql
SELECT 'klines' t, count(*) FROM klines
UNION ALL SELECT 'open_interest', count(*) FROM open_interest
UNION ALL SELECT 'funding',       count(*) FROM funding
UNION ALL SELECT 'book_ticker',   count(*) FROM book_ticker
UNION ALL SELECT 'liquidations',  count(*) FROM liquidations
UNION ALL SELECT 'underlying',    count(*) FROM underlying_prices;
```

**빠져나올 때는** `\q` 를 치고 Enter. (역슬래시 + q)

---

## 자주 나는 에러

빨간 글씨가 나와도 당황하지 마세요. 대부분 아래 여섯 가지 중 하나입니다.

| 화면에 나오는 말 | 원인 | 해결 |
|---|---|---|
| `'th' 용어가 cmdlet … 인식되지 않습니다` | 가상환경이 안 켜졌거나 설치가 안 됨 | `.\.venv\Scripts\Activate.ps1` 로 `(.venv)` 를 띄우고, 그래도 안 되면 4단계 `pip install` 을 다시 |
| `이 시스템에서 스크립트를 실행할 수 없으므로` | 윈도우 스크립트 실행 차단 | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| `error during connect … docker_engine` | Docker Desktop이 안 켜짐 | 시작 메뉴에서 실행 → "Engine running" 확인 후 재시도 |
| `password authentication failed for user "th"` | DB를 만든 뒤에 `.env` 비밀번호를 바꿈 | `docker compose down -v` 후 `docker compose up -d` (데이터도 지워지니 다시 백필) |
| `port is already allocated` | 5432 포트를 다른 프로그램이 씀 | `.env` 에서 `POSTGRES_PORT=5433`, `TH_DB_PORT=5433` 으로 바꾸고 재실행 |
| `relation "klines" does not exist` | DB 초기화 스크립트가 안 돌았음 | `docker compose down -v` → `docker compose up -d` |

---

## 막혔을 때 저에게 보내는 방법

정확히 이 세 가지를 주시면 대부분 바로 원인을 찾을 수 있습니다.

1. **몇 단계에서 멈췄는지** (예: "5단계 th doctor")
2. **터미널에 나온 글자 전부** — 마우스로 긁어서 `Ctrl`+`C`.
   요약하지 말고 통째로 붙여넣어 주세요. 에러의 진짜 원인은 대개 마지막 줄이 아니라 중간에 있습니다.
3. **`th doctor` 결과표** — 지금 상태가 한눈에 보입니다.

화면을 직접 보여주고 싶으시면 대화에서 **"내 화면 보면서 같이 해줘"** 라고 말씀해 주세요.

---

## 다음은 Phase 1

프리장·본장·애프터·휴장 판정과, 기술적 분석이 통하지 않는 시간대를 차단하는 규칙입니다.
`src/th/sessions/` 폴더가 그 자리입니다.
