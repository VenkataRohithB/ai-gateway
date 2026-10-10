# AI Agent Gateway

Central gateway in front of company AI agents: attribute and cap spend per agent and per
mandate, with hard ceilings and kill-switches. First consumer: SynBioAI.

**Status:** Milestone 1 (LiteLLM + Postgres + Gemini). FastAPI authorization gate, policy,
budgets and revocation are upcoming milestones.

## Architecture (target)

```
Agent → FastAPI auth gate (:8080, todo) → LiteLLM Proxy (internal only) → Gemini API
                     └──────── PostgreSQL (dbs: gateway, litellm) ────────┘
```

LiteLLM and Postgres are on an internal Docker network and are **not published to the host**,
so agents cannot bypass the gate.

## Setup

Prereqs: Docker + Compose v2, a Gemini API key from https://aistudio.google.com/apikey.

```bash
scripts/gen-env.sh                 # creates .env with random secrets (never overwrites)
$EDITOR .env                       # set GEMINI_API_KEY=...
docker compose up -d
docker compose ps                  # db + litellm should be "healthy"
scripts/smoke-test.sh              # real completion through LiteLLM
```

Keep `.env`: `LITELLM_SALT_KEY` must stay stable, LiteLLM encrypts stored credentials with it.

| File | Purpose |
|---|---|
| `docker-compose.yml` | Postgres 16 + LiteLLM 1.104.0 (pinned by digest) |
| `docker-compose.debug.yml` | Dev only: exposes LiteLLM on 127.0.0.1:4000 (`docker compose -f docker-compose.yml -f docker-compose.debug.yml up -d`) |
| `config/litellm.yaml` | Model routing (version-controlled; models are not stored in DB/UI) |
| `scripts/litellm_api.py` | Call the LiteLLM admin API from inside the container without printing the master key |

Admin API example: `docker compose exec -T litellm python3 - GET /key/list < scripts/litellm_api.py`

## Stop / restart

`docker compose stop` / `docker compose up -d`. Data lives in volume `ai-agent-gateway_pgdata`.
`docker compose down -v` **deletes all data**.

## Adding a model or provider

Add an entry to `config/litellm.yaml` (`api_key: os.environ/NAME`), pass `NAME` through in
`docker-compose.yml`, set it in `.env`, then `docker compose up -d --force-recreate litellm`.
