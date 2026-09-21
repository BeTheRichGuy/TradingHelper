-- 브래킷 복구에 빠져 있던 값들.
--
--   expire_after_s : 진입 지정가 유효시간. 저장하지 않으면 재시작 뒤 기본값(300초)으로
--                    되돌아가 사용자가 --expire 로 준 값이 사라진다.
--   qty_step / tick_size : 거래소 스텝. TP 분할 수량을 여기에 맞춰야
--                    손절 수량 == TP1 + TP2 가 재시작 뒤에도 유지된다.

ALTER TABLE brackets ADD COLUMN IF NOT EXISTS expire_after_s integer NOT NULL DEFAULT 300;
ALTER TABLE brackets ADD COLUMN IF NOT EXISTS qty_step  double precision;
ALTER TABLE brackets ADD COLUMN IF NOT EXISTS tick_size double precision;
