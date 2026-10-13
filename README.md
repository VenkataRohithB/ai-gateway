# AI Agent Gateway

A central gateway in front of company AI agents. Every LLM call an agent makes goes through it, and
the gateway decides **who** is calling, **under which mandate**, whether policy **allows** it, and
whether the agent's and the mandate's **monthly budgets** can cover it, *before* anything reaches a
model provider. Everything is managed from an admin console, and any agent can be stopped on its next call.

First product: SynBioAI (5 agents). First real client: ResearchOS. Plans and design record: `docs/plan/`.

**Status:** v3 MVP. 93 automated tests plus an end-to-end proof through the real LiteLLM, all passing,
all against a fake model at **no cost**.

```
 Agent (ResearchOS, SynBioAI…) ── gw_ token ──► GATEWAY :8080 ──► LiteLLM ──► Gemini (+ any provider)
                                                 │  (FastAPI)       (internal only)
                                                 ├─ /admin/ui  admin console
                                                 ├─ /admin/*   admin API
                                                 └─ PostgreSQL: agents, mandates, budgets, usage, policy
```

| Component | Responsibility |
|---|---|
| **Gateway** (`gateway/`) | Agent identity, policy, monthly budgets (USD or INR), rate limits, fallback models, kill switches, usage records, admin API, admin console |
| **LiteLLM 1.104.0** (pinned by digest, unmodified) | Provider adapters and keys, price table, per-call cost, a $10 safety cap on the gateway's own key |
| **PostgreSQL 16** | db `gateway` (ours) and db `litellm` (LiteLLM's), each with its own least-privilege role |

Only the gateway is published, on `127.0.0.1:8080`. LiteLLM and Postgres sit on an internal Docker network, so
agents cannot bypass the gate.

---

## 1. Setup

Prerequisites: Docker with Compose v2, and a provider key (e.g. Gemini: https://aistudio.google.com/apikey).

```bash
scripts/gen-env.sh                 # creates .env with random secrets (never overwrites; never prints)
$EDITOR .env                       # set GEMINI_API_KEY=...
docker compose up -d --build       # db -> litellm -> migrate (one-shot) -> gateway
scripts/litellm-setup.sh           # creates the scoped "gateway" LiteLLM key (idempotent)
curl -s 127.0.0.1:8080/health/ready   # {"db":true,"litellm":true,"prices":true}
```

Open the console at **http://127.0.0.1:8080/admin/ui** and sign in with `ADMIN_TOKEN` from `.env`.

**Stop or restart:** `docker compose stop` / `docker compose up -d`. All state lives in the volume `ai-agent-gateway_pgdata`.
`docker compose down -v` **deletes everything**. Database changes are applied automatically by the `migrate` service.

### Where keys live (and why the database is safe to lose)

Provider keys are **only in `.env`** on the gateway machine. They are never written to either database, never sent to
agents and never shown in the console. Someone who steals the database gets no provider keys and no usable agent tokens.

| Secret (`.env`) | Read by | Purpose |
|---|---|---|
| `GEMINI_API_KEY` (and any other provider key) | LiteLLM only | Calling the provider |
| `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY` | LiteLLM only | LiteLLM admin / credential encryption |
| `LITELLM_GATEWAY_KEY` | gateway | Scoped LiteLLM key: configured models only, $10 backstop |
| `ADMIN_TOKEN` | you | Admin console and API |
| `POSTGRES_PASSWORD` | bootstrap only | Superuser, which no service uses |
| `LITELLM_DB_PASSWORD`, `GATEWAY_OWNER_PASSWORD`, `GATEWAY_APP_PASSWORD` | litellm / migrate / gateway | Least-privilege DB roles |
| agent tokens `gw_…` | each agent | Shown once when issued; only their sha256 is stored |

**On a server later:** keep the values as GitHub Actions secrets and have the deploy job write them into the server's
`.env`. The gateway itself does not change. Keep `LITELLM_SALT_KEY` stable forever.

---

## 2. The admin console (`/admin/ui`)

| Page | What you do there |
|---|---|
| **Overview** | This month's spend, charged calls, blocked calls, a 30-day spend chart, and a "Needs attention" list (budgets ≥ 80%, suspended agents, calls charged without an answer, agents without a token or mandate) |
| **Agents** | Create agents (token shown once), switch Active / Suspended / Revoked, monthly budget in USD or INR, rate limit, rotate token, assign mandates, copy each mandate's base URL, recent calls and changes |
| **Mandates** | Create mandates, ordered model list (order = fallback order), fallback on/off, default answer limit, monthly budget, assign agents, suspend |
| **Usage** | Every call with result, model (and fallback), tokens, cost and time taken, filterable; monthly totals by agent, mandate or model |
| **Policy** | Built-in rules (read-only) and your own rules, saved as numbered versions with history |
| **Activity** | Every admin change with before → after values |
| **Settings** | USD→INR rate, configured models and prices, system health |

The console is static files served by the gateway with a strict Content-Security-Policy (no inline scripts, no
external requests). The admin token is kept in the browser tab's session storage only.

---

## 3. Connecting an agent

Any OpenAI-compatible client works. Change only its configuration:

```
base URL : http://127.0.0.1:8080/m/<mandate-id>/v1
API key  : gw_…   (the agent's own token)
model    : one of the mandate's models, e.g. gemini-3.7-flash
```

Create the agent in the console (Agents → New agent); it shows the token and the base URLs once. Or use the CLI:
```bash
A=scripts/gw_admin.py
$A POST /admin/mandates '{"id":"synbio","name":"SynBioAI","budget":"20","budget_currency":"USD","allowed_models":["gemini-3.7-flash","gemini-flash-latest"]}'
$A POST /admin/agents   '{"id":"synbio-agent-1","name":"SynBio agent 1","budget":"500","budget_currency":"INR","rpm_limit":60}'
$A PUT  /admin/agents/synbio-agent-1/mandates/synbio
$A POST /admin/agents/synbio-agent-1/token --save-token .secrets/synbio-agent-1.token
```

### Request contract (`POST /m/{mandate}/v1/chat/completions`)

- **Identity** comes only from the bearer token. Body fields such as `user` are rejected.
- **Allowed fields:** `model, messages, max_tokens | max_completion_tokens (not both), temperature, top_p,
  stop, seed, tools (type "function" only), tool_choice, response_format, n (=1), stream (=false)`.
  Anything else (`api_base`, `api_key`, `metadata`, `fallbacks`, `service_tier`, `extra_body`…) returns 400 `FIELD_NOT_ALLOWED`.
- Message content must be text. The body is capped at 2 MB.
- `max_tokens`: when omitted, the mandate's **default answer limit** is used (4096 for new mandates). It may not exceed
  the model's maximum output. It is always forwarded, so it caps both the answer and the budget hold.
- The gateway **rebuilds** the outbound request and forwards **no client headers**.
- Responses carry `x-request-id` (the id in Usage) and `x-gateway-model` (the model that actually answered).

### Status codes

| Code | `error.code` | Meaning |
|---|---|---|
| 401 | `UNAUTHORIZED` | missing or invalid token |
| 403 | `AGENT_REVOKED`, `AGENT_INACTIVE`, `MANDATE_NOT_ASSIGNED`, `MANDATE_INACTIVE`, `MODEL_NOT_ALLOWED`, or a custom rule's code | policy deny (includes `policy_version`) |
| 402 | `AGENT_BUDGET_EXCEEDED`, `MANDATE_BUDGET_EXCEEDED` | this month's budget can't cover the request's worst case |
| 429 | `AGENT_RATE_LIMITED` | the agent's calls-per-minute limit |
| 429 | `UPSTREAM_RATE_LIMITED` | provider quota, after trying every fallback model |
| 400 / 413 | `FIELD_NOT_ALLOWED`, `TOOL_NOT_ALLOWED`, `STREAMING_NOT_SUPPORTED`, `CONTENT_NOT_ALLOWED`, `INVALID_REQUEST`, `REQUEST_TOO_LARGE` | bad request |
| 503 / 504 | `UPSTREAM_UNAVAILABLE` / `UPSTREAM_TIMEOUT` | LiteLLM or the provider is down, or the call timed out |

Error bodies have the shape `{"error": {"code", "message", "request_id", …}}` and never contain internal details.

---

## 4. Policy

Every request is checked against an ordered list of deny rules; the first match blocks it.

- **Built-in rules** (always on, checked first): agent revoked, agent suspended, not assigned to the mandate (unknown
  mandates look the same), mandate suspended, model not in the mandate's list. The budget transaction re-checks the
  same states in SQL, and a test proves the two always agree.
- **Your rules** are added in the console (Policy page): block when any combination of **agent**, **mandate** and
  **model** matches, with your own reason code and message. Saving creates a new numbered version (`v3-1a2b3c4d`),
  recorded on every usage row and in Activity. It applies to the next request; no restart.
- Rules are validated on save: unknown conditions, empty conditions or a built-in reason code are refused.

## 5. Budgets

Every agent and every mandate has a **monthly** budget in **USD or INR**. A request must fit both.

- **Month:** calendar month in India time; spend resets at midnight on the 1st. Monthly totals are kept forever.
- **Currency:** providers bill in USD, so spend is recorded in USD. INR budgets are converted with the USD→INR rate set
  in Settings; a rate change applies to the next request.
- **Estimate before the call (worst case):**
  ```
  hold = request bytes × highest input price that could apply
       + max_tokens    × highest output price that could apply
  ```
  "Highest" counts cache writes, reasoning tokens and long-prompt tiers from LiteLLM's price table. A token is at
  least one byte, so a long-prompt tier only counts when the request is long enough to reach it. Cheaper modes
  (batch, flex, cache reads) are ignored.
- **Reserve:** one transaction checks the rate limit, then adds the hold to the agent and the mandate, re-checking
  status and `spent this month + held + hold ≤ budget`. If anything fails, nothing is forwarded.
- **Settle:**

  | Outcome | Charge |
  |---|---|
  | Answer | The real cost from LiteLLM's `x-litellm-response-cost` header |
  | Answer, cost unknown | The full hold |
  | Timeout or connection lost (outcome unknown) | The full hold (shown in Overview as "charged without an answer") |
  | Provider error (429, 5xx…) | Nothing |

- **Sweeper:** holds abandoned by a crash are charged at the hold after 9.5 minutes (and at startup).
- **Concurrency:** 50 simultaneous requests cannot overspend (tested). LiteLLM's own budget check, in strict mode
  (`fail_closed_budget_enforcement`), is a second safety net on the gateway's key.

## 6. Fallback models

A mandate's model list is ordered. If the requested model answers **429 (busy) or 503**, or can't be reached, the
gateway tries the **next models after it in the list**. Each attempt passes policy and gets its own budget hold at its
own price; busy attempts cost nothing. Timeouts do **not** fall back (LiteLLM gives up at 240 s and agents at 300 s,
so a second try would arrive too late). Fallback can be switched off per mandate. LiteLLM itself never retries.

## 7. Kill switches and rate limits

| Action | Effect on the **next** request |
|---|---|
| Agent → Suspended / Revoked | blocked under every mandate (`AGENT_INACTIVE` / `AGENT_REVOKED`) |
| Mandate → Suspended | every agent under it blocked (`MANDATE_INACTIVE`) |
| Remove agent from mandate | `MANDATE_NOT_ASSIGNED` |
| Budget → 0 | `…_BUDGET_EXCEEDED` |
| Rate limit | calls per minute per agent, exact under concurrency; extra calls get 429 `AGENT_RATE_LIMITED` |

A request already forwarded finishes and is charged; it is not cancelled. State is in Postgres, so it survives restarts.

## 8. Records and logs

- **Usage** (`usage` table): one row per request, allowed or blocked: agent, mandate, model requested and served,
  result, reason, attempts, tokens, cost, time taken, policy version. Kept `USAGE_RETENTION_DAYS` (90) days, cleaned
  hourly. **Monthly totals** (`usage_monthly`) are kept forever. No prompt or response content is ever stored.
- **Activity** (`audit_events`): every admin change with before/after values. Append-only for the gateway's DB role
  (not tamper-proof against the Postgres superuser).
- **Application logs:** JSON lines on stdout, level set by `LOG_LEVEL` in `.env` (`DEBUG` shows every step of every
  request: policy facts, holds, each attempt; `INFO` default; `WARNING`). Never content or secrets. Docker rotates
  them (5 × 10 MB per service). On a server, ship them to a central log store instead.
- LiteLLM's own per-request spend log is turned off (`disable_spend_logs`); its key spend still updates, and the
  end-to-end test checks that it equals what the gateway charged.

## 9. Tests (free: no real model calls)

```bash
scripts/test.sh          # 93 pytest tests + end-to-end proof
scripts/test.sh unit     # pytest only
```

Everything runs in a separate Compose project, `aigw-test`, with its own database. LiteLLM is pointed at
`tests/mock_upstream.py`, a fake model that counts every call (and can be told to answer "busy").

- **`tests/test_policy.py`:** built-in rules, custom rules, validation, version hashing, agreement with the SQL guard.
- **`tests/test_gateway.py`:** real Postgres with a counting mock in place of LiteLLM: auth, impersonation, every kill
  switch (proving the upstream count does not move), monthly rollover, INR budgets and rate changes, 50-way
  concurrency, exact rate limits, every fallback path, the worst-case estimate on real Claude/GPT/Gemini price shapes,
  failure reconciliation, sweeper, retention, reporting endpoints, console lock-down, no secrets or prompts in records.
- **`tests/e2e_killswitch.py`:** two agents through the **real LiteLLM**: budget withdrawal, revocation, gateway down,
  restart persistence, LiteLLM key spend = gateway charges, fallback when a model is busy, deployment checks.

**Live check (costs money on a paid key):** `tests/live_gemini.sh` makes one call through the gateway and asks first.

## 10. ResearchOS (first real client)

Config change only; its code is unchanged. Its `.env`:
```
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://127.0.0.1:8080/m/researchos-dev/v1
LLM_API_KEY=<.secrets/researchos.token>
LLM_MODEL=gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-flash-latest   # client-side fallback
LLM_TIMEOUT_SECONDS=300     # > gateway 270s > LiteLLM 240s
```
The mandate's own fallback order can now be set on Mandates → ResearchOS development → Models.

## 11. SynBioAI onboarding (needs approval before touching live agents)

1. Create a mandate per SynBioAI purpose, with a monthly budget and an ordered model list.
2. Create one gateway agent per SynBioAI agent (5), each with its own budget, rate limit and token. Never share tokens.
3. Assign each agent only to the mandates it needs.
4. Point each agent's OpenAI-compatible config at `…/m/<mandate>/v1` with its own token; remove provider keys from agents.
5. Smoke-test with a suspended agent first (denied, free), then activate one at a time.
6. If they run on another host, the gateway must be exposed **behind TLS**, which is not part of this MVP.

## 12. Adding a provider or model

1. Add the key to `.env` and pass it to the `litellm` service in `docker-compose.yml`.
2. Add the model to `config/litellm.yaml` with `api_key: os.environ/NAME`. A model missing from LiteLLM's price
   table needs `input_cost_per_token` / `output_cost_per_token` there, or the gateway refuses it.
3. `docker compose up -d --force-recreate litellm`, then (after step 4) `docker compose restart gateway` to reload prices.
4. Run `scripts/litellm-setup.sh`: it lets the gateway's LiteLLM key use every model in `config/litellm.yaml`.
5. Add the model to mandates in the console.

## 13. Troubleshooting

| Symptom | Check |
|---|---|
| `/health/ready` shows `"prices": false` | LiteLLM still starting, or the `gateway` key is missing: run `scripts/litellm-setup.sh` |
| 503 `UPSTREAM_UNAVAILABLE` on every call | `docker compose logs gateway \| grep litellm_rejected`; the gateway key may be blocked or over its $10 backstop |
| 429 `UPSTREAM_RATE_LIMITED` | Every model in the chain was busy. Free tier: ~5 requests/min, ~20/day per model |
| 504 `UPSTREAM_TIMEOUT` | The provider took over 240 s; the call was charged at its hold |
| Agent blocked unexpectedly | Usage page, filter by agent: the Result column says why |
| See every step of a request | `LOG_LEVEL=DEBUG` in `.env`, `docker compose up -d gateway`, then `docker compose logs -f gateway` |

## 14. Known limitations

- In-flight requests are not cancelled by a kill switch; the next request is blocked.
- Timeouts are charged at the full hold, even if the provider billed less.
- Whether Gemini "thinking" tokens can exceed `max_tokens` is unverified; if they can, a call can cost more than its hold.
- Prices are LiteLLM's table, not the provider's invoice; compare monthly.
- No streaming, images or provider built-in tools. One shared admin token, no TLS: local use only.
- Single gateway replica (startup sweep and the in-memory policy/rate cache assume it).
- Agents must never be attached to the `backend` Docker network.

## Repository layout

```
docker-compose.yml          db, litellm, migrate, gateway
docker-compose.debug.yml    dev only: LiteLLM on 127.0.0.1:4000
docker-compose.test.yml     test overlay: fake model, test runner (project aigw-test)
config/  litellm.yaml  litellm.test.yaml  db-init.sh
gateway/ app.py (API)  db.py (ledger)  policy.py  migrate.py  migrations/  ui/ (console)  Dockerfile
scripts/ gen-env.sh  litellm-setup.sh  gw_admin.py  litellm_api.py  smoke-test.sh  test.sh
tests/   test_policy.py  test_gateway.py  e2e_killswitch.py  mock_upstream.py  live_gemini.sh
docs/plan/  PLAN.txt (v1 plan), PLAN-v3.txt (this version), design debate record
```
