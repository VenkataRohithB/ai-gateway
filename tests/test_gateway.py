"""Integration + security tests: real Postgres, gateway app in-process, LiteLLM replaced by a
counting mock (httpx.MockTransport). The mock's call list is the proof of "never forwarded"."""
import asyncio, json, os, uuid
from decimal import Decimal

import asyncpg, httpx, pytest
from fastapi.routing import APIRoute

import app as gw
import db

pytestmark = pytest.mark.anyio
OWNER_DSN = os.environ["OWNER_DATABASE_URL"]
ADMIN = {"Authorization": "Bearer " + os.environ["ADMIN_TOKEN"]}
BODY = {"model": "mock-model", "messages": [{"role": "user", "content": "hello"}]}
DEFAULT_MAX = 4096  # mandate default_max_tokens when not set
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
        self.by_model: dict[str, int] = {}  # model -> status to return for that model only
        self.message = "upstream says no (internal-detail)"

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/liveliness":
            return httpx.Response(200, json="I'm alive!")
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": [
                {"model_name": m, "model_info": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06}}
                for m in ("mock-model", "mock-model-2", "mock-model-3")]})
        assert request.url.path == "/v1/chat/completions"
        self.calls.append(request)
        if self.gate:
            await self.gate.wait()
        if self.exc:
            raise self.exc
        status = self.by_model.get(json.loads(request.content)["model"], self.status)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": self.message}})
        return httpx.Response(200, json=COMPLETION, headers={"x-litellm-response-cost": self.cost} if self.cost else {})

    def models(self):
        return [json.loads(c.content)["model"] for c in self.calls]


@pytest.fixture
async def upstream():
    return Upstream()


@pytest.fixture
async def owner():
    conn = await asyncpg.connect(OWNER_DSN)
    await conn.execute("TRUNCATE audit_events, reservations, agent_mandates, agents, mandates, usage, usage_monthly, policy_versions, provider_tiers RESTART IDENTITY")
    await conn.execute("UPDATE settings SET value='85' WHERE key='usd_inr_rate'")
    yield conn
    await conn.close()


@pytest.fixture
async def client(upstream, owner):
    gw.UPSTREAM_TRANSPORT = httpx.MockTransport(upstream.handler)
    gw.state.model_keys = {}
    async with gw.lifespan(gw.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw") as c:
            yield c


async def admin(c, method, path, body=None, expect=None):
    r = await c.request(method, path, json=body, headers=ADMIN)
    if expect:
        assert r.status_code == expect, r.text
    return r.json()


async def make_agent(c, agent, mandate="m1", agent_budget="1", mandate_budget="10", models=("mock-model",), **mandate_opts):
    await admin(c, "POST", "/admin/agents", {"id": agent, "name": agent, "budget": agent_budget}, 201)
    r = await c.post("/admin/mandates", headers=ADMIN, json={"id": mandate, "name": mandate, "budget": mandate_budget,
                                                               "allowed_models": list(models), **mandate_opts})
    assert r.status_code in (201, 409), r.text                      # 409: mandate shared by several agents
    await admin(c, "PUT", f"/admin/agents/{agent}/mandates/{mandate}", expect=200)
    return (await admin(c, "POST", f"/admin/agents/{agent}/token", expect=200))["token"]


async def chat(c, token, mandate="m1", body=None, **headers):
    return await c.post(f"/m/{mandate}/v1/chat/completions", json=body or BODY,
                        headers={"Authorization": f"Bearer {token}", **headers})


def estimate(body=BODY, max_tokens=DEFAULT_MAX) -> int:
    outbound, model, requested = gw.validate_body(json.dumps(body).encode())
    mt = requested or max_tokens
    sent = json.dumps({"model": model, **outbound, "max_tokens": mt}, separators=(",", ":")).encode()
    return gw.estimate_micros(model, len(sent), mt)


def usd(micros) -> str:
    return str(Decimal(micros) / 1_000_000)


async def ledger(owner, table, id_):
    return await owner.fetchrow(f"SELECT spent_micros, reserved_micros, budget_limit_micros FROM {table} WHERE id=$1", id_)


async def usage_row(owner, request_id):
    return await owner.fetchrow("SELECT * FROM usage WHERE request_id=$1", uuid.UUID(request_id))


# --------------------------------------------------------------------------- happy path + attribution

async def test_allowed_request_is_forwarded_attributed_and_charged(client, upstream, owner):
    token = await make_agent(client, "agent-a")
    r = await chat(client, token, **{"x-evil-header": "1"})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hi"
    rid = r.headers["x-request-id"]
    assert r.headers["x-gateway-model"] == "mock-model"
    assert not any(h.startswith("x-litellm") for h in r.headers)          # upstream headers not leaked
    [sent] = upstream.calls
    assert sent.headers["authorization"] == f"Bearer {gw.LITELLM_GATEWAY_KEY}"  # gateway key, never the agent's
    assert sent.headers["x-litellm-call-id"] == rid
    assert "x-evil-header" not in sent.headers and "x-litellm-spend-logs-metadata" not in sent.headers
    assert json.loads(sent.content) == {**BODY, "max_tokens": DEFAULT_MAX}  # rebuilt body, mandate default cap
    u = await usage_row(owner, rid)
    assert (u["outcome"], u["agent_id"], u["mandate_id"], u["model_served"], u["attempts"]) == ("ok", "agent-a", "m1", "mock-model", 1)
    assert (u["cost_micros"], u["cost_source"], u["prompt_tokens"], u["completion_tokens"]) == (20, "litellm_header", 10, 5)
    assert u["reserved_micros"] == estimate()                     # the hold is recorded next to the real charge
    for table, id_ in (("agents", "agent-a"), ("mandates", "m1")):
        assert tuple(await ledger(owner, table, id_))[:2] == (20, 0)
    month = await owner.fetchrow("SELECT * FROM usage_monthly")
    assert (month["agent_id"], month["mandate_id"], month["model"], month["requests"], month["cost_micros"]) == \
        ("agent-a", "m1", "mock-model", 1, 20)


async def test_multiple_calls_in_one_task_are_each_metered(client, upstream, owner):
    token = await make_agent(client, "agent-a")
    for _ in range(3):
        assert (await chat(client, token)).status_code == 200
    assert len(upstream.calls) == 3
    assert (await ledger(owner, "agents", "agent-a"))["spent_micros"] == 60
    assert await owner.fetchval("SELECT count(*) FROM usage WHERE outcome='ok'") == 3
    assert await owner.fetchval("SELECT requests FROM usage_monthly") == 3


async def test_mandate_default_max_tokens_and_model_cap(client, upstream):
    token = await make_agent(client, "agent-a", default_max_tokens=100)
    assert (await chat(client, token)).status_code == 200
    assert json.loads(upstream.calls[0].content)["max_tokens"] == 100
    r = await chat(client, token, body={**BODY, "max_tokens": gw.MAX_OUTPUT_FALLBACK + 1})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_REQUEST" and len(upstream.calls) == 1


# --------------------------------------------------------------------------- authentication / identity

@pytest.mark.parametrize("auth", [None, "Bearer", "Bearer gw_not-a-real-token", "Basic abc", "Bearer adm_test-admin-token"])
async def test_missing_or_invalid_token_is_rejected_and_not_forwarded(client, upstream, owner, auth):
    await make_agent(client, "agent-a")
    r = await client.post("/m/m1/v1/chat/completions", json=BODY, headers={"Authorization": auth} if auth else {})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    assert r.headers["x-request-id"] == r.json()["error"]["request_id"]
    assert upstream.calls == []
    assert await owner.fetchval("SELECT count(*) FROM usage") == 0


async def test_agent_cannot_impersonate_another(client, upstream, owner):
    token_a = await make_agent(client, "agent-a", mandate="m1")
    await make_agent(client, "agent-b", mandate="m2")
    r = await chat(client, token_a, mandate="m2", **{"x-agent-id": "agent-b"})   # B's mandate with A's token
    assert r.status_code == 403 and r.json()["error"]["code"] == "MANDATE_NOT_ASSIGNED"
    r = await chat(client, token_a, body={**BODY, "user": "agent-b"})              # identity in body
    assert r.status_code == 400 and r.json()["error"]["code"] == "FIELD_NOT_ALLOWED"
    assert upstream.calls == []
    assert await owner.fetchval("SELECT count(*) FROM usage WHERE agent_id='agent-b'") == 0


async def test_token_rotation_invalidates_old_token(client, upstream):
    old = await make_agent(client, "agent-a")
    new = (await admin(client, "POST", "/admin/agents/agent-a/token", expect=200))["token"]
    assert (await chat(client, old)).status_code == 401
    assert (await chat(client, new)).status_code == 200


# --------------------------------------------------------------------------- policy

async def test_unassigned_and_unknown_mandates_look_the_same(client, upstream):
    token = await make_agent(client, "agent-a")
    await admin(client, "POST", "/admin/mandates", {"id": "m9", "name": "m9", "budget": "1", "allowed_models": ["mock-model"]}, 201)
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
    row = await owner.fetchrow("SELECT * FROM usage WHERE outcome='denied' ORDER BY id DESC LIMIT 1")
    assert row["reason_code"] == "MODEL_NOT_ALLOWED" and row["policy_version"] == gw.state.policy.version


async def test_custom_policy_rule_from_admin_api(client, upstream, owner):
    token = await make_agent(client, "agent-a", models=("mock-model", "mock-model-2"))
    rule = {"reason": "NO_MODEL_2_FOR_A", "when": {"agent.id": "agent-a", "model.name": "mock-model-2"}, "message": "not for you"}
    bad = await client.put("/admin/policy", headers=ADMIN, json={"rules": [{"reason": "x", "when": {}}]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_POLICY"
    p = await admin(client, "PUT", "/admin/policy", {"rules": [rule], "note": "block model 2"}, 200)
    assert p["number"] == 1 and p["custom"][0]["reason"] == "NO_MODEL_2_FOR_A" and p["history"][0]["note"] == "block model 2"
    r = await chat(client, token, body={**BODY, "model": "mock-model-2"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "NO_MODEL_2_FOR_A"
    assert r.json()["error"]["policy_version"] == p["version"]
    assert (await chat(client, token)).status_code == 200 and upstream.models() == ["mock-model"]
    assert await owner.fetchval("SELECT count(*) FROM audit_events WHERE decision='policy_updated'") == 1


async def test_policy_version_survives_restart(upstream, owner):
    gw.UPSTREAM_TRANSPORT = httpx.MockTransport(upstream.handler)
    rule = {"reason": "BLOCK_ALL_M1", "when": {"mandate.id": "m1"}}
    async with gw.lifespan(gw.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw") as c:
            token = await make_agent(c, "agent-a")
            version = (await admin(c, "PUT", "/admin/policy", {"rules": [rule]}, 200))["version"]
    gw.state.policy = gw.Policy()                                   # forget the in-memory copy
    async with gw.lifespan(gw.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw") as c:
            r = await chat(c, token)
            assert r.json()["error"]["code"] == "BLOCK_ALL_M1" and gw.state.policy.version == version
    assert upstream.calls == []


@pytest.mark.parametrize("extra, code", [
    ({"api_base": "http://evil"}, "FIELD_NOT_ALLOWED"),
    ({"base_url": "http://evil"}, "FIELD_NOT_ALLOWED"),
    ({"api_key": "sk-mine"}, "FIELD_NOT_ALLOWED"),
    ({"metadata": {"x": 1}}, "FIELD_NOT_ALLOWED"),
    ({"fallbacks": ["gemini-3.8-flash"]}, "FIELD_NOT_ALLOWED"),
    ({"extra_body": {"api_base": "x"}}, "FIELD_NOT_ALLOWED"),
    ({"mock_response": "free"}, "FIELD_NOT_ALLOWED"),
    ({"service_tier": "priority"}, "FIELD_NOT_ALLOWED"),
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
    assert await owner.fetchval("SELECT reason_code FROM usage WHERE outcome='denied'") == code


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
    (("PATCH", "/admin/agents/agent-a", {"budget": "0"}), ("PATCH", "/admin/agents/agent-a", {"budget": "1"}), 402, "AGENT_BUDGET_EXCEEDED"),
    (("PATCH", "/admin/mandates/m1", {"budget": "0"}), ("PATCH", "/admin/mandates/m1", {"budget": "10"}), 402, "MANDATE_BUDGET_EXCEEDED"),
]


@pytest.mark.parametrize("action, restore, status, reason", KILL_SWITCHES, ids=[k[3] for k in KILL_SWITCHES])
async def test_kill_switch_denies_next_request_before_upstream(client, upstream, owner, action, restore, status, reason):
    token = await make_agent(client, "agent-a")
    assert (await chat(client, token)).status_code == 200 and len(upstream.calls) == 1
    await admin(client, *action, expect=200)
    r = await chat(client, token)                                   # same credential, unchanged client
    assert r.status_code == status and r.json()["error"]["code"] == reason
    assert len(upstream.calls) == 1                                 # PROOF: the denied request never reached upstream
    u = await usage_row(owner, r.json()["error"]["request_id"])
    assert (u["outcome"], u["reason_code"], u["attempts"]) == ("denied", reason, 0)
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
    upstream.cost = usd(est)                            # each call costs the full estimate
    token_a = await make_agent(client, "agent-a", agent_budget=usd(2 * est), mandate_budget="10")
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
    upstream.cost = usd(est)
    token_a = await make_agent(client, "agent-a", mandate_budget=usd(2 * est))
    token_b = await make_agent(client, "agent-b")
    assert (await chat(client, token_a)).status_code == 200
    assert (await chat(client, token_b)).status_code == 200
    r = await chat(client, token_a)
    assert r.status_code == 402 and r.json()["error"]["code"] == "MANDATE_BUDGET_EXCEEDED"
    assert len(upstream.calls) == 2


async def test_budget_resets_in_a_new_month(client, upstream, owner):
    est = estimate()
    upstream.cost = usd(est)
    token = await make_agent(client, "agent-a", agent_budget=usd(est))
    assert [(await chat(client, token)).status_code for _ in range(2)] == [200, 402]
    # pretend the spend happened last month
    await owner.execute("UPDATE agents SET period = (period - interval '1 month')::date")
    assert (await admin(client, "GET", "/admin/agents/agent-a", expect=200))["spent"] == "0.000000"
    assert (await chat(client, token)).status_code == 200            # new month, fresh budget
    assert (await ledger(owner, "agents", "agent-a"))["spent_micros"] == est
    assert await owner.fetchval("SELECT period = budget_period() FROM agents") is True


async def test_inr_budget_uses_the_admin_exchange_rate(client, upstream, owner):
    est = estimate()
    upstream.cost = usd(est)
    inr = Decimal(est) * 85 * Decimal("1.5") / 1_000_000              # 1.5 calls' worth at 85 INR/USD
    token = await make_agent(client, "agent-a", agent_budget="1")
    a = await admin(client, "PATCH", "/admin/agents/agent-a", {"budget": f"{inr:.6f}", "budget_currency": "INR"}, 200)
    assert a["budget_currency"] == "INR"
    assert [(await chat(client, token)).status_code for _ in range(2)] == [200, 402]
    a = await admin(client, "GET", "/admin/agents/agent-a", expect=200)
    assert Decimal(a["spent"]) == Decimal(est) * 85 / 1_000_000 and Decimal(a["spent_usd"]) == Decimal(est) / 1_000_000
    s = await admin(client, "PUT", "/admin/settings", {"usd_inr_rate": "50"}, 200)  # rupee stronger: budget worth more USD
    assert s["usd_inr_rate"] == "50"
    assert (await chat(client, token)).status_code == 200
    assert await owner.fetchval("SELECT count(*) FROM audit_events WHERE decision='settings_updated'") == 1


async def test_concurrent_requests_cannot_overspend(client, upstream, owner):
    """50 simultaneous requests while the upstream is held: only floor(limit/estimate) may be reserved."""
    est = estimate()
    token = await make_agent(client, "agent-a", agent_budget=usd(5 * est), mandate_budget="100")
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
    recorded = await owner.fetchval("SELECT sum(cost_micros) FROM usage WHERE outcome='ok' AND agent_id='agent-a'")
    assert (a["spent_micros"], a["reserved_micros"]) == (recorded, 0)


async def test_rate_limit_is_exact_under_concurrency_and_never_forwarded(client, upstream, owner):
    token = await make_agent(client, "agent-a")
    await admin(client, "PATCH", "/admin/agents/agent-a", {"rpm_limit": 3}, 200)
    codes = sorted(r.status_code for r in await asyncio.gather(*[chat(client, token) for _ in range(10)]))
    assert codes == [200] * 3 + [429] * 7 and len(upstream.calls) == 3
    assert await owner.fetchval("SELECT count(*) FROM usage WHERE reason_code='AGENT_RATE_LIMITED'") == 7
    await admin(client, "PATCH", "/admin/agents/agent-a", {"rpm_limit": 0}, 200)  # 0 = no limit
    assert (await chat(client, token)).status_code == 200


@pytest.mark.parametrize("setup, status, charged", [
    ({"status": 500}, 500, 0),
    ({"status": 429}, 429, 0),
    ({"status": 503}, 503, 0),
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
    u = await usage_row(owner, r.headers.get("x-request-id") or r.json()["error"]["request_id"])
    assert u["cost_micros"] == expected


# --------------------------------------------------------------------------- fallback models

async def test_busy_model_falls_back_to_next_in_mandate_order(client, upstream, owner):
    token = await make_agent(client, "agent-a", models=("mock-model", "mock-model-2", "mock-model-3"))
    upstream.by_model = {"mock-model": 429, "mock-model-2": 503}
    r = await chat(client, token)
    assert r.status_code == 200 and r.headers["x-gateway-model"] == "mock-model-3"
    assert upstream.models() == ["mock-model", "mock-model-2", "mock-model-3"]
    u = await usage_row(owner, r.headers["x-request-id"])
    assert (u["model_requested"], u["model_served"], u["attempts"], u["cost_micros"]) == ("mock-model", "mock-model-3", 3, 20)
    holds = await owner.fetchval("SELECT sum(amount_micros) FROM reservations WHERE client_request_id=$1", uuid.UUID(r.headers["x-request-id"]))
    assert u["reserved_micros"] == holds > 0                       # one hold per attempt, summed
    assert tuple(await ledger(owner, "agents", "agent-a"))[:2] == (20, 0)  # busy attempts cost nothing
    assert await owner.fetchval("SELECT count(*) FROM reservations WHERE client_request_id=$1", uuid.UUID(r.headers["x-request-id"])) == 3


async def test_model_not_available_falls_back(client, upstream, owner):
    """e.g. Gemini: 'This model is no longer available to new users' (404). Not billed; try the next model."""
    token = await make_agent(client, "agent-a", models=("mock-model", "mock-model-2"))
    upstream.by_model = {"mock-model": 404}
    r = await chat(client, token)
    assert r.status_code == 200 and r.headers["x-gateway-model"] == "mock-model-2"
    assert tuple(await ledger(owner, "agents", "agent-a"))[:2] == (20, 0)


async def test_fallback_starts_after_the_requested_model(client, upstream):
    token = await make_agent(client, "agent-a", models=("mock-model", "mock-model-2", "mock-model-3"))
    upstream.by_model = {"mock-model-2": 429}
    r = await chat(client, token, body={**BODY, "model": "mock-model-2"})
    assert r.status_code == 200 and upstream.models() == ["mock-model-2", "mock-model-3"]


async def test_fallback_can_be_switched_off(client, upstream):
    token = await make_agent(client, "agent-a", models=("mock-model", "mock-model-2"), fallback_enabled=False)
    upstream.by_model = {"mock-model": 429}
    r = await chat(client, token)
    assert r.status_code == 429 and upstream.models() == ["mock-model"]


async def test_no_fallback_on_timeout_or_bad_request(client, upstream, owner):
    token = await make_agent(client, "agent-a", models=("mock-model", "mock-model-2"))
    upstream.exc = httpx.ReadTimeout("slow")
    r = await chat(client, token)
    assert r.status_code == 504 and upstream.models() == ["mock-model"]
    upstream.exc, upstream.by_model = None, {"mock-model": 400}
    assert (await chat(client, token)).status_code == 400 and upstream.models() == ["mock-model"] * 2


async def test_fallback_model_must_pass_policy_and_be_reservable(client, upstream, owner):
    token = await make_agent(client, "agent-a", models=("mock-model", "mock-model-2", "mock-model-3"))
    await admin(client, "PUT", "/admin/policy", {"rules": [{"reason": "NO_M2", "when": {"model.name": "mock-model-2"}}]}, 200)
    upstream.by_model = {"mock-model": 429}
    r = await chat(client, token)
    assert r.status_code == 200 and upstream.models() == ["mock-model", "mock-model-3"]  # mock-model-2 skipped
    # the fallback attempt cannot be reserved (here: rate limit of 1), so the agent gets the first model's 429
    await owner.execute("DELETE FROM reservations")                 # forget the calls above for the rate window
    await admin(client, "PATCH", "/admin/agents/agent-a", {"rpm_limit": 1}, 200)
    upstream.calls.clear()
    r = await chat(client, token)
    assert r.status_code == 429 and upstream.models() == ["mock-model"]


# --------------------------------------------------------------------------- free / paid tier

GEMINI_FREE_429 = ('litellm.RateLimitError: GeminiException - {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
                   '"details": [{"violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests", '
                   '"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}')


async def test_free_tier_is_detected_from_the_providers_quota_error(client, upstream, owner):
    token = await make_agent(client, "agent-a", models=("mock-model", "mock-model-2"))
    gw.state.model_keys = {"mock-model": "gemini-main", "mock-model-2": "other-key"}
    upstream.by_model, upstream.message = {"mock-model": 429}, "rate limited"     # a 429 without a free-tier quota id
    assert (await chat(client, token)).status_code == 200
    assert await owner.fetchval("SELECT count(*) FROM provider_tiers") == 0       # no signal, no guess
    upstream.message = GEMINI_FREE_429
    assert (await chat(client, token)).status_code == 200                        # fallback answered meanwhile
    row = await owner.fetchrow("SELECT * FROM provider_tiers")
    assert (row["key_name"], row["tier"], row["source"]) == ("gemini-main", "free", "detected")
    assert "FreeTier" not in row["evidence"]                                      # verdict stored, not the message
    models = {m["model"]: m for m in await admin(client, "GET", "/admin/models", expect=200)}
    assert models["mock-model"]["tier"] == "free" and models["mock-model-2"]["tier"] == "unknown"


async def test_admin_tier_overrides_detection_and_auto_restores_it(client, upstream, owner):
    token = await make_agent(client, "agent-a", models=("mock-model", "mock-model-2"))
    gw.state.model_keys = {"mock-model": "gemini-main"}
    assert (await admin(client, "PUT", "/admin/providers/gemini-main/tier", {"tier": "paid"}, 200))["tier"] == "paid"
    upstream.by_model, upstream.message = {"mock-model": 429}, GEMINI_FREE_429
    assert (await chat(client, token)).status_code == 200
    assert await owner.fetchval("SELECT tier || '/' || source FROM provider_tiers") == "paid/admin"   # admin wins
    assert (await admin(client, "PUT", "/admin/providers/gemini-main/tier", {"tier": "auto"}, 200))["tier"] == "unknown"
    assert (await chat(client, token)).status_code == 200
    assert await owner.fetchval("SELECT tier || '/' || source FROM provider_tiers") == "free/detected"
    assert await owner.fetchval("SELECT count(*) FROM audit_events WHERE decision='tier_set'") == 2
    assert (await client.put("/admin/providers/gemini-main/tier", headers=ADMIN, json={"tier": "gold"})).status_code == 400


# --------------------------------------------------------------------------- estimate

async def test_worst_rates_cover_cache_writes_tiers_and_reasoning():
    # price shapes copied from LiteLLM 1.104.0's table (per token)
    claude = {"input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05, "cache_creation_input_token_cost": 3.75e-06,
              "cache_creation_input_token_cost_above_1hr": 6e-06, "input_cost_per_token_above_200k_tokens": 6e-06,
              "output_cost_per_token_above_200k_tokens": 2.25e-05, "cache_read_input_token_cost": 3e-07,
              "input_cost_per_token_batches": 1.5e-06, "cache_creation_input_token_cost_above_1hr_above_200k_tokens": 1.2e-05}
    assert gw.worst_rates(claude, 1000) == (Decimal("0.000006"), Decimal("0.000015"))
    assert gw.worst_rates(claude, 300_000) == (Decimal("0.000012"), Decimal("0.0000225"))
    gemini_pro = {"input_cost_per_token": 1.25e-06, "output_cost_per_token": 1e-05, "input_cost_per_token_priority": 2.25e-06,
                  "input_cost_per_token_above_200k_tokens": 2.5e-06, "output_cost_per_token_above_200k_tokens": 1.5e-05,
                  "output_cost_per_token_above_200k_tokens_priority": 2.7e-05}
    assert gw.worst_rates(gemini_pro, 200_000) == (Decimal("0.00000125"), Decimal("0.00001"))   # tier needs > 200k bytes
    assert gw.worst_rates(gemini_pro, 200_001) == (Decimal("0.0000025"), Decimal("0.000015"))
    reasoning = {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06, "output_cost_per_reasoning_token": 8e-06}
    assert gw.worst_rates(reasoning, 10)[1] == Decimal("0.000008")
    tiered = {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06,
              "tiered_pricing": [{"input_cost_per_token": 4e-06, "output_cost_per_token": 9e-06}]}
    assert gw.worst_rates(tiered, 10) == (Decimal("0.000004"), Decimal("0.000009"))


# --------------------------------------------------------------------------- records, sweeper, retention

async def test_sweeper_charges_abandoned_reservation_and_late_settle_is_ignored(client, owner):
    await make_agent(client, "agent-a")
    rid = uuid.uuid4()
    assert await db.reserve(gw.state.pool, rid, rid, "agent-a", "m1", "mock-model", 1000, Decimal(85)) is None
    assert await db.sweep(gw.state.pool, None) == 1
    assert tuple(await ledger(owner, "agents", "agent-a"))[:2] == (1000, 0)
    assert await db.settle(gw.state.pool, rid, "settled", 5) is None
    assert tuple(await ledger(owner, "agents", "agent-a"))[:2] == (1000, 0)  # no double charge
    assert await owner.fetchval("SELECT reason_code FROM usage WHERE request_id=$1", rid) == "ABANDONED"


async def test_retention_deletes_old_rows_but_keeps_monthly_totals(client, owner):
    token = await make_agent(client, "agent-a")
    assert (await chat(client, token)).status_code == 200
    await owner.execute("UPDATE usage SET at = now() - interval '100 days'")
    await owner.execute("UPDATE reservations SET settled_at = now() - interval '3 days'")
    assert await db.cleanup(gw.state.pool, 90) == (1, 1)
    assert await owner.fetchval("SELECT count(*) FROM usage") == 0
    assert await owner.fetchval("SELECT requests FROM usage_monthly") == 1


async def test_admin_reporting_endpoints(client, upstream):
    token = await make_agent(client, "agent-a")
    assert (await chat(client, token)).status_code == 200
    assert (await chat(client, token, body={**BODY, "model": "mock-model-2"})).status_code == 403
    rows = await admin(client, "GET", "/admin/usage?agent=agent-a", expect=200)
    assert [r["outcome"] for r in rows] == ["denied", "ok"] and rows[1]["cost_usd"] == "0.000020"
    assert len(await admin(client, "GET", f"/admin/usage?before_id={rows[0]['id']}", expect=200)) == 1
    daily = await admin(client, "GET", "/admin/usage/daily?days=7", expect=200)
    assert len(daily) == 7 and daily[-1]["requests"] == 1 and daily[-1]["denied"] == 1 and daily[-1]["cost_usd"] == "0.000020"
    monthly = await admin(client, "GET", "/admin/usage/monthly", expect=200)
    assert monthly["month"] == monthly["current_month"] and monthly["rows"][0]["requests"] == 1
    o = await admin(client, "GET", "/admin/overview", expect=200)
    assert o["month_cost_usd"] == "0.000020" and o["today"] == {"ok": 1, "denied": 1, "failed": 0, "cost_usd": "0.000020"}
    assert o["agents"][0]["last_request_at"] is not None
    models = await admin(client, "GET", "/admin/models", expect=200)
    assert models[0] == {"model": "mock-model", "provider": None, "input_usd_per_1m": "1.000000", "output_usd_per_1m": "2.000000",
                         "max_output_tokens": gw.MAX_OUTPUT_FALLBACK, "mandates": ["m1"], "tier": "unknown",
                         "tier_source": None, "tier_evidence": None, "tier_updated_at": None}
    r = await client.post("/admin/mandates", headers=ADMIN, json={"id": "m2", "name": "x", "budget": "1",
                                                                  "allowed_models": ["mock-model", "mock-model"]})
    assert r.status_code == 400
    assert (await client.post("/admin/agents", headers=ADMIN, json={"id": "agent-a", "name": "dup", "budget": "1"})).status_code == 409


# --------------------------------------------------------------------------- admin + audit security

async def test_every_admin_route_requires_admin_token(client):
    # every route on the admin router (FastAPI keeps included routers as one _IncludedRouter entry)
    routes = [(m, r.path) for r in gw.admin.routes if isinstance(r, APIRoute) for m in r.methods]
    assert len(routes) >= 20 and all(p.startswith("/admin/") for _, p in routes)
    public = {r.path for r in gw.app.routes if isinstance(r, APIRoute)}
    assert public <= {"/health/live", "/health/ready", "/m/{mandate_id}/v1/chat/completions", "/admin/ui", "/admin/ui/{asset}"}, public
    for method, path in routes:
        url = path.replace("{agent_id}", "a").replace("{mandate_id}", "m")
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer gw_agenttoken"}):
            r = await client.request(method, url, headers=headers, json={})
            assert r.status_code == 401, (method, path, headers)


async def test_admin_ui_is_static_and_locked_down(client):
    for path in ("/admin/ui", "/admin/ui/app.js", "/admin/ui/app.css"):
        r = await client.get(path)
        assert r.status_code == 200 and "unsafe-inline" not in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
    for path in ("/admin/ui/../app.py", "/admin/ui/index.html.bak", "/admin/ui/app.py"):
        assert (await client.get(path)).status_code == 404


async def test_agent_token_cannot_use_admin_api(client):
    token = await make_agent(client, "agent-a")
    r = await client.get("/admin/agents", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_audit_log_is_append_only_for_gateway_role(client):
    async with gw.state.pool.acquire() as c:
        for sql in ("DELETE FROM audit_events", "UPDATE audit_events SET decision='allow'", "TRUNCATE audit_events",
                    "DELETE FROM policy_versions", "UPDATE policy_versions SET note='x'"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.execute(sql)


async def test_no_secret_or_prompt_in_records(client, owner):
    token = await make_agent(client, "agent-a")
    await chat(client, token, body={**BODY, "messages": [{"role": "user", "content": "TOP-SECRET-PROMPT"}]})
    rows = [dict(r) for t in ("audit_events", "usage", "usage_monthly", "reservations") for r in await owner.fetch(f"SELECT * FROM {t}")]
    dump = json.dumps(rows, default=str)
    assert "TOP-SECRET-PROMPT" not in dump and token not in dump and gw.LITELLM_GATEWAY_KEY not in dump
