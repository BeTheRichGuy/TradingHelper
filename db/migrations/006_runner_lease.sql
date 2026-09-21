-- 페이퍼 러너 임대 (docs/15 7절).
--
-- v2.1 부터 페이퍼 엔진(PaperRunner)이 `th serve` 안에서 돈다. 같은 브래킷을 두 프로세스가
-- 관리하면 손절이 두 번 나가거나 청산 요청을 서로 뺏는다. 그래서 러너는 시작할 때 여기에
-- 자기 이름(holder: "serve" | "cli")을 적고 10초마다 heartbeat 를 찍는다.
-- 다른 holder 의 heartbeat 가 30초 이내면 두 번째 러너는 경고하고 종료한다.

CREATE TABLE IF NOT EXISTS runner_lease (
    mode         text PRIMARY KEY,           -- "paper" (실거래 러너는 존재하지 않는다)
    holder       text NOT NULL,
    heartbeat_at timestamptz NOT NULL DEFAULT now()
);
