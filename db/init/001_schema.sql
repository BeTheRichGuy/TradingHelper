-- Trading Helper 스키마 (Phase 0)
-- 컨테이너 최초 기동 시 자동 실행됩니다.
--
-- 설계 원칙 3가지 — 나중에 자동매매/멀티유저로 확장할 때 마이그레이션을 줄이기 위한 것:
--   1. 모든 시각은 timestamptz(UTC). 로컬 시각은 저장하지 않는다.
--   2. 사용자에 귀속되는 테이블은 처음부터 owner_id 를 갖는다 (지금은 단일 사용자).
--   3. 시세 데이터는 사용자와 무관하므로 owner_id 를 두지 않는다 (멀티유저가 돼도 공유).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─────────────────────────────────────────────────────────
-- 사용자 (지금은 1명. 나중에 결제/멀티테넌시가 붙는 자리)
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id          uuid PRIMARY KEY,
    email       text UNIQUE,
    plan        text NOT NULL DEFAULT 'owner',   -- owner | free | pro  (결제 확장점)
    created_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO users (id, email, plan)
VALUES ('00000000-0000-0000-0000-000000000001', 'owner@local', 'owner')
ON CONFLICT (id) DO NOTHING;

-- ─────────────────────────────────────────────────────────
-- 심볼 메타
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS symbols (
    symbol      text PRIMARY KEY,
    kind        text NOT NULL,                  -- perp | equity | index
    underlying  text,                           -- SOXLUSDT -> SOXL
    exchange    text NOT NULL DEFAULT 'binance',
    tick_size   numeric,
    qty_step    numeric,
    is_active   boolean NOT NULL DEFAULT true,
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────
-- 캔들 (하이퍼테이블)
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS klines (
    symbol          text             NOT NULL,
    tf              text             NOT NULL,   -- 1m | 5m | 15m
    ts              timestamptz      NOT NULL,   -- 봉 시작 시각 (UTC)
    open            double precision NOT NULL,
    high            double precision NOT NULL,
    low             double precision NOT NULL,
    close           double precision NOT NULL,
    volume          double precision NOT NULL,
    quote_volume    double precision,
    trades          integer,
    taker_buy_base  double precision,            -- CVD 계산에 사용
    source          text NOT NULL DEFAULT 'binance',
    inserted_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, tf, ts)
);

SELECT create_hypertable('klines', 'ts',
    chunk_time_interval => interval '7 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS klines_symbol_tf_ts_desc
    ON klines (symbol, tf, ts DESC);

-- ─────────────────────────────────────────────────────────
-- 미결제약정 / 펀딩 / 호가
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS open_interest (
    symbol   text NOT NULL,
    ts       timestamptz NOT NULL,
    oi       double precision NOT NULL,          -- 계약 수
    oi_value double precision,                   -- 명목가(USDT)
    PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('open_interest', 'ts',
    chunk_time_interval => interval '30 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS funding (
    symbol          text NOT NULL,
    ts              timestamptz NOT NULL,
    rate            double precision NOT NULL,
    mark_price      double precision,
    index_price     double precision,
    next_funding_ts timestamptz,
    PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('funding', 'ts',
    chunk_time_interval => interval '90 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS book_ticker (
    symbol     text NOT NULL,
    ts         timestamptz NOT NULL,
    bid        double precision NOT NULL,
    ask        double precision NOT NULL,
    spread_bps double precision NOT NULL,
    PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('book_ticker', 'ts',
    chunk_time_interval => interval '1 day', if_not_exists => TRUE);

-- 호가는 양이 많으므로 7일만 보관
SELECT add_retention_policy('book_ticker', INTERVAL '7 days', if_not_exists => TRUE);

-- ─────────────────────────────────────────────────────────
-- 본주 / 지수 가격 (basis 계산, 변동성 필터)
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS underlying_prices (
    ticker text NOT NULL,                        -- SOXL, SOXX, ^VIX ...
    ts     timestamptz NOT NULL,
    open   double precision,
    high   double precision,
    low    double precision,
    close  double precision NOT NULL,
    volume double precision,
    tf     text NOT NULL DEFAULT '1m',
    source text NOT NULL DEFAULT 'yfinance',
    PRIMARY KEY (ticker, tf, ts)
);
SELECT create_hypertable('underlying_prices', 'ts',
    chunk_time_interval => interval '30 days', if_not_exists => TRUE);

-- ─────────────────────────────────────────────────────────
-- 강제청산 스트림 (청산 캐스케이드 감지용)
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS liquidations (
    symbol   text NOT NULL,
    ts       timestamptz NOT NULL,
    side     text NOT NULL,                      -- BUY(숏청산) | SELL(롱청산)
    price    double precision NOT NULL,
    qty      double precision NOT NULL,
    notional double precision NOT NULL
);
SELECT create_hypertable('liquidations', 'ts',
    chunk_time_interval => interval '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS liquidations_symbol_ts ON liquidations (symbol, ts DESC);

-- ─────────────────────────────────────────────────────────
-- 수집 상태 — 재시작/재연결 시 어디부터 백필할지
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingest_state (
    stream     text NOT NULL,                    -- kline_1m | open_interest | funding | underlying
    symbol     text NOT NULL,
    last_ts    timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (stream, symbol)
);

-- 봉 누락 기록 (Phase 0 완료 기준: 24시간 무중단 후 여기가 비어 있어야 함)
CREATE TABLE IF NOT EXISTS ingest_gaps (
    id         bigserial PRIMARY KEY,
    symbol     text NOT NULL,
    tf         text NOT NULL,
    gap_start  timestamptz NOT NULL,
    gap_end    timestamptz NOT NULL,
    missing    integer NOT NULL,
    filled     boolean NOT NULL DEFAULT false,
    detected_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingest_gaps_open ON ingest_gaps (symbol, filled, gap_start DESC);

-- ─────────────────────────────────────────────────────────
-- 5분봉 / 15분봉 — 1분봉에서 자동 집계 (직접 수집하지 않는다)
-- ─────────────────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS klines_5m
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket(INTERVAL '5 minutes', ts) AS ts,
    first(open, ts)  AS open,
    max(high)        AS high,
    min(low)         AS low,
    last(close, ts)  AS close,
    sum(volume)      AS volume,
    sum(quote_volume) AS quote_volume,
    sum(trades)      AS trades,
    sum(taker_buy_base) AS taker_buy_base
FROM klines
WHERE tf = '1m'
GROUP BY symbol, time_bucket(INTERVAL '5 minutes', ts)
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS klines_15m
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket(INTERVAL '15 minutes', ts) AS ts,
    first(open, ts)  AS open,
    max(high)        AS high,
    min(low)         AS low,
    last(close, ts)  AS close,
    sum(volume)      AS volume,
    sum(quote_volume) AS quote_volume,
    sum(trades)      AS trades,
    sum(taker_buy_base) AS taker_buy_base
FROM klines
WHERE tf = '1m'
GROUP BY symbol, time_bucket(INTERVAL '15 minutes', ts)
WITH NO DATA;

SELECT add_continuous_aggregate_policy('klines_5m',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE);

SELECT add_continuous_aggregate_policy('klines_15m',
    start_offset => INTERVAL '6 hours',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE);
