# Phase 5′ 실행 엔진 — 테스트 가이드

> 완전 처음이라고 생각하고 씁니다. 한 줄씩 그대로 따라 하면 됩니다.
> **API 키는 저에게 보내지 마세요.** 오류 메시지만 복사해서 주시면 됩니다.

## 이번에 만든 것

전략(어디서 사고 팔지)이 아직 없어도 **자동매매 뼈대는 만들 수 있습니다.**
나중에 규칙을 찾으면 함수 하나만 꽂으면 바로 돌아가는 구조로 만들었습니다.

| 파일 | 하는 일 |
|---|---|
| `execution/models.py` | 주문·체결·브래킷 자료형. **손절이 필수 필드** |
| `execution/guard.py` | 주문 나가기 **전에** 막는 안전장치 11가지 |
| `execution/paper.py` | 페이퍼 체결 엔진 (돈 안 나감) |
| `execution/manager.py` | 진입 체결되면 **즉시** 손절을 건다 |
| `execution/store.py` | DB 저장 + 재시작 복구 |
| `execution/live.py` | 실거래 — **잠겨 있음** |

### 설계에서 제일 중요한 한 가지

`Bracket` 이라는 자료형이 **진입과 손절을 한 덩어리로 묶습니다.** 손절은 기본값이
없는 필수 필드라, "손절 거는 걸 깜빡했다"가 **문법적으로 불가능**합니다.

Phase 2 에서 만든 `NO_STOP_ORDER` 경고는 이미 벌어진 일을 알려주는 것이었고,
이건 아예 벌어지지 않게 하는 쪽입니다.

---

## 0단계 — 준비

PowerShell 을 열고:

```powershell
cd C:\Users\nemo0\source\trading-helper
.\.venv\Scripts\Activate.ps1
```

프롬프트 앞에 `(.venv)` 가 붙으면 켜진 겁니다.

Docker 가 떠 있어야 합니다:

```powershell
docker compose ps
```

**예상 출력** — `db` 와 `redis` 가 `running` 이어야 합니다.

```
NAME              IMAGE                    STATUS
th-db-1           timescale/timescaledb...  Up 2 hours
th-redis-1        redis:7-alpine            Up 2 hours
```

안 떠 있으면: `docker compose up -d`

---

## 1단계 — 실행 모드 켜기

`.env` 파일을 메모장으로 열고 아래 줄을 찾으세요.

```
TH_EXECUTION_MODE=none
```

없으면 맨 아래에 추가하고, 있으면 `paper` 로 바꿉니다.

```
TH_EXECUTION_MODE=paper
```

> ⚠️ `live` 로 바꾸지 마세요. 바꿔도 프로그램이 거부하지만, 습관을 들이지 않는 게 좋습니다.

---

## 2단계 — DB 표 만들기

```powershell
python -m th.cli migrate
```

**예상 출력:**

```
적용: 003_execution.sql
마이그레이션 완료 — 적용 1건 / 전체 3건
```

이미 되어 있으면 `적용 0건` 이라고 나옵니다. 그것도 정상입니다.

---

## 3단계 — 안전장치가 실제로 막는지 확인 ⭐

**여기가 이번 단계의 핵심입니다.** 잘 되는 걸 보는 게 아니라
**막아야 할 걸 막는지** 보는 겁니다.

### 3-1. 정상적인 주문 — 통과해야 합니다

```powershell
python -m th.cli paper check --entry 126.00 --stop 124.80 --tp1 128.40 --dir LONG
```

**예상 출력:**

```
        SOXLUSDT LONG — 사전 점검
 진입        126.0000
 손절        124.8000  (0.95%)
 TP1        128.4000  (2.00R)
 수량        41.6667  (1R = 50.00 USDT)
 명목        5,250.00 USDT
 실효 배율    0.53배

통과 — 안전장치에 걸리는 것 없음
```

> 💡 **수량이 어떻게 나왔는지**: 자본 10,000 의 0.5% = 50 USDT 가 1R 입니다.
> 손절폭이 1.20 달러니까 50 ÷ 1.20 = 41.67개. **레버리지로 정한 게 아니라
> 손절폭에서 역산한 겁니다.** 이게 뒤집히면 손절이 좁을 때 위험이 몇 배로 뜁니다.

### 3-2. 손절을 잘못된 쪽에 둬보기 — 막혀야 합니다

```powershell
python -m th.cli paper check --entry 126.00 --stop 127.50 --dir LONG
```

**예상 출력:**

```
막힘
  · 롱인데 손절(127.5)이 현재가(126) 위에 있다
```

### 3-3. 손절을 너무 좁게 — 막혀야 합니다

```powershell
python -m th.cli paper check --entry 126.00 --stop 125.90 --dir LONG
```

**예상 출력:**

```
막힘
  · 손절폭 0.08% — 너무 좁다 (최소 0.2%)
```

### 3-4. 여러 개가 동시에 틀렸을 때 — 전부 보여줘야 합니다

```powershell
python -m th.cli paper check --entry 126.00 --stop 130.00 --tp1 120.00 --dir LONG
```

**예상 출력** (이유가 3개 이상):

```
막힘
  · 롱인데 손절(130)이 현재가(126) 위에 있다
  · 손절폭 3.17% — 너무 넓다 (최대 3.0%)
  · 롱인데 TP1(120)이 현재가 아래다
```

> 하나씩 고치게 하면 세 번 시도하게 되니까, 한 번에 다 보여주게 만들었습니다.

### 3-5. 실행 모드가 꺼져 있으면

`.env` 에서 `TH_EXECUTION_MODE=none` 으로 잠깐 되돌리고:

```powershell
python -m th.cli paper check --entry 126.00 --stop 124.80
```

`paper check` 는 주문을 만들지 않는 사전 점검이라 `none` 에서도 **돌아가긴 합니다.**
대신 안전장치가 이유 목록에 넣어 알려줍니다.

**예상 출력** (표는 그대로 나오고 맨 아래가 이렇게):

```
막힘
  · 실행 모드가 none 이다 (TH_EXECUTION_MODE)
```

확인했으면 다시 `paper` 로 돌려놓으세요.

---

## 4단계 — 실거래가 잠겨 있는지 확인

`.env` 에서 `TH_EXECUTION_MODE=live` 로 바꾸고:

```powershell
python -m th.cli paper status
```

> 🐛 **첫 배포에서 여기가 안 막혔습니다.** 확인 함수를 만들어놓고 호출을 빠뜨렸는데,
> 이름이 없는 게 아니라 있는데 안 쓴 거라 정적 검사기가 못 잡았습니다.
> 고쳤고, **모든 `paper` 명령이 실행 모드를 확인하는지 검사하는 테스트**를 추가했습니다
> (`test_모든_paper_명령이_실행모드를_확인한다`). 호출을 다시 빼면 그 테스트가 실패합니다.

**예상 출력:**

```
실거래는 아직 잠겨 있습니다.
  · Phase 4 백테스트 통과
  · 페이퍼 트레이딩 4주 + 괴리 10%p 이내
  · 재시작 복구 실증
  · 주문 권한 API 키 분리 발급

.env 에서 TH_EXECUTION_MODE=paper 로 바꾸세요.
```

**반드시 다시 `paper` 로 되돌려 놓으세요.**

---

## 5단계 — 자동 테스트

```powershell
pytest tests/test_execution.py -v
```

**예상 출력** (26개 전부 `PASSED`):

```
tests/test_execution.py::test_실행모드가_none이면_주문이_막힌다 PASSED
tests/test_execution.py::test_롱인데_손절이_현재가_위면_막힌다 PASSED
...
tests/test_execution.py::test_같은_봉에서_손절과_익절이_둘_다_닿으면_손절이_이긴다 PASSED
tests/test_execution.py::test_진입이_체결되면_손절이_자동으로_걸린다 PASSED
tests/test_execution.py::test_account_패키지에는_주문_코드가_없다 PASSED

tests/test_execution.py::test_모든_paper_명령이_실행모드를_확인한다 PASSED
tests/test_execution.py::test_안_쓰이는_안전장치_함수가_없다 PASSED
tests/test_execution.py::test_테스트_코드가_get_event_loop_를_쓰지_않는다 PASSED

======================== 26 passed in 0.4s ========================
```

전체도 한번 돌려주세요 (기존 게 안 깨졌는지):

```powershell
pytest -q
```

**예상: 385개 passed, 0 failed.**

> 🐛 **첫 배포에서 여기가 8개 실패했습니다.** 원인은 제품 코드가 아니라 제 테스트
> 헬퍼였습니다 — `asyncio.get_event_loop()` 를 썼는데, 그건 앞선 테스트가
> `asyncio.run` 을 쓴 적 있으면 오류를 냅니다. 그래서 **`test_execution.py` 만
> 돌리면 통과하고 전체를 돌리면 깨지는** 상태였습니다.
> `asyncio.run` 으로 바꿨고, 테스트 파일에서 `get_event_loop` 호출을 AST 로 찾아
> 금지하는 규칙을 추가했습니다.

---

## 6단계 — 매매 기록 시작 ⭐ 이게 지금 제일 값어치 있습니다

유효한 자동매매 규칙을 아직 못 찾았습니다. 그래서 **지금 가장 값어치 있는 데이터는
형이 실제로 어디서 잡았는가** 입니다. 그게 쌓여야 그 판단을 검정할 수 있습니다.

매매를 잡으면:

```powershell
python -m th.cli journal add --entry 126.30 --stop 125.10 --dir LONG --reason "5분봉 EMA99 지지 확인" --tag ema99_support
```

**예상 출력:**

```
기록됨 #1 — SOXLUSDT LONG @ 126.3000
청산하면: th journal close 1 --exit <가격>
```

청산하면:

```powershell
python -m th.cli journal close 1 --exit 127.80
```

목록:

```powershell
python -m th.cli journal list
```

**예상 출력:**

```
                      매매 기록 1건
 #   진입시각      심볼        방향    진입       청산       R      꼬리표          이유
 1   09-08 21:15  SOXLUSDT   LONG   126.3000  127.8000  +1.25  ema99_support  5분봉 EMA99 지지 확인
```

> **`--reason` 을 꼭 적으세요.** 나중에 "이 이유로 잡은 것들만 모아서 성적을 보자"가
> 가능해집니다. 30건쯤 쌓이면 형의 진입이 무작위보다 나은지 처음으로 측정할 수 있습니다.
> 지금까지 우리가 못 했던 게 정확히 그거였습니다 — 형 머릿속 규칙을 몰라서 테스트를 못 했죠.

---

## 오류가 나면

| 증상 | 원인 | 해결 |
|---|---|---|
| `No module named 'th'` | 가상환경 / 설치 | `pip install -e .` |
| `relation "brackets" does not exist` | 마이그레이션 안 함 | 2단계 |
| `Connection refused` | Docker 안 떠 있음 | `docker compose up -d` |
| `실행 모드가 'none'` | `.env` 미설정 | 1단계 |

에러는 **전체를 그대로** 복사해서 보내주세요. 🔑 **API 키는 절대 보내지 마세요.**
이번 단계는 키가 아예 필요 없습니다 — 주문을 실제로 내지 않으니까요.

---

## 다음

이게 통과하면 **웹 대시보드(Phase 5)** 로 갑니다. 지금 터미널로 보는 것을
브라우저와 폰에서 보게 만드는 일이고, 여기서 만든 페이퍼 트레이딩 상태와
매매 기록도 같이 보여주게 됩니다.
