"""AI Agent Gateway: authorization gate in front of LiteLLM.

Agent route : POST /m/{mandate}/v1/chat/completions   (agent bearer token)
Admin routes: /admin/*                                 (ADMIN_TOKEN)
Health      : /health/live, /health/ready
"""
import json, logging, os
from contextlib import asynccontextmanager
from decimal import Decimal

import asyncpg, httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

DATABASE_URL = os.environ["DATABASE_URL"]
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_GATEWAY_KEY = os.environ["LITELLM_GATEWAY_KEY"]
UPSTREAM_TIMEOUT = 270  # LiteLLM 240s < gateway 270s < client 300s
UPSTREAM_TRANSPORT = None  # tests replace this with httpx.MockTransport

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
