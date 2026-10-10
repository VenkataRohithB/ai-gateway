# AI Agent Gateway: MVP plan v1 (draft for review)

Scope: a working MVP up to and including the FastAPI authorization gate, with an optional small dashboard at the end.
Everything below was checked against the real repo and the pinned LiteLLM 1.104.0 image. Source paths are relative to
`/app/.venv/lib/python3.13/site-packages/litellm/` inside the image.

---

## 0. TL;DR (decisions)

| Topic | Decision |
|---|---|
| Building on LiteLLM | **Use LiteLLM unmodified, as a dependency we configure.** No fork, no overlay image, no in-proxy hooks. Our code is a separate FastAPI service in front of it (option a). |
| Hop FastAPI → LiteLLM | **One LiteLLM virtual key for the gateway** (`gateway`): not the master key, limited to the configured models, with a global `max_budget` as a backstop. Agent/mandate attribution is sent as metadata. |
| Who enforces what | FastAPI handles identity, policy, per-agent and per-mandate budgets, kill switch and audit. LiteLLM handles provider adapters, the price table, cost per call, retries and spend logs (as a second record). |
| Money | `BIGINT` micro-USD (1 = $0.000001) everywhere in our DB. Reservations are made with one atomic conditional `UPDATE`. |
| Policy | `config/policy.toml` (parsed with stdlib `tomllib`) plus an evaluator of about 40 lines in Python. The version is the sha256 of the file. Rules are written as forbid rules so they map one-to-one to Cedar later. |
| DB | The gateway owns db `gateway` through its own least-privilege role. Migrations are plain numbered `.sql` files plus a runner of about 15 lines (no Alembic or SQLAlchemy). The driver is `asyncpg`. |
| Streaming | **Out of the MVP.** A request with `stream=true` gets 400. ResearchOS does not stream. |
| Fallbacks | **Client-side for the MVP** (ResearchOS keeps a list of 4 models). Each model in the list is authorized and priced on its own. Gateway-side router fallbacks are deferred (reason in §8). |
| Kill switch proof | Two layers. (1) In-process: FastAPI with LiteLLM replaced by `httpx.MockTransport` that counts calls. (2) End-to-end: Compose test stack with **real LiteLLM** in front of a stdlib mock OpenAI server that counts calls, including a gateway restart. |
| Dashboard | Optional final milestone: one static `dashboard.html` served by FastAPI, read-only tables plus suspend/restore buttons. No SPA, no Jinja. |
| Container Python | `python:3.13-slim` (the same minor version LiteLLM ships; asyncpg 0.31.0 has cp313 wheels). |

---

## 1. Architecture: "build on top of LiteLLM, fork-kind but not full fork"

### What LiteLLM 1.104.0 actually does (verified in source)

| Question | Finding | Evidence |
|---|---|---|
| Is the key `max_budget` checked before the call? | Yes, but it is **check-then-act**: `spend >= max_budget`. It reserves nothing. N concurrent requests at $max−ε all pass. | `proxy/auth/auth_checks.py::_virtual_key_max_budget_check` (L5402–5475) |
| Where does spend come from? | An in-memory or Redis counter that is updated **after** the call. The DB is written in batches every `PROXY_BATCH_WRITE_AT` = 10 s. | `proxy/proxy_server.py::get_current_spend` (L2742); `constants.py` L1819; `proxy/db/db_spend_update_writer.py::DBSpendUpdateWriter` |
| Spend as money type | `float` USD | same |
| Cost per call | `x-litellm-response-cost` response header (non-streaming), computed from the model price map | `proxy/common_request_processing.py::get_custom_headers` L1769 |
| Prices available for our models | Yes, for all 7 Gemini models the key can use (e.g. gemini-3.8-flash: $0.75/M input, $3.75/M output, max_output 65 536). They are exposed via `GET /model/info`. | `litellm.model_cost`; live `/model/info` checked |
| Correlating our request id | The `x-litellm-call-id` request header is accepted verbatim. It lands in `LiteLLM_SpendLogs.litellm_call_id`. | `common_request_processing.py::resolve_litellm_call_id` L266, L2124 |
| Attribution metadata | The `x-litellm-spend-logs-metadata` header (JSON) is stored in the SpendLogs metadata. Not enterprise-gated. | `proxy/litellm_pre_call_utils.py` L1301–1309, L1542 |
| Client routing overrides | LiteLLM already rejects `api_base`, `base_url`, `vertex_credentials`, … with a **blocklist**, and it also inspects request-body `fallbacks`. | `proxy/auth/auth_utils.py::is_request_body_safe` L475, `_BANNED_REQUEST_BODY_PARAMS` L306, `iter_request_fallback_targets` L456 |
| Router retries | The default is `openai.DEFAULT_MAX_RETRIES` (2). LiteLLM does not parse Gemini `RetryInfo`. It only sets `retry-after` on a router cooldown. | `router.py` L1056–1060; `common_request_processing.py` L3608 |
| Its own "policy engine" | This is for **guardrails** (PII, toxicity), scoped to teams and keys. It is not an access-control or budget policy. | `proxy/policy_engine/__init__.py` docstring |
| Enterprise-gated features we must avoid | Key `tags`, per-key `model_max_budget`, `enforced_params`, some spend-report endpoints, OAuth2 auth. **We need none of them.** | `management_endpoints/key_management_endpoints.py` L1425, L7837; `litellm_pre_call_utils.py` L2933; `spend_tracking/spend_management_endpoints.py` L1488, L1803 |
| Size and churn | About 855k lines of Python. `proxy_server.py` alone is 20k lines. The source cites multiple GHSA advisories. | `wc` over the package |

Conclusion: LiteLLM's budgets are good dashboards plus a soft ceiling. They **cannot be the hard ceiling** the manager asked for: they have no reservation, they are not race-free, and spend is float.

### The four options

| Option | What it is | Pros | Cons | Verdict |
|---|---|---|---|---|
| **(a) Separate FastAPI service in front** | Our service of about 600 lines. LiteLLM is internal-only. | The gate is a small, readable trust boundary that we can test in isolation, including the "never forwarded" proof with a mock. LiteLLM upgrades are a digest bump. LiteLLM's attack surface is not exposed to agents. This is also the spec's default. | One extra HTTP hop (~1–3 ms locally). Per-agent and per-mandate attribution must be passed as metadata. | **Chosen** |
| (b) In-proxy extension points | `general_settings.custom_auth: module.fn` is loaded from a `.py` **next to config.yaml**, so no image rebuild is needed (`proxy/types_utils/utils.py::get_instance_fn`). Also `CustomLogger.async_pre_call_hook` / `async_post_call_success_hook` / `async_post_call_failure_hook` (`integrations/custom_logger.py` L413–507). | No extra hop. Hooks see the final cost. | `custom_auth` returns early and **skips all of LiteLLM's built-in checks** unless `enable_post_custom_auth_checks` is set (`user_api_key_auth.py` L1562–1572). Our money logic would run in LiteLLM's event loop and depend on internal types (`UserAPIKeyAuth`) that change every release. The kill-switch proof would have to start the whole LiteLLM process. Agents would talk directly to a 20k-line server. | Rejected for the MVP. **Kept as an escape hatch**: if we ever need streaming-cost settlement inside the proxy, a single mounted hook file is enough. |
| (c) Thin overlay image `FROM litellm@digest` + our package/patches | Rebuilds LiteLLM with our code or patches baked in. | Can patch a bug. | Patch files rot on every upgrade, and we would own a custom image of a security-sensitive proxy. Nothing we need requires it, because (b) already loads code from the config dir. | Rejected (YAGNI) |
| (d) Git fork | Our own copy of LiteLLM. | Total control. | Merging ~weekly security fixes into an 855k-line codebase. Nothing we need justifies it. | Rejected |

So "fork-kind" here means: **LiteLLM is a pinned, unmodified dependency. We extend it only through its config file** (model list, router settings, a scoped virtual key), and our product logic lives in our own small service.

### LiteLLM features: reuse vs own

| Reuse from LiteLLM | Own in FastAPI |
|---|---|
| Provider adapters and multiple deployments/credentials (`config/litellm.yaml`, `os.environ/…`). Clients cannot choose them. | Agent identity (token hash) and the admin credential |
| Price table and per-call cost (`x-litellm-response-cost`, `/model/info`) | Policy, its reason codes and policy version |
| Retries on transient provider errors | Budgets per agent and per mandate with reservations (hard ceiling) |
| SpendLogs as an independent second record, cross-referenced by `litellm_call_id` = our `request_id` | Kill switch and the audit log |
| One virtual key `gateway`: restricts models, gives a global `max_budget` backstop and makes revocation possible on its side too | Body/header allowlist (defense in depth on top of LiteLLM's blocklist) |
| **Not used:** teams, per-agent virtual keys, LiteLLM UI, LiteLLM tags | |

**Single key vs one key per agent.** I chose a single key. Per-agent LiteLLM keys would create a second source of truth for revocation and budget, plus sync code, and LiteLLM's per-key budget is not a hard ceiling anyway. Attribution in LiteLLM's own logs still works through `x-litellm-spend-logs-metadata: {"agent":…, "mandate":…}` together with `x-litellm-call-id`. The gateway container receives only this key, **never the master key**.

---

## 2. Request flow (one request)

```
Agent ──Bearer gw_…──▶ FastAPI :8080
  1. auth: sha256(token) → agents row; hmac.compare_digest       (401 if no/bad token)
  2. parse body against a strict top-level allowlist             (400 on unknown field / stream=true / n≠1)
  3. mandate from URL path /m/{mandate}/v1/… or X-Mandate header (400 if missing or the two disagree)
  4. load facts from DB (no cache) → policy.evaluate()           (403 + reason, audited)
  5. estimate worst-case cost → atomic reserve (agent+mandate)   (402 + reason, audited)
  6. POST LiteLLM /chat/completions with gateway key, x-litellm-call-id=request_id, metadata header;
     NO client headers forwarded
  7. settle: actual = x-litellm-response-cost (fallback: reservation amount) → audit
  8. return the LiteLLM JSON body + x-request-id. Internal x-litellm-* headers are stripped (they leak key spend).
```

Step 3 and ResearchOS: ResearchOS sends only `Authorization`, so it cannot add a header without a code change. With the path form, mandate selection is pure config: `LLM_BASE_URL=http://127.0.0.1:8080/m/researchos-dev/v1`. It is still explicit and validated.

---

## 3. Data model (db `gateway`, role `gateway_app`)

| Table | Columns (key ones) |
|---|---|
| `agents` | `id text pk` (slug `^[a-z0-9-]{1,64}$`), `name`, `status` ∈ {active, suspended, revoked}, `token_sha256 bytea unique`, `budget_limit_micros bigint ≥0`, `spent_micros bigint`, `reserved_micros bigint ≥0`, timestamps |
| `mandates` | `id text pk`, `name`, `status` ∈ {active, suspended}, `allowed_models text[]`, `budget_limit_micros`, `spent_micros`, `reserved_micros`, timestamps |
| `agent_mandates` | `(agent_id, mandate_id)` pk, FKs. An agent can have many mandates. |
| `reservations` | `request_id uuid pk`, `agent_id`, `mandate_id`, `amount_micros`, `status` ∈ {held, settled, released}, `cost_micros`, `created_at`, `settled_at`. Partial index on `created_at WHERE status='held'`. |
| `audit_events` | `id bigserial`, `at`, `kind` ∈ {request, settle, admin}, `actor`, `request_id`, `agent_id`, `mandate_id`, `model`, `served_model`, `decision`, `reason_code`, `policy_version`, `reserved_micros`, `cost_micros`, `cost_source` ∈ {litellm_header, reservation, none}, `prompt_tokens`, `completion_tokens`, `upstream_status`, `detail jsonb` (admin before/after; **never prompt content**) |
| `schema_migrations` | `version text pk`, `applied_at` |

- **Ownership.** A one-time bootstrap SQL creates the role `gateway_app` (password `GATEWAY_DB_PASSWORD` in `.env`). The role owns db `gateway` and gets `REVOKE CONNECT ON DATABASE litellm`. On `audit_events` it has only `INSERT, SELECT`, so the app role cannot edit history. This is append-only, **not tamper-proof**: the superuser can still edit, and the docs will say so.
- **Migrations.** Files `gateway/migrations/001_init.sql`, … At startup the app takes `pg_advisory_lock` and applies any versions not yet recorded in `schema_migrations`, each in its own transaction. That is about 15 lines. Alembic would add SQLAlchemy for no benefit.
- The existing volume is already initialized, so `postgres-init.sql` will not re-run. The role is created by `scripts/db-bootstrap.sql`, which the user runs once via `docker compose exec db psql` (shown in M1).

---

## 4. Policy

`config/policy.toml` is read-only, loaded at startup, and needs a restart to change. `policy_version = sha256(file)[:12]`, which never needs a manual bump; the git log maps it back to a commit.

```toml
# Evaluated top to bottom; first matching forbid wins. No match = permit.
max_tokens_default = 8192        # injected when the client sends none (bounds the reservation)
max_tokens_cap     = 16384       # above this → 400 MAX_TOKENS_TOO_HIGH

[[forbid]]
reason = "AGENT_REVOKED";        when = { "agent.status" = "revoked" }
message = "Agent credential has been revoked"
[[forbid]]
reason = "AGENT_INACTIVE";       when = { "agent.status" = "suspended" }
[[forbid]]
reason = "MANDATE_NOT_ASSIGNED"; when = { "assignment.exists" = false }   # also covers unknown mandate (no enumeration)
[[forbid]]
reason = "MANDATE_INACTIVE";     when = { "mandate.status" = "suspended" }
[[forbid]]
reason = "MODEL_NOT_ALLOWED";    when = { "model.allowed" = false }       # request.model ∈ mandate.allowed_models
```

- **Evaluator** (`gateway/policy.py`): a rule matches when every `fact = value` pair in `when` is equal. It returns `Decision(allow, reason, message, policy_version)`. Unknown fact names make the file fail to load at startup (fail closed). There are no expressions and no `eval`.
- **Fact gathering**: one SQL query joins agent, mandate and assignment, plus one membership test in Python. Facts are always read fresh from the DB, with no cache, which is what makes revocation take effect on the next request.
- **Budgets are not in the policy file.** The policy says *who may call what*. The budget ledger says *how much*. A budget check is only correct as an atomic DB operation (§5), and an evaluator over a snapshot would reintroduce the race. The reason codes `AGENT_BUDGET_EXCEEDED` / `MANDATE_BUDGET_EXCEEDED` come from the reserve step, and the decision record has the same shape.
- **Cedar later**: each `[[forbid]]` becomes `forbid(principal, action == Action::"invoke", resource) when { … }` with an `@id("AGENT_REVOKED")` annotation, and the facts become entity attributes or context. Only `policy.py` is replaced. Two things change in that move. Cedar is order-independent, so we pick the first reason in a fixed priority list. And the policy version becomes the hash of the policy set.

---

## 5. Budgets: reservation, settlement, recovery

**Estimate (worst case, before the call):**
`est_micros = ceil( input_bytes × in_price + max_tokens × out_price ) × 1e6`

- `input_bytes = len(json(messages + tools))`. Bytes are an upper bound on tokens for text; real usage is about 4 bytes per token, so input is overestimated about 4×. Input is cheap, and the cost of the overestimate is a slightly early denial near the limit.
- Prices come from LiteLLM `GET /model/info` (using the gateway key), loaded at startup. A model without a price → readiness fails, and the admin API refuses that model in `allowed_models`.
- `max_tokens`: if absent, the policy default is injected; if above the cap, 400. Example: 8192 × $3.75/M ≈ $0.031 reserved per call.
- Image or audio content parts → 400 `UNSUPPORTED_CONTENT` in the MVP (no reliable estimate).

**Reserve (one transaction; locks always taken agent first, then mandate, so concurrent requests cannot deadlock):**
```sql
UPDATE agents   SET reserved_micros = reserved_micros + $est
 WHERE id = $agent   AND spent_micros + reserved_micros + $est <= budget_limit_micros RETURNING 1;
-- 0 rows → ROLLBACK, deny AGENT_BUDGET_EXCEEDED
UPDATE mandates SET reserved_micros = reserved_micros + $est
 WHERE id = $mandate AND spent_micros + reserved_micros + $est <= budget_limit_micros RETURNING 1;
-- 0 rows → ROLLBACK, deny MANDATE_BUDGET_EXCEEDED
INSERT INTO reservations (request_id, agent_id, mandate_id, amount_micros, status) VALUES (…, 'held');
INSERT INTO audit_events (... decision='allow' ...);
COMMIT;
```
The row lock taken by `UPDATE` serializes concurrent reservations, so the invariant `spent + reserved ≤ limit` holds at READ COMMITTED. A test fires 50 parallel requests and asserts that exactly ⌊limit/est⌋ are admitted.

**Settle or release.** The settle statement is idempotent because of `WHERE status='held'`:
```sql
UPDATE reservations SET status=$s, cost_micros=$c, settled_at=now()
 WHERE request_id=$r AND status='held' RETURNING agent_id, mandate_id, amount_micros;
UPDATE agents   SET reserved_micros = reserved_micros - $amt, spent_micros = spent_micros + $c WHERE id=$a;
UPDATE mandates SET reserved_micros = reserved_micros - $amt, spent_micros = spent_micros + $c WHERE id=$m;
INSERT INTO audit_events (kind='settle', …);
```

| What happened on the LiteLLM call | Action | `cost_source` |
|---|---|---|
| 2xx with `x-litellm-response-cost` | settle at `ceil(Decimal(header) × 1e6)` | `litellm_header` |
| 2xx without the header | settle at the reservation amount | `reservation` |
| Any non-2xx HTTP response from LiteLLM (4xx, 429, 5xx) | release (cost 0). LiteLLM returned no completion, and providers do not bill failed calls. | `none` |
| Connect error (request never sent) | release | `none` |
| Read timeout or connection dropped after the request was sent (**unknown outcome**) | settle at the reservation amount (conservative) | `reservation` |

- **Crash recovery.** A sweeper loop in the app runs every 60 s and at startup. It settles any `held` reservation older than `upstream_timeout + 5 min` **at its reserved amount** (conservative, audited). Upgrade path, recorded as a comment in the code: look up `LiteLLM_SpendLogs.litellm_call_id` for the real cost, which needs read access to db `litellm`.
- **Overshoot.** The actual cost can exceed the estimate, for example if Gemini thinking tokens are not bounded by `max_tokens`. We always record the actual amount, so `spent` can exceed `limit` by at most one request's estimate error, and every later request is denied. This is documented, not hidden.
- **Retries.** LiteLLM's internal retries are invisible to us. Only the final success is billed, which is correct because failed attempts are not billed by the provider.
- **Budget period.** Lifetime ceilings for the MVP. The admin raises or lowers the limit. Periodic reset is an open question.

---

## 6. Kill switch semantics and the proof

- **Actions** (all through `PATCH`, all audited with before/after):
  - agent `status`: active / suspended / revoked
  - mandate `status`: active / suspended
  - `budget_limit_micros`, where withdrawing = set it to 0
  - restore = set the value back
- **Guarantee.** Every request whose policy check starts after the admin's commit is denied before any upstream call, because facts are read from the DB on every request and there is no cache. Requests already in flight finish and are charged; we do not cancel them (documented). After withdrawal the budget is enforced with up to one request of in-flight lag.
- **Persistence.** The state lives in Postgres, so a gateway restart changes nothing.

**Proof tests**

1. `tests/test_gateway.py` runs in-process against real Postgres, with LiteLLM replaced by an `httpx.MockTransport` that counts calls. Sequence: allowed request (count = 1) → revoke → request gets 403 `AGENT_REVOKED`, **count still 1**, and an audit row exists → same check for suspend mandate and withdraw budget → restore → count = 2.
2. `tests/e2e_killswitch.py` is a stdlib-only script run on the host. It uses the Compose test project `-p aigw-test`, which has separate volumes. The chain is: real gateway → **real LiteLLM** (`config/litellm.test.yaml` model `mock-model` → `openai/` with `api_base: http://mock-upstream:9000/v1` and explicit `input_cost_per_token`/`output_cost_per_token`) → `tests/mock_upstream.py`, a stdlib `http.server` of about 30 lines with `GET /calls` (internal; the script reads it via `docker compose exec`).
   - Steps: allowed → revoke → denied with `/calls` unchanged → `docker compose restart gateway` → still denied, `/calls` unchanged, audit rows present → restore → allowed.
   - This also checks that the cost header path works end to end against the pinned LiteLLM digest, which catches header renames on upgrade.

---

## 7. API surface

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /m/{mandate}/v1/chat/completions` | agent Bearer | OpenAI-compatible; the mandate comes from the path (works for any OpenAI client by config) |
| `POST /v1/chat/completions` + `X-Mandate: id` | agent Bearer | Same handler, header form |
| `GET /health/live` | none | process up |
| `GET /health/ready` | none | DB `SELECT 1`, LiteLLM `/health/liveliness`, policy loaded, prices loaded. Returns status booleans only. |
| `POST /admin/agents`, `PATCH /admin/agents/{id}` | admin | create, change status or limit |
| `POST /admin/agents/{id}/token` | admin | issue or rotate the token. **Returned once**; only the sha256 is stored. |
| `POST /admin/mandates`, `PATCH /admin/mandates/{id}` | admin | create, change status, limit, `allowed_models` |
| `PUT` / `DELETE /admin/agents/{id}/mandates/{mandate}` | admin | assign or unassign |
| `GET /admin/summary` | admin | agents and mandates with limit, spent, reserved |
| `GET /admin/audit?limit=&agent=&mandate=` | admin | recent audit events |

- **Tokens.** An agent token is `gw_` + 32 random bytes (`secrets.token_urlsafe`). It is looked up by sha256 and confirmed with `hmac.compare_digest`. sha256 is enough because the token has 256 bits of entropy, so a slow KDF is not needed.
- **Admin auth.** A separate `ADMIN_TOKEN` in `.env`, written by `gen-env.sh` and never printed, compared in constant time. Every `/admin/*` route depends on it, and a test enumerates the app's routes and asserts that none is unauthenticated except `/health/*`.
- **Admin helper.** `scripts/gw_admin.py` is stdlib, like `litellm_api.py`. It reads `ADMIN_TOKEN` from `.env` and, with `--save-token FILE`, writes a new agent token to a 0600 file instead of printing it.
- **Request body.** The top-level allowlist is: `model, messages, max_tokens, max_completion_tokens, temperature, top_p, stop, seed, tools, tool_choice, response_format, n(=1), stream(=false)`. Anything else gets 400 `FIELD_NOT_ALLOWED`, which covers `api_base`, `api_key`, `user`, `metadata`, `fallbacks`, `extra_body` and friends. Inside a message, extra keys are allowed, because ResearchOS echoes provider-specific tool-call fields (`openai_compatible.py::_encode_message` merges `provider_data`).
- **Request headers.** No client header is forwarded, so `x-litellm-*` sent by a client are dropped.
- **Status codes, chosen for ResearchOS** (`openai_compatible.py`: 401/402/403 → `LLMAuthorizationError`, fatal; 429/5xx → retry, then switch model):
  - 401 for a missing or bad token
  - 403 for a policy deny
  - 402 for budget exceeded
  - 400 for validation
  - LiteLLM's status is passed through with its body. On a 429 we also pass `retry-after` if LiteLLM set it.

So a kill switch **stops ResearchOS on its next request** (fatal error) instead of making it retry.

---

## 8. Rate limits and fallbacks (free-tier Gemini)

- Free tier: 5 requests/min and 20 requests/day **per model**. Real usage of ResearchOS needs a **paid key**. The plan does not depend on one, because every automated test uses the mock upstream.
- **MVP: client-side fallback.** `config/litellm.yaml` gains aliases for 4 models: gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash and gemini-flash-latest, each with LiteLLM pricing. The ResearchOS mandate allows all 4, and ResearchOS sets `LLM_MODEL=gemini-3.8-flash,gemini-3.7-flash,…`, exactly as it did before the gateway.
  - Gemini's 429 reaches ResearchOS through LiteLLM and the gateway. LiteLLM does not forward Google's `RetryInfo`, so ResearchOS uses exponential backoff for `max_retries` attempts, then switches model. That is slower than before but correct.
  - Every model that actually runs was explicitly authorized and priced.
- **Why not LiteLLM `router_settings.fallbacks` now.** The model that runs would differ from the model we authorized and priced. That forces "reserve at the chain's maximum price" and "the policy must allow the whole chain", which adds code for a convenience. It can be added later as config plus about 10 lines, recording `served_model` from the response (already a column).
- Note: LiteLLM's default of 2 internal retries plus ResearchOS's retries multiplies the attempts made against a 5 requests/min quota. We may set `router_settings.num_retries: 0` and let the client own retries. To verify in M7.

---

## 9. Testing, layout, dependencies

**Layout** (about 8 new files):
```
gateway/Dockerfile            python:3.13-slim (digest-pinned at build time), non-root user; stage "test" adds pytest
gateway/requirements.txt      top-level pins + frozen transitive pins
gateway/app.py                routes: agent, admin, health (+ dashboard file route in M8)
gateway/db.py                 pool, migration runner, reserve/settle/release/sweep SQL
gateway/policy.py             load toml + evaluate
gateway/migrations/001_init.sql
config/policy.toml
config/litellm.test.yaml      mock-model only
docker-compose.yml            + gateway service (127.0.0.1:${GATEWAY_PORT:-8080})
docker-compose.test.yml       mock-upstream, litellm.test.yaml mount, test target
scripts/db-bootstrap.sql, scripts/gw_admin.py
tests/test_policy.py  tests/test_gateway.py  tests/mock_upstream.py  tests/e2e_killswitch.py  tests/live_gemini.sh
```

**Pins** (each at least 2 weeks old as of 2026-10-10, checked on PyPI): `fastapi==0.141.1` (2026-07-29), `uvicorn==0.53.0` (2026-09-14), `httpx==0.28.1`, `asyncpg==0.31.0` (has cp313 manylinux wheels). Test only: `pytest==9.1.1`. We use anyio's pytest plugin, which comes with Starlette, so pytest-asyncio is not needed. Host Python 3.14 is not used for the app; all Python runs in the container except the stdlib-only scripts.

| Suite | Runs where | Hits |
|---|---|---|
| `test_policy.py` (unit) | test container | nothing |
| `test_gateway.py` (integration and security) | test container on the `backend` net | Postgres db `gateway_test`; LiteLLM is mocked |
| `e2e_killswitch.py` | host, stdlib | Compose test project: real LiteLLM and mock upstream |
| `live_gemini.sh` (opt-in) | host | **one** real Gemini call through the gateway |

The security cases in `test_gateway.py` are:
- missing or invalid token
- an agent token used on another agent's mandate (impersonation)
- unassigned or unknown mandate
- disallowed model
- `api_base`/`api_key`/`fallbacks`/`metadata` in the body
- revoked, suspended, budget exhausted
- route enumeration (all admin routes need the admin token)
- 50-way concurrent budget race

The checks that `LiteLLM is not reachable from host` (`curl 127.0.0.1:4000` fails and `docker compose port litellm 4000` is empty) and `no secrets in git` (`git ls-files` contains no `.env*` except the example, plus a grep for key patterns) are in `e2e_killswitch.py`.

---

## 10. Milestones

Each milestone is one commit (no AI attribution) after you OK it. I explain each file before writing it.

| # | Builds | Files | You verify | Done when |
|---|---|---|---|---|
| **M1** | Gateway skeleton, health checks, DB role, migrations | `gateway/{Dockerfile,requirements.txt,app.py,db.py,migrations/001_init.sql}`, `scripts/db-bootstrap.sql`, compose + `.env.example` + `gen-env.sh` (`ADMIN_TOKEN`, `GATEWAY_DB_PASSWORD`, `LITELLM_GATEWAY_KEY`) | `docker compose exec -T db psql -U gateway -f - < scripts/db-bootstrap.sql`; `docker compose up -d --build`; `curl -s 127.0.0.1:8080/health/ready`; `docker compose exec db psql -U gateway -d gateway -c '\dt'` | ready = all true; 6 tables exist; LiteLLM is still not published |
| **M2** | LiteLLM side: 4 model aliases; `gateway` virtual key (models = the 4, `max_budget` backstop); old test keys blocked | `config/litellm.yaml`, README | `scripts/litellm_api.py GET /model/info` (filtered); `GET /key/info` | the 4 aliases are priced; the gateway key works; the master key is absent from the gateway container (`docker compose exec gateway env \| grep -c MASTER` → 0) |
| **M3** | Admin API, agent tokens, admin audit | `app.py`, `db.py`, `scripts/gw_admin.py` | create agent `demo-a`, mandate `demo`, assign, issue token to `.secrets/demo-a.token` (0600); `GET /admin/audit` | every admin action has an audit row with before/after; no admin route answers without the token |
| **M4** | Policy and the agent endpoint (forward plus deny path, body/header allowlist) | `policy.py`, `config/policy.toml`, `app.py`, `tests/test_policy.py`, `tests/test_gateway.py` (part) | `docker compose -f … -f docker-compose.test.yml run --rm tests`; curl an unassigned mandate → 403 `MANDATE_NOT_ASSIGNED` with `policy_version` | all reason codes are covered; MockTransport count stays 0 on denials |
| **M5** | Budgets: estimate, reserve, settle, release, sweeper | `db.py`, `app.py`, tests | test output; `GET /admin/summary` shows spent and reserved | concurrency test passes; spent equals the sum of the audit costs |
| **M6** | Kill-switch e2e proof plus security checks | `docker-compose.test.yml`, `config/litellm.test.yaml`, `tests/mock_upstream.py`, `tests/e2e_killswitch.py` | `python3 tests/e2e_killswitch.py` prints each step with the upstream call count | count unchanged across revoke, suspend, withdraw and restart; audit rows present |
| **M7** | Demo with 2 simulated agents (mock upstream; optional single real Gemini call via `tests/live_gemini.sh`) | `scripts/demo_agents.py` (stdlib) | run it: agent A runs out of budget mid-loop and gets a 402, agent B continues; summary table | output matches the narrative in the README |
| **M8** | **Migrate ResearchOS** (config only) | No ResearchOS code. With your OK, its `.env`: `LLM_BASE_URL=http://127.0.0.1:8080/m/researchos-dev/v1`, `LLM_API_KEY=<gw token>`, `LLM_MODEL=<4-model list>`. Then block LiteLLM key `researchos` and stop using `docker-compose.debug.yml`. | run one small ResearchOS task; `GET /admin/audit?agent=researchos`; suspend the agent mid-run → ResearchOS stops with an authorization error | ResearchOS works through the gate, and a suspension halts it at its next call |
| **M9** (optional) | Dashboard: `gateway/dashboard.html` (vanilla JS; the admin token is pasted and kept in `sessionStorage`) showing summary, recent audit and suspend/restore buttons | `dashboard.html`, one route in `app.py` | open `127.0.0.1:8080/admin/ui` | read-only views plus status toggles; all calls go through the same admin API |
| **M10** | Docs: README (architecture, run, verify, threat model, policy format, **known limitations**). The README is the only doc file. | `README.md` | read it | limitations listed (below) |

---

## 11. Risks, known limitations, open questions

**Known limitations (will be in the README)**
- In-flight requests are not cancelled by the kill switch.
- `spent` may overshoot a limit by one request's estimate error.
- Cost accuracy depends on LiteLLM's price table.
- Unknown-outcome failures are charged at the reserved amount.
- No streaming, no image input, and lifetime budgets only.
- The audit log is append-only for the app role but **not tamper-proof**.
- A single shared admin token, with no per-admin identity.
- There is no TLS locally; anything on the Docker `backend` network that holds the gateway key could reach LiteLLM directly. Agents must never run on that network.
- The policy reloads only on restart.

**Risks**
- A LiteLLM upgrade could change header names or price data. Mitigations: digest pin, and the e2e test asserts the cost header.
- On the free tier, Gemini's daily quota makes manual testing flaky. Mitigation: mocks.
- Gemini thinking tokens vs `max_tokens`: to be measured in M7 with the single live call.

**Open questions for you (defaults in brackets)**
1. Should budgets be lifetime, or reset monthly? [lifetime + manual top-up]
2. Will we get a paid Gemini key before the ResearchOS migration (M8)? [proceed on free tier with the 4-model list]
3. Is the dashboard (M9) in scope for the MVP demo? [yes, minimal]
4. Do you need a budget per (agent, mandate) pair in addition to per-agent and per-mandate? [no]
