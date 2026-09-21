-- Phase 1.5 — 캘린더 이벤트 자동 수집
--
-- db/init/ 은 컨테이너를 처음 만들 때만 실행된다. 이미 돌고 있는 DB에는
-- `th migrate` 로 이 파일을 적용한다. 전부 IF NOT EXISTS 라 여러 번 돌려도 안전하다.

CREATE TABLE IF NOT EXISTS calendar_events (
    id          text PRIMARY KEY,              -- source|kind|date|이름 해시
    ts_utc      timestamptz NOT NULL,
    kind        text NOT NULL,                 -- fomc | econ | earnings
    name        text NOT NULL,
    importance  text NOT NULL DEFAULT 'medium',
    symbols     text[] NOT NULL DEFAULT '{}',
    source      text NOT NULL,                 -- manual | fred | yfinance | finnhub
    note        text NOT NULL DEFAULT '',
    synced_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS calendar_events_ts   ON calendar_events (ts_utc);
CREATE INDEX IF NOT EXISTS calendar_events_kind ON calendar_events (kind, ts_utc);

-- 소스별 마지막 동기화 상태. '언제 마지막으로 성공했는가'를 화면에 띄우기 위한 것.
CREATE TABLE IF NOT EXISTS calendar_sync_state (
    source      text PRIMARY KEY,
    last_ok_at  timestamptz,
    last_try_at timestamptz NOT NULL DEFAULT now(),
    ok          boolean NOT NULL DEFAULT true,
    count       integer NOT NULL DEFAULT 0,
    detail      text NOT NULL DEFAULT ''
);
