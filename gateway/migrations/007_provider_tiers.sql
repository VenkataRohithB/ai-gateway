-- Free / paid tag per provider key. No provider API reports a key's tier, so it is either detected from the
-- provider's own quota errors (Gemini names "...FreeTier" quotas in its 429s) or set by the admin (admin wins).
-- key_name = the LiteLLM credential name, or "env-<provider>" for keys that come from .env.
CREATE TABLE provider_tiers (
  key_name   text PRIMARY KEY,
  tier       text NOT NULL CHECK (tier IN ('free', 'paid')),
  source     text NOT NULL CHECK (source IN ('detected', 'admin')),
  evidence   text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE, DELETE ON provider_tiers TO gateway_app;
