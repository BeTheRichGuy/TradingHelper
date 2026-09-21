# PHASE 6 — 전수 코드 리뷰 수정 적용 후 확인 절차

2026-09-09 리뷰에서 44건을 고쳤다 (`docs/14` §5). 데스크탑에서 아래 순서로 확인한다.

## 1. 테스트 전체 실행

```powershell
cd C:\Users\nemo0\source\trading-helper
.\.venv\Scripts\Activate.ps1
python -m pytest -q
```

**기대:** `... passed` 만 나오고 `failed` 가 0 이어야 한다. (컨테이너에서 446개 통과 확인.
데스크탑에서만 도는 fixture 기반 테스트 6개 + 패키지 순수성 테스트 4개가 추가로 돈다.)

실패가 있으면 출력 전체를 복사해서 보내줘. 🔑 API 키는 절대 보내지 마.

## 2. DB 마이그레이션 (새 컬럼 3개)

```powershell
th migrate
```

`004_bracket_meta.sql` 이 적용됐다고 나와야 한다. 이걸 안 하면 `th paper resume` 가
`expire_after_s` 컬럼을 못 찾는다.

## 3. 페이퍼 실행 재점검

```powershell
th paper status
th paper run --symbol SOXLUSDT --dir LONG --stop <손절가> --tp1 <익절가>
```

바뀐 점: 가드가 이제 **페이퍼 자체 손익**을 본다(실계좌 아님). 세션이 CLOSED 면 차단된다.
`--dir` 에 LONG/SHORT 외 값은 거부된다.

## 4. 저장소 정리 (선택, 권장)

`dataset/` 폴더 900MB 가 git 에 추적되고 있다. 인덱스에서만 뺀다 (파일은 남는다):

```powershell
git rm -r --cached dataset
git add .gitignore
git commit -m "chore: dataset/ 를 git 추적에서 제외"
```

## 5. 커밋

```powershell
git add -A
git commit -m "fix: 전수 코드 리뷰 44건 수정 + docs/09,10,13,14"
```
