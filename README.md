# Trading Helper — Phase 0 (데이터 수집)

바이낸스 주식 perp(SOXLUSDT 등) 데이 트레이딩 보조 도구.
이 단계에서는 **화면도 신호도 없다.** 1분봉·미결제약정·펀딩·호가·청산 데이터를
빠짐없이 모으는 것만 한다. 그 위에 얹는 것들은 전부 이 데이터가 정확하다는
전제 위에 서 있기 때문이다.

---

## 1. 준비물

| 항목 | 비고 |
|---|---|
| Docker Desktop | DB(TimescaleDB)와 Redis를 띄운다. [설치](https://www.docker.com/products/docker-desktop/) |
| Python 3.11 이상 | 이미 Anaconda가 있으면 그걸 써도 된다 |

## 2. 설치

PowerShell 또는 CMD에서:

```powershell
cd %USERPROFILE%\source\trading-helper

# 1) 설정 파일 만들기
copy .env.example .env
#    → .env 를 열어 POSTGRES_PASSWORD 를 아무 값으로나 바꾼다

# 2) DB · Redis 띄우기
docker compose up -d
docker compose ps          # db, redis 둘 다 healthy 여야 한다

# 3) 파이썬 환경
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

# 4) 점검  ← 여기서 전부 ✓ 가 떠야 다음으로 간다
th doctor
```

`th doctor` 가 확인하는 것: DB 연결, TimescaleDB 확장, 테이블 생성, Redis,
바이낸스 API 응답, **PC 시각 오차**(5초 이상 벌어지면 봉 정렬이 틀어진다),
그리고 `.env` 에 적은 심볼이 바이낸스에 실제로 존재하는지.

## 3. 데이터 채우기

```powershell
th init                    # 심볼 메타(틱사이즈·상장일) 저장
th backfill --full         # 상장일부터 1분봉 전부  (수 분~수십 분 소요)
th refresh-aggregates      # 5분·15분봉 집계 재계산 (--full 직후 1회)
th underlying --days 7     # 본주 SOXL·지수 SOXX·VIX
th status                  # 얼마나 쌓였는지 확인
```

5분·15분봉은 따로 수집하지 않는다. 1분봉에서 TimescaleDB가 자동 집계한다
(`klines_5m`, `klines_15m` 뷰). 실시간 구간은 1분마다 자동 갱신되지만,
과거를 한꺼번에 백필한 직후에는 `th refresh-aggregates` 를 한 번 돌려야 한다.

## 4. 실시간 수집

```powershell
th stream                  # Ctrl+C 로 종료
```

이 창을 켜둔 채로 두면 계속 쌓인다. 하는 일:

- 1분봉을 **마감된 것만** 저장 (진행 중인 봉은 버린다)
- 마크가격·펀딩률을 1분마다, 호가 스프레드를 1초마다 샘플링
- 강제청산 이벤트 기록 (청산 캐스케이드 감지용)
- 미결제약정을 5분마다 REST로 폴링
- **연결이 끊기면 자동 재연결하고, 끊긴 구간을 REST로 백필**한다

## 5. Phase 0 완료 기준

`th stream` 을 **24시간 켜둔 뒤** 이걸 실행한다:

```powershell
th gaps --hours 24
```

`누락 없음 ✓` 이 나와야 Phase 1로 넘어간다. 구멍이 있으면:

```powershell
th gaps --hours 24 --repair
```

로 메우고, 왜 생겼는지 `logs/th.log` 를 확인한다. 데이터에 구멍이 있으면
지표가 조용히 틀린 값을 내고, 그 위에 쌓은 백테스트는 전부 무의미해진다.

---

## 6. 나중에 도쿄 VPS로 옮기기 — 어렵지 않다

**결론: 30분~1시간.** 지금 구조가 이미 그렇게 만들어져 있다.

옮길 때 하는 일 전부:

```bash
# VPS에서
git clone <저장소>
cd trading-helper
scp 로 .env 복사 (또는 새로 작성)
# .env 에서 딱 두 줄만 바꾼다
#   TH_ENV=vps
#   TH_DB_HOST=db        ← compose 가 자동으로 덮어쓰므로 실은 안 바꿔도 됨
docker compose --profile server up -d --build
```

**코드는 한 줄도 고치지 않는다.** 그렇게 만든 장치:

| 장치 | 효과 |
|---|---|
| 설정을 전부 `.env` 환경변수로 | 호스트명·비밀번호·경로가 코드에 없다 |
| DB를 처음부터 Postgres(컨테이너) | SQLite→Postgres 같은 마이그레이션이 없다 |
| `docker-compose.yml` 하나를 로컬/VPS 공용 | VPS는 `--profile server` 만 추가 |
| 모든 시각을 UTC로 저장 | 서버 타임존이 바뀌어도 데이터가 안 틀어진다 |
| 데이터는 DB 안에만 | `pg_dump` 한 번으로 통째로 옮긴다 |

데이터까지 옮기려면:

```powershell
docker exec th-db pg_dump -U th trading_helper | gzip > th.sql.gz
# VPS에서
gunzip -c th.sql.gz | docker exec -i th-db psql -U th trading_helper
```

옮기지 않고 VPS에서 `th backfill --full` 로 다시 받아도 된다.

### 로컬로 시작해도 괜찮은 이유 / 언제 옮겨야 하는지

지금 단계(전략이 되는지 확인)에는 로컬로 충분하다. 옮겨야 하는 시점은:

- **PC를 끄면 데이터에 구멍이 생기기 시작할 때** — 백테스트 표본이 그만큼 줄어든다
- **읽기 전용 API 키를 붙일 때(Phase 2)** — IP 화이트리스트를 걸려면 고정 IP가 필요하다
- 실시간 신호를 켜둔 채로 매매하고 싶을 때

즉 **Phase 2에 들어가는 시점이 자연스러운 이전 시기**다. 그전까지는 비용 0원.

---

## 7. 자동매매·다중 사용자 확장을 미리 깔아둔 곳

지금은 쓰지 않지만, 나중에 붙일 때 구조를 뜯어고치지 않아도 되도록 자리만 잡아뒀다.

| 확장 | 지금 상태 | 나중에 할 일 |
|---|---|---|
| 자동매매 | `exchange/base.py` 의 `ExecutionClient` 프로토콜 + `NoopExecutor`(항상 거부) | `BinanceExecutor` 를 추가하고 `TH_EXECUTION_MODE` 를 바꾼다. 전략 코드는 주문 API를 직접 부르지 않으므로 그대로 |
| 다중 사용자 | `users` 테이블과 `owner_id` 설정값이 이미 존재 (지금 1명) | 사용자별 테이블에 `owner_id` 컬럼을 쓰고 인증을 붙인다. 시세 데이터는 공유라 손댈 필요 없음 |
| 결제 | `users.plan` 컬럼 (`owner` / `free` / `pro`) | 플랜별 심볼 수·기능 제한을 여기서 읽는다 |
| 다른 거래소 | `MarketData` 프로토콜 | 같은 인터페이스로 어댑터 추가 |

**중요:** `TH_EXECUTION_MODE` 는 `none` 이외의 값을 넣으면 지금은 즉시
`NotImplementedError` 로 죽는다. 반쯤 만들어진 주문 코드가 조용히 도는 것보다
낫기 때문이다.

---

## 8. 알아둘 제약

- **본주 1분봉 장기 히스토리**: yfinance는 1분봉을 최근 30일치만 준다.
  설계 문서의 "SOXL 1분봉 10년치"는 yfinance로는 불가능하다. Phase 4~6에서
  긴 히스토리가 필요해지면 Alpaca(무료, 수년치 분봉·IEX 피드) 어댑터를
  `src/th/underlying/` 에 추가한다. 일봉은 제약이 없어 이미 10년치를 받는다.
- **주식 perp 히스토리**: 상장한 지 오래되지 않아 백테스트 표본이 짧을 수 있다.
  `th status` 의 "첫 봉" 날짜로 확인한다.
- `aggTrade` 스트림은 구독하지 않는다. 초 단위 델타가 필요해지면 켜되,
  디스크를 크게 먹는다는 걸 감안한다. 1분 단위 CVD는 봉의 `taker_buy_base`로 충분하다.

## 9. 명령어 요약

```
th doctor                  환경 점검
th init                    심볼 메타 동기화
th backfill [--full]       1분봉 채우기
th underlying [--days 7]   본주·지수 수집
th stream                  실시간 수집
th gaps [--repair]         누락 검사·복구
th status                  적재 현황
```

## 10. 테스트

```powershell
pytest -q
```

## 11. 다음 단계

Phase 1 — 세션·이벤트 게이트 (`05-세션-이벤트-타임맵` 문서).
`src/th/sessions/` 폴더가 그 자리다. 프리/본장/애프터/휴장 판정과
`rules.yaml` 기반 차단·감점 규칙이 들어간다.
