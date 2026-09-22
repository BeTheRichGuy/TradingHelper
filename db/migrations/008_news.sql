-- v3.0 (docs/17) — 뉴스·이벤트 매매 엔진.
--
--   news_items          : 수집한 뉴스 한 건. 텔레그램 메시지·RSS 헤드라인·SEC 공시가 같은 모양으로 들어온다.
--                         dedupe_key 로 같은 것을 두 번 넣지 않는다. symbols 는 유니버스 심볼(…USDT).
--   news_judgments      : Claude 판단 기록. 트리아지(저가)와 본판단을 stage 로 구분한다.
--                         usage·cost_usd 를 남겨 하루 비용 상한을 센다. 실패도 error 로 남긴다 — "판단이 없었다" 와
--                         "판단이 실패했다" 는 다른 사실이다.
--   news_campaigns      : 작전 = 종목 하나·방향 하나·다리 1~3개. 승인 흐름의 상태 기계.
--   news_campaign_legs  : 다리 ↔ 브래킷. execution 스키마는 건드리지 않고 여기서 잇는다.

CREATE TABLE IF NOT EXISTS news_items (
    id            bigserial PRIMARY KEY,
    ts            timestamptz NOT NULL,                  -- 발행 시각 (소스 기준, UTC)
    received_at   timestamptz NOT NULL DEFAULT now(),    -- 우리가 받은 시각
    source        text NOT NULL,                         -- telegram | edgar | yahoo | gnews | finnhub
    source_id     text NOT NULL DEFAULT '',              -- 소스 안의 고유 id (메시지 id · accession no · guid)
    channel       text NOT NULL DEFAULT '',              -- 텔레그램 채널명 · RSS 매체명
    url           text NOT NULL DEFAULT '',
    title         text NOT NULL,
    body          text NOT NULL DEFAULT '',
    symbols       text[] NOT NULL DEFAULT '{}',          -- 매핑된 유니버스 심볼
    dedupe_key    text NOT NULL UNIQUE,
    title_key     text NOT NULL DEFAULT '',              -- 제목 정규화 해시 — URL 이 달라도 같은 기사를 잡는다
    raw           jsonb NOT NULL DEFAULT '{}'::jsonb,
    judged_at     timestamptz,                           -- NULL 이면 아직 판단 안 함
    judgment_id   bigint
);

CREATE INDEX IF NOT EXISTS news_items_ts_idx       ON news_items (ts DESC);
CREATE INDEX IF NOT EXISTS news_items_symbols_idx  ON news_items USING GIN (symbols);
CREATE INDEX IF NOT EXISTS news_items_title_idx    ON news_items (title_key, ts DESC);
CREATE INDEX IF NOT EXISTS news_items_unjudged_idx ON news_items (received_at) WHERE judged_at IS NULL;

CREATE TABLE IF NOT EXISTS news_judgments (
    id                bigserial PRIMARY KEY,
    news_id           bigint NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    ts                timestamptz NOT NULL DEFAULT now(),
    stage             text NOT NULL,                     -- triage | full
    model             text NOT NULL,
    relevant          boolean,
    symbols           text[] NOT NULL DEFAULT '{}',
    direction         text,                              -- LONG | SHORT | NONE
    confidence        double precision,
    magnitude         text,                              -- low | medium | high
    horizon_hours     integer,
    thesis_ko         text NOT NULL DEFAULT '',
    risks             jsonb NOT NULL DEFAULT '[]'::jsonb,
    already_priced_in boolean,
    plan              jsonb,                             -- Plan (docs/17 §4) — 없으면 NULL
    usage             jsonb NOT NULL DEFAULT '{}'::jsonb,
    cost_usd          double precision NOT NULL DEFAULT 0,
    error             text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS news_judgments_ts_idx   ON news_judgments (ts DESC);
CREATE INDEX IF NOT EXISTS news_judgments_news_idx ON news_judgments (news_id);

CREATE TABLE IF NOT EXISTS news_campaigns (
    id            text PRIMARY KEY,                      -- camp-xxxxxxxxxxxx
    created_at    timestamptz NOT NULL DEFAULT now(),
    symbol        text NOT NULL,
    direction     text NOT NULL,                         -- LONG | SHORT
    judgment_id   bigint REFERENCES news_judgments(id) ON DELETE SET NULL,
    news_id       bigint REFERENCES news_items(id) ON DELETE SET NULL,
    status        text NOT NULL,                         -- proposed | pending_approval | approved | rejected | expired | open | closed
    exec_mode     text NOT NULL,                         -- observe | paper | approve
    plan          jsonb NOT NULL,                        -- Plan 그대로 (다리·손절·목표·최대보유)
    thesis_ko     text NOT NULL DEFAULT '',
    approval      jsonb NOT NULL DEFAULT '{}'::jsonb,    -- {by, at, via: telegram|web, note}
    opened_at     timestamptz,
    closed_at     timestamptz,
    close_reason  text NOT NULL DEFAULT '',
    realized_r    double precision,
    summary       jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS news_campaigns_created_idx ON news_campaigns (created_at DESC);
CREATE INDEX IF NOT EXISTS news_campaigns_open_idx    ON news_campaigns (symbol) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS news_campaign_legs (
    campaign_id   text NOT NULL REFERENCES news_campaigns(id) ON DELETE CASCADE,
    leg_no        integer NOT NULL,
    bracket_id    text,                                  -- 열렸을 때만. brackets(bracket_id) 와 느슨하게 잇는다
    margin_pct    double precision NOT NULL,
    entry_type    text NOT NULL,                         -- market | limit
    status        text NOT NULL DEFAULT '',              -- '' | open | filled | expired | closed | skipped
    note          text NOT NULL DEFAULT '',
    PRIMARY KEY (campaign_id, leg_no)
);
