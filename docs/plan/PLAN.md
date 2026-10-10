# AI Agent Gateway: MVP plan v2

Scope: a working MVP up to and including the FastAPI authorization gate, with an optional small dashboard at the end. The design was checked against the repo, the live DB (read-only), the ResearchOS source, and the pinned LiteLLM 1.104.0 image. LiteLLM paths below are relative to `site-packages/litellm/` inside the image.

What changed since v1 is in `response_v1.md`. The main changes are:
- kill-switch linearization
- the outbound body is rebuilt from validated fields
- DB roles for LiteLLM and the gateway
- retry and timeout settings
- milestones reordered

---

## 0. Decisions at a glance

| Topic | Decision |
|---|---|
| Building on LiteLLM | Use LiteLLM **unmodified**, extended **only through config/env**. Our logic lives in a separate FastAPI gate. No fork, overlay image or in-proxy hooks. |
| FastAPI → LiteLLM | One scoped virtual key `gateway`, limited to 4 models, with a global `max_budget` backstop. Never the master key. |
| Ownership of responsibilities | FastAPI: identity, policy, per-agent and per-mandate hard budgets, kill switch, audit. LiteLLM: provider adapters, prices, per-call cost, SpendLogs as a second record. |
| Money | `BIGINT` micro-USD. Reservations are atomic conditional UPDATEs, and that same transaction re-checks the kill-switch facts. |
| Policy | `config/policy.toml` holds data-only `forbid` rules. A stdlib `tomllib` evaluator of about 25 lines reads it. The version is the sha256 of the file. The format maps 1:1 to Cedar. |
| Mandate selection | URL path `/m/{mandate}/v1/chat/completions`, so any OpenAI client can select one through config. |
| Streaming, images, LiteLLM fallbacks | Out of the MVP: 400 for streaming and images; fallbacks stay client-side. |
| Kill-switch proof | (1) An in-process test with a mock that counts calls. (2) An e2e run through real LiteLLM to a counting mock upstream, with two agents, a restart, and SpendLogs cross-checked. |
| Dashboard | Optional. One static HTML page that uses the admin API. |
| Runtime | `python:3.13-slim` (same minor version as LiteLLM), `asyncpg`, plain SQL migrations. |

---

## 1. Architecture: "build on top of LiteLLM, fork-kind but not a full fork"

### What LiteLLM actually enforces (verified)

| Question | Finding | Evidence |
|---|---|---|
| Key `max_budget` | **Check-then-act** (`spend >= max_budget`). Nothing is reserved, so concurrent requests overshoot. Spend is a float. When over budget it returns **422** and its message includes the key's spend. | `proxy/auth/auth_checks.py::_virtual_key_max_budget_check` L5402–5476; `__init__.py:404` |
| When spend is recorded | A counter updated after the call. DB batch writes every 10 s. | `proxy_server.py::get_current_spend` L2742; `constants.py:1819` |
| Per-call cost | Response header `x-litellm-response-cost` (non-streaming). Its value is `str(float)`. It is missing if the cost is unknown and `0.0` on paths LiteLLM treats as unbilled. | `proxy/common_request_processing.py` L1727, L1769, L2885–2896 |
| Price table | All 4 planned aliases are priced. Example for gemini-3.8-flash: input $0.75/M, output $3.75/M, max output 65 536. **By default the table is fetched from GitHub at startup.** The process argv0 is `litellm`, which is not in `{"lite","litellm-proxy"}`. | `get_model_cost_map.py:43, 643`; `/proc/1/cmdline` |
| Correlation id | The `x-litellm-call-id` request header is used verbatim (≤256 chars) and stored as `SpendLogs.litellm_call_id`. | `common_request_processing.py:266, 2124` |
| Attribution | The `x-litellm-spend-logs-metadata` header (JSON) goes into SpendLogs metadata. Not enterprise-gated. | `proxy/litellm_pre_call_utils.py:1301–1310, 1542` |
| Retries / timeout | The router defaults to 2 retries, and live 429 rows show `attempted_retries=2`. Retries can be configured through `router_settings` or the `x-litellm-num-retries` header. The default timeout is **6000 s**. A timeout returns status **408**. | `router.py:1056`; `proxy_server.py:6738`; `litellm_pre_call_utils.py:1291`; `constants.py:554`; `exceptions.py:374` |
| Gemini parameter mapping | `max_tokens` and `max_completion_tokens` both map to `max_output_tokens`. `tools` can carry Gemini built-in tools (`googleSearch`, `urlContext`, `codeExecution`, `googleMaps`), which are billed per query. | `llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py:1139, 378–577` |
| Client routing overrides | LiteLLM has a blocklist (`api_base`, `base_url`, …) and checks body `fallbacks`. | `proxy/auth/auth_utils.py::is_request_body_safe` L475, L306 |
| LiteLLM's own "policy engine" | Guardrail scoping (PII, toxicity), not access control. | `proxy/policy_engine/__init__.py` |
| Enterprise-only features | Key tags, `model_max_budget`, `enforced_params`, some spend reports, OAuth2. **We need none of them.** | `key_management_endpoints.py:1426, 7837`; `litellm_pre_call_utils.py:2933` |

**Conclusion.** LiteLLM is excellent for metering and provider plumbing. Its budgets cannot be the hard ceiling the manager asked for.

### Options

| Option | Verdict | Why |
|---|---|---|
| **(a) Separate FastAPI gate; LiteLLM internal only** | **Chosen** | The trust boundary is about 600 lines we can read and test. The "never forwarded" proof is a mocked transport. A LiteLLM upgrade is a digest bump. Agents never touch LiteLLM's large surface. It is the spec default. |
| (b) In-proxy `custom_auth` / `CustomLogger` hooks | Escape hatch only | Feasible without rebuilding the image: `get_instance_fn` loads a `.py` from the config directory (`proxy/types_utils/utils.py:9–39`), though it would need a directory mount. But `custom_auth` **skips LiteLLM's own checks** unless `enable_post_custom_auth_checks` is set (`user_api_key_auth.py:1550–1572`). Our ledger would live in its event loop and depend on internal types. Kept for a possible future streaming settlement. |
| (c) Overlay image `FROM litellm@digest` | Rejected | Patches rot, and nothing we need requires one. |
| (d) Git fork | Rejected | About 855k lines; we would carry its security-fix stream ourselves. |

### What "fork-kind" means here: LiteLLM config and env only
- `LITELLM_LOCAL_MODEL_COST_MAP: "True"`, so prices change only when the digest changes.
- `router_settings: {num_retries: 0}`. The gateway also sends `x-litellm-num-retries: 0`. The client owns retries.
- `litellm_settings: {request_timeout: 240}`.
- 4 model aliases: gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash, gemini-flash-latest.
- A non-superuser DB role, `litellm_app`.
- Virtual key `gateway` with `models` set to the 4 aliases and a `max_budget` backstop. Attribution goes through `x-litellm-call-id` and `x-litellm-spend-logs-metadata`.
- Not used: teams, per-agent LiteLLM keys (a second source of truth for revocation), the LiteLLM UI.

---

## 2. One request through the gate

```
Agent ── Bearer gw_… ──▶ POST /m/{mandate}/v1/chat/completions   (FastAPI :8080)
 1. body ≤ 2 MB (else 413); auth: lookup agents by sha256(token)     → 401 (stdout log only, no DB row)
 2. validate body (strict allowlist, see §6)                         → 400
 3. facts from DB, no cache → policy.evaluate()                      → 403 + reason (audited)
 4. build outbound body from validated fields; est = worst-case cost
 5. RESERVE txn: lock assignment row, conditional UPDATE agent+mandate
    (re-checks status + budget atomically)                           → 402/403 + reason (audited)
 6. POST LiteLLM with gateway key, x-litellm-call-id=request_id,
    x-litellm-num-retries: 0, metadata header; no client headers
 7. settle or release (§5) → audit (tokens, cost, cost_source, finish_reason)
 8. response: LiteLLM JSON body; headers limited to content-type, retry-after, x-request-id
```

---

## 3. Data and DB roles

### Tables (db `gateway`)
| Table | Key columns |
|---|---|
| `agents` | `id text pk` (slug `^[a-z0-9-]{1,64}$`), `name`, `status` ∈ {active, suspended, revoked}, `token_sha256 bytea unique`, `budget_limit_micros`, `spent_micros`, `reserved_micros ≥ 0`, timestamps |
| `mandates` | `id text pk`, `name`, `status` ∈ {active, suspended}, `allowed_models text[]`, `budget_limit_micros`, `spent_micros`, `reserved_micros`, timestamps |
| `agent_mandates` | `(agent_id, mandate_id)` pk. An agent can have many mandates. |
| `reservations` | `request_id uuid pk`, `agent_id`, `mandate_id`, `amount_micros`, `status` ∈ {held, settled, released}, `cost_micros`, `created_at`, `settled_at`. Partial index `WHERE status='held'`. |
| `audit_events` | `id bigserial`, `at`, `kind` ∈ {request, settle, admin}, `actor`, `request_id`, `agent_id`, `mandate_id`, `model`, `decision`, `reason_code`, `policy_version`, `reserved_micros`, `cost_micros`, `cost_source` ∈ {litellm_header, reservation, none, late_ignored}, `prompt_tokens`, `completion_tokens`, `finish_reason`, `upstream_status`, `detail jsonb` (admin before/after). **No prompt or response content.** |
| `schema_migrations` | `version`, `applied_at` |

There is deliberately no `CHECK (spent ≤ limit)`: an actual cost must always be able to settle.

### Roles (one idempotent `config/db-init.sh`)
On a fresh volume it runs from `docker-entrypoint-initdb.d`. On the existing dev volume you run it once by hand. It reads role passwords from the db service env.

| Role | Rights |
|---|---|
| `gateway` (existing superuser) | Bootstrap only. Not used by any service after M1. |
| `litellm_app` | Owns db `litellm` and everything in it. Ownership is moved in place with a `DO` loop over the tables, views, sequence and enum in `public`, plus the schema and the database. No data loss, so the `researchos` key keeps working until M8. |
| `gateway_owner` | Owns db `gateway` and its tables. Used **only** by the one-shot Compose service `migrate`, the only place its password is passed. |
| `gateway_app` | `SELECT, INSERT, UPDATE` on the ledger tables; `SELECT, INSERT` only on `audit_events` (plus sequence `USAGE`). Cannot alter or delete history. |

All roles: `REVOKE CONNECT ON DATABASE litellm, gateway FROM PUBLIC`, then an explicit `GRANT CONNECT` to the right role.

The README says plainly: the audit log is append-only for the app role, **not tamper-proof** (the superuser can still edit).

**Migrations:** `gateway/migrations/NNN_*.sql`, applied by `python -m migrate` (about 15 lines, under an advisory lock) in the `migrate` service. `gateway` starts only after `migrate` completes (`depends_on: condition: service_completed_successfully`). No Alembic or SQLAlchemy.

---

## 4. Policy

`config/policy.toml` is mounted read-only and needs a restart to change. `policy_version = sha256(file)[:12]`.

```toml
# Evaluated top to bottom; first matching forbid wins; no match = permit.
[[forbid]]
reason = "AGENT_REVOKED";        when = { "agent.status" = "revoked" };   message = "Agent credential revoked"
[[forbid]]
reason = "AGENT_INACTIVE";       when = { "agent.status" = "suspended" }
[[forbid]]
reason = "MANDATE_NOT_ASSIGNED"; when = { "assignment.exists" = false }   # unknown mandate looks the same (no enumeration)
[[forbid]]
reason = "MANDATE_INACTIVE";     when = { "mandate.status" = "suspended" }
[[forbid]]
reason = "MODEL_NOT_ALLOWED";    when = { "model.allowed" = false }       # request.model ∈ mandate.allowed_models
```

- **Why a data file and not Python.** The requirement is "explicit, auditable policy rather than code branches". A data file can be reviewed without reading Python, cannot execute code, and has the same shape as the Cedar file that replaces it.
- **Evaluator.** About 25 lines. A rule matches when every `fact = value` pair in it is equal. If a fact name is unknown, the app refuses to start, because a typo must never silently fail to match. The decision is `{allow, reason, message, policy_version}`.
- **Budgets are not policy.** They are a ledger invariant enforced atomically (§5). Their reason codes `AGENT_BUDGET_EXCEEDED` / `MANDATE_BUDGET_EXCEEDED` come from the reserve step, and the decision has the same shape.
- **Kill-switch guard.** The reserve transaction re-asserts the three kill-switch facts in SQL (agent active, mandate active, assignment exists) so that they are linearized with admin changes. A unit test runs every combination of facts and asserts "policy permits ⇒ SQL guard passes" and the reverse.
- **Cedar later.** Each rule becomes `@id("AGENT_REVOKED") forbid(principal, action, resource) when {…}`, and only `policy.py` is swapped. Cedar is order-independent, so the reason code shown is picked by a fixed priority list.

---

## 5. Budgets

**Estimate** (`Decimal`, prices taken as `Decimal(str(p))` from LiteLLM `/model/info` at startup):
`est_micros = ceil( (len(outbound_body_bytes) × in_price + max_tokens × out_price) × 10^6 )`

- `max_tokens`: the client may send `max_tokens` **or** `max_completion_tokens`; sending both → 400. If neither is sent, default 8192; above 16384 → 400 (both are constants). Exactly one `max_tokens` is forwarded, and it is the value used in the estimate.
- Bytes overestimate input tokens about 4×. For ResearchOS, whose cost is about 97% input, that means a reservation of about $0.087 against an actual cost of about $0.014 (~6×). This is a deliberate conservative choice, and its effect is that a denial near the limit comes earlier than strictly necessary.
- Images or non-function tools → 400. Their cost cannot be bounded.

**Reserve.** One transaction. Lock order is always assignment → agent → mandate, so concurrent reserves cannot deadlock.
```sql
SELECT 1 FROM agent_mandates WHERE agent_id=$a AND mandate_id=$m FOR SHARE;   -- waits for/defeats concurrent unassign
UPDATE agents   SET reserved_micros = reserved_micros + $est
 WHERE id=$a AND status='active' AND spent_micros + reserved_micros + $est <= budget_limit_micros RETURNING 1;
UPDATE mandates SET reserved_micros = reserved_micros + $est
 WHERE id=$m AND status='active' AND spent_micros + reserved_micros + $est <= budget_limit_micros RETURNING 1;
INSERT INTO reservations (…,'held');  INSERT INTO audit_events (… 'allow' …);
COMMIT;
```
- If any step returns 0 rows: `ROLLBACK`, re-read the facts to pick the reason code (status or assignment change → that code, otherwise the budget code), then write the deny audit row **after** the rollback in its own statement.
- Why this is linearized: the UPDATEs re-check `status` on the newest row version, and an admin PATCH on the same row waits for, or is waited on by, the reserve. `FOR SHARE` makes an unassign and a reserve serialize. A plain `EXISTS` subquery would not: in READ COMMITTED it reads the statement's old snapshot.

**Settle or release.** One transaction. The ledger is updated only if the first statement returned a row.
```sql
UPDATE reservations SET status=$s, cost_micros=$c, settled_at=now()
 WHERE request_id=$r AND status='held' RETURNING agent_id, mandate_id, amount_micros;
-- if 1 row: agents/mandates  reserved -= amount, spent += $c ; INSERT audit
-- if 0 rows (already swept): INSERT audit cost_source='late_ignored' with the actual cost; no ledger change
```

| Outcome | Action | `cost_source` |
|---|---|---|
| 2xx and the header is a finite value > 0 | settle at `ceil(Decimal(header) × 10^6)` | `litellm_header` |
| 2xx and the header is missing, ≤ 0 or non-finite while `usage.total_tokens > 0` | settle at the reserved amount | `reservation` |
| LiteLLM 408 or 504, gateway read timeout, connection dropped after sending (**outcome unknown**) | settle at the reserved amount | `reservation` |
| Other 4xx/5xx including 429; connect error (request never sent) | release | `none` |

- **Timeouts are ordered** so that the inner layer always gives up first: LiteLLM 240 s < gateway 270 s < ResearchOS `LLM_TIMEOUT_SECONDS=300`. The live maximum call duration is 103 s.
- **Sweeper.** At startup it settles **all** `held` rows at their reserved amount (`# ponytail: single replica; add instance id if scaled`). After that it runs every 60 s on rows older than 270 s + 5 min. Each reservation is settled in its own transaction.
- **Overshoot.** Once the outbound body is under our control, the only remaining source is a wrong price or Gemini thinking tokens beyond `max_tokens`. The bound is (in-flight requests) × (estimate error). Spent may exceed the limit by that much, and every later request is denied. The `finish_reason` and token counts in the audit show it.
- **Budget period.** Lifetime ceilings; the admin raises or lowers the limit.

---

## 6. API surface

| Route | Auth | Notes |
|---|---|---|
| `POST /m/{mandate}/v1/chat/completions` | agent Bearer | OpenAI-compatible; the mandate comes from the path |
| `GET /health/live` | none | process up |
| `GET /health/ready` | none | DB, LiteLLM `/health/liveliness`, prices loaded. Booleans only. |
| `POST /admin/agents`, `PATCH /admin/agents/{id}` (status, limit) | admin | audited with before/after |
| `POST /admin/agents/{id}/token` | admin | Issue or rotate. The token is returned once; only its sha256 is stored. |
| `POST /admin/mandates`, `PATCH /admin/mandates/{id}` (status, limit, allowed_models) | admin | Models are validated against the priced LiteLLM models. |
| `PUT` / `DELETE /admin/agents/{id}/mandates/{mid}` | admin | assign or unassign |
| `GET /admin/summary`, `GET /admin/audit?agent=&mandate=&limit=` | admin | read |

- **Agent token.** `gw_` + `secrets.token_urlsafe(32)`, found by sha256 lookup. With 256 bits of entropy a plain hash is enough and the lookup leaks nothing useful. No extra compare step is claimed as a security property.
- **Admin token.** `ADMIN_TOKEN` in `.env`, compared with `hmac.compare_digest` on **bytes**. A test lists all app routes and asserts that every route except `/health/*` and the agent route requires the admin token.
- **Body allowlist.** Top-level fields allowed: `model, messages, max_tokens | max_completion_tokens, temperature, top_p, stop, seed, tools, tool_choice, response_format, n (=1), stream (=false)`. Anything else → 400 `FIELD_NOT_ALLOWED`, so `api_base`, `api_key`, `user`, `metadata`, `fallbacks`, `extra_body`, `cache` are all rejected.
  - `tools[*].type` must be `"function"`.
  - Message `content` must be a string, null, or `text` parts.
  - Extra keys are allowed **inside** a message, because ResearchOS echoes provider-specific tool-call fields.
  - The body is parsed manually, so every validation error has our own 400 shape.
- **Status codes**, checked against `openai_compatible.py` and `agent.py`:

| Code | Meaning | How ResearchOS reacts |
|---|---|---|
| 401 | bad or missing token | fatal: run stops `LLM_REFUSED` |
| 402 | budget exceeded | fatal: run stops `LLM_REFUSED` |
| 403 | policy deny | fatal: run stops `LLM_REFUSED` |
| 400 / 413 | invalid request | fatal: run stops `LLM_FAILED` |
| LiteLLM 429 / 5xx | passed through with a rebuilt short body | retry, then switch to the next model |
| LiteLLM 401 / 403 / 422 | gateway misconfiguration or backstop hit; becomes **503** `UPSTREAM_UNAVAILABLE` | retry, then switch model |

  The full upstream error goes only to the gateway logs.

---

## 7. Kill switch and proof

- **Actions.** `PATCH` on the agent status (suspend or revoke), the mandate status (suspend), `budget_limit_micros` (0 = withdraw), and unassign. "Restore" means setting the value back. Every action is audited.
- **Guarantee.** Every request not yet reserved when the admin change commits is denied before any upstream call. Requests that are already reserved (in flight) finish and are charged; they are not cancelled. The state lives in Postgres, so it survives restarts.
- **Proof 1: in-process** (`tests/test_gateway.py`). Real Postgres; LiteLLM is replaced by an `httpx.MockTransport` that counts calls. For each of revoke, suspend agent, suspend mandate, unassign and withdraw: request denied with the right reason, counter unchanged, audit row present. After restore, the counter goes up.
- **Concurrency.** The mock blocks on an `asyncio.Event`, and the test fires 50 identical requests. At the barrier it asserts admitted == ⌊limit/est⌋ and `reserved ≤ limit`. Then it releases the barrier and asserts that `spent` equals the sum of the audited costs.
- **Proof 2: e2e** (`tests/e2e_killswitch.py`, stdlib, run on the host). It uses Compose project `aigw-test` (separate volumes and port). The chain is gateway → **real LiteLLM** (`config/litellm.test.yaml`: `mock-model` → `openai/` with `api_base: http://mock-upstream:9000/v1` and explicit prices) → `tests/mock_upstream.py` (stdlib, with a call counter). The test LiteLLM key has a fixed value `sk-test-…`. The script prints a narrative for **two agents**, A and B:
  1. A and B both get answers.
  2. A's budget is withdrawn: A gets 402, B still gets answers, and the counter goes up only for B.
  3. A is revoked: 403 `AGENT_REVOKED`.
  4. While `docker compose restart gateway` is running, a request fails closed (connection error or 503) and the counter is unchanged.
  5. After the restart A is still denied.
  6. After waiting ~15 s for the SpendLogs flush: allowed request ids are **in** `LiteLLM_SpendLogs`, denied ids are **not**.
  7. Restore: A works again.
  8. Security checks: LiteLLM is not published (`docker compose -f docker-compose.yml config`), no `.env*`/`.secrets` tracked in git, no secret env var names inside the gateway container.
- **Out of scope, documented:** cancelling in-flight requests. LiteLLM retries are 0, and caching is off and not allowlisted.

---

## 8. Rate limits and fallbacks (free-tier Gemini)

- The free tier allows 5 requests/min and 20 requests/day **per model**, so real ResearchOS use needs a paid key. Automated tests never use Gemini. One opt-in live call (`tests/live_gemini.sh`) checks the real path, including `finish_reason` with the default `max_tokens`.
- **Client-side fallbacks.** The ResearchOS mandate allows the 4 aliases, and `LLM_MODEL` lists them, the same as before the gateway. Every model that runs is explicitly authorized and priced.
  - 429 behaviour is unchanged for ResearchOS, because LiteLLM already drops Google's `RetryInfo` today.
  - With `num_retries: 0`, ResearchOS's own 3 retries are the only ones, instead of 3×3.
  - Whether `gemini-flash-latest` has its own quota bucket is checked at M8. If it shares 3.8-flash's bucket, we swap it for `gemini-3.5-flash` (priced higher; the reservation covers it).
- **LiteLLM router fallbacks are deferred.** With them, the model that runs could differ from the model that was authorized and priced. Adding them later needs the reservation priced at the chain maximum and a policy check of every model in the chain.

---

## 9. Layout, dependencies, tests

```
gateway/Dockerfile               python:3.13-slim@digest, non-root; target "test" adds pytest
gateway/requirements.txt         pinned (+ frozen transitive)
gateway/app.py                   routes: agent, admin, health
gateway/db.py                    pool, reserve/settle/release/sweep
gateway/policy.py                tomllib load + evaluate
gateway/migrate.py               applies migrations/*.sql (owner role)
gateway/migrations/001_init.sql
config/policy.toml   config/db-init.sh   config/litellm.test.yaml
docker-compose.yml               + migrate, gateway (127.0.0.1:${GATEWAY_PORT:-8080}, explicit environment list)
docker-compose.test.yml          mock-upstream, test litellm config, test runner
scripts/gw_admin.py              stdlib; reads ADMIN_TOKEN from .env; --save-token writes a 0600 file under .secrets/
tests/test_policy.py  tests/test_gateway.py  tests/mock_upstream.py  tests/e2e_killswitch.py  tests/live_gemini.sh
```

- **Pins.** Each version was released at least 2 weeks ago according to PyPI:
  - `fastapi==0.141.1`
  - `uvicorn==0.53.0`
  - `httpx==0.28.1`
  - `asyncpg==0.31.0` (has cp313 manylinux wheels)
  - `pytest==9.1.1`, with anyio's pytest plugin (no pytest-asyncio)
- **Where things run.** The app and the test suites run in the container. The host runs only stdlib scripts.

| Suite | Where | Hits |
|---|---|---|
| `test_policy.py` | test container | nothing |
| `test_gateway.py` (integration and security: missing or invalid token, impersonation, unassigned or unknown mandate, model, `api_base`/`fallbacks`/`metadata`/both max_tokens fields/built-in tools, revoked, suspended, budget, route enumeration, concurrency) | test container in `aigw-test` | Postgres; LiteLLM is mocked |
| `e2e_killswitch.py` | host | `aigw-test` stack: real LiteLLM and the mock upstream |
| `live_gemini.sh` (opt-in) | host | one real Gemini call through the gateway |

---

## 10. Milestones

Each milestone is one commit with no AI attribution, made only after your OK. I explain each file before I write it.

| # | Builds | Files | You verify | Done when |
|---|---|---|---|---|
| **M1** LiteLLM hardening | DB roles plus in-place ownership move for LiteLLM; local price map; `num_retries: 0`; `request_timeout: 240`; 4 aliases; key `gateway`; block the 3 leftover non-ResearchOS keys; `gen-env.sh --add-missing` | `config/db-init.sh`, `config/litellm.yaml`, `docker-compose.yml`, `.env.example`, `scripts/gen-env.sh`, `.gitignore` (`.secrets/`) | `scripts/gen-env.sh --add-missing`; `docker compose exec -T db bash < config/db-init.sh`; `docker compose up -d`; `docker exec ai-agent-gateway-db-1 psql -U gateway -c '\du'`; `/model/info` shows 4 priced aliases; `scripts/smoke-test.sh` | LiteLLM connects as `litellm_app` (not superuser); ResearchOS still works through :4000; the leftover keys are blocked |
| **M2** Gateway skeleton | migrate service, schema, health checks | `gateway/{Dockerfile,requirements.txt,app.py,db.py,migrate.py,migrations/001_init.sql}`, compose | `docker compose up -d --build`; `curl -s 127.0.0.1:8080/health/ready`; `\dt` in db `gateway`; `docker compose exec gateway env \| grep -cE 'MASTER\|SALT\|GEMINI\|POSTGRES_PASSWORD\|OWNER'` → 0 | ready = `{db, litellm, prices}` all true; 6 tables owned by `gateway_owner` |
| **M3** Admin API | agents, mandates, assignments, tokens, admin audit | `app.py`, `db.py`, `scripts/gw_admin.py` | create `demo-a`, `demo`, assign, `--save-token .secrets/demo-a.token`; `GET /admin/audit`; `git status` does not show `.secrets` | every action is audited with before/after; no admin route answers without the token |
| **M4** Policy and gate | policy, body allowlist, outbound rebuild, forward, deny audit, header allowlist | `policy.py`, `config/policy.toml`, `app.py`, `tests/test_policy.py`, `tests/test_gateway.py` | `docker compose -p aigw-test -f docker-compose.yml -f docker-compose.test.yml run --rm tests`; a curl to an unassigned mandate → 403 with `reason` and `policy_version` | all reason codes and security cases pass; the mock counter stays 0 on every denial |
| **M5** Budgets | estimate, guarded reserve, settle/release, sweeper, error mapping | `db.py`, `app.py`, tests | test run; `GET /admin/summary` | barrier concurrency test passes; spent equals the sum of audited costs; the guard-equivalence test passes |
| **M6** Kill-switch proof and demo | test stack, mock upstream, 2-agent e2e narrative | `docker-compose.test.yml`, `config/litellm.test.yaml`, `tests/mock_upstream.py`, `tests/e2e_killswitch.py` | `python3 tests/e2e_killswitch.py` | every step prints PASS with the counter values; SpendLogs cross-check passes |
| **M7** One live call (opt-in) | live smoke | `tests/live_gemini.sh` | run it when quota is available | 200, a `litellm_header` cost, a sensible `finish_reason` |
| **M8** Migrate ResearchOS (config only, with your OK) | agent `researchos`, mandate `researchos-dev` with the 4 models; ResearchOS `.env`: `LLM_BASE_URL=http://127.0.0.1:8080/m/researchos-dev/v1`, `LLM_API_KEY` from `.secrets/researchos.token`, the 4-model `LLM_MODEL`, `LLM_TIMEOUT_SECONDS=300`. Then block LiteLLM key `researchos`, delete `.env.researchos`, stop using the debug overlay. | ResearchOS `.env` only; README | a small ResearchOS run; `GET /admin/audit?agent=researchos` (check `finish_reason`, costs); suspend mid-run → the run ends `LLM_REFUSED`; `curl 127.0.0.1:4000` refused | ResearchOS works only through the gate, and a suspension halts it at its next call |
| **M9** (optional) Dashboard | `gateway/dashboard.html` (vanilla JS; admin token kept in `sessionStorage`): summary, recent audit, suspend/restore buttons | `dashboard.html`, one route | open `127.0.0.1:8080/admin/ui` | it uses only the admin API |
| **M10** Docs | README: architecture, run, verify, policy format, threat model, known limitations | `README.md` | read it | the limitations below are listed |

---

## 11. Limitations, risks, open questions

**Known limitations (in the README)**
- In-flight requests are not cancelled.
- Overshoot is bounded by in-flight requests × estimate error.
- Input is over-reserved about 6× for long prompts.
- Unknown outcomes are charged at the reserved amount.
- No streaming, images or Gemini built-in tools.
- Lifetime budgets only.
- The audit log is append-only for the app role but not tamper-proof.
- One shared admin token.
- No TLS locally. Agents must never be attached to the `backend` network.
- The policy reloads only on restart.
- A single gateway replica (the startup sweep assumes it).

**Risks**
- Price data changes when the LiteLLM digest is bumped. The e2e test asserts the cost header.
- Whether Gemini 3.x thinking tokens are capped by `max_tokens` is unverified. M7/M8 look at `finish_reason` and completion tokens.
- The free-tier quota makes manual testing slow.

**Open questions (defaults in brackets)**
1. Should budgets be lifetime, or reset monthly? [lifetime + manual top-up]
2. Will there be a paid Gemini key before M8? [proceed on free tier]
3. Is the dashboard (M9) in scope for the demo? [yes, minimal]
