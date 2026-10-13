"""AI Agent Gateway: authorization gate in front of LiteLLM.

Agent route : POST /m/{mandate}/v1/chat/completions   (agent bearer token)
Admin routes: /admin/*                                 (ADMIN_TOKEN)
Admin UI    : /admin/ui                                (static; asks for ADMIN_TOKEN)
Health      : /health/live, /health/ready
"""
import asyncio, hashlib, hmac, json, logging, os, re, secrets, time, uuid
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Literal

import asyncpg, httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import db
from db import audit
from policy import BUILTIN, CUSTOM_FACTS, Policy

DATABASE_URL = os.environ["DATABASE_URL"]
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_GATEWAY_KEY = os.environ["LITELLM_GATEWAY_KEY"]
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")  # only for the Providers page (keys + models in LiteLLM)
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"].encode()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
USAGE_RETENTION_DAYS = int(os.environ.get("USAGE_RETENTION_DAYS", "90"))
UPSTREAM_TIMEOUT = 270  # LiteLLM 240s < gateway 270s < client 300s
UPSTREAM_TRANSPORT = None  # tests replace this with httpx.MockTransport
SLUG = r"^[a-z0-9-]{1,64}$"
MICROS = 1_000_000
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_FALLBACK = 16384  # output cap for a model whose price entry has no max_output_tokens
SWEEP_EVERY, SWEEP_AFTER, CLEANUP_EVERY = 60, UPSTREAM_TIMEOUT + 300, 3600
BUDGET_TIMEZONE = "Asia/Kolkata"  # must match SQL budget_period() in migrations/003_v3.sql
RETRY_STATUSES = {404, 429, 503}  # model not available for this key / busy / overloaded: never billed, try the next model

log = logging.getLogger("gateway")
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # no per-request noise

def log_event(level: int = logging.INFO, **fields) -> None:
    """Structured JSON log line. Never prompt/response content or secrets, at any level."""
    if log.isEnabledFor(level):
        log.log(level, json.dumps({"level": logging.getLevelName(level).lower(), **fields}, default=str))

class State:
    pool: asyncpg.Pool
    http: httpx.AsyncClient
    litellm_admin: httpx.AsyncClient  # master key; used only by the provider/model management routes
    prices: dict[str, dict] = {}   # model -> LiteLLM model_info (every price field)
    policy: Policy = Policy()
    inr_rate: Decimal = Decimal("85")
    inr_rate_updated: datetime | None = None

state = State()

async def load_prices() -> None:
    """Price entries for the models the gateway key may use, from LiteLLM's pinned price table."""
    r = await state.http.get("/model/info")
    r.raise_for_status()
    prices = {}
    for m in r.json()["data"]:
        info = m.get("model_info") or {}
        if info.get("input_cost_per_token") is not None and info.get("output_cost_per_token") is not None:
            prices[m["model_name"]] = info
    state.prices = prices
    log_event(event="prices_loaded", models=sorted(prices))

async def load_policy() -> None:
    row = await state.pool.fetchrow("SELECT version, rules FROM policy_versions ORDER BY version DESC LIMIT 1")
    state.policy = Policy(json.loads(row["rules"]), row["version"]) if row else Policy()

async def load_settings() -> None:
    row = await state.pool.fetchrow("SELECT value, updated_at FROM settings WHERE key='usd_inr_rate'")
    if row:
        state.inr_rate, state.inr_rate_updated = Decimal(row["value"]), row["updated_at"]

@asynccontextmanager
async def lifespan(app: FastAPI):
    state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    state.http = httpx.AsyncClient(base_url=LITELLM_URL, transport=UPSTREAM_TRANSPORT,
                                   timeout=httpx.Timeout(UPSTREAM_TIMEOUT, connect=5),
                                   headers={"Authorization": f"Bearer {LITELLM_GATEWAY_KEY}"})
    state.litellm_admin = httpx.AsyncClient(base_url=LITELLM_URL, transport=UPSTREAM_TRANSPORT, timeout=60,
                                            headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"})
    await load_policy()   # an invalid stored policy refuses to start
    await load_settings()
    try:
        await load_prices()
    except Exception as e:  # LiteLLM may still be starting; /health/ready retries
        log_event(logging.WARNING, event="prices_unavailable", error=type(e).__name__)
    log_event(event="startup", policy_version=state.policy.version, swept=await db.sweep(state.pool, None))
    sweeper = asyncio.create_task(sweep_forever())
    yield
    sweeper.cancel()
    await state.http.aclose()
    await state.litellm_admin.aclose()
    await state.pool.close()

async def sweep_forever() -> None:
    last_cleanup = 0.0
    while True:
        await asyncio.sleep(SWEEP_EVERY)
        try:
            if n := await db.sweep(state.pool, SWEEP_AFTER):
                log_event(logging.WARNING, event="swept_stale_reservations", count=n)
            if time.monotonic() - last_cleanup > CLEANUP_EVERY:
                usage, res = await db.cleanup(state.pool, USAGE_RETENTION_DAYS)
                last_cleanup = time.monotonic()
                log_event(event="cleanup", usage_rows_deleted=usage, reservations_deleted=res)
        except Exception:
            log.exception("sweeper failed")

app = FastAPI(title="AI Agent Gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------- errors
class GatewayError(Exception):
    """Every non-2xx answer: {"error": {"code", "message", ...}}. Never contains internals."""
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None, **extra):
        self.status, self.headers, self.code, self.message = status, headers, code, message
        self.body = {"error": {"code": code, "message": message, **extra}}

@app.exception_handler(GatewayError)
async def _gateway_error(request: Request, e: GatewayError):
    rid = e.body["error"].get("request_id")
    return JSONResponse(e.body, status_code=e.status, headers={**(e.headers or {}), **({"x-request-id": rid} if rid else {})})

@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, e: RequestValidationError):
    fields = [".".join(str(x) for x in err["loc"][1:]) or "body" for err in e.errors()]
    return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "invalid fields: " + ", ".join(fields),
                                   "fields": fields}}, status_code=400)

@app.exception_handler(Exception)
async def _unexpected(request: Request, e: Exception):
    log.exception("unhandled error")
    return JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": "internal error"}}, status_code=500)


# ---------------------------------------------------------------------------- money helpers
def to_micros(amount: Decimal) -> int:
    return int((amount * MICROS).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))

def amount(micros: int | Decimal | None) -> str | None:
    return None if micros is None else f"{Decimal(micros) / MICROS:.6f}"

def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()

def ledger_view(row) -> dict:
    """Agent/mandate row for the admin API. Budget, spent and reserved are in the row's own currency;
    *_usd fields are the underlying USD ledger. Spend is this budget month's."""
    d = dict(row)
    d.pop("period", None)
    if "token_sha256" in d:
        d["token_issued"] = d.pop("token_sha256") is not None
    rate = state.inr_rate if d["budget_currency"] == "INR" else Decimal(1)
    spent, reserved, limit = d.pop("month_spent_micros"), d.pop("reserved_micros"), d.pop("budget_limit_micros")
    d.pop("spent_micros", None)
    d.update(budget=amount(limit), spent=amount(spent * rate), reserved=amount(reserved * rate),
             spent_usd=amount(spent), reserved_usd=amount(reserved),
             used_pct=round(float((spent + reserved) * rate / limit * 100), 1) if limit else (100.0 if spent + reserved else 0.0))
    return d


# ---------------------------------------------------------------------------- admin API
def require_admin(authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), ADMIN_TOKEN):
        raise GatewayError(401, "ADMIN_UNAUTHORIZED", "missing or invalid admin token")

admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

Currency = Literal["USD", "INR"]
def money(default=...):
    return Field(default, ge=0, le=1_000_000_000, max_digits=18, decimal_places=6)

class AgentCreate(_Strict):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    budget: Decimal = money()
    budget_currency: Currency = "USD"
    rpm_limit: int = Field(default=0, ge=0, le=100_000)      # 0 = no limit

class AgentPatch(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "suspended", "revoked"] | None = None
    budget: Decimal | None = money(None)
    budget_currency: Currency | None = None
    rpm_limit: int | None = Field(default=None, ge=0, le=100_000)

class MandateCreate(_Strict):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    budget: Decimal = money()
    budget_currency: Currency = "USD"
    allowed_models: list[str] = Field(default=[], max_length=50)   # order = fallback order
    default_max_tokens: int = Field(default=4096, ge=1, le=65536)
    fallback_enabled: bool = True

class MandatePatch(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "suspended"] | None = None
    budget: Decimal | None = money(None)
    budget_currency: Currency | None = None
    allowed_models: list[str] | None = Field(default=None, max_length=50)
    default_max_tokens: int | None = Field(default=None, ge=1, le=65536)
    fallback_enabled: bool | None = None

class PolicyUpdate(_Strict):
    rules: list[dict]
    note: str = Field(default="", max_length=300)

class SettingsUpdate(_Strict):
    usd_inr_rate: Decimal = Field(gt=1, le=1000, max_digits=10, decimal_places=4)

def check_models(models: list[str] | None) -> None:
    if not models:
        return
    if len(set(models)) != len(models):
        raise GatewayError(400, "INVALID_REQUEST", "a model is listed twice")
    if unknown := [m for m in models if m not in state.prices]:
        raise GatewayError(400, "UNKNOWN_MODEL", f"not configured/priced in LiteLLM: {', '.join(unknown)}")

def to_columns(body: BaseModel) -> dict:
    cols = body.model_dump(exclude_unset=True, exclude_none=True)
    if "budget" in cols:
        cols["budget_limit_micros"] = to_micros(cols.pop("budget"))
    if "rpm_limit" in cols:
        cols["rpm_limit"] = cols["rpm_limit"] or None
    return cols

AGENT_SELECT = ("SELECT a.*, CASE WHEN a.period = budget_period() THEN a.spent_micros ELSE 0 END AS month_spent_micros, "
                "coalesce(array_agg(am.mandate_id ORDER BY am.mandate_id) FILTER (WHERE am.mandate_id IS NOT NULL), '{{}}') AS mandates, "
                "(SELECT max(at) FROM usage u WHERE u.agent_id = a.id) AS last_request_at "
                "FROM agents a LEFT JOIN agent_mandates am ON am.agent_id=a.id {where} GROUP BY a.id ORDER BY a.id")
MANDATE_SELECT = ("SELECT m.*, CASE WHEN m.period = budget_period() THEN m.spent_micros ELSE 0 END AS month_spent_micros, "
                  "coalesce(array_agg(am.agent_id ORDER BY am.agent_id) FILTER (WHERE am.agent_id IS NOT NULL), '{{}}') AS agents "
                  "FROM mandates m LEFT JOIN agent_mandates am ON am.mandate_id=m.id {where} GROUP BY m.id ORDER BY m.id")

async def fetch_one(kind: str, id_: str, conn=None) -> dict:
    sql = (AGENT_SELECT if kind == "agents" else MANDATE_SELECT).format(where=f"WHERE {kind[0]}.id=$1")
    row = await (conn or state.pool).fetchrow(sql, id_)
    if not row:
        raise GatewayError(404, "NOT_FOUND", f"{kind[:-1]} {id_} not found")
    return ledger_view(row)

async def admin_update(table: str, id_: str, body: BaseModel, action: str):
    """PATCH helper: update given columns, audit before/after in the same transaction."""
    cols = to_columns(body)
    if not cols:
        raise GatewayError(400, "INVALID_REQUEST", "nothing to update")
    async with state.pool.acquire() as c, c.transaction():
        before = await c.fetchrow(f"SELECT * FROM {table} WHERE id=$1 FOR UPDATE", id_)
        if not before:
            raise GatewayError(404, "NOT_FOUND", f"{table[:-1]} {id_} not found")
        sets = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(cols))
        after = await c.fetchrow(f"UPDATE {table} SET {sets}, updated_at=now() WHERE id=$1 RETURNING *", id_, *cols.values())
        changed = {k: {"before": before[k], "after": after[k]} for k in cols if before[k] != after[k]}
        if changed:
            await audit(c, kind="admin", actor="admin", decision=action,
                        **({"agent_id": id_} if table == "agents" else {"mandate_id": id_}), detail={"changed": changed})
        return await fetch_one(table, id_, c)

@admin.get("/agents")
async def list_agents():
    return [ledger_view(r) for r in await state.pool.fetch(AGENT_SELECT.format(where=""))]

@admin.get("/agents/{agent_id}")
async def get_agent(agent_id: str):
    return await fetch_one("agents", agent_id)

@admin.post("/agents", status_code=201)
async def create_agent(body: AgentCreate):
    async with state.pool.acquire() as c, c.transaction():
        row = await c.fetchrow("INSERT INTO agents (id, name, budget_limit_micros, budget_currency, rpm_limit) "
                               "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING RETURNING id", body.id, body.name,
                               to_micros(body.budget), body.budget_currency, body.rpm_limit or None)
        if not row:
            raise GatewayError(409, "ALREADY_EXISTS", f"an agent with id {body.id} already exists")
        await audit(c, kind="admin", actor="admin", decision="agent_created", agent_id=body.id,
                    detail={"name": body.name, "budget": str(body.budget), "currency": body.budget_currency})
        return await fetch_one("agents", body.id, c)

@admin.patch("/agents/{agent_id}")
async def patch_agent(agent_id: str, body: AgentPatch):
    return await admin_update("agents", agent_id, body, "agent_updated")

@admin.post("/agents/{agent_id}/token")
async def issue_token(agent_id: str):
    """Issue or rotate the agent's bearer token. Returned once; only its sha256 is stored."""
    token = "gw_" + secrets.token_urlsafe(32)
    async with state.pool.acquire() as c, c.transaction():
        rotated = await c.fetchval("SELECT token_sha256 IS NOT NULL FROM agents WHERE id=$1 FOR UPDATE", agent_id)
        if rotated is None:
            raise GatewayError(404, "NOT_FOUND", f"agent {agent_id} not found")
        await c.execute("UPDATE agents SET token_sha256=$2, updated_at=now() WHERE id=$1", agent_id, token_hash(token))
        await audit(c, kind="admin", actor="admin", decision="token_rotated" if rotated else "token_issued", agent_id=agent_id)
    return {"agent_id": agent_id, "token": token, "note": "shown once; store it securely"}

@admin.get("/mandates")
async def list_mandates():
    return [ledger_view(r) for r in await state.pool.fetch(MANDATE_SELECT.format(where=""))]

@admin.get("/mandates/{mandate_id}")
async def get_mandate(mandate_id: str):
    return await fetch_one("mandates", mandate_id)

@admin.post("/mandates", status_code=201)
async def create_mandate(body: MandateCreate):
    check_models(body.allowed_models)
    async with state.pool.acquire() as c, c.transaction():
        row = await c.fetchrow(
            "INSERT INTO mandates (id, name, description, budget_limit_micros, budget_currency, allowed_models, "
            "default_max_tokens, fallback_enabled) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING RETURNING id",
            body.id, body.name, body.description, to_micros(body.budget), body.budget_currency, body.allowed_models,
            body.default_max_tokens, body.fallback_enabled)
        if not row:
            raise GatewayError(409, "ALREADY_EXISTS", f"a mandate with id {body.id} already exists")
        await audit(c, kind="admin", actor="admin", decision="mandate_created", mandate_id=body.id,
                    detail={"name": body.name, "budget": str(body.budget), "currency": body.budget_currency,
                            "allowed_models": body.allowed_models})
        return await fetch_one("mandates", body.id, c)

@admin.patch("/mandates/{mandate_id}")
async def patch_mandate(mandate_id: str, body: MandatePatch):
    check_models(body.allowed_models)
    return await admin_update("mandates", mandate_id, body, "mandate_updated")

@admin.put("/agents/{agent_id}/mandates/{mandate_id}")
async def assign(agent_id: str, mandate_id: str):
    async with state.pool.acquire() as c, c.transaction():
        try:
            added = await c.fetchval("INSERT INTO agent_mandates (agent_id, mandate_id) VALUES ($1,$2) "
                                     "ON CONFLICT DO NOTHING RETURNING 1", agent_id, mandate_id)
        except asyncpg.ForeignKeyViolationError:
            raise GatewayError(404, "NOT_FOUND", "agent or mandate not found")
        if added:
            await audit(c, kind="admin", actor="admin", decision="assigned", agent_id=agent_id, mandate_id=mandate_id)
    return {"agent_id": agent_id, "mandate_id": mandate_id, "assigned": True}

@admin.delete("/agents/{agent_id}/mandates/{mandate_id}")
async def unassign(agent_id: str, mandate_id: str):
    async with state.pool.acquire() as c, c.transaction():
        removed = await c.fetchval("DELETE FROM agent_mandates WHERE agent_id=$1 AND mandate_id=$2 RETURNING 1",
                                   agent_id, mandate_id)
        if removed:
            await audit(c, kind="admin", actor="admin", decision="unassigned", agent_id=agent_id, mandate_id=mandate_id)
    return {"agent_id": agent_id, "mandate_id": mandate_id, "assigned": False}

def usage_view(r) -> dict:
    d = dict(r)
    d["cost_usd"] = amount(d.pop("cost_micros"))
    return d

@admin.get("/usage")
async def get_usage(agent: str | None = None, mandate: str | None = None, outcome: str | None = None,
                    model: str | None = None, before_id: int | None = None, limit: int = Query(50, ge=1, le=500)):
    """Request records, newest first. Page with before_id = the last id you received."""
    rows = await state.pool.fetch(
        "SELECT * FROM usage WHERE ($1::text IS NULL OR agent_id=$1) AND ($2::text IS NULL OR mandate_id=$2) "
        "AND ($3::text IS NULL OR outcome=$3) AND ($4::text IS NULL OR model_requested=$4 OR model_served=$4) "
        "AND ($5::bigint IS NULL OR (at, id) < (SELECT at, id FROM usage WHERE id = $5)) ORDER BY at DESC, id DESC LIMIT $6",
        agent, mandate, outcome, model, before_id, limit)
    return [usage_view(r) for r in rows]

@admin.get("/usage/daily")
async def usage_daily(days: int = Query(30, ge=1, le=90), agent: str | None = None, mandate: str | None = None):
    """Per day (India time) for the last `days` days, from the raw usage rows."""
    rows = await state.pool.fetch(
        "SELECT d::date AS day, coalesce(sum(u.cost_micros), 0) AS cost_micros, count(u.id) FILTER (WHERE u.outcome <> 'denied') AS requests, "
        "count(u.id) FILTER (WHERE u.outcome = 'denied') AS denied "
        "FROM generate_series((now() AT TIME ZONE 'Asia/Kolkata')::date - ($1 - 1), (now() AT TIME ZONE 'Asia/Kolkata')::date, interval '1 day') d "
        "LEFT JOIN usage u ON (u.at AT TIME ZONE 'Asia/Kolkata')::date = d::date "
        "AND ($2::text IS NULL OR u.agent_id=$2) AND ($3::text IS NULL OR u.mandate_id=$3) GROUP BY d ORDER BY d", days, agent, mandate)
    return [usage_view(r) for r in rows]

@admin.get("/usage/monthly")
async def usage_monthly(month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"), agent: str | None = None,
                        mandate: str | None = None):
    """Monthly totals (kept forever) per agent x mandate x model. Default: the current budget month."""
    months = [r["month"].strftime("%Y-%m") for r in
              await state.pool.fetch("SELECT DISTINCT month FROM usage_monthly ORDER BY month DESC")]
    current = (await state.pool.fetchval("SELECT budget_period()")).strftime("%Y-%m")
    month = month or current
    rows = await state.pool.fetch(
        "SELECT agent_id, mandate_id, model, requests, prompt_tokens, completion_tokens, cost_micros FROM usage_monthly "
        "WHERE month = to_date($1, 'YYYY-MM') AND ($2::text IS NULL OR agent_id=$2) AND ($3::text IS NULL OR mandate_id=$3) "
        "ORDER BY cost_micros DESC", month, agent, mandate)
    return {"month": month, "current_month": current, "months": sorted(set(months) | {current}, reverse=True),
            "rows": [usage_view(r) for r in rows]}

@admin.get("/overview")
async def overview():
    month = await state.pool.fetchrow(
        "SELECT budget_period() AS month, coalesce(sum(cost_micros), 0) AS cost_micros, coalesce(sum(requests), 0) AS requests "
        "FROM usage_monthly WHERE month = budget_period()")
    today = await state.pool.fetchrow(
        "SELECT count(*) FILTER (WHERE outcome='ok') AS ok, count(*) FILTER (WHERE outcome='denied') AS denied, "
        "count(*) FILTER (WHERE outcome IN ('error','timeout')) AS failed, coalesce(sum(cost_micros), 0) AS cost_micros "
        "FROM usage WHERE (at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date")
    timeouts = await state.pool.fetchrow(
        "SELECT count(*) AS n, coalesce(sum(cost_micros), 0) AS cost_micros FROM usage "
        "WHERE outcome IN ('timeout', 'error') AND cost_micros > 0 AND at >= (budget_period()::timestamp AT TIME ZONE 'Asia/Kolkata')")
    return {"month": month["month"].strftime("%Y-%m"), "month_cost_usd": amount(month["cost_micros"]),
            "month_requests": month["requests"],
            "today": {"ok": today["ok"], "denied": today["denied"], "failed": today["failed"], "cost_usd": amount(today["cost_micros"])},
            "charged_without_answer": {"count": timeouts["n"], "cost_usd": amount(timeouts["cost_micros"])},
            "usd_inr_rate": str(state.inr_rate), "policy_version": state.policy.version,
            "agents": await list_agents(), "mandates": await list_mandates()}

@admin.get("/audit")
async def get_audit(agent: str | None = None, mandate: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    """Admin changes: who changed what, before and after. Append-only."""
    rows = await state.pool.fetch(
        "SELECT id, at, actor, agent_id, mandate_id, decision, detail FROM audit_events WHERE kind='admin' "
        "AND ($1::text IS NULL OR agent_id=$1) AND ($2::text IS NULL OR mandate_id=$2) ORDER BY id DESC LIMIT $3",
        agent, mandate, limit)
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

@admin.get("/policy")
async def get_policy():
    history = await state.pool.fetch(
        "SELECT version, note, created_at, jsonb_array_length(rules) AS rule_count FROM policy_versions ORDER BY version DESC LIMIT 50")
    return {"version": state.policy.version, "number": state.policy.number, "builtin": BUILTIN,
            "custom": state.policy.custom, "custom_facts": sorted(CUSTOM_FACTS), "history": [dict(r) for r in history]}

@admin.put("/policy")
async def put_policy(body: PolicyUpdate):
    """Save the custom rules as a new policy version. Takes effect for the next request."""
    try:
        new = Policy(body.rules)
    except ValueError as e:
        raise GatewayError(400, "INVALID_POLICY", str(e))
    async with state.pool.acquire() as c, c.transaction():
        number = await c.fetchval("INSERT INTO policy_versions (rules, note) VALUES ($1::jsonb, $2) RETURNING version",
                                  json.dumps(new.custom), body.note)
        before = state.policy
        state.policy = Policy(new.custom, number)  # ponytail: in-memory reload assumes one gateway replica
        await audit(c, kind="admin", actor="admin", decision="policy_updated", policy_version=state.policy.version,
                    detail={"from": before.version, "to": state.policy.version, "note": body.note,
                            "rules": {"before": before.custom, "after": new.custom}})
    return await get_policy()

@admin.get("/settings")
async def get_settings():
    return {"usd_inr_rate": str(state.inr_rate), "usd_inr_rate_updated_at": state.inr_rate_updated,
            "budget_timezone": BUDGET_TIMEZONE, "usage_retention_days": USAGE_RETENTION_DAYS,
            "policy_version": state.policy.version, "log_level": LOG_LEVEL}

@admin.put("/settings")
async def put_settings(body: SettingsUpdate):
    async with state.pool.acquire() as c, c.transaction():
        before = await c.fetchval("SELECT value FROM settings WHERE key='usd_inr_rate' FOR UPDATE")
        await c.execute("INSERT INTO settings (key, value, updated_at) VALUES ('usd_inr_rate', $1, now()) "
                        "ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=now()", str(body.usd_inr_rate))
        await audit(c, kind="admin", actor="admin", decision="settings_updated",
                    detail={"changed": {"usd_inr_rate": {"before": before, "after": str(body.usd_inr_rate)}}})
    await load_settings()
    return await get_settings()

@admin.get("/models")
async def list_models():
    used = {}
    for r in await state.pool.fetch("SELECT id, allowed_models FROM mandates ORDER BY id"):
        for m in r["allowed_models"]:
            used.setdefault(m, []).append(r["id"])
    per_m = lambda v: None if v is None else str(Decimal(str(v)) * MICROS)
    return [{"model": m, "provider": info.get("litellm_provider"),
             "input_usd_per_1m": per_m(info.get("input_cost_per_token")),
             "output_usd_per_1m": per_m(info.get("output_cost_per_token")),
             "max_output_tokens": info.get("max_output_tokens") or MAX_OUTPUT_FALLBACK,
             "mandates": used.get(m, [])} for m, info in sorted(state.prices.items())]

# ---------------------------------------------------------------------------- providers (keys + models)
# Provider keys are stored by LiteLLM in its own database, encrypted with LITELLM_SALT_KEY, which only LiteLLM
# has. LiteLLM's API never returns a stored key unmasked, so neither does this API. Keys never touch the
# gateway's database or logs; the audit trail records only the key's name and provider.
PROVIDERS = {  # console choice -> (label, LiteLLM model prefix)
    "gemini": ("Google Gemini", "gemini"), "openai": ("OpenAI", "openai"), "anthropic": ("Anthropic", "anthropic"),
    "mistral": ("Mistral", "mistral"), "deepseek": ("DeepSeek", "deepseek"), "openrouter": ("OpenRouter", "openrouter"),
    "openai_compatible": ("OpenAI-compatible (custom URL)", "openai"),
}
MODEL_ALIAS = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

class ProviderKeyCreate(_Strict):
    name: str = Field(pattern=SLUG)
    provider: Literal["gemini", "openai", "anthropic", "mistral", "deepseek", "openrouter", "openai_compatible"]
    api_key: str = Field(min_length=8, max_length=4096)
    api_base: str | None = Field(default=None, pattern=r"^https?://[^\s]{1,500}$")

class ModelCreate(_Strict):
    name: str = Field(pattern=MODEL_ALIAS)                          # what agents and mandates use
    provider_key: str = Field(pattern=SLUG)
    provider_model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")  # the provider's own model id
    input_usd_per_1m: Decimal | None = Field(default=None, ge=0, le=100_000)
    output_usd_per_1m: Decimal | None = Field(default=None, ge=0, le=100_000)

async def litellm_call(method: str, path: str, body: dict | None = None) -> dict:
    if not LITELLM_MASTER_KEY:
        raise GatewayError(503, "PROVIDERS_DISABLED", "provider management needs LITELLM_MASTER_KEY in the gateway's environment")
    try:
        r = await state.litellm_admin.request(method, path, json=body)
    except httpx.TransportError:
        raise GatewayError(503, "UPSTREAM_UNAVAILABLE", "LiteLLM is not reachable")
    if r.status_code >= 400:  # never echo LiteLLM's body: it could contain request values
        log_event(logging.WARNING, event="litellm_admin_error", path=path.split("?")[0], status=r.status_code)
        if r.status_code == 409:
            raise GatewayError(409, "ALREADY_EXISTS", "that name is already used")
        raise GatewayError(502, "LITELLM_ERROR", f"LiteLLM refused the change (HTTP {r.status_code}); see gateway logs")
    return r.json() if r.content else {}

async def sync_gateway_key() -> None:
    """Let the gateway's LiteLLM key use every configured model, then reload prices."""
    models = sorted(m["id"] for m in (await litellm_call("GET", "/v1/models"))["data"])
    await litellm_call("POST", "/key/update", {"key": LITELLM_GATEWAY_KEY, "models": models})
    await load_prices()

async def provider_state() -> tuple[list[dict], list[dict]]:
    creds = (await litellm_call("GET", "/credentials")).get("credentials", [])
    deployments = (await litellm_call("GET", "/model/info")).get("data", [])
    return creds, deployments

def mandates_using(model: str, rows) -> list[str]:
    return [r["id"] for r in rows if model in r["allowed_models"]]

@admin.get("/providers")
async def list_providers():
    creds, deployments = await provider_state()
    mandate_rows = await state.pool.fetch("SELECT id, allowed_models FROM mandates ORDER BY id")
    models = []
    for d in deployments:
        lp, mi = d.get("litellm_params") or {}, d.get("model_info") or {}
        info = state.prices.get(d["model_name"]) or {}
        per_m = lambda v: None if v is None else str(Decimal(str(v)) * MICROS)
        models.append({"name": d["model_name"], "provider_model": lp.get("model"), "provider_key": lp.get("litellm_credential_name"),
                       "provider": mi.get("litellm_provider"), "source": "console" if mi.get("db_model") else "config",
                       "priced": d["model_name"] in state.prices, "input_usd_per_1m": per_m(info.get("input_cost_per_token")),
                       "output_usd_per_1m": per_m(info.get("output_cost_per_token")),
                       "mandates": mandates_using(d["model_name"], mandate_rows)})
    keys = []
    for c in creds:
        values, cinfo = c.get("credential_values") or {}, c.get("credential_info") or {}
        provider = cinfo.get("gateway_provider")
        keys.append({"name": c["credential_name"], "provider": provider, "label": PROVIDERS.get(provider, (provider,))[0],
                     "key_hint": values.get("api_key"),  # masked by LiteLLM, e.g. "sk-****wxyz"
                     "api_base": values.get("api_base"), "added_at": cinfo.get("added_at"),
                     "models": [m["name"] for m in models if m["provider_key"] == c["credential_name"]]})
    env_providers = sorted({m["provider"] for m in models if m["source"] == "config" and m["provider"]})
    return {"enabled": True, "keys": keys, "models": models, "env_providers": env_providers,
            "provider_choices": [{"id": k, "label": v[0]} for k, v in PROVIDERS.items()]}

@admin.post("/providers", status_code=201)
async def add_provider_key(body: ProviderKeyCreate):
    if body.provider == "openai_compatible" and not body.api_base:
        raise GatewayError(400, "INVALID_REQUEST", "an OpenAI-compatible provider needs its base URL")
    values = {"api_key": body.api_key.strip(), **({"api_base": body.api_base} if body.api_base else {})}
    await litellm_call("POST", "/credentials", {"credential_name": body.name, "credential_values": values,
                       "credential_info": {"gateway_provider": body.provider, "added_at": datetime.now().astimezone().isoformat()}})
    await audit(state.pool, kind="admin", actor="admin", decision="provider_key_added",
                detail={"name": body.name, "provider": body.provider})
    return {"name": body.name, "provider": body.provider}

@admin.delete("/providers/{name}")
async def remove_provider_key(name: str):
    creds, deployments = await provider_state()
    if not any(c["credential_name"] == name for c in creds):
        raise GatewayError(404, "NOT_FOUND", f"provider key {name} not found")
    if used := [d["model_name"] for d in deployments if (d.get("litellm_params") or {}).get("litellm_credential_name") == name]:
        raise GatewayError(409, "IN_USE", f"remove these models first: {', '.join(used)}")
    await litellm_call("DELETE", f"/credentials/{name}")
    await audit(state.pool, kind="admin", actor="admin", decision="provider_key_removed", detail={"name": name})
    return {"name": name, "removed": True}

@admin.post("/models", status_code=201)
async def add_model(body: ModelCreate):
    creds, deployments = await provider_state()
    cred = next((c for c in creds if c["credential_name"] == body.provider_key), None)
    if not cred:
        raise GatewayError(404, "NOT_FOUND", f"provider key {body.provider_key} not found")
    if any(d["model_name"] == body.name for d in deployments):
        raise GatewayError(409, "ALREADY_EXISTS", f"a model named {body.name} already exists")
    if (body.input_usd_per_1m is None) != (body.output_usd_per_1m is None):
        raise GatewayError(400, "INVALID_REQUEST", "give both prices or neither")
    prefix = PROVIDERS.get((cred.get("credential_info") or {}).get("gateway_provider"), ("", "openai"))[1]
    params = {"model": f"{prefix}/{body.provider_model}", "litellm_credential_name": body.provider_key}
    if body.input_usd_per_1m is not None:
        params |= {"input_cost_per_token": float(body.input_usd_per_1m / MICROS),
                   "output_cost_per_token": float(body.output_usd_per_1m / MICROS)}
    created = await litellm_call("POST", "/model/new", {"model_name": body.name, "litellm_params": params,
                                                         "model_info": {"created_via": "gateway-console"}})
    await sync_gateway_key()
    if body.name not in state.prices:  # unpriced models can't be budgeted: undo and ask for prices
        if model_id := (created.get("model_info") or {}).get("id") or created.get("model_id"):
            await litellm_call("POST", "/model/delete", {"id": model_id})
            await sync_gateway_key()
        raise GatewayError(400, "PRICE_UNKNOWN", f"LiteLLM has no price for {params['model']}; enter its input and output price per 1M tokens")
    await audit(state.pool, kind="admin", actor="admin", decision="model_added",
                detail={"name": body.name, "provider_model": params["model"], "provider_key": body.provider_key})
    return {"name": body.name, "provider_model": params["model"]}

@admin.delete("/models/{name}")
async def remove_model(name: str):
    _, deployments = await provider_state()
    d = next((d for d in deployments if d["model_name"] == name), None)
    if not d:
        raise GatewayError(404, "NOT_FOUND", f"model {name} not found")
    if not (d.get("model_info") or {}).get("db_model"):
        raise GatewayError(400, "CONFIG_MODEL", "this model is defined in config/litellm.yaml; remove it there")
    if used := mandates_using(name, await state.pool.fetch("SELECT id, allowed_models FROM mandates")):
        raise GatewayError(409, "IN_USE", f"remove it from these mandates first: {', '.join(used)}")
    await litellm_call("POST", "/model/delete", {"id": d["model_info"]["id"]})
    await sync_gateway_key()
    await audit(state.pool, kind="admin", actor="admin", decision="model_removed", detail={"name": name})
    return {"name": name, "removed": True}

@app.get("/health/live")
async def live():
    return {"status": "ok"}

@app.get("/health/ready")
async def ready():
    checks = {"db": False, "litellm": False, "prices": False}
    try:
        checks["db"] = await state.pool.fetchval("SELECT 1") == 1
    except Exception:
        pass
    try:
        checks["litellm"] = (await state.http.get("/health/liveliness", timeout=3)).status_code == 200
    except Exception:
        pass
    if not state.prices and checks["litellm"]:
        try:
            await load_prices()
        except Exception:
            pass
    checks["prices"] = bool(state.prices)
    return JSONResponse(checks, status_code=200 if all(checks.values()) else 503)


app.include_router(admin)

# Admin UI: static files only; they hold no data and call the admin API above with the admin token.
UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
UI_FILES = {"": "index.html", "app.js": "app.js", "app.css": "app.css"}
UI_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store"}

@app.get("/admin/ui", include_in_schema=False)
async def ui_index():
    return FileResponse(os.path.join(UI_DIR, "index.html"), headers=UI_HEADERS)

@app.get("/admin/ui/{asset}", include_in_schema=False)
async def ui_asset(asset: str):
    if asset not in UI_FILES or not asset:
        raise GatewayError(404, "NOT_FOUND", "not found")
    return FileResponse(os.path.join(UI_DIR, UI_FILES[asset]), headers=UI_HEADERS)


# ---------------------------------------------------------------------------- cost estimate
IN_RATE = re.compile(r"^(?:input_cost_per_token|cache_creation_input_token_cost(?:_above_1hr)?)(?:_above_(\d+)k_tokens)?$")
OUT_RATE = re.compile(r"^(?:output_cost_per_token|output_cost_per_reasoning_token)(?:_above_(\d+)k_tokens)?$")

def worst_rates(info: dict, n_bytes: int) -> tuple[Decimal, Decimal]:
    """Highest per-token input and output price that could apply to this request.

    Counts cache writes, reasoning tokens and long-prompt tiers. A token is at least one byte, so an
    "above N k tokens" tier can only apply when the request is longer than N*1000 bytes. Cheaper modes
    (batch, flex, cache reads) are ignored; priority is never requested (service_tier is refused).
    LiteLLM's tiered_pricing lists, when present, are all counted (conservative)."""
    def best(pattern: re.Pattern, tier_keys: tuple[str, ...]) -> Decimal:
        rates = [Decimal(str(v)) for k, v in info.items()
                 if isinstance(v, (int, float)) and (m := pattern.match(k)) and (m[1] is None or n_bytes > int(m[1]) * 1000)]
        for tier in info.get("tiered_pricing") or []:
            rates += [Decimal(str(tier[k])) for k in tier_keys if isinstance(tier, dict) and isinstance(tier.get(k), (int, float))]
        return max(rates, default=Decimal(0))
    return (best(IN_RATE, ("input_cost_per_token",)),
            best(OUT_RATE, ("output_cost_per_token", "output_cost_per_reasoning_token")))

def max_output(model: str) -> int:
    return int(state.prices[model].get("max_output_tokens") or MAX_OUTPUT_FALLBACK)

def estimate_micros(model: str, n_bytes: int, max_tokens: int) -> int:
    """Worst-case cost: every request byte counted as an input token (bytes >= tokens) at the highest
    input price that could apply, plus the full output cap at the highest output price."""
    pin, pout = worst_rates(state.prices[model], n_bytes)
    return int(((n_bytes * pin + max_tokens * pout) * MICROS).to_integral_value(rounding=ROUND_CEILING))

def parse_cost(header: str | None) -> Decimal | None:
    try:
        cost = Decimal(header)
    except (TypeError, InvalidOperation):
        return None
    return cost if cost.is_finite() and cost > 0 else None


# ---------------------------------------------------------------------------- agent API
ALLOWED_FIELDS = {"model", "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop",
                  "seed", "tools", "tool_choice", "response_format", "n", "stream"}
MAX_REQUESTED_TOKENS = 65536

def _bad(code: str, message: str):
    raise GatewayError(400, code, message)

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def validate_body(raw: bytes) -> tuple[dict, str, int | None]:
    """Strict allowlist. Returns (outbound body without model/token cap, model, requested max_tokens or None).
    Anything that could re-route the request, change credentials, or make cost unbounded is rejected."""
    try:
        body = json.loads(raw)
    except ValueError:
        _bad("INVALID_JSON", "body is not valid JSON")
    if not isinstance(body, dict):
        _bad("INVALID_REQUEST", "body must be a JSON object")
    body = {k: v for k, v in body.items() if v is not None}
    if extra := sorted(set(body) - ALLOWED_FIELDS):
        _bad("FIELD_NOT_ALLOWED", f"field(s) not allowed: {', '.join(extra)}")
    model = body.get("model")
    if not isinstance(model, str) or not 0 < len(model) <= 128:
        _bad("INVALID_REQUEST", "model must be a non-empty string")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        _bad("INVALID_REQUEST", "messages must be a non-empty list")
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            _bad("INVALID_REQUEST", "each message needs a string role")
        content = m.get("content")
        if isinstance(content, list):
            if not all(isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str) for p in content):
                _bad("CONTENT_NOT_ALLOWED", "only text content parts are supported (no images/audio/files)")
        elif content is not None and not isinstance(content, str):
            _bad("INVALID_REQUEST", "message content must be a string, null, or text parts")
    if "max_tokens" in body and "max_completion_tokens" in body:
        _bad("INVALID_REQUEST", "send max_tokens or max_completion_tokens, not both")
    requested = body.get("max_tokens", body.get("max_completion_tokens"))
    if requested is not None and (type(requested) is not int or not 1 <= requested <= MAX_REQUESTED_TOKENS):
        _bad("INVALID_REQUEST", f"max_tokens must be an integer from 1 to {MAX_REQUESTED_TOKENS}")
    if body.get("stream", False) is not False:
        _bad("STREAMING_NOT_SUPPORTED", "stream=true is not supported yet")
    if body.get("n", 1) != 1 or type(body.get("n", 1)) is not int:
        _bad("INVALID_REQUEST", "n must be 1")
    tools = body.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, dict) and t.get("type") == "function"
                                              and isinstance(t.get("function"), dict) for t in tools):
        _bad("TOOL_NOT_ALLOWED", "only function tools are allowed (no provider built-in tools)")
    for k in ("temperature", "top_p"):
        if k in body and not _is_num(body[k]):
            _bad("INVALID_REQUEST", f"{k} must be a number")
    if "seed" in body and type(body["seed"]) is not int:
        _bad("INVALID_REQUEST", "seed must be an integer")
    if "stop" in body and not (isinstance(body["stop"], str) or
                               (isinstance(body["stop"], list) and all(isinstance(x, str) for x in body["stop"]))):
        _bad("INVALID_REQUEST", "stop must be a string or list of strings")
    if "tool_choice" in body and not isinstance(body["tool_choice"], (str, dict)):
        _bad("INVALID_REQUEST", "tool_choice must be a string or object")
    if "response_format" in body and not isinstance(body["response_format"], dict):
        _bad("INVALID_REQUEST", "response_format must be an object")
    outbound = {k: v for k, v in body.items() if k not in ("model", "max_tokens", "max_completion_tokens", "stream", "n")}
    return outbound, model, requested

async def load_request_context(agent_id: str, mandate_id: str):
    return await state.pool.fetchrow(
        "SELECT a.status AS agent_status, m.status AS mandate_status, m.allowed_models, m.default_max_tokens, "
        "m.fallback_enabled, EXISTS (SELECT 1 FROM agent_mandates WHERE agent_id=a.id AND mandate_id=$2) AS assigned "
        "FROM agents a LEFT JOIN mandates m ON m.id=$2 WHERE a.id=$1", agent_id, mandate_id)

def facts_for(ctx, agent_id: str, mandate_id: str, model: str) -> dict:
    return {"agent.status": ctx["agent_status"], "assignment.exists": ctx["assigned"],
            "mandate.status": ctx["mandate_status"] or "unknown",
            "model.allowed": model in (ctx["allowed_models"] or []) and model in state.prices,
            "agent.id": agent_id, "mandate.id": mandate_id, "model.name": model}

@app.post("/m/{mandate_id}/v1/chat/completions")
async def chat_completions(mandate_id: str, request: Request, authorization: str = Header(default="")):
    started = time.monotonic()
    request_id = uuid.uuid4()
    rid = str(request_id)
    mandate_id = mandate_id[:64]

    # 1. Authenticate: identity comes only from the token.
    scheme, _, token = authorization.partition(" ")
    agent_id = None
    if scheme.lower() == "bearer" and token.startswith("gw_"):
        agent_id = await state.pool.fetchval("SELECT id FROM agents WHERE token_sha256=$1", token_hash(token))
    if not agent_id:  # not recorded (unauthenticated callers could flood the usage table)
        log_event(logging.WARNING, event="auth_failed", request_id=rid, mandate=mandate_id,
                  client=request.client.host if request.client else None)
        raise GatewayError(401, "UNAUTHORIZED", "missing or invalid agent token", request_id=rid)
    policy = state.policy  # one policy version for the whole request

    async def finish(outcome: str, http_status: int, **fields) -> None:
        await db.record_usage(state.pool, request_id=request_id, agent_id=agent_id, mandate_id=mandate_id,
                              outcome=outcome, http_status=http_status, policy_version=policy.version,
                              latency_ms=int((time.monotonic() - started) * 1000), **fields)

    async def deny(status: int, code: str, message: str, model: str | None = None):
        await finish("denied", status, reason_code=code, model_requested=model)
        log_event(event="deny", request_id=rid, agent=agent_id, mandate=mandate_id, model=model, reason=code)
        extra = {"policy_version": policy.version} if status in (402, 403) else {}
        raise GatewayError(status, code, message, request_id=rid, **extra)

    # 2. Validate size and body.
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        await deny(413, "REQUEST_TOO_LARGE", f"body over {MAX_BODY_BYTES} bytes")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        await deny(413, "REQUEST_TOO_LARGE", f"body over {MAX_BODY_BYTES} bytes")
    try:
        body, model, requested_tokens = validate_body(raw)
    except GatewayError as e:
        await deny(e.status, e.code, e.message)

    # 3. Policy on fresh facts.
    ctx = await load_request_context(agent_id, mandate_id)
    decision = policy.evaluate(facts_for(ctx, agent_id, mandate_id, model))
    log_event(logging.DEBUG, event="policy", request_id=rid, agent=agent_id, mandate=mandate_id, model=model, decision=decision)
    if not decision["allow"]:
        await deny(403, decision["reason"], decision["message"], model)

    # 4. Attempt the requested model, then (if enabled) the models after it in the mandate's list.
    allowed = list(ctx["allowed_models"])
    chain = [model] + (allowed[allowed.index(model) + 1:] if ctx["fallback_enabled"] else [])
    total_cost, attempts, last = 0, 0, None
    for i, m in enumerate(chain):
        if i:  # every fallback model passes policy on its own
            if not policy.evaluate(facts_for(ctx, agent_id, mandate_id, m))["allow"]:
                log_event(logging.DEBUG, event="fallback_skipped", request_id=rid, model=m, reason="policy")
                continue
        cap = max_output(m)
        if requested_tokens and requested_tokens > cap:
            if not i:
                await deny(400, "INVALID_REQUEST", f"max_tokens for {m} must be at most {cap}", model)
            continue
        max_tokens = requested_tokens or min(ctx["default_max_tokens"], cap)
        out_bytes = json.dumps({"model": m, **body, "max_tokens": max_tokens}, separators=(",", ":")).encode()
        est = estimate_micros(m, len(out_bytes), max_tokens)
        reservation_id = request_id if not i else uuid.uuid4()
        failed = await db.reserve(state.pool, reservation_id, request_id, agent_id, mandate_id, m, est, state.inr_rate)
        log_event(logging.DEBUG, event="reserve", request_id=rid, model=m, estimate_micros=est, max_tokens=max_tokens,
                  result=failed or "held")
        if failed:
            if i:  # budget ran out mid-chain: answer with the last upstream error
                break
            if failed == "rate":
                await deny(429, "AGENT_RATE_LIMITED", "agent request rate limit reached; retry in a minute", model)
            ctx = await load_request_context(agent_id, mandate_id)
            decision = policy.evaluate(facts_for(ctx, agent_id, mandate_id, model))
            if not decision["allow"]:  # state changed concurrently (e.g. revoked just now)
                await deny(403, decision["reason"], decision["message"], model)
            if failed == "assignment":
                await deny(403, "MANDATE_NOT_ASSIGNED", "agent is not assigned to this mandate", model)
            await deny(402, "AGENT_BUDGET_EXCEEDED" if failed == "agent" else "MANDATE_BUDGET_EXCEEDED",
                       f"{failed} budget cannot cover this request (worst-case estimate {amount(est)} USD)", model)
        attempts += 1
        kind, result, info = await forward(reservation_id, request_id, m, out_bytes, est)
        total_cost += info.get("cost", 0)
        log_event(event="attempt", request_id=rid, agent=agent_id, mandate=mandate_id, model=m, attempt=attempts,
                  result=kind, status=info.get("status"), reserved=est, cost=info.get("cost", 0), source=info.get("source"))
        if kind == "ok":
            await finish("ok", 200, model_requested=model, model_served=m, attempts=attempts, cost_micros=total_cost,
                         cost_source=info["source"], prompt_tokens=info.get("prompt_tokens"),
                         completion_tokens=info.get("completion_tokens"))
            return JSONResponse(result, headers={"x-request-id": rid, "x-gateway-model": m})
        last = (result, info)
        if kind == "final":
            break
    result, info = last
    await finish(info.get("outcome", "error"), result.status, model_requested=model, attempts=attempts,
                 cost_micros=total_cost, cost_source=info.get("source"), reason_code=result.code)
    result.body["error"]["request_id"] = rid
    raise result

async def forward(reservation_id: uuid.UUID, request_id: uuid.UUID, model: str, out_bytes: bytes, est: int):
    """One upstream attempt, settled. Returns (kind, response-or-error, info); kind is "ok", "retry"
    (try the next fallback model) or "final"."""
    try:
        r = await state.http.post("/v1/chat/completions", content=out_bytes,
                                  headers={"content-type": "application/json", "x-litellm-call-id": str(reservation_id)})
    except (httpx.ConnectError, httpx.ConnectTimeout):  # never sent: nothing billed
        await db.settle(state.pool, reservation_id, "released", 0)
        return "retry", GatewayError(503, "UPSTREAM_UNAVAILABLE", "model gateway unavailable"), {"source": "none"}
    except httpx.TransportError:  # sent, outcome unknown: charge the reservation
        row = await db.settle(state.pool, reservation_id, "settled", None)
        return "final", GatewayError(504, "UPSTREAM_TIMEOUT", "model call timed out; charged at the reserved amount"), \
            {"outcome": "timeout", "source": "reservation", "cost": row["cost_micros"] if row else 0}

    if r.status_code == 200:
        try:
            data = r.json()
            data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            row = await db.settle(state.pool, reservation_id, "settled", None)
            return "final", GatewayError(502, "UPSTREAM_ERROR", "invalid response from model"), \
                {"status": 200, "source": "reservation", "cost": row["cost_micros"] if row else 0}
        usage = data.get("usage") or {}
        cost = parse_cost(r.headers.get("x-litellm-response-cost"))
        if cost is not None:
            charge, source = int((cost * MICROS).to_integral_value(rounding=ROUND_CEILING)), "litellm_header"
        elif (usage.get("total_tokens") or 0) > 0:
            charge, source = None, "reservation"  # tokens used but cost unknown: charge the reservation
        else:
            charge, source = 0, "none"
        row = await db.settle(state.pool, reservation_id, "settled", charge, usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return "ok", data, {"status": 200, "source": source, "cost": row["cost_micros"] if row else 0,
                            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}

    upstream_msg = ""
    try:
        upstream_msg = str((r.json().get("error") or {}).get("message", ""))[:500]
    except Exception:
        pass
    log_event(logging.WARNING, event="upstream_error", request_id=str(request_id), model=model, status=r.status_code,
              message=upstream_msg[:200])
    info = {"status": r.status_code, "source": "none"}
    if r.status_code in (408, 504):
        row = await db.settle(state.pool, reservation_id, "settled", None)
        return "final", GatewayError(504, "UPSTREAM_TIMEOUT", "model call timed out; charged at the reserved amount"), \
            {**info, "outcome": "timeout", "source": "reservation", "cost": row["cost_micros"] if row else 0}
    await db.settle(state.pool, reservation_id, "released", 0)
    if r.status_code in (401, 403, 422):  # gateway key problem or LiteLLM backstop budget: never leak details
        log_event(logging.ERROR, event="litellm_rejected_gateway_key", status=r.status_code)
        return "final", GatewayError(503, "UPSTREAM_UNAVAILABLE", "model gateway unavailable"), info
    if r.status_code == 429:
        retry = {"retry-after": r.headers["retry-after"]} if "retry-after" in r.headers else None
        return "retry", GatewayError(429, "UPSTREAM_RATE_LIMITED", "model provider rate limit; retry later or use another model",
                                     headers=retry), info
    status = r.status_code if 400 <= r.status_code < 600 else 502
    err = GatewayError(status, "UPSTREAM_ERROR", upstream_msg if status in (400, 404) else "model provider error")
    return ("retry" if status in RETRY_STATUSES else "final"), err, info
