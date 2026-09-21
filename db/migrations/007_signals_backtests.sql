-- v2.2 (docs/15 10~12절) — 자동 시그널 엔진 기록 + 웹 백테스트 실행 기록.
--
--   signal_log    : 5분봉이 닫힐 때마다 엔진이 본 것과 한 일. "왜 안 잡았나"가 "왜 잡았나"만큼 중요하다.
--                   페이퍼 브래킷과는 bracket_id 로 잇는다 (열렸을 때만).
--   backtest_runs : 웹 백테스트 페이지에서 돌린 결과. 매개변수·지표·매매 목록을 JSON 그대로 둔다.
--                   같은 조건을 다시 돌리면 같은 답이 나와야 하므로 params 를 통째로 남긴다.

CREATE TABLE IF NOT EXISTS signal_log (
    id           bigserial PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),   -- 엔진이 판단한 시각
    bar_ts       timestamptz NOT NULL,                 -- 판단 근거가 된 (닫힌) 5분봉 시작 시각
    symbol       text NOT NULL,
    setup_id     text NOT NULL,                        -- video200_20
    action       text NOT NULL,                        -- open | skip | exit | none | error
    direction    text,                                 -- LONG | SHORT (신호가 있을 때)
    kind         text NOT NULL DEFAULT '',             -- 눌림 | 스퀴즈 | 눌림+스퀴즈
    close        double precision,
    ema20        double precision,
    ema200       double precision,
    stop_price   double precision,
    bracket_id   text,
    reason       text NOT NULL DEFAULT '',             -- 열었으면 근거, 건너뛰었으면 가드 사유
    params       jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS signal_log_ts_idx ON signal_log (ts DESC);
CREATE INDEX IF NOT EXISTS signal_log_symbol_idx ON signal_log (symbol, bar_ts DESC);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    symbol       text NOT NULL,
    setup_id     text NOT NULL DEFAULT 'video200_20',
    source       text NOT NULL DEFAULT '',             -- csv | db | binance
    since        text NOT NULL DEFAULT '',             -- YYYY-MM (비면 전체)
    status       text NOT NULL DEFAULT 'queued',       -- queued | running | done | error
    params       jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- 목록에 보일 핵심 지표
    result       jsonb,                                -- 전체 결과 (매매 목록 포함)
    error        text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS backtest_runs_created_idx ON backtest_runs (created_at DESC);
