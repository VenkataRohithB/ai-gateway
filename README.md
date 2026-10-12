# AI Agent Gateway

A central gateway in front of company AI agents. Every LLM call an agent makes goes through it, and
the gateway decides **who** is calling, **under which mandate**, whether policy **allows** it, and
whether the agent's and the mandate's **budgets** can cover it, *before* anything reaches a model
provider. Every decision is audited, and any agent can be stopped from the dashboard on its next call.

First product: SynBioAI (5 agents). First real client: ResearchOS. Plan and design record: `docs/plan/`.

**Status:** MVP milestones M1–M10 are complete. Automated tests: 66 pytest tests plus an end-to-end
kill-switch proof, all passing, and all run against a fake model at **no cost**.

```
 Agent (ResearchOS, SynBioAI…) ── gw_ token ──► GATEWAY :8080 ──► LiteLLM ──► Gemini
                                                 │  (FastAPI)       (internal only)
                                                 ├─ /admin/ui  dashboard
                                                 ├─ /admin/*   admin API
                                                 └─ PostgreSQL: agents, mandates, budgets, audit
```

| Component | Responsibility |
|---|---|
| **Gateway** (`gateway/`) | Agent authentication, policy, per-agent and per-mandate hard budgets, kill switch, audit log, admin API, dashboard |
| **LiteLLM 1.104.0** (pinned by digest, unmodified) | Provider adapters and keys, price table, per-call cost, its own spend log (second record) |
| **PostgreSQL 16** | db `gateway` (ours) and db `litellm` (LiteLLM's), each with its own least-privilege role |

Only the gateway is published, on `127.0.0.1:8080`. LiteLLM and Postgres sit on an internal Docker network, so
agents cannot bypass the gate.

---

## 1. Setup

Prerequisites: Docker with Compose v2, and a Gemini API key (https://aistudio.google.com/apikey).

```bash
scripts/gen-env.sh                 # creates .env with random secrets (never overwrites; never prints)
$EDITOR .env                       # set GEMINI_API_KEY=...
docker compose up -d --build       # db -> litellm -> migrate (one-shot) -> gateway
scripts/litellm-setup.sh           # creates the scoped "gateway" LiteLLM key (idempotent)
curl -s 127.0.0.1:8080/health/ready   # {"db":true,"litellm":true,"prices":true}
```

Open the dashboard at **http://127.0.0.1:8080/admin/ui** and sign in with `ADMIN_TOKEN` from `.env`.

**Stop or restart:** `docker compose stop` / `docker compose up -d`. All state lives in the volume `ai-agent-gateway_pgdata`.
`docker compose down -v` **deletes everything**.

**Upgrading an older checkout:** run `scripts/gen-env.sh --add-missing` (adds new secrets only), then
`docker compose exec -T db bash < config/db-init.sh` (DB roles, idempotent), then `docker compose up -d --build`.

Keep `.env` safe and stable. `LITELLM_SALT_KEY` must never change, and `.env` / `.secrets/` are git-ignored.

### Secrets: who holds what

| Secret (`.env`) | Held by | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | LiteLLM only | Calling Google |
| `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY` | LiteLLM only | LiteLLM admin / credential encryption |
| `LITELLM_GATEWAY_KEY` | gateway | Scoped LiteLLM key: the 4 models only, $10 backstop |
| `ADMIN_TOKEN` | you | Admin API and dashboard |
| `POSTGRES_PASSWORD` | bootstrap only | Superuser `gateway`, which no service uses |
| `LITELLM_DB_PASSWORD`, `GATEWAY_OWNER_PASSWORD`, `GATEWAY_APP_PASSWORD` | litellm / migrate / gateway | Least-privilege DB roles |
| agent tokens `gw_…` | each agent | Shown once at issue; only their sha256 is stored |

---

## 2. Connecting an agent

Any OpenAI-compatible client works. Change only its configuration:

```
base URL : http://127.0.0.1:8080/m/<mandate-id>/v1
API key  : gw_…   (the agent's own token)
model    : one of the mandate's allowed models, e.g. gemini-3.7-flash
```

1. Create a mandate, an agent, the assignment, and a token, from the dashboard or the CLI:
   ```bash
   A=scripts/gw_admin.py
   $A POST /admin/mandates '{"id":"synbio","name":"SynBioAI","budget_usd":"20","allowed_models":["gemini-3.7-flash"]}'
   $A POST /admin/agents   '{"id":"synbio-agent-1","name":"SynBio agent 1","budget_usd":"5"}'
   $A PUT  /admin/agents/synbio-agent-1/mandates/synbio
   $A POST /admin/agents/synbio-agent-1/token --save-token .secrets/synbio-agent-1.token
   ```
2. Put the token in the agent's own secret config. Never commit it.

### Request contract (`POST /m/{mandate}/v1/chat/completions`)

- **Identity** comes only from the bearer token. Body fields such as `user` are rejected.
- **Allowed fields:** `model, messages, max_tokens | max_completion_tokens (not both), temperature, top_p,
  stop, seed, tools (type "function" only), tool_choice, response_format, n (=1), stream (=false)`.
  Anything else, such as `api_base`, `api_key`, `metadata`, `fallbacks` or `extra_body`, returns 400 `FIELD_NOT_ALLOWED`.
- Message content must be text: images, audio and files are rejected. The body is capped at 2 MB.
- `max_tokens` defaults to **8192** when omitted, with a maximum of 16384. It is forwarded and priced.
- The gateway **rebuilds** the outbound request from the validated fields and forwards **no client headers**.
- Each response carries `x-request-id`, which is also the id in the audit log and in LiteLLM's spend log.

### Status codes

| Code | `error.code` | Meaning |
|---|---|---|
| 401 | `UNAUTHORIZED` | missing or invalid token |
| 403 | `AGENT_REVOKED`, `AGENT_INACTIVE`, `MANDATE_NOT_ASSIGNED`, `MANDATE_INACTIVE`, `MODEL_NOT_ALLOWED` | policy deny (includes `policy_version`) |
| 402 | `AGENT_BUDGET_EXCEEDED`, `MANDATE_BUDGET_EXCEEDED` | budget cannot cover this request's worst case |
| 400 / 413 | `FIELD_NOT_ALLOWED`, `TOOL_NOT_ALLOWED`, `STREAMING_NOT_SUPPORTED`, `CONTENT_NOT_ALLOWED`, `INVALID_REQUEST`, `REQUEST_TOO_LARGE` | bad request |
| 429 | `UPSTREAM_RATE_LIMITED` | provider quota; retry later or use another model |
| 503 / 504 | `UPSTREAM_UNAVAILABLE` / `UPSTREAM_TIMEOUT` | LiteLLM or the provider is down, or the call timed out |

All error bodies have the shape `{"error": {"code", "message", "request_id", …}}` and never contain internal details.

---

## 3. Policy (`config/policy.toml`)

Policy is explicit, reviewable **data**, not code. The rules are ordered `forbid` entries, and the first match denies:

```toml
[[forbid]]
reason  = "AGENT_REVOKED"
when    = { "agent.status" = "revoked" }
message = "agent credential has been revoked"
```

- **Facts:** `agent.status`, `assignment.exists`, `mandate.status`, `model.allowed`. They are read **fresh from the DB
  for every request**, with no cache.
- **Versioning:** each decision records `policy_version`, the first 12 hex digits of the file's sha256.
- **Applying a change:** edit the file, review it in git, then run `docker compose restart gateway`.
- **Load-time checks:** an unknown fact name, for example a typo, stops the gateway from starting.
- **Rules and facts are separate.** Rules (what gets blocked) live in this file. Facts (who is revoked or assigned, which
  models a mandate allows) live in the DB and are changed instantly from the dashboard or API.
- **Cedar later:** each rule maps one-to-one to a Cedar `forbid` with `@id(reason)`. Only `gateway/policy.py` would change.

## 4. Budgets

Budgets are hard, lifetime ceilings for each agent **and** each mandate. Money is held as integer micro-USD.

1. **Estimate (worst case):** request bytes × input price + `max_tokens` × output price. Bytes are always ≥ tokens,
   so input is over-estimated by roughly 4–6×. Prices come from LiteLLM's pinned table (`/model/info`).
2. **Reserve:** one transaction locks the assignment, then atomically adds the estimate to the agent's
   and the mandate's `reserved` amount, re-checking status and `spent + reserved + estimate ≤ limit`. If either fails, the
   request is denied and nothing is forwarded.
3. **Settle.** The charge depends on how the call ends:

   | Outcome | Charge |
   |---|---|
   | Success | The real cost from LiteLLM's `x-litellm-response-cost` header |
   | Success, but the cost is unknown | The full reservation |
   | Timeout or connection lost (outcome unknown) | The full reservation |
   | Provider error (429, 5xx and so on) | Nothing; the reservation is released |

4. **Sweeper:** at startup, and then every 60 s, the gateway charges any reservation abandoned by a crash at its
   reserved amount. A late settle after a sweep is audited as `late_ignored` and never double-charged.

**Concurrency:** 50 simultaneous requests cannot overspend; there is a test for this. Budgets are raised or lowered by
the admin, and setting a budget to 0 withdraws it. Spend cannot be edited.

## 5. Kill switch

| Action | Effect on the **next** request |
|---|---|
| Suspend / revoke agent | denied under every mandate (`AGENT_INACTIVE` / `AGENT_REVOKED`) |
| Suspend mandate | every agent under it denied (`MANDATE_INACTIVE`) |
| Unassign agent from mandate | `MANDATE_NOT_ASSIGNED` |
| Budget → 0 (agent or mandate) | `…_BUDGET_EXCEEDED` |
| Restore | set the value back |

- **Guarantee:** every request that has not reserved yet when the change commits is denied **before** anything is
  sent to LiteLLM or the provider. The state is in Postgres, so it survives restarts.
- **Not guaranteed:** a request that has already been forwarded finishes and is charged; it is not cancelled.
- **No cooperation needed:** the agent does not have to stop itself. ResearchOS treats 401, 402 and 403 as fatal (`LLMAuthorizationError`).

## 6. Audit

Every request (allow/deny), settle (`settled`, `released`, `late_ignored`) and admin action is written to `audit_events`.
Each row records: time, request id, agent, mandate, model, decision, reason code, policy version, reserved amount,
cost and cost source, tokens, finish reason, upstream status, and the before/after values of admin changes.

- **Never stored:** prompt or response content, or any token.
- **Invalid-token attempts** go only to the operational log (stdout), so they can't flood the audit table.
- **Protection:** the gateway's DB role can only `INSERT` and `SELECT` on `audit_events`, so the log is append-only for
  the app. It is **not tamper-proof**: the Postgres superuser can still edit it.
- **Where to read it:** the dashboard's Audit tab, `scripts/gw_admin.py GET '/admin/audit?agent=…&decision=deny'`, or
  `docker compose logs gateway` for the operational log (one JSON line per outcome).

## 7. Tests (free: no real model calls)

```bash
scripts/test.sh          # 66 pytest tests + end-to-end kill-switch proof
scripts/test.sh unit     # pytest only
```

Everything runs in a separate Compose project, `aigw-test`, with its own volume, so your data is never touched.
LiteLLM is pointed at `tests/mock_upstream.py`, a fake model that counts every call.

- **`tests/test_policy.py`:** every rule, the order of rules, typo protection, and agreement between the policy file and the SQL guard.
- **`tests/test_gateway.py`:** real Postgres, with a counting mock in place of LiteLLM. It covers:
  - auth, impersonation, unassigned/unknown mandate, the model allowlist, and 16 routing-override / unbounded-cost payloads
  - every kill switch (proving that the **upstream call count does not move**)
  - persistence across restart, in-flight semantics, budget isolation, shared mandate budgets, and 50-way concurrency
  - failure reconciliation (500, 429, 400, 422, connect error, timeout, missing cost), the sweeper and late settle
  - admin-route protection, the append-only audit log, and no secrets or prompts in the audit
- **`tests/e2e_killswitch.py`:** two agents through the **real LiteLLM** to the counting fake model. It checks:
  - budget withdrawal and revocation
  - fail-closed behaviour while the gateway is down, and persistence after a restart
  - LiteLLM's own spend log containing every allowed request id and none of the denied ones
  - that only the gateway port is published, and that no secrets are in git or in the gateway container

**Paid checks (ask first):** `tests/live_gemini.sh` (one call through the gateway) and `scripts/smoke-test.sh`
(one call direct to LiteLLM). Each costs about $0.0005 and asks for confirmation.

## 8. ResearchOS (first real client, migrated)

ResearchOS required config changes only; its code is unchanged. Its `.env`:
```
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://127.0.0.1:8080/m/researchos-dev/v1
LLM_API_KEY=<.secrets/researchos.token>
LLM_MODEL=gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-flash-latest   # client-side fallback
LLM_TIMEOUT_SECONDS=300     # > gateway 270s > LiteLLM 240s
```

- Gateway records: agent `researchos` ($2) under mandate `researchos-dev` ($5, the 4 models).
- Its old direct LiteLLM key is blocked, and port 4000 is closed.
- Backups of its previous config: `.env.before-gateway` and `.env.before-gateway-v2`.

## 9. SynBioAI onboarding (needs approval before touching live agents)

1. Create a mandate per SynBioAI purpose (e.g. `synbio-research`), with a budget and allowed models.
2. Create one gateway agent per SynBioAI agent (5), each with its own budget and token. Never share tokens.
3. Assign each agent only to the mandates it needs.
4. Change each agent's OpenAI-compatible config to the base URL `…/m/<mandate>/v1` and its own token, and remove any
   provider keys from the agents.
5. Smoke-test with a suspended agent first: that call is denied and free. Then activate the agents one at a time.
6. If they run on another host, the gateway must be exposed beyond localhost **behind TLS**, which is not part of this MVP.

## 10. Adding a provider key or model

1. Add the key to `.env`.
2. Pass it into the `litellm` service in `docker-compose.yml`.
3. Add an entry in `config/litellm.yaml` with `api_key: os.environ/NAME`.
4. Run `docker compose up -d --force-recreate litellm`.
5. Add the model to the `gateway` LiteLLM key, for example by deleting and recreating it, or through `/key/update`.
6. Restart the gateway so it reloads prices, then allow the model on mandates.

Two entries under the same `model_name` are load-balanced. For separate free-tier quota, the keys must come from
different Google projects.

## 11. Troubleshooting

| Symptom | Check |
|---|---|
| `/health/ready` shows `"prices": false` | LiteLLM still starting, or the `gateway` key is missing: run `scripts/litellm-setup.sh` |
| 503 `UPSTREAM_UNAVAILABLE` on every call | `docker compose logs gateway \| grep upstream_error`; the gateway key may be blocked or over its $10 backstop |
| 429 / 503 "high demand" from Gemini | Free tier allows 5 requests/min and 20 requests/day **per model**, and Gemini can be overloaded. Agents should list several models; a paid key is recommended |
| 504 `UPSTREAM_TIMEOUT` | The provider took over 240 s; the call was charged at its reservation (outcome unknown) |
| Agent denied unexpectedly | Dashboard → Audit, filter by agent: `reason_code` says why |
| Debugging LiteLLM directly | `docker compose -f docker-compose.yml -f docker-compose.debug.yml up -d` opens 127.0.0.1:4000. Dev only; it bypasses the gate |

## 12. Known limitations and security assumptions

- In-flight requests are not cancelled by a kill switch; the next request is denied.
- Budget overshoot is bounded by (requests in flight) × (estimate error), for example Gemini "thinking" tokens
  beyond `max_tokens`. Input is over-reserved, so agents near their limit are stopped a little early.
- Unknown outcomes (timeouts, lost connections) are charged at the full reservation, even if the provider billed less.
- No streaming, images, or provider built-in tools (Google Search, Maps). Budgets are lifetime only.
- There is a single shared admin token: no per-person admin accounts and no TLS. Local use only.
- The audit log is append-only for the app role, not tamper-proof.
- Policy changes need a gateway restart.
- The gateway runs as a single replica: the startup sweep assumes no other gateway is running.
- Agents must never be attached to the `backend` Docker network.
- Free-tier Gemini quota is far too small for real agent work.

## Repository layout

```
docker-compose.yml          db, litellm, migrate, gateway
docker-compose.debug.yml    dev only: LiteLLM on 127.0.0.1:4000
docker-compose.test.yml     test overlay: fake model, test runner (project aigw-test)
config/  litellm.yaml  litellm.test.yaml  policy.toml  db-init.sh
gateway/ app.py (routes)  db.py (ledger)  policy.py  migrate.py  migrations/  dashboard.html  Dockerfile
scripts/ gen-env.sh  litellm-setup.sh  gw_admin.py  litellm_api.py  smoke-test.sh  test.sh
tests/   test_policy.py  test_gateway.py  e2e_killswitch.py  mock_upstream.py  live_gemini.sh
docs/plan/  PLAN.txt (plan of action) + design debate record
```
