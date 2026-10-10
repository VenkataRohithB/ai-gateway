# Review of plan_v1.md

Reviewer method: read the plan, the repo (/home/rohith/Desktop/atria/ai_gateway), ResearchOS `src/researchos/llm/openai_compatible.py`, `agent.py`, `config.py`. Read LiteLLM 1.104.0 source in the pinned image (`/app/.venv/lib/python3.13/site-packages/litellm/`, abbreviated `LL/`). Ran read-only SELECTs on the live db. Made no Gemini calls and printed no secrets.

## A. LiteLLM claims: verification table

| # | Plan claim | Verdict | Evidence |
|---|---|---|---|
| A1 | key `max_budget` is check-then-act (`spend >= max_budget`) | VERIFIED | `LL/proxy/auth/auth_checks.py:5461` `if math.isfinite(...) and spend >= valid_token.max_budget`. Note: when the key is over budget it raises `BudgetExceededError` with status **422** (`LL/__init__.py:404 budget_exceeded_status_code = 422`), not 402/429. See issue 13. |
| A2 | batch DB write every 10 s | VERIFIED | `LL/constants.py:1819 PROXY_BATCH_WRITE_AT ... 10` |
| A3 | `x-litellm-response-cost` on non-streaming responses | VERIFIED, with caveats | `LL/proxy/common_request_processing.py:1769`. The header is **dropped** when the cost is `""`/None (`exclude_values` at L1727 and L1835; when the cost can't be recovered it becomes `""` at L2889). It is **`"0.0"`** for unbilled calls (L2893–2896). Values are `str(float)`, so they can look like `1.2e-05`. See issue 12. |
| A4 | `x-litellm-call-id` honoured verbatim | VERIFIED (≤256 chars) | `common_request_processing.py:266-269`, `MAX_LITELLM_CALL_ID_LENGTH = 256` (`constants.py:152`). Live SpendLogs rows have `request_id == litellm_call_id`. |
| A5 | `x-litellm-spend-logs-metadata` not enterprise-gated | VERIFIED | `LL/proxy/litellm_pre_call_utils.py:1301-1310, 1542-1544`, called unconditionally from L2109. Live SpendLogs metadata contains a `spend_logs_metadata` key. |
| A6 | `num_retries` default = 2 | VERIFIED, and **observed live** | `LL/router.py:1056-1061` and `constants.py:68`. In the live DB, every recent failure row (status `failure`, 429 RateLimitError) has `metadata.attempted_retries = 2` and `max_retries = 2`. So each 429 that ResearchOS saw cost 3 Gemini attempts. |
| A7 | LiteLLM only sets `retry-after` on router cooldown | MOSTLY VERIFIED | `common_request_processing.py:3606-3608`. On errors it also copies provider headers as `llm_provider-*` (L3662-3667, `get_headers.py`). Google does not send Retry-After, so the conclusion holds. |
| A8 | `custom_auth` skips built-in checks unless `enable_post_custom_auth_checks` | VERIFIED | `LL/proxy/auth/user_api_key_auth.py:1550-1572` |
| A9 | `custom_auth` module loaded next to config | VERIFIED | `LL/proxy/types_utils/utils.py:9-39`. Note: compose mounts a **single file** (`./config/litellm.yaml:/app/config.yaml`), so a hook would need a directory mount. That is trivial either way. |
| A10 | `/model/info` exposes prices and is usable by a virtual key | VERIFIED (source) | It is in `llm_api_routes` (`LL/proxy/_types.py:613-628`). Prices for all 4 planned aliases are in the shipped map: in 7.5e-07, out 3.75e-06, reasoning out 3.75e-06, max_output 65536. There is **no** above-200k tier for them. |
| A11 | Prices are pinned by the image digest | **WRONG (implicit)** | `LL/__init__.py:557` fetches the map from a remote URL at import time. The local-only switch is `LITELLM_LOCAL_MODEL_COST_MAP=true` or argv0 ∈ {`lite`, `litellm-proxy`} (`get_model_cost_map.py:43, 643`). The running process is `/app/.venv/bin/litellm` (checked `/proc/1/cmdline`), so **it fetches remote prices from GitHub at startup**. See issue 11. |
| A12 | `is_request_body_safe` / banned params blocklist | VERIFIED | `LL/proxy/auth/auth_utils.py:306, 475-520` |
| A13 | Key `tags` enterprise-gated | VERIFIED | `key_management_endpoints.py:1426`. The other enterprise items were not checked; they don't matter. |
| A14 | max_tokens → Gemini maxOutputTokens | VERIFIED | `LL/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py:1139-1140` maps both `max_tokens` **and** `max_completion_tokens` to `max_output_tokens`, and the last one iterated wins (see issue 2). Whether maxOutputTokens caps *thinking* on Gemini 3.x is UNVERIFIABLE without a live call. Public Gemini 2.5 behaviour (empty reply with MAX_TOKENS when thinking eats the budget) says thinking counts toward the cap. Live evidence that completion_tokens already includes thinking: one SpendLogs row has 4446 completion tokens for a tool-call turn. |
| A15 | Free tier 5 RPM / 20 RPD; `gemini-flash-latest` is a separate quota bucket | UNVERIFIABLE (no API calls allowed) | `-latest` may be an alias of 3.8-flash and share its quota, which would make it useless as a fallback. |
| A16 | Package pins and their dates (fastapi 0.141.1 etc.) | UNVERIFIED | Not checked offline. Low impact. |

## B. Issues

### 1. MAJOR · §2 steps 4–5, §6 "Guarantee": the kill switch is not linearized with the reservation
- **Problem.** Policy facts are read in step 4 and the reserve runs in step 5, as separate statements. If an admin commits revoke, suspend or unassign between them, the request still reserves and goes upstream. The plan's wording ("whose *policy check* starts after the commit") is honest but weaker than "provably halt", and the e2e test cannot detect this window. A budget withdrawal is already safe, because the limit is inside the conditional UPDATE.
- **Fix (no new code paths).** Make the reservation the single point that decides:
  `UPDATE agents SET reserved_micros=… WHERE id=$a AND status='active' AND spent+reserved+$est<=limit AND EXISTS(SELECT 1 FROM agent_mandates WHERE agent_id=$a AND mandate_id=$m)`
  and `UPDATE mandates … WHERE id=$m AND status='active' AND …`. On 0 rows, re-read the facts to pick the reason code. Restate the guarantee as "any request whose reservation commits after the admin's commit is denied". An admin PATCH takes the same row lock, so the two are serialized.

### 2. MAJOR · §7 request allowlist and §5 estimate: `max_tokens` + `max_completion_tokens` lets a client get around the estimate
- **Problem.** Both fields are allowlisted. LiteLLM maps **both** to `max_output_tokens` in iteration order (A14). A client can send `max_tokens=16` (the estimate uses 16) together with `max_completion_tokens=65536` (what Gemini gets). That is up to ~$0.25 of overshoot per request, multiplied by concurrency, which breaks the "hard ceiling".
- **Fix.** Reject requests that carry both, or accept either one and **build the outbound body yourself** from the validated fields, with exactly one `max_tokens` set to the value used in the estimate. Never forward the raw client JSON.

### 3. MAJOR · §7 allowlist: `tools` must be restricted to `type == "function"`
- **Problem.** LiteLLM passes Gemini built-in tools through `tools`: `googleSearch`, `urlContext`, `codeExecution`, `googleMaps` (`vertex_and_google_ai_studio_gemini.py:372-424, 523-577`). The price map has `search_context_cost_per_query` and `google_maps_grounding_cost_per_query` = **$0.014 per query**, which is neither in the estimate nor controlled by policy.
- **Fix.** Return 400 unless every tool is `{"type":"function","function":{…}}`. ResearchOS only sends that shape (`openai_compatible.py:145-153`).

### 4. MAJOR · §3 Ownership: the "append-only" and "REVOKE CONNECT" claims are wrong as designed
- **Problem 1.** `gateway_app` owns db `gateway` and runs the migrations, so it **owns `audit_events`**. A table owner can always `GRANT UPDATE, DELETE` back to itself, or `TRUNCATE`. "The app role cannot edit history" is false.
- **Problem 2.** `REVOKE CONNECT ON DATABASE litellm FROM gateway_app` does nothing, because CONNECT is granted to **PUBLIC** by default. The `\l` output shows empty ACLs (= default PUBLIC CONNECT) for both `gateway` and `litellm`.
- **Fix (minimal).** Use two roles. `gateway_owner` (NOLOGIN, or used only by the migration step) owns the schema. `gateway_app` (LOGIN) gets `SELECT,INSERT,UPDATE` on agents/mandates/reservations/agent_mandates and `INSERT,SELECT` on audit_events plus `USAGE` on its sequence. Run `REVOKE CONNECT ON DATABASE litellm, gateway FROM PUBLIC` and then `GRANT CONNECT ON DATABASE gateway TO gateway_app`. The simplest way to run migrations as the owner is a one-shot `docker compose run gateway python -m migrate` with the owner DSN. Otherwise keep one role and **drop the append-only claim** from the README. Do not ship a false claim.

### 5. MAJOR · §3 / threat model: LiteLLM connects to Postgres as the **superuser** that also owns the gateway's ledger
- **Evidence.** `docker-compose.yml`: `DATABASE_URL: postgresql://${POSTGRES_USER}…@db/litellm`. `.env` has `POSTGRES_USER=gateway`, and `\du` shows `gateway | Superuser, Create role, Create DB, Replication, Bypass RLS`.
- **Problem.** The 855k-line proxy that the plan itself calls security-sensitive (it has GHSA history) can rewrite budgets and audit rows, and as superuser can run `COPY … TO PROGRAM`, which is RCE in the db container. The superuser is also confusingly named the same as db `gateway`.
- **Fix.** In the M2 bootstrap SQL, create `litellm_app` (LOGIN, not superuser). The simplest route is to `DROP` and recreate db `litellm` owned by `litellm_app`. Its data is disposable test spend, and M2 recreates the keys anyway. Avoid `REASSIGN OWNED`, which would also hand over the *databases* owned by `gateway`. Point LiteLLM's `DATABASE_URL` at `litellm_app`. Mention in the README that the superuser is used only for bootstrap.

### 6. MAJOR · §5 Retries, §8 note: decide retries and timeouts now, not "verify in M7"
- **Evidence.**
  - Retries: live SpendLogs failures show `attempted_retries=2` (A6). ResearchOS has `llm_max_retries=3` (`config.py:25`), so one exhausted model costs up to 4×3 = 12 Gemini attempts against a 5 RPM quota.
  - Timeouts: LiteLLM's default `request_timeout` is **6000 s** (`constants.py:554, 562`). ResearchOS's httpx timeout is **120 s** (`config.py:24`). The live max success duration is **103.4 s** and the average is 20.4 s (SpendLogs `endTime-startTime`).
  - The plan gives no value for `upstream_timeout`.
- **Problems.**
  - (a) Retry multiplication burns the quota.
  - (b) A slow call (>~115 s once the extra hop is added) makes ResearchOS time out and resend. LiteLLM keeps the first call running for up to 100 min and it gets billed. Both are reserved, so the budget is safe, but money is wasted.
  - (c) If you set a LiteLLM timeout, its 408 would be "released" under the §5 table, although Google may bill a generation the client gave up on.
- **Fix (config only).**
  - Set `litellm_settings: num_retries: 0`. The gateway can also send `x-litellm-num-retries: 0`, which is honoured per request (`litellm_pre_call_utils.py:1291-1297, 1518-1520`).
  - Order the timeouts: LiteLLM `request_timeout` < gateway `upstream_timeout` < ResearchOS `LLM_TIMEOUT_SECONDS`, for example 240 / 270 / 300, with the last one set in ResearchOS `.env` at M8 (env only, no code).
  - In the §5 table, treat LiteLLM **408 and 5xx timeouts** as "unknown outcome → settle at reservation". Release only on 4xx/429 and connect errors.

### 7. MAJOR · §5 test "50 parallel requests → exactly ⌊limit/est⌋ admitted": flaky as designed
- **Problem.** With a `MockTransport` that answers immediately, early requests settle (spent = small mock cost, reservation freed) while later ones are still reserving. More than ⌊limit/est⌋ get admitted and the assertion fails nondeterministically.
- **Fix.** Hold the mock responses on an `asyncio.Event` until all 50 requests have either reserved or been denied. Assert admitted == ⌊limit/est⌋ and that `reserved_micros ≤ limit` at the barrier, then release. Use byte-identical bodies so `est` is constant. Size the asyncpg pool so that pool exhaustion doesn't time out the test.

### 8. MAJOR · §10 M1 acceptance "ready = all true" cannot pass at M1
- **Problem.** `/health/ready` requires "policy loaded" (policy.toml arrives at M4) and "prices loaded" (needs the gateway LiteLLM key, created at M2). M1 as specified can never go green. M1 also says "LiteLLM is still not published", but the debug overlay **is running now** (`docker ps`: `127.0.0.1:4000->4000/tcp`), and ResearchOS depends on it until M8 (`.env.researchos: LLM_BASE_URL=http://127.0.0.1:4000/v1`).
- **Fix.** Swap M1 and M2: create the LiteLLM-side config and key first, because the gateway needs it. Make `/health/ready` check only what exists at each milestone, or define M1 done as "db ok, litellm ok". Reword the "not published" check as "not published by `docker-compose.yml`" (`docker compose -f docker-compose.yml config | grep -c 4000` → 0) until M8 retires the overlay.

### 9. MAJOR · §10 M3 `.secrets/demo-a.token`: `.secrets/` is not git-ignored
- **Evidence.** `.gitignore` covers `.env`, `.env.*`, `*.pem` and `*.key` only.
- **Fix.** Add `.secrets/` to `.gitignore` in M3. Extend the e2e "no secrets in git" check to cover `git ls-files .secrets`.

### 10. MINOR · §5 estimate formula is wrong as written, and "input is cheap" is contradicted by live data
- **Formula.** `ceil(input_bytes × in_price + max_tokens × out_price) × 1e6` rounds to **whole dollars** before scaling. It must be `ceil((…) × 1e6)`, computed in `Decimal` from `Decimal(str(price))`.
- **"Input is cheap".** ResearchOS rows show prompt 18 602 / completion 130 tokens for $0.0144 (input ≈ 97% of the cost). With bytes ≈ 4×tokens, the reservation is ≈ $0.056 input + $0.031 output ≈ $0.087 vs $0.014 actual, about 6×. That is acceptable as a conservative MVP, but say so honestly. Measure `input_bytes` as `len(outbound_body_bytes)` (this also covers `response_format` schemas and tool definitions) instead of `json(messages+tools)`.
- **Overshoot.** "spent can exceed limit by at most one request's estimate error" is wrong under concurrency: it is *one per in-flight request*. After fixes 2 and 3, the only overshoot source left is a wrong price or uncapped thinking, so state it as "N in-flight × estimate error".

### 11. MINOR · §1, §11 Risks: the price map is not pinned by the digest
- **Evidence.** A11. A remote map change, or a tampered map that sets price 0, changes the `x-litellm-response-cost` you settle on, and the digest pin doesn't protect against it.
- **Fix.** Add `LITELLM_LOCAL_MODEL_COST_MAP: "True"` to the litellm service env (one line). Prices then change only with a digest bump, which the e2e cost-header test already covers.

### 12. MINOR · §5 settle table: header `0.0` and edge parsing
- **Problem.** A 2xx with `x-litellm-response-cost: 0.0` (unbilled path, A3) would settle at 0. `Decimal("nan")` and negative values are also possible from a broken map.
- **Fix.** If the header is missing, non-finite or ≤0 while `usage.total_tokens > 0`, settle at the reservation (`cost_source=reservation`). That is one `if`.

### 13. MINOR · §2 step 8 and §7 status codes: LiteLLM error passthrough leaks data and returns the wrong code for the backstop
- **Problems.**
  - (a) The backstop budget error from LiteLLM is **422**, with the message `Budget has been exceeded! Key=… Current cost: X, Max budget: Y` (`auth_checks.py:5466-5476`). Passing it through tells every agent the gateway-wide spend. ResearchOS maps 422 to `LLMResponseError` → `LLM_FAILED` instead of `LLM_REFUSED`.
  - (b) Error responses also carry `x-litellm-key-spend`, `x-litellm-key-max-budget` and `llm_provider-*` headers. The plan strips `x-litellm-*` only on the success path.
- **Fix.** Use a **response-header allowlist** (`content-type`, `retry-after`, `x-request-id`) on all paths. On non-2xx, return `{"error":{"message":<short>, "type":…, "code":…}}` and pass the status through, except LiteLLM 401/403/422-budget, which mean a gateway misconfiguration and become 503 `UPSTREAM_UNAVAILABLE` (the agent didn't do anything wrong). Keep the full upstream error only in container logs, not in the audit table.

### 14. MINOR · §5 settle and sweeper concurrency details
- **Settle.** Check the settle UPDATE's RETURNING row count and **skip** the agents/mandates updates when it is 0. Otherwise a double settle subtracts twice. Run the whole settle in one transaction.
- **Sweeper.** Process **one reservation per transaction**. Batching several reservations across different agents in one transaction can deadlock with a concurrent reserve (sweeper holds mandate M and wants agent B; a reserve holds B and wants M).
- **Startup.** With a single instance, every `held` row present at startup is orphaned, so sweep them all right away rather than waiting `timeout+5 min` (`# ponytail: single replica; use owner/instance id if scaled`).
- **Late settle after a sweep.** It finds `status != 'held'` and does nothing, so the actual cost is lost (and it could exceed the reservation). Log it as `cost_source=late_ignored` in the audit table so it can be reconciled. No more code than that.

### 15. MINOR · §2/§6 audit and denial details
- **Lost deny rows.** The 402 path ROLLBACKs the reserve transaction, which would also roll back the deny audit row written inside it. Write the deny audit in its own statement after the rollback.
- **Unauthenticated requests.** Do **not** write DB audit rows for 401s, which would be write amplification by unauthenticated callers. Log them to stdout.
- **Body size.** Add a request body cap (for example 2 MB → 413). uvicorn has none, and the estimate needs the whole body in memory.
- **Status code.** If you use pydantic models, FastAPI returns 422 rather than 400. ResearchOS treats both as fatal, so this is fine, but pick one and test it.

### 16. MINOR · §4/§7 Over-engineering that can be cut without violating any requirement
- **TOML policy DSL + evaluator + fact-name validation.** The 5 rules are fixed equality checks over facts hard-coded in Python, so the TOML gives no real flexibility and will be thrown away for Cedar. Use a literal ordered tuple in `policy.py`, `RULES = (("AGENT_REVOKED", lambda f: f.agent_status=="revoked"), …)`, with `policy_version = sha256(policy.py)[:12]`. It is just as auditable (one file in git) and needs no parser or loader-failure path. Keep `max_tokens_default/cap` as two env vars or constants.
- **`X-Mandate` header form.** It is a second way to do the same thing, plus a "disagree → 400" branch. ResearchOS can't use it. Ship the path form only.
- **`hmac.compare_digest` after a DB lookup by sha256.** This is redundant, because the DB equality on a hash of a 256-bit token leaks nothing useful. Harmless, but don't describe it as a security property. For the **admin** token, compare `bytes` (`compare_digest` on a `str` with non-ASCII input raises TypeError → 500).
- **`served_model` column.** No router fallbacks exist, so this is YAGNI.
- **M7 "2 simulated agents" demo.** It is nearly the same as e2e step "withdraw budget"; fold it into `e2e_killswitch.py` output, or keep it only if the manager demo needs it.

### 17. MINOR · §4 `max_tokens_default = 8192` injected for ResearchOS (which sends none)
- **Problem.** This is a behaviour change. If thinking consumes the cap, the reply has no content and no tool calls. ResearchOS then counts an idle turn and eventually ends `STALLED` (`agent.py:101-107`). The live max completion is 4446, so 8192 is probably fine, but you can't tell when it isn't.
- **Fix.** Store `finish_reason` in the audit row and check it in M8.

### 18. MINOR · §8 claims about ResearchOS behaviour
- **"Slower than before".** This is wrong. ResearchOS already talks to LiteLLM today (`.env.researchos` → `:4000`), which already drops Google's `RetryInfo`, so 429 behaviour is unchanged. With `num_retries: 0` (issue 6) it actually gets *faster*.
- **Fallback list.** Verify that `gemini-flash-latest` is a distinct quota bucket the first time a live 429 occurs; if it is an alias of 3.8-flash, it is a useless fallback.

### 19. MINOR · §10 leftovers and bootstrap gaps
- **Leftover LiteLLM keys.** There are 4: `m1-persistence-check`, **two unnamed keys with `models={}` (= all models)**, and `researchos`. Block all of them at M2, not only "old test keys", and block `researchos` at M8. Delete `.env.researchos`, which holds a live LiteLLM key, at M8.
- **`gen-env.sh`.** It refuses to run when `.env` exists (`gen-env.sh:5`). M1 needs an "append missing keys only" mode (`grep -q '^ADMIN_TOKEN=' .env || echo …>>.env`).
- **Test infrastructure.** Nothing says who creates db `gateway_test` and the roles inside the **test** project, whose fresh volume runs `postgres-init.sql` but not `db-bootstrap.sql`. Put role and db creation into an init script that runs in both cases. The test stack's gateway key must be created with a fixed value (`/key/generate {"key":"sk-test-…"}`, allowed unless disabled: `key_management_endpoints.py:1487-1497`) so the gateway env can be static.
- **Gateway environment.** Use an explicit `environment:` list (no `env_file: .env`). Extend the M2 check to `GEMINI_API_KEY`, `POSTGRES_PASSWORD` and `LITELLM_SALT_KEY` (count must be 0), not only `MASTER`.

### 20. MINOR · §6 proof test design: small gaps
- **Restart step.** "count unchanged across restart" is good. Also issue a request **during** `docker compose restart` and assert that it fails closed (connection refused or 503, never forwarded).
- **Upstream check in e2e.** Assert that LiteLLM's SpendLogs gained no row for the denied request ids, in addition to the mock counter. It is cheap and proves the deny happened before LiteLLM, not merely before Gemini.
- **What is out of scope.** In-flight requests and LiteLLM-internal retries are correctly out of scope; with `num_retries: 0` the second goes away. The e2e mock counter is the right "never reached upstream" oracle. LiteLLM caching is off (no `cache:` in config) and `cache` is not allowlisted, so there is no cache gap.

## C. Verdict on "build on top of LiteLLM" (§1)

Agree with option (a): a separate FastAPI gate with LiteLLM unmodified and internal.

- The decisive reasons hold up against the source: `custom_auth` bypasses LiteLLM's own checks (A8), budgets are check-then-act and float (A1, A2), and the deny-before-forward proof is far simpler against a small service with a mocked transport.
- An in-proxy hook adds nothing the cost header doesn't already give for non-streaming.
- An overlay image or a fork would be pure cost.

The right "fork-kind" layer is **LiteLLM config and env only**, and the plan should use more of it:
- `num_retries: 0`
- `request_timeout`
- `LITELLM_LOCAL_MODEL_COST_MAP=True`
- a non-superuser DB role for LiteLLM
- the gateway key restricted to the 4 models

Keep the hook as the documented escape hatch for streaming, as the plan says. No hybrid is needed for the MVP.

## D. What the plan got right (keep)

- **Architecture.** Separate gate, LiteLLM internal-only, a single scoped LiteLLM key, and the master key never in the gateway.
- **Money handling.** BIGINT micro-USD, a reservation via conditional UPDATE, a fixed agent→mandate lock order (no deadlock between reserves), idempotent settle on `status='held'`, and no CHECK `spent ≤ limit`, so an overshoot can still settle.
- **Status codes.** 401/402/403 to ResearchOS map to `LLMAuthorizationError` → `RunStatus.LLM_REFUSED`, which stops the run (`openai_compatible.py:30,108-109`, `agent.py:96-97`). 429/5xx are retried, then the client switches model (`:29,113-121`). VERIFIED.
- **Mandate in the URL path.** It works with ResearchOS by config alone. The comma-separated `LLM_MODEL` is split client-side (`config.py:40`), so each model is authorized on its own. ResearchOS's body (`model, messages, tools, tool_choice`, extra keys inside `tool_calls`) passes the planned allowlist. VERIFIED.
- **Headers.** No client headers forwarded, and `x-litellm-call-id` = request_id to cross-reference SpendLogs.
- **No prompt logging.** Already true in LiteLLM: SpendLogs `messages`, `response` and `proxy_server_request` are all `{}` (max length 2) in the live DB. The gateway audit has no content.
- **Proof tests.** Two layers (in-process MockTransport, and real LiteLLM + mock upstream + restart), mock upstream for all automated tests, and one opt-in live call.
- **Scope cuts.** Streaming out, client-side fallbacks, lifetime budgets, append-only stated as "not tamper-proof", and an honest known-limitations list.
