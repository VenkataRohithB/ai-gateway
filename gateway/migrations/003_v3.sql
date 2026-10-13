-- v3: monthly budgets + currency, fallback settings, per-agent rate limit, lean usage records,
-- monthly totals, policy versions edited from the admin UI, settings.

-- Budget month = calendar month in India time. Every ledger write rolls a row into the current month.
CREATE FUNCTION budget_period() RETURNS date LANGUAGE sql STABLE
  AS $$ SELECT date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata')::date $$;

-- budget_limit_micros is now in micro-units of budget_currency (USD or INR).
-- spent_micros (this month) and reserved_micros stay micro-USD: providers bill in USD.
ALTER TABLE agents
  ADD COLUMN budget_currency text NOT NULL DEFAULT 'USD' CHECK (budget_currency IN ('USD', 'INR')),
  ADD COLUMN period date NOT NULL DEFAULT budget_period(),
  ADD COLUMN rpm_limit integer CHECK (rpm_limit > 0);              -- NULL = no limit

ALTER TABLE mandates
  ADD COLUMN budget_currency text NOT NULL DEFAULT 'USD' CHECK (budget_currency IN ('USD', 'INR')),
  ADD COLUMN period date NOT NULL DEFAULT budget_period(),
  ADD COLUMN default_max_tokens integer NOT NULL DEFAULT 4096 CHECK (default_max_tokens BETWEEN 1 AND 65536),
  ADD COLUMN fallback_enabled boolean NOT NULL DEFAULT true;     -- allowed_models order = fallback order
UPDATE mandates SET default_max_tokens = 8192;                     -- existing mandates keep today's behaviour

-- One reservation per attempt; fallback attempts point at the client's request id.
ALTER TABLE reservations ADD COLUMN client_request_id uuid;
CREATE INDEX reservations_agent_recent ON reservations (agent_id, created_at);

CREATE TABLE settings (
  key        text PRIMARY KEY,
  value      text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO settings (key, value) VALUES ('usd_inr_rate', '85.00');  -- INR per 1 USD; set it in the admin UI

-- One row per agent request (allowed or denied). Raw rows are deleted after the retention period;
-- usage_monthly keeps the totals forever. Never holds prompt or response content.
CREATE TABLE usage (
  id                bigserial PRIMARY KEY,
  at                timestamptz NOT NULL DEFAULT now(),
  request_id        uuid NOT NULL,
  agent_id          text NOT NULL,
  mandate_id        text,
  model_requested   text,
  model_served      text,
  outcome           text NOT NULL CHECK (outcome IN ('ok', 'denied', 'error', 'timeout')),
  reason_code       text,
  http_status       integer,
  attempts          smallint NOT NULL DEFAULT 0,
  prompt_tokens     integer,
  completion_tokens integer,
  cost_micros       bigint NOT NULL DEFAULT 0,
  cost_source       text,                                           -- litellm_header | reservation | none
  latency_ms        integer,
  policy_version    text
);
CREATE INDEX usage_at ON usage (at DESC);
CREATE INDEX usage_agent ON usage (agent_id, at DESC);
CREATE INDEX usage_mandate ON usage (mandate_id, at DESC);

CREATE TABLE usage_monthly (
  month             date NOT NULL,
  agent_id          text NOT NULL,
  mandate_id        text NOT NULL,
  model             text NOT NULL,
  requests          integer NOT NULL DEFAULT 0,
  prompt_tokens     bigint NOT NULL DEFAULT 0,
  completion_tokens bigint NOT NULL DEFAULT 0,
  cost_micros       bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (month, agent_id, mandate_id, model)
);
INSERT INTO usage_monthly (month, agent_id, mandate_id, model, requests, prompt_tokens, completion_tokens, cost_micros)
SELECT date_trunc('month', at AT TIME ZONE 'Asia/Kolkata')::date, agent_id, mandate_id, model, count(*),
       coalesce(sum(prompt_tokens), 0), coalesce(sum(completion_tokens), 0), sum(cost_micros)
FROM audit_events
WHERE kind = 'settle' AND decision = 'settled' AND agent_id IS NOT NULL AND mandate_id IS NOT NULL AND model IS NOT NULL
GROUP BY 1, 2, 3, 4;

-- Custom policy rules; the newest version is active. Append-only for the gateway.
CREATE TABLE policy_versions (
  version    serial PRIMARY KEY,
  rules      jsonb NOT NULL,
  note       text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON reservations TO gateway_app;
GRANT SELECT, INSERT, UPDATE ON settings TO gateway_app;
GRANT SELECT, INSERT, DELETE ON usage TO gateway_app;
GRANT USAGE ON SEQUENCE usage_id_seq TO gateway_app;
GRANT SELECT, INSERT, UPDATE ON usage_monthly TO gateway_app;
GRANT SELECT, INSERT ON policy_versions TO gateway_app;
GRANT USAGE ON SEQUENCE policy_versions_version_seq TO gateway_app;
