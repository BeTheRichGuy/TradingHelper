-- Phase 5' — 자동매매 실행 계층
--
-- 여기 저장하는 것은 **재시작 복구**와 **사후 검증** 두 가지를 위한 것이다.
--
--   재시작 복구 : 프로세스가 죽어도 열려 있던 브래킷과 주문을 그대로 되살린다.
--                이게 없으면 '손절이 걸려 있다고 믿는데 실제로는 없는' 상태가 된다.
--   사후 검증   : 표시한 근거(reason)와 실제 결과를 나란히 남긴다. 몇 달 뒤에
--                "그때 왜 잡았지"를 재현할 수 있어야 전략을 고칠 수 있다.

CREATE TABLE IF NOT EXISTS brackets (
    bracket_id    text PRIMARY KEY,
    symbol        text NOT NULL,
    direction     text NOT NULL,                 -- LONG | SHORT
    qty           double precision NOT NULL,
    stop_price    double precision NOT NULL,     -- NOT NULL — 손절 없는 브래킷은 존재할 수 없다
    entry_type    text NOT NULL,
    entry_price   double precision,
    tp1_price     double precision,
    tp2_price     double precision,
    tp1_ratio     double precision NOT NULL DEFAULT 0.5,
    reason        text NOT NULL DEFAULT '',
    setup_id      text NOT NULL DEFAULT '',
    mode          text NOT NULL,                 -- paper | live
    -- 살아있는 상태
    position_qty  double precision NOT NULL DEFAULT 0,
    avg_entry     double precision,
    realized_pnl  double precision NOT NULL DEFAULT 0,
    realized_r    double precision NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    closed_at     timestamptz,
    close_reason  text NOT NULL DEFAULT '',
    CONSTRAINT stop_price_positive CHECK (stop_price > 0)
);

CREATE INDEX IF NOT EXISTS brackets_open_idx ON brackets (closed_at) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS brackets_symbol_idx ON brackets (symbol, created_at DESC);

CREATE TABLE IF NOT EXISTS exec_orders (
    client_id     text PRIMARY KEY,              -- 우리가 만드는 멱등 키
    bracket_id    text NOT NULL REFERENCES brackets(bracket_id) ON DELETE CASCADE,
    symbol        text NOT NULL,
    side          text NOT NULL,
    type          text NOT NULL,
    purpose       text NOT NULL,                 -- ENTRY | STOP | TP1 | TP2 | EXIT
    qty           double precision NOT NULL,
    price         double precision,
    trigger       double precision,
    reduce_only   boolean NOT NULL DEFAULT false,
    status        text NOT NULL,
    filled_qty    double precision NOT NULL DEFAULT 0,
    avg_price     double precision,
    exchange_id   text,
    note          text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS exec_orders_bracket_idx ON exec_orders (bracket_id);
CREATE INDEX IF NOT EXISTS exec_orders_open_idx ON exec_orders (status)
    WHERE status IN ('PENDING', 'OPEN');

CREATE TABLE IF NOT EXISTS exec_fills (
    id              bigserial PRIMARY KEY,
    order_client_id text NOT NULL,
    bracket_id      text NOT NULL REFERENCES brackets(bracket_id) ON DELETE CASCADE,
    symbol          text NOT NULL,
    side            text NOT NULL,
    purpose         text NOT NULL,
    qty             double precision NOT NULL,
    price           double precision NOT NULL,
    fee             double precision NOT NULL DEFAULT 0,
    slippage_pct    double precision NOT NULL DEFAULT 0,
    filled_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS exec_fills_bracket_idx ON exec_fills (bracket_id, filled_at);

-- 손으로 한 매매를 적어두는 곳.
-- 자동매매 규칙을 아직 못 찾았으므로, 당분간 가장 값어치 있는 데이터는
-- **사용자가 실제로 어디서 잡았는가** 이다. 그게 쌓여야 그 판단을 검정할 수 있다.
CREATE TABLE IF NOT EXISTS trade_journal (
    id            bigserial PRIMARY KEY,
    symbol        text NOT NULL,
    direction     text NOT NULL,
    entry_at      timestamptz NOT NULL,
    entry_price   double precision NOT NULL,
    stop_price    double precision,
    exit_at       timestamptz,
    exit_price    double precision,
    qty           double precision,
    reason        text NOT NULL DEFAULT '',      -- 왜 잡았나 (자유 서술)
    setup_tag     text NOT NULL DEFAULT '',      -- ema99_support 같은 짧은 꼬리표
    outcome_r     double precision,
    note          text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS trade_journal_time_idx ON trade_journal (entry_at DESC);
