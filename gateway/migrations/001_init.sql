-- Gateway schema. Applied by migrate.py as gateway_owner. Money is BIGINT micro-USD (1 USD = 1_000_000).

CREATE TABLE agents (
  id                  text PRIMARY KEY CHECK (id ~ '^[a-z0-9-]{1,64}$'),
  name                text NOT NULL,
  status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'revoked')),
  token_sha256        bytea UNIQUE,                       -- sha256 of the bearer token; raw token never stored
  budget_limit_micros bigint NOT NULL DEFAULT 0 CHECK (budget_limit_micros >= 0),
  spent_micros        bigint NOT NULL DEFAULT 0,          -- no CHECK vs limit: a real cost must always settle
  reserved_micros     bigint NOT NULL DEFAULT 0 CHECK (reserved_micros >= 0),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE mandates (
  id                  text PRIMARY KEY CHECK (id ~ '^[a-z0-9-]{1,64}$'),
  name                text NOT NULL,
  description         text NOT NULL DEFAULT '',
  status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
  allowed_models      text[] NOT NULL DEFAULT '{}',
  budget_limit_micros bigint NOT NULL DEFAULT 0 CHECK (budget_limit_micros >= 0),
  spent_micros        bigint NOT NULL DEFAULT 0,
  reserved_micros     bigint NOT NULL DEFAULT 0 CHECK (reserved_micros >= 0),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_mandates (
  agent_id   text NOT NULL REFERENCES agents(id),
  mandate_id text NOT NULL REFERENCES mandates(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, mandate_id)
);

CREATE TABLE reservations (
  request_id    uuid PRIMARY KEY,
  agent_id      text NOT NULL REFERENCES agents(id),
  mandate_id    text NOT NULL REFERENCES mandates(id),
  model         text NOT NULL,
  amount_micros bigint NOT NULL CHECK (amount_micros >= 0),
  status        text NOT NULL DEFAULT 'held' CHECK (status IN ('held', 'settled', 'released')),
  cost_micros   bigint,
  created_at    timestamptz NOT NULL DEFAULT now(),
  settled_at    timestamptz
);
CREATE INDEX reservations_held ON reservations (created_at) WHERE status = 'held';

-- Append-only for gateway_app (INSERT/SELECT only). Not tamper-proof against a DB superuser.
CREATE TABLE audit_events (
  id                bigserial PRIMARY KEY,
  at                timestamptz NOT NULL DEFAULT now(),
  kind              text NOT NULL CHECK (kind IN ('request', 'settle', 'admin')),
  actor             text NOT NULL,                      -- agent id, 'admin', or 'sweeper'
  request_id        uuid,
  agent_id          text,
  mandate_id        text,
  model             text,
  decision          text,                               -- allow | deny | settled | released | late_ignored | <admin action>
  reason_code       text,
  policy_version    text,
  reserved_micros   bigint,
  cost_micros       bigint,
  cost_source       text CHECK (cost_source IN ('litellm_header', 'reservation', 'none', 'late_ignored')),
  prompt_tokens     integer,
  completion_tokens integer,
  finish_reason     text,
  upstream_status   integer,
  detail            jsonb NOT NULL DEFAULT '{}'        -- admin before/after etc. Never prompt/response content.
);
CREATE INDEX audit_events_agent ON audit_events (agent_id, at DESC);
CREATE INDEX audit_events_mandate ON audit_events (mandate_id, at DESC);
CREATE INDEX audit_events_request ON audit_events (request_id);

GRANT USAGE ON SCHEMA public TO gateway_app;
GRANT SELECT, INSERT, UPDATE ON agents, mandates, reservations TO gateway_app;
GRANT SELECT, INSERT, DELETE ON agent_mandates TO gateway_app;
GRANT SELECT, INSERT ON audit_events TO gateway_app;
GRANT USAGE ON SEQUENCE audit_events_id_seq TO gateway_app;
