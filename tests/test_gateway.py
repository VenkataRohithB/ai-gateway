"""Integration + security tests: real Postgres, gateway app in-process, LiteLLM replaced by a
counting mock (httpx.MockTransport). The mock's call list is the proof of "never forwarded"."""
import asyncio, json, os, uuid

import asyncpg, httpx, pytest
from fastapi.routing import APIRoute

import app as gw
import db

pytestmark = pytest.mark.anyio
OWNER_DSN = os.environ["OWNER_DATABASE_URL"]
ADMIN = {"Authorization": "Bearer " + os.environ["ADMIN_TOKEN"]}
BODY = {"model": "mock-model", "messages": [{"role": "user", "content": "hello"}]}
COMPLETION = {"id": "x", "object": "chat.completion", "model": "mock-model",
              "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Upstream:
    """Stands in for LiteLLM. Records every chat-completion request it receives."""
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status, self.cost, self.exc, self.gate = 200, "0.000020", None, None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/liveliness":
            return httpx.Response(200, json="I'm alive!")
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": [
                {"model_name": m, "model_info": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06}}
                for m in ("mock-model", "mock-model-2")]})
        assert request.url.path == "/v1/chat/completions"
        self.calls.append(request)
        if self.gate:
            await self.gate.wait()
        if self.exc:
            raise self.exc
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "upstream says no (internal-detail)"}})
        return httpx.Response(200, json=COMPLETION, headers={"x-litellm-response-cost": self.cost} if self.cost else {})


@pytest.fixture
async def upstream():
    return Upstream()


@pytest.fixture
async def owner():
    conn = await asyncpg.connect(OWNER_DSN)
    await conn.execute("TRUNCATE audit_events, reservations, agent_mandates, agents, mandates")
    yield conn
    await conn.close()


@pytest.fixture
async def client(upstream, owner):
    gw.UPSTREAM_TRANSPORT = httpx.MockTransport(upstream.handler)
    async with gw.lifespan(gw.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw") as c:
            yield c


async def admin(c, method, path, body=None, expect=None):
    r = await c.request(method, path, json=body, headers=ADMIN)
    if expect:
        assert r.status_code == expect, r.text
    return r.json()


async def make_agent(c, agent, mandate="m1", agent_budget="1", mandate_budget="10", models=("mock-model",)):
    await admin(c, "POST", "/admin/agents", {"id": agent, "name": agent, "budget_usd": agent_budget}, 201)
    r = await c.post("/admin/mandates", headers=ADMIN, json={"id": mandate, "name": mandate, "budget_usd": mandate_budget,
                                                               "allowed_models": list(models)})
    assert r.status_code in (201, 409), r.text                      # 409: mandate shared by several agents
    await admin(c, "PUT", f"/admin/agents/{agent}/mandates/{mandate}", expect=200)
    return (await admin(c, "POST", f"/admin/agents/{agent}/token", expect=200))["token"]


async def chat(c, token, mandate="m1", body=None, **headers):
    return await c.post(f"/m/{mandate}/v1/chat/completions", json=body or BODY,
                        headers={"Authorization": f"Bearer {token}", **headers})


def estimate(body=BODY) -> int:
    outbound, model, max_tokens = gw.validate_body(json.dumps(body).encode())
    return gw.estimate_micros(model, len(json.dumps(outbound, separators=(",", ":")).encode()), max_tokens)


async def ledger(owner, table, id_):
    return await owner.fetchrow(f"SELECT spent_micros, reserved_micros, budget_limit_micros FROM {table} WHERE id=$1", id_)


async def audit_rows(owner, request_id):
    return await owner.fetch("SELECT * FROM audit_events WHERE request_id=$1 ORDER BY id", uuid.UUID(request_id))


# --------------------------------------------------------------------------- happy path + attribution

async def test_allowed_request_is_forwarded_attributed_and_charged(client, upstream, owner):
    token = await make_agent(client, "agent-a")
    r = await chat(client, token, **{"x-evil-header": "1"})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hi"
    rid = r.headers["x-request-id"]
    assert not any(h.startswith("x-litellm") for h in r.headers)          # upstream headers not leaked
    [sent] = upstream.calls
    assert sent.headers["authorization"] == f"Bearer {gw.LITELLM_GATEWAY_KEY}"  # gateway key, never the agent's
    assert sent.headers["x-litellm-call-id"] == rid
    assert json.loads(sent.headers["x-litellm-spend-logs-metadata"]) == {"agent_id": "agent-a", "mandate_id": "m1"}
    assert "x-evil-header" not in sent.headers
    assert json.loads(sent.content) == {**BODY, "max_tokens": gw.DEFAULT_MAX_TOKENS}  # rebuilt body
    allow, settle = await audit_rows(owner, rid)
    assert (allow["decision"], allow["agent_id"], allow["mandate_id"], allow["reserved_micros"]) == ("allow", "agent-a", "m1", estimate())
    assert (settle["decision"], settle["cost_micros"], settle["cost_source"]) == ("settled", 20, "litellm_header")
    assert (settle["prompt_tokens"], settle["completion_tokens"], settle["finish_reason"]) == (10, 5, "stop")
    for table, id_ in (("agents", "agent-a"), ("mandates", "m1")):
        assert tuple(await ledger(owner, table, id_))[:2] == (20, 0)


async def test_multiple_calls_in_one_task_are_each_metered(client, upstream, owner):
    token = await make_agent(client, "agent-a")
    for _ in range(3):
        assert (await chat(client, token)).status_code == 200
    assert len(upstream.calls) == 3
    assert (await ledger(owner, "agents", "agent-a"))["spent_micros"] == 60
    assert await owner.fetchval("SELECT count(*) FROM audit_events WHERE decision='settled'") == 3


# --------------------------------------------------------------------------- authentication / identity

@pytest.mark.parametrize("auth", [None, "Bearer", "Bearer gw_not-a-real-token", "Basic abc", "Bearer adm_test-admin-token"])
async def test_missing_or_invalid_token_is_rejected_and_not_forwarded(client, upstream, owner, auth):
    await make_agent(client, "agent-a")
    r = await client.post("/m/m1/v1/chat/completions", json=BODY, headers={"Authorization": auth} if auth else {})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    assert upstream.calls == []
    assert await owner.fetchval("SELECT count(*) FROM audit_events WHERE kind='request'") == 0


async def test_agent_cannot_impersonate_another(client, upstream, owner):
    token_a = await make_agent(client, "agent-a", mandate="m1")
    await make_agent(client, "agent-b", mandate="m2")
    r = await chat(client, token_a, mandate="m2", **{"x-agent-id": "agent-b"})   # B's mandate with A's token
    assert r.status_code == 403 and r.json()["error"]["code"] == "MANDATE_NOT_ASSIGNED"
    r = await chat(client, token_a, body={**BODY, "user": "agent-b"})              # identity in body
    assert r.status_code == 400 and r.json()["error"]["code"] == "FIELD_NOT_ALLOWED"
    assert upstream.calls == []
    assert await owner.fetchval("SELECT count(*) FROM audit_events WHERE agent_id='agent-b' AND kind='request'") == 0


async def test_token_rotation_invalidates_old_token(client, upstream):
    old = await make_agent(client, "agent-a")
    new = (await admin(client, "POST", "/admin/agents/agent-a/token", expect=200))["token"]
    assert (await chat(client, old)).status_code == 401
    assert (await chat(client, new)).status_code == 200


# --------------------------------------------------------------------------- policy

async def test_unassigned_and_unknown_mandates_look_the_same(client, upstream):
    token = await make_agent(client, "agent-a")
    await admin(client, "POST", "/admin/mandates", {"id": "m9", "name": "m9", "budget_usd": "1", "allowed_models": ["mock-model"]}, 201)
    for mandate in ("m9", "does-not-exist", "UPPER", "x" * 200):
        r = await chat(client, token, mandate=mandate)
        assert r.status_code == 403 and r.json()["error"]["code"] == "MANDATE_NOT_ASSIGNED", mandate
    assert upstream.calls == []


async def test_model_allowlist_cannot_be_bypassed(client, upstream, owner):
    token = await make_agent(client, "agent-a", models=("mock-model",))
    for model in ("mock-model-2", "gemini-3.8-flash", "openai/mock-model", "MOCK-MODEL"):
        r = await chat(client, token, body={**BODY, "model": model})
        assert r.status_code == 403 and r.json()["error"]["code"] == "MODEL_NOT_ALLOWED", model
    assert upstream.calls == []
    row = await owner.fetchrow("SELECT * FROM audit_events WHERE decision='deny' ORDER BY id DESC LIMIT 1")
    assert row["reason_code"] == "MODEL_NOT_ALLOWED" and row["policy_version"] == gw.POLICY.version


@pytest.mark.parametrize("extra, code", [
    ({"api_base": "http://evil"}, "FIELD_NOT_ALLOWED"),
    ({"base_url": "http://evil"}, "FIELD_NOT_ALLOWED"),
    ({"api_key": "sk-mine"}, "FIELD_NOT_ALLOWED"),
    ({"metadata": {"x": 1}}, "FIELD_NOT_ALLOWED"),
    ({"fallbacks": ["gemini-3.8-flash"]}, "FIELD_NOT_ALLOWED"),
    ({"extra_body": {"api_base": "x"}}, "FIELD_NOT_ALLOWED"),
    ({"mock_response": "free"}, "FIELD_NOT_ALLOWED"),
    ({"max_tokens": 10, "max_completion_tokens": 9000}, "INVALID_REQUEST"),
    ({"max_tokens": 10**6}, "INVALID_REQUEST"),
    ({"max_tokens": True}, "INVALID_REQUEST"),
    ({"tools": [{"googleSearch": {}}]}, "TOOL_NOT_ALLOWED"),
    ({"tools": [{"type": "code_execution"}]}, "TOOL_NOT_ALLOWED"),
    ({"stream": True}, "STREAMING_NOT_SUPPORTED"),
    ({"n": 2}, "INVALID_REQUEST"),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]}, "CONTENT_NOT_ALLOWED"),
    ({"messages": []}, "INVALID_REQUEST"),
])
async def test_routing_overrides_and_unbounded_cost_fields_are_rejected(client, upstream, owner, extra, code):
    token = await make_agent(client, "agent-a")
    r = await chat(client, token, body={**BODY, **extra})
    assert r.status_code == 400 and r.json()["error"]["code"] == code
    assert upstream.calls == []
    assert await owner.fetchval("SELECT reason_code FROM audit_events WHERE decision='deny'") == code


async def test_function_tools_and_max_completion_tokens_are_forwarded(client, upstream):
    token = await make_agent(client, "agent-a")
    tools = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]
    r = await chat(client, token, body={**BODY, "tools": tools, "tool_choice": "auto", "max_completion_tokens": 100})
    assert r.status_code == 200
    sent = json.loads(upstream.calls[0].content)
    assert sent["max_tokens"] == 100 and "max_completion_tokens" not in sent and sent["tools"] == tools


async def test_oversized_body_rejected(client, upstream):
    token = await make_agent(client, "agent-a")
    big = {**BODY, "messages": [{"role": "user", "content": "x" * (gw.MAX_BODY_BYTES + 1)}]}
    r = await chat(client, token, body=big)
    assert r.status_code == 413 and upstream.calls == []


# --------------------------------------------------------------------------- kill switch

KILL_SWITCHES = [
    # (action, restore, expected status, expected reason)
    (("PATCH", "/admin/agents/agent-a", {"status": "revoked"}), ("PATCH", "/admin/agents/agent-a", {"status": "active"}), 403, "AGENT_REVOKED"),
    (("PATCH", "/admin/agents/agent-a", {"status": "suspended"}), ("PATCH", "/admin/agents/agent-a", {"status": "active"}), 403, "AGENT_INACTIVE"),
    (("PATCH", "/admin/mandates/m1", {"status": "suspended"}), ("PATCH", "/admin/mandates/m1", {"status": "active"}), 403, "MANDATE_INACTIVE"),
    (("DELETE", "/admin/agents/agent-a/mandates/m1", None), ("PUT", "/admin/agents/agent-a/mandates/m1", None), 403, "MANDATE_NOT_ASSIGNED"),
    (("PATCH", "/admin/agents/agent-a", {"budget_usd": "0"}), ("PATCH", "/admin/agents/agent-a", {"budget_usd": "1"}), 402, "AGENT_BUDGET_EXCEEDED"),
    (("PATCH", "/admin/mandates/m1", {"budget_usd": "0"}), ("PATCH", "/admin/mandates/m1", {"budget_usd": "10"}), 402, "MANDATE_BUDGET_EXCEEDED"),
]


@pytest.mark.parametrize("action, restore, status, reason", KILL_SWITCHES, ids=[k[3] for k in KILL_SWITCHES])
async def test_kill_switch_denies_next_request_before_upstream(client, upstream, owner, action, restore, status, reason):
    token = await make_agent(client, "agent-a")
    assert (await chat(client, token)).status_code == 200 and len(upstream.calls) == 1
    await admin(client, *action, expect=200)
    r = await chat(client, token)                                   # same credential, unchanged client
    assert r.status_code == status and r.json()["error"]["code"] == reason
    assert len(upstream.calls) == 1                                 # PROOF: the denied request never reached upstream
    deny = await audit_rows(owner, r.json()["error"]["request_id"])
    assert [(d["decision"], d["reason_code"]) for d in deny] == [("deny", reason)]
    admin_row = await owner.fetchrow("SELECT * FROM audit_events WHERE kind='admin' ORDER BY id DESC LIMIT 1")
    assert admin_row["decision"] in ("agent_updated", "mandate_updated", "unassigned")
    await admin(client, *restore, expect=200)
    assert (await chat(client, token)).status_code == 200 and len(upstream.calls) == 2


async def test_revocation_persists_across_gateway_restart(upstream, owner):
    gw.UPSTREAM_TRANSPORT = httpx.MockTransport(upstream.handler)
    async with gw.lifespan(gw.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw") as c:
            token = await make_agent(c, "agent-a")
            await admin(c, "PATCH", "/admin/agents/agent-a", {"status": "revoked"}, 200)
    async with gw.lifespan(gw.app):                                  # fresh pool, nothing cached
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw") as c:
            r = await chat(c, token)
            assert r.status_code == 403 and r.json()["error"]["code"] == "AGENT_REVOKED"
    assert upstream.calls == []


async def test_in_flight_request_completes_but_next_is_denied(client, upstream, owner):
    """Consistency boundary: revocation does not cancel a request already forwarded."""
    token = await make_agent(client, "agent-a")
    upstream.gate = asyncio.Event()
    in_flight = asyncio.create_task(chat(client, token))
    async with asyncio.timeout(10):
        while not upstream.calls:
            await asyncio.sleep(0.01)
    await admin(client, "PATCH", "/admin/agents/agent-a", {"status": "revoked"}, 200)
    upstream.gate.set()
    assert (await in_flight).status_code == 200
    r = await chat(client, token)
    assert r.json()["error"]["code"] == "AGENT_REVOKED" and len(upstream.calls) == 1


# --------------------------------------------------------------------------- budgets

async def test_agent_budget_exhaustion_is_isolated_and_mandate_spend_is_shared(client, upstream, owner):
    est = estimate()
    upstream.cost = str(est / 1_000_000)                            # each call costs the full estimate
    token_a = await make_agent(client, "agent-a", agent_budget=str(2 * est / 1_000_000), mandate_budget="10")
    token_b = await make_agent(client, "agent-b", agent_budget="10")
    assert [(await chat(client, token_a)).status_code for _ in range(3)] == [200, 200, 402]
    assert (await chat(client, token_b)).status_code == 200          # B unaffected by A's exhaustion
    assert len(upstream.calls) == 3
    a, b, m = (await ledger(owner, "agents", "agent-a"), await ledger(owner, "agents", "agent-b"),
               await ledger(owner, "mandates", "m1"))
    assert (a["spent_micros"], b["spent_micros"], m["spent_micros"]) == (2 * est, est, 3 * est)
    assert a["spent_micros"] <= a["budget_limit_micros"]


async def test_mandate_budget_caps_all_its_agents(client, upstream):
    est = estimate()
    upstream.cost = str(est / 1_000_000)
    token_a = await make_agent(client, "agent-a", mandate_budget=str(2 * est / 1_000_000))
    token_b = await make_agent(client, "agent-b")
    assert (await chat(client, token_a)).status_code == 200
    assert (await chat(client, token_b)).status_code == 200
    r = await chat(client, token_a)
    assert r.status_code == 402 and r.json()["error"]["code"] == "MANDATE_BUDGET_EXCEEDED"
    assert len(upstream.calls) == 2


async def test_concurrent_requests_cannot_overspend(client, upstream, owner):
    """50 simultaneous requests while the upstream is held: only floor(limit/estimate) may be reserved."""
    est = estimate()
    token = await make_agent(client, "agent-a", agent_budget=str(5 * est / 1_000_000), mandate_budget="100")
    upstream.gate = asyncio.Event()
    tasks = [asyncio.create_task(chat(client, token)) for _ in range(50)]
    async with asyncio.timeout(10):
        while len(upstream.calls) + sum(t.done() for t in tasks) < 50:
            await asyncio.sleep(0.02)
    a = await ledger(owner, "agents", "agent-a")
    assert len(upstream.calls) == 5 and a["reserved_micros"] == 5 * est <= a["budget_limit_micros"]
    upstream.gate.set()
    codes = sorted(r.status_code for r in await asyncio.gather(*tasks))
    assert codes == [200] * 5 + [402] * 45
    a = await ledger(owner, "agents", "agent-a")
    audited = await owner.fetchval("SELECT sum(cost_micros) FROM audit_events WHERE decision='settled' AND agent_id='agent-a'")
    assert (a["spent_micros"], a["reserved_micros"]) == (audited, 0)


@pytest.mark.parametrize("setup, status, charged", [
    ({"status": 500}, 500, 0),
    ({"status": 429}, 429, 0),
    ({"status": 400}, 400, 0),
    ({"status": 422}, 503, 0),                                      # backstop/key problem -> generic 503
    ({"exc": httpx.ConnectError("refused")}, 503, 0),               # never sent -> released
    ({"exc": httpx.ReadTimeout("slow")}, 504, "est"),               # outcome unknown -> charge reservation
    ({"status": 408}, 504, "est"),
    ({"cost": None}, 200, "est"),                                   # tokens used, cost unknown -> reservation
])
async def test_failures_reconcile_reservations(client, upstream, owner, setup, status, charged):
    token = await make_agent(client, "agent-a")
    for k, v in setup.items():
        setattr(upstream, k, v)
    r = await chat(client, token)
    assert r.status_code == status
    if status not in (200, 400):                                     # 400 messages are passed through on purpose
        assert "internal-detail" not in r.text                      # upstream details (e.g. spend) not leaked
    a = await ledger(owner, "agents", "agent-a")
    expected = estimate() if charged == "est" else charged
    assert (a["spent_micros"], a["reserved_micros"]) == (expected, 0)
    assert await owner.fetchval("SELECT count(*) FROM reservations WHERE status='held'") == 0


async def test_sweeper_charges_abandoned_reservation_and_late_settle_is_ignored(client, owner):
    await make_agent(client, "agent-a")
    rid = uuid.uuid4()
    assert await db.reserve(gw.state.pool, rid, "agent-a", "m1", "mock-model", 1000, gw.POLICY.version) is None
    assert await db.sweep(gw.state.pool, None) == 1
    assert tuple(await ledger(owner, "agents", "agent-a"))[:2] == (1000, 0)
    assert await db.settle(gw.state.pool, rid, "settled", 5, "litellm_header", actor="agent-a") is None
    assert tuple(await ledger(owner, "agents", "agent-a"))[:2] == (1000, 0)  # no double charge
    assert await owner.fetchval("SELECT decision FROM audit_events WHERE request_id=$1 ORDER BY id DESC LIMIT 1", rid) == "late_ignored"


# --------------------------------------------------------------------------- admin + audit security

async def test_every_admin_route_requires_admin_token(client):
    # every route on the admin router (FastAPI keeps included routers as one _IncludedRouter entry)
    routes = [(m, r.path) for r in gw.admin.routes if isinstance(r, APIRoute) for m in r.methods]
    assert len(routes) >= 10 and all(p.startswith("/admin/") for _, p in routes)
    public = {r.path for r in gw.app.routes if isinstance(r, APIRoute)}
    assert public <= {"/health/live", "/health/ready", "/m/{mandate_id}/v1/chat/completions", "/admin/ui"}, public
    for method, path in routes:
        url = path.replace("{agent_id}", "a").replace("{mandate_id}", "m")
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer gw_agenttoken"}):
            r = await client.request(method, url, headers=headers, json={})
            assert r.status_code == 401, (method, path, headers)


async def test_agent_token_cannot_use_admin_api(client):
    token = await make_agent(client, "agent-a")
    r = await client.get("/admin/agents", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_audit_log_is_append_only_for_gateway_role(client):
    async with gw.state.pool.acquire() as c:
        for sql in ("DELETE FROM audit_events", "UPDATE audit_events SET decision='allow'", "TRUNCATE audit_events"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.execute(sql)


async def test_no_secret_or_prompt_in_audit(client, owner):
    token = await make_agent(client, "agent-a")
    await chat(client, token, body={**BODY, "messages": [{"role": "user", "content": "TOP-SECRET-PROMPT"}]})
    dump = json.dumps([dict(r) for r in await owner.fetch("SELECT * FROM audit_events")], default=str)
    assert "TOP-SECRET-PROMPT" not in dump and token not in dump and gw.LITELLM_GATEWAY_KEY not in dump
