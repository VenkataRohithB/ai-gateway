"""AI Agent Gateway: authorization gate in front of LiteLLM.

Agent route : POST /m/{mandate}/v1/chat/completions   (agent bearer token)
Admin routes: /admin/*                                 (ADMIN_TOKEN)
Health      : /health/live, /health/ready
"""
import hashlib, hmac, json, logging, os, secrets, uuid
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

import asyncpg, httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

DATABASE_URL = os.environ["DATABASE_URL"]
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_GATEWAY_KEY = os.environ["LITELLM_GATEWAY_KEY"]
UPSTREAM_TIMEOUT = 270  # LiteLLM 240s < gateway 270s < client 300s
UPSTREAM_TRANSPORT = None  # tests replace this with httpx.MockTransport
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"].encode()
SLUG = r"^[a-z0-9-]{1,64}$"
MICROS = 1_000_000

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(message)s")

def log_event(**fields) -> None:
    log.info(json.dumps(fields, default=str))

class State:
    pool: asyncpg.Pool
    http: httpx.AsyncClient
    prices: dict[str, tuple[Decimal, Decimal]] = {}  # model -> (input, output) USD per token

state = State()

async def load_prices() -> None:
    """Per-token prices for the models the gateway key may use, from LiteLLM's pinned price table."""
    r = await state.http.get("/model/info")
    r.raise_for_status()
    prices = {}
    for m in r.json()["data"]:
        info = m.get("model_info") or {}
        if info.get("input_cost_per_token") is not None and info.get("output_cost_per_token") is not None:
            prices[m["model_name"]] = (Decimal(str(info["input_cost_per_token"])), Decimal(str(info["output_cost_per_token"])))
    state.prices = prices
    log_event(event="prices_loaded", models=sorted(prices))

@asynccontextmanager
async def lifespan(app: FastAPI):
    state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    state.http = httpx.AsyncClient(base_url=LITELLM_URL, transport=UPSTREAM_TRANSPORT,
                                   timeout=httpx.Timeout(UPSTREAM_TIMEOUT, connect=5),
                                   headers={"Authorization": f"Bearer {LITELLM_GATEWAY_KEY}"})
    try:
        await load_prices()
    except Exception as e:  # LiteLLM may still be starting; /health/ready retries
        log_event(event="prices_unavailable", error=type(e).__name__)
    yield
    await state.http.aclose()
    await state.pool.close()

app = FastAPI(title="AI Agent Gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------- errors
class GatewayError(Exception):
    """Every non-2xx answer: {"error": {"code", "message", ...}}. Never contains internals."""
    def __init__(self, status: int, code: str, message: str, **extra):
        self.status, self.body = status, {"error": {"code": code, "message": message, **extra}}

@app.exception_handler(GatewayError)
async def _gateway_error(request: Request, e: GatewayError):
    return JSONResponse(e.body, status_code=e.status)

@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, e: RequestValidationError):
    fields = [".".join(str(x) for x in err["loc"][1:]) or "body" for err in e.errors()]
    return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "invalid fields: " + ", ".join(fields)}},
                        status_code=400)

@app.exception_handler(Exception)
async def _unexpected(request: Request, e: Exception):
    log.exception("unhandled error")
    return JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": "internal error"}}, status_code=500)


# ---------------------------------------------------------------------------- helpers
def to_micros(usd: Decimal) -> int:
    return int((usd * MICROS).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))

def usd(micros: int | None) -> str | None:
    return None if micros is None else f"{Decimal(micros) / MICROS:.6f}"

def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()

AUDIT_COLUMNS = {"kind", "actor", "request_id", "agent_id", "mandate_id", "model", "decision", "reason_code",
                 "policy_version", "reserved_micros", "cost_micros", "cost_source", "prompt_tokens",
                 "completion_tokens", "finish_reason", "upstream_status", "detail"}

async def audit(conn, **fields) -> None:
    assert set(fields) <= AUDIT_COLUMNS, set(fields) - AUDIT_COLUMNS
    fields["detail"] = json.dumps(fields.get("detail") or {}, default=str)
    cols = list(fields)
    await conn.execute(f"INSERT INTO audit_events ({', '.join(cols)}) VALUES "
                       f"({', '.join(f'${i + 1}' + ('::jsonb' if c == 'detail' else '') for i, c in enumerate(cols))})",
                       *fields.values())

def ledger_view(row) -> dict:
    d = dict(row)
    d.pop("token_sha256", None)
    for k in ("budget_limit_micros", "spent_micros", "reserved_micros"):
        d[k.replace("_micros", "_usd")] = usd(d[k])
    return d


# ---------------------------------------------------------------------------- admin API
def require_admin(authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), ADMIN_TOKEN):
        raise GatewayError(401, "ADMIN_UNAUTHORIZED", "missing or invalid admin token")

admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class AgentCreate(_Strict):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    budget_usd: Decimal = Field(ge=0, le=1_000_000)

class AgentPatch(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "suspended", "revoked"] | None = None
    budget_usd: Decimal | None = Field(default=None, ge=0, le=1_000_000)

class MandateCreate(_Strict):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    budget_usd: Decimal = Field(ge=0, le=1_000_000)
    allowed_models: list[str] = []

class MandatePatch(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "suspended"] | None = None
    budget_usd: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    allowed_models: list[str] | None = None

def check_models(models: list[str] | None) -> None:
    unknown = sorted(set(models or []) - set(state.prices))
    if unknown:
        raise GatewayError(400, "UNKNOWN_MODEL", f"not configured/priced in LiteLLM: {', '.join(unknown)}")

def to_columns(body: BaseModel) -> dict:
    cols = body.model_dump(exclude_unset=True, exclude_none=True)
    if "budget_usd" in cols:
        cols["budget_limit_micros"] = to_micros(cols.pop("budget_usd"))
    return cols

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
        await audit(c, kind="admin", actor="admin", decision=action,
                    **({"agent_id": id_} if table == "agents" else {"mandate_id": id_}), detail={"changed": changed})
    return ledger_view(after)

@admin.get("/agents")
async def list_agents():
    rows = await state.pool.fetch(
        "SELECT a.*, coalesce(array_agg(am.mandate_id ORDER BY am.mandate_id) FILTER (WHERE am.mandate_id IS NOT NULL), '{}') AS mandates "
        "FROM agents a LEFT JOIN agent_mandates am ON am.agent_id=a.id GROUP BY a.id ORDER BY a.id")
    return [ledger_view(r) for r in rows]

@admin.post("/agents", status_code=201)
async def create_agent(body: AgentCreate):
    async with state.pool.acquire() as c, c.transaction():
        row = await c.fetchrow("INSERT INTO agents (id, name, budget_limit_micros) VALUES ($1,$2,$3) "
                               "ON CONFLICT DO NOTHING RETURNING *", body.id, body.name, to_micros(body.budget_usd))
        if not row:
            raise GatewayError(409, "ALREADY_EXISTS", f"agent {body.id} exists")
        await audit(c, kind="admin", actor="admin", decision="agent_created", agent_id=body.id,
                    detail={"name": body.name, "budget_usd": usd(row["budget_limit_micros"])})
    return ledger_view(row)

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
    rows = await state.pool.fetch(
        "SELECT m.*, coalesce(array_agg(am.agent_id ORDER BY am.agent_id) FILTER (WHERE am.agent_id IS NOT NULL), '{}') AS agents "
        "FROM mandates m LEFT JOIN agent_mandates am ON am.mandate_id=m.id GROUP BY m.id ORDER BY m.id")
    return [ledger_view(r) for r in rows]

@admin.post("/mandates", status_code=201)
async def create_mandate(body: MandateCreate):
    check_models(body.allowed_models)
    async with state.pool.acquire() as c, c.transaction():
        row = await c.fetchrow("INSERT INTO mandates (id, name, description, budget_limit_micros, allowed_models) "
                               "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING RETURNING *", body.id, body.name,
                               body.description, to_micros(body.budget_usd), sorted(set(body.allowed_models)))
        if not row:
            raise GatewayError(409, "ALREADY_EXISTS", f"mandate {body.id} exists")
        await audit(c, kind="admin", actor="admin", decision="mandate_created", mandate_id=body.id,
                    detail={"name": body.name, "budget_usd": usd(row["budget_limit_micros"]), "allowed_models": row["allowed_models"]})
    return ledger_view(row)

@admin.patch("/mandates/{mandate_id}")
async def patch_mandate(mandate_id: str, body: MandatePatch):
    check_models(body.allowed_models)
    if body.allowed_models is not None:
        body.allowed_models = sorted(set(body.allowed_models))
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

@admin.get("/audit")
async def get_audit(agent: str | None = None, mandate: str | None = None, request_id: uuid.UUID | None = None,
                    decision: str | None = None, kind: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    rows = await state.pool.fetch(
        "SELECT * FROM audit_events WHERE ($1::text IS NULL OR agent_id=$1) AND ($2::text IS NULL OR mandate_id=$2) "
        "AND ($3::uuid IS NULL OR request_id=$3) AND ($4::text IS NULL OR decision=$4) AND ($5::text IS NULL OR kind=$5) "
        "ORDER BY id DESC LIMIT $6", agent, mandate, request_id, decision, kind, limit)
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"])
        d["reserved_usd"], d["cost_usd"] = usd(d["reserved_micros"]), usd(d["cost_micros"])
        out.append(d)
    return out

@admin.get("/summary")
async def summary():
    counts = await state.pool.fetchrow(
        "SELECT count(*) FILTER (WHERE decision='allow') AS allowed, count(*) FILTER (WHERE decision='deny') AS denied "
        "FROM audit_events WHERE kind='request' AND at > now() - interval '24 hours'")
    return {"agents": await list_agents(), "mandates": await list_mandates(), "last_24h": dict(counts),
            "models": {m: {"input_usd_per_1m": str(i * MICROS), "output_usd_per_1m": str(o * MICROS)}
                       for m, (i, o) in sorted(state.prices.items())}}

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
