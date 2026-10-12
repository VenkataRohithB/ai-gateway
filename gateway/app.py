"""AI Agent Gateway: authorization gate in front of LiteLLM.

Agent route : POST /m/{mandate}/v1/chat/completions   (agent bearer token)
Admin routes: /admin/*                                 (ADMIN_TOKEN)
Health      : /health/live, /health/ready
"""
import asyncio, hashlib, hmac, json, logging, os, secrets, uuid
from contextlib import asynccontextmanager
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Literal

import asyncpg, httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import db
from db import audit
from policy import Policy

DATABASE_URL = os.environ["DATABASE_URL"]
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_GATEWAY_KEY = os.environ["LITELLM_GATEWAY_KEY"]
UPSTREAM_TIMEOUT = 270  # LiteLLM 240s < gateway 270s < client 300s
UPSTREAM_TRANSPORT = None  # tests replace this with httpx.MockTransport
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"].encode()
SLUG = r"^[a-z0-9-]{1,64}$"
MICROS = 1_000_000
POLICY = Policy(os.environ.get("POLICY_FILE", "/config/policy.toml"))  # invalid policy -> refuse to start
MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TOKENS = 8192   # used when the agent sends no max_tokens; it is forwarded and priced
MAX_MAX_TOKENS = 16384
SWEEP_EVERY, SWEEP_AFTER = 60, UPSTREAM_TIMEOUT + 300

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # no per-request noise

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
    log_event(event="startup", policy_version=POLICY.version, swept=await db.sweep(state.pool, None))
    sweeper = asyncio.create_task(sweep_forever())
    yield
    sweeper.cancel()
    await state.http.aclose()
    await state.pool.close()

async def sweep_forever() -> None:
    while True:
        await asyncio.sleep(SWEEP_EVERY)
        try:
            if n := await db.sweep(state.pool, SWEEP_AFTER):
                log_event(event="swept_stale_reservations", count=n)
        except Exception:
            log.exception("sweeper failed")

app = FastAPI(title="AI Agent Gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------- errors
class GatewayError(Exception):
    """Every non-2xx answer: {"error": {"code", "message", ...}}. Never contains internals."""
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None, **extra):
        self.status, self.headers = status, headers
        self.body = {"error": {"code": code, "message": message, **extra}}

@app.exception_handler(GatewayError)
async def _gateway_error(request: Request, e: GatewayError):
    return JSONResponse(e.body, status_code=e.status, headers=e.headers)

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

def ledger_view(row) -> dict:
    d = dict(row)
    if "token_sha256" in d:
        d["token_issued"] = d.pop("token_sha256") is not None
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
            "policy": {"version": POLICY.version, "rules": POLICY.rules},
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

DASHBOARD = os.path.join(os.path.dirname(__file__), "dashboard.html")

@app.get("/admin/ui", include_in_schema=False)
async def dashboard():
    """Static page; holds no data. It asks for the admin token and calls the admin API above."""
    return FileResponse(DASHBOARD, headers={
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                   "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------- agent API
ALLOWED_FIELDS = {"model", "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop",
                  "seed", "tools", "tool_choice", "response_format", "n", "stream"}

def _bad(code: str, message: str):
    raise GatewayError(400, code, message)

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def validate_body(raw: bytes) -> tuple[dict, str, int]:
    """Strict allowlist. Returns (outbound body rebuilt from validated fields, model, max_tokens).
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
    max_tokens = body.get("max_tokens", body.get("max_completion_tokens", DEFAULT_MAX_TOKENS))
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_MAX_TOKENS:
        _bad("INVALID_REQUEST", f"max_tokens must be an integer from 1 to {MAX_MAX_TOKENS}")
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
    outbound = {k: v for k, v in body.items() if k not in ("max_tokens", "max_completion_tokens", "stream", "n")}
    outbound["max_tokens"] = max_tokens  # exactly one cap, the one that was priced
    return outbound, model, max_tokens

def estimate_micros(model: str, outbound_len: int, max_tokens: int) -> int:
    """Worst-case cost: every request byte counted as an input token (bytes >= tokens) plus the full output cap."""
    pin, pout = state.prices[model]
    return int(((outbound_len * pin + max_tokens * pout) * MICROS).to_integral_value(rounding=ROUND_CEILING))

def parse_cost(header: str | None) -> Decimal | None:
    try:
        cost = Decimal(header)
    except (TypeError, InvalidOperation):
        return None
    return cost if cost.is_finite() and cost > 0 else None

async def gather_facts(agent_id: str, mandate_id: str, model: str) -> dict:
    row = await state.pool.fetchrow(
        "SELECT a.status AS agent_status, m.status AS mandate_status, m.allowed_models, "
        "EXISTS (SELECT 1 FROM agent_mandates WHERE agent_id=a.id AND mandate_id=$2) AS assigned "
        "FROM agents a LEFT JOIN mandates m ON m.id=$2 WHERE a.id=$1", agent_id, mandate_id)
    return {"agent.status": row["agent_status"], "assignment.exists": row["assigned"],
            "mandate.status": row["mandate_status"] or "unknown",
            "model.allowed": model in (row["allowed_models"] or []) and model in state.prices}

@app.post("/m/{mandate_id}/v1/chat/completions")
async def chat_completions(mandate_id: str, request: Request, authorization: str = Header(default="")):
    request_id = uuid.uuid4()
    rid = str(request_id)
    mandate_id = mandate_id[:64]

    # 1. Authenticate: identity comes only from the token.
    scheme, _, token = authorization.partition(" ")
    agent_id = None
    if scheme.lower() == "bearer" and token.startswith("gw_"):
        agent_id = await state.pool.fetchval("SELECT id FROM agents WHERE token_sha256=$1", token_hash(token))
    if not agent_id:  # not audited (unauthenticated callers could flood the audit table)
        log_event(event="auth_failed", request_id=rid, mandate=mandate_id, client=request.client.host if request.client else None)
        raise GatewayError(401, "UNAUTHORIZED", "missing or invalid agent token", request_id=rid)

    async def deny(status: int, code: str, message: str, model: str | None = None, policy_version: str | None = None):
        await audit(state.pool, kind="request", actor=agent_id, request_id=request_id, agent_id=agent_id,
                    mandate_id=mandate_id, model=model, decision="deny", reason_code=code, policy_version=policy_version)
        log_event(event="deny", request_id=rid, agent=agent_id, mandate=mandate_id, model=model, reason=code)
        raise GatewayError(status, code, message, request_id=rid,
                           **({"policy_version": policy_version} if policy_version else {}))

    # 2. Validate size and body.
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        await deny(413, "REQUEST_TOO_LARGE", f"body over {MAX_BODY_BYTES} bytes")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        await deny(413, "REQUEST_TOO_LARGE", f"body over {MAX_BODY_BYTES} bytes")
    try:
        outbound, model, max_tokens = validate_body(raw)
    except GatewayError as e:
        await deny(e.status, e.body["error"]["code"], e.body["error"]["message"])

    # 3. Policy (explicit rules in config/policy.toml) on fresh facts.
    decision = POLICY.evaluate(await gather_facts(agent_id, mandate_id, model))
    if not decision["allow"]:
        await deny(403, decision["reason"], decision["message"], model, decision["policy_version"])

    # 4. Reserve worst-case cost on agent + mandate; the same transaction re-checks the kill-switch facts.
    out_bytes = json.dumps(outbound, separators=(",", ":")).encode()
    est = estimate_micros(model, len(out_bytes), max_tokens)
    failed = await db.reserve(state.pool, request_id, agent_id, mandate_id, model, est, POLICY.version)
    if failed:
        decision = POLICY.evaluate(await gather_facts(agent_id, mandate_id, model))
        if not decision["allow"]:  # state changed concurrently (e.g. revoked just now)
            await deny(403, decision["reason"], decision["message"], model, decision["policy_version"])
        code = {"assignment": "MANDATE_NOT_ASSIGNED", "agent": "AGENT_BUDGET_EXCEEDED",
                "mandate": "MANDATE_BUDGET_EXCEEDED"}[failed]
        await deny(402 if failed != "assignment" else 403, code,
                   f"{'agent' if failed == 'agent' else 'mandate'} budget cannot cover this request "
                   f"(worst-case estimate {usd(est)} USD)" if failed != "assignment" else "agent is not assigned to this mandate",
                   model, POLICY.version)

    # 5. Forward with the gateway's own LiteLLM key. No client headers are forwarded.
    async def close(outcome: str, cost: int | None = None, source: str = "none", **extra):
        await db.settle(state.pool, request_id, outcome, cost, source, actor=agent_id, **extra)
        log_event(event=outcome, request_id=rid, agent=agent_id, mandate=mandate_id, model=model,
                  reserved=est, cost=cost if outcome == "settled" else 0, source=source, **extra)

    headers = {"content-type": "application/json", "x-litellm-call-id": rid,
               "x-litellm-spend-logs-metadata": json.dumps({"agent_id": agent_id, "mandate_id": mandate_id})}
    try:
        r = await state.http.post("/v1/chat/completions", content=out_bytes, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout):  # never sent: nothing billed
        await close("released", upstream_status=None)
        raise GatewayError(503, "UPSTREAM_UNAVAILABLE", "model gateway unavailable", request_id=rid)
    except httpx.TransportError:  # sent, outcome unknown: charge the reservation
        await close("settled", None, "reservation", detail={"error": "upstream timeout or connection lost"})
        raise GatewayError(504, "UPSTREAM_TIMEOUT", "model call timed out; charged at the reserved amount", request_id=rid)

    # 6. Settle and respond.
    if r.status_code == 200:
        try:
            data = r.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            await close("settled", None, "reservation", upstream_status=200, detail={"error": "unparseable response"})
            raise GatewayError(502, "UPSTREAM_ERROR", "invalid response from model", request_id=rid)
        usage = data.get("usage") or {}
        cost = parse_cost(r.headers.get("x-litellm-response-cost"))
        if cost is not None:
            charge, source = int((cost * MICROS).to_integral_value(rounding=ROUND_CEILING)), "litellm_header"
        elif (usage.get("total_tokens") or 0) > 0:
            charge, source = None, "reservation"  # tokens used but cost unknown: charge the reservation
        else:
            charge, source = 0, "none"
        await close("settled", charge, source, upstream_status=200, prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"), finish_reason=choice.get("finish_reason"))
        return JSONResponse(data, headers={"x-request-id": rid})

    upstream_msg = ""
    try:
        upstream_msg = str((r.json().get("error") or {}).get("message", ""))[:500]
    except Exception:
        pass
    log_event(event="upstream_error", request_id=rid, status=r.status_code, message=upstream_msg[:200])
    if r.status_code in (408, 504):
        await close("settled", None, "reservation", upstream_status=r.status_code)
        raise GatewayError(504, "UPSTREAM_TIMEOUT", "model call timed out; charged at the reserved amount", request_id=rid)
    await close("released", upstream_status=r.status_code)
    if r.status_code in (401, 403, 422):  # gateway key problem or backstop budget: never leak details to agents
        raise GatewayError(503, "UPSTREAM_UNAVAILABLE", "model gateway unavailable", request_id=rid)
    if r.status_code == 429:
        retry = {"retry-after": r.headers["retry-after"]} if "retry-after" in r.headers else None
        raise GatewayError(429, "UPSTREAM_RATE_LIMITED", "model provider rate limit; retry later or use another model",
                           headers=retry, request_id=rid)
    status = r.status_code if 400 <= r.status_code < 600 else 502
    raise GatewayError(status, "UPSTREAM_ERROR", upstream_msg if status in (400, 404) else "model provider error",
                       request_id=rid)
