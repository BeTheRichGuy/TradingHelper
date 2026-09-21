-- 브라우저의 "청산" 버튼 → 페이퍼 러너 (docs/15 3절).
--
-- 웹 서버 프로세스에는 BracketManager 가 없다. 그래서 서버는 **요청만 표시**하고,
-- 실제 청산은 `th paper run` 루프가 이 표시를 보고 `close_now()` 로 수행한 뒤 지운다.
-- 실거래 브래킷에는 이 경로가 없다 (서버가 페이퍼 모드가 아니면 403).

ALTER TABLE brackets ADD COLUMN IF NOT EXISTS close_requested_at timestamptz;
