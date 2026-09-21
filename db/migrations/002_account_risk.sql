-- Phase 2 — 리스크 엔진 · 계정 연동
--
-- 계정 원본은 바이낸스에 있다. 여기 저장하는 건 두 가지 목적뿐이다.
--   1. 일일 손실 한도를 '한 번 걸리면 그날은 계속 잠기게' 하기 위한 기록
--   2. 나중에 실제 승률과 표시 승률을 비교하기 위한 이력

CREATE TABLE IF NOT EXISTS daily_pnl (
    date_kst     date PRIMARY KEY,
    realized     double precision NOT NULL DEFAULT 0,
    commission   double precision NOT NULL DEFAULT 0,
    funding      double precision NOT NULL DEFAULT 0,
    trade_count  integer NOT NULL DEFAULT 0,
    r_unit       double precision NOT NULL DEFAULT 0,
    r_value      double precision NOT NULL DEFAULT 0,
    -- 한 번 한도에 닿으면 이 값이 남아, 손실이 회복돼도 그날은 계속 잠긴다
    limit_hit_at timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    ts        timestamptz PRIMARY KEY,
    symbol    text NOT NULL,
    wallet    double precision,
    available double precision,
    payload   jsonb NOT NULL DEFAULT '{}'::jsonb
);

SELECT create_hypertable('account_snapshots', 'ts',
    chunk_time_interval => interval '30 days', if_not_exists => TRUE);

SELECT add_retention_policy('account_snapshots', INTERVAL '90 days', if_not_exists => TRUE);
