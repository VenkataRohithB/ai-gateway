"""MCP tool gate: real Postgres, gateway in-process, tool server replaced by a mock (httpx.MockTransport).
The mock's call list is the proof that a denied message never reached the tool server."""
import asyncio, json

import cedarpy, httpx, jwt, pytest

import app as gw
from test_gateway import ADMIN, Upstream, admin, anyio_backend, make_agent, owner, usage_row  # noqa: F401 (fixtures)

pytestmark = pytest.mark.anyio
TOOLS_URL = "http://tools.test/mcp"
CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"x": 1}}}


class ToolServer:
    """Stands in for a ToolHive proxy. Records every request it receives."""
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.status == "down":
            raise httpx.ConnectError("connection refused")
        self.calls.append(request)
        if request.method == "GET":  # an endless SSE stream
            async def pings():
                while True:
                    yield b": ping\n\n"
                    await asyncio.sleep(0.05)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=pings())
        if self.status != 200:
            return httpx.Response(self.status, json={"jsonrpc": "2.0", "id": 1, "error": {"code": self.status, "message": "no"}})
        msg = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "application/json", "mcp-session-id": "s1", "x-internal": "1"},
                              json={"jsonrpc": "2.0", "id": msg.get("id"), "result": {"echo": msg.get("params")}})


@pytest.fixture
async def tools():
    return ToolServer()


@pytest.fixture
async def client(tools, owner):
    upstream = Upstream()
    route = lambda r: tools.handler(r) if r.url.host == "tools.test" else upstream.handler(r)
    gw.UPSTREAM_TRANSPORT = httpx.MockTransport(route)
    gw.state.model_keys = {}
    async with gw.lifespan(gw.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw") as c:
            yield c


async def setup(c, agent="agent-a", mandate="m1", tools=None, **mandate_opts) -> str:
    token = await make_agent(c, agent, mandate, **mandate_opts)
    r = await c.post("/admin/tool-servers", headers=ADMIN, json={"id": "echo-srv", "name": "Echo", "url": TOOLS_URL})
    assert r.status_code in (201, 409), r.text
    if tools is not None:
        await admin(c, "PUT", f"/admin/mandates/{mandate}/tool-servers/echo-srv", {"tools": tools}, 200)
    return token


async def settled():
    """Allowed tool calls are written to usage when their response closes."""
    await asyncio.gather(*gw.settling)


async def mcp(c, token, body=CALL, mandate="m1", server="echo-srv", method="POST", **headers):
    return await c.request(method, f"/m/{mandate}/mcp/{server}", headers={"Authorization": f"Bearer {token}", **headers},
                           **({"json": body} if method == "POST" else {}))


async def test_allowed_tool_call_is_forwarded_with_a_signed_pass_not_the_agent_token(client, tools, owner):
    token = await setup(client, tools=[])
    r = await mcp(client, token, **{"mcp-session-id": "s1", "x-evil": "1"})
    assert r.status_code == 200 and r.json()["result"]["echo"]["name"] == "echo"
    assert r.headers["mcp-session-id"] == "s1" and "x-internal" not in r.headers and r.headers["x-request-id"]
    [sent] = tools.calls
    assert token not in json.dumps(dict(sent.headers)) and "x-evil" not in sent.headers and sent.headers["mcp-session-id"] == "s1"
    assert json.loads(sent.content) == CALL
    # the pass verifies against the gateway's published key, for this server's audience only
    conf = (await client.get("/.well-known/openid-configuration")).json()
    assert conf["issuer"] == gw.GATEWAY_ISSUER and conf["jwks_uri"].endswith("/.well-known/jwks.json")
    [key] = (await client.get("/.well-known/jwks.json")).json()["keys"]
    assert "d" not in key and key["alg"] == "RS256"  # public half only
    claims = jwt.decode(sent.headers["authorization"].split()[1], jwt.PyJWK(key).key, ["RS256"], audience="gateway-tools-echo-srv",
                        issuer=gw.GATEWAY_ISSUER)
    assert (claims["sub"], claims["mandate"], claims["server"], claims["exp"] - claims["iat"]) == ("agent-a", "m1", "echo-srv", 60)
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(sent.headers["authorization"].split()[1], jwt.PyJWK(key).key, ["RS256"], audience="another-server")
    await settled()
    u = await usage_row(owner, r.headers["x-request-id"])
    assert (u["outcome"], u["tool_server"], u["tool_name"], u["model_requested"], u["cost_micros"]) == ("ok", "echo-srv", "echo", None, 0)


async def test_server_must_be_allowed_under_the_mandate(client, tools):
    token = await setup(client)                                   # server exists, not allowed under m1
    for server in ("echo-srv", "no-such-server"):                 # unknown looks the same: no discovery
        r = await mcp(client, token, server=server)
        assert r.status_code == 403 and r.json()["error"]["code"] == "TOOL_SERVER_NOT_ALLOWED"
    await admin(client, "PUT", "/admin/mandates/m1/tool-servers/echo-srv", {"tools": []}, 200)
    await admin(client, "PATCH", "/admin/tool-servers/echo-srv", {"status": "suspended"}, 200)
    assert (await mcp(client, token)).json()["error"]["code"] == "TOOL_SERVER_NOT_ALLOWED"
    assert not tools.calls
    await admin(client, "PATCH", "/admin/tool-servers/echo-srv", {"status": "active"}, 200)
    assert (await mcp(client, token)).status_code == 200 and len(tools.calls) == 1


async def test_tool_allowlist_is_enforced_per_tools_call(client, tools, owner):
    token = await setup(client, tools=["echo"])
    other = {**CALL, "params": {"name": "delete_everything"}}
    r = await mcp(client, token, other)
    assert r.status_code == 403 and r.json()["error"]["code"] == "MCP_TOOL_NOT_ALLOWED"
    assert (await usage_row(owner, r.headers["x-request-id"]))["tool_name"] == "delete_everything"
    r = await mcp(client, token, [CALL, other])                 # one bad call in a batch blocks the batch
    assert r.status_code == 403
    assert (await mcp(client, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).status_code == 200
    assert (await mcp(client, token)).status_code == 200
    assert len(tools.calls) == 2


@pytest.mark.parametrize("change, status, code", [
    (("agents", "agent-a", {"status": "suspended"}), 403, "AGENT_INACTIVE"),
    (("agents", "agent-a", {"status": "revoked"}), 403, "AGENT_REVOKED"),
    (("mandates", "m1", {"status": "suspended"}), 403, "MANDATE_INACTIVE"),
    (("agents", "agent-a", {"budget": "0"}), 402, "AGENT_BUDGET_EXCEEDED"),     # withdrawing budget stops tools too
    (("mandates", "m1", {"budget": "0"}), 402, "MANDATE_BUDGET_EXCEEDED"),
])
async def test_kill_switches_stop_tool_calls_before_the_server(client, tools, change, status, code):
    token = await setup(client, tools=[])
    assert (await mcp(client, token)).status_code == 200
    table, id_, body = change
    await admin(client, "PATCH", f"/admin/{table}/{id_}", body, 200)
    for method in ("POST", "GET", "DELETE"):
        r = await mcp(client, token, method=method)
        assert r.status_code == status and r.json()["error"]["code"] == code, method
    assert len(tools.calls) == 1


async def test_unassigned_agent_and_bad_tokens_are_refused(client, tools):
    token = await setup(client, tools=[])
    await admin(client, "DELETE", "/admin/agents/agent-a/mandates/m1", expect=200)
    assert (await mcp(client, token)).json()["error"]["code"] == "MANDATE_NOT_ASSIGNED"
    for bad in ("", "gw_nope", gw.ADMIN_TOKEN.decode()):
        r = await client.post("/m/m1/mcp/echo-srv", json=CALL, headers={"Authorization": f"Bearer {bad}"} if bad else {})
        assert r.status_code == 401
    assert not tools.calls


async def test_custom_policy_rule_on_a_tool_server(client, tools):
    token = await setup(client, tools=[])
    await admin(client, "PUT", "/admin/policy", {"rules": [{"reason": "NO_ECHO_FOR_A", "when": {"agent.id": "agent-a", "tool.server": "echo-srv"}}]}, 200)
    r = await mcp(client, token)
    assert r.status_code == 403 and r.json()["error"]["code"] == "NO_ECHO_FOR_A" and r.json()["error"]["policy_version"].startswith("v1-")
    assert not tools.calls


async def test_open_stream_ends_when_the_agent_is_suspended(client, tools):
    token = await setup(client, tools=[])
    stream = asyncio.create_task(mcp(client, token, method="GET"))
    for _ in range(100):
        if gw.state.streams._open:
            break
        await asyncio.sleep(0.02)
    assert gw.state.streams._open, "stream never opened"
    await asyncio.sleep(0.2)
    assert not stream.done()                                       # endless until stopped
    await admin(client, "PATCH", "/admin/agents/agent-a", {"status": "suspended"}, 200)
    r = await asyncio.wait_for(stream, 5)                          # ended by the change, not by the server
    assert r.status_code == 200 and b"ping" in r.content and not gw.state.streams._open
    assert (await mcp(client, token)).status_code == 403          # and the next message is refused


async def test_open_stream_ends_when_the_server_is_taken_off_the_mandate(client, tools):
    token = await setup(client, tools=[])
    stream = asyncio.create_task(mcp(client, token, method="GET"))
    for _ in range(100):
        if gw.state.streams._open:
            break
        await asyncio.sleep(0.02)
    await admin(client, "DELETE", "/admin/mandates/m1/tool-servers/echo-srv", expect=200)
    await asyncio.wait_for(stream, 5)
    assert (await mcp(client, token)).json()["error"]["code"] == "TOOL_SERVER_NOT_ALLOWED"


async def test_tool_server_problems_never_look_like_the_agents_fault(client, tools, owner):
    token = await setup(client, tools=[])
    tools.status = 401                                             # refused the gateway's pass: configuration
    r = await mcp(client, token)
    assert r.status_code == 502 and r.json()["error"]["code"] == "TOOL_SERVER_AUTH_FAILED"
    tools.status = 403                                             # ToolHive's own per-tool Cedar said no
    r = await mcp(client, token)
    assert r.status_code == 403
    await settled()
    assert (await usage_row(owner, r.headers["x-request-id"]))["reason_code"] == "TOOL_DENIED_BY_SERVER"
    tools.status = "down"
    r = await mcp(client, token)
    assert r.status_code == 503 and r.json()["error"]["code"] == "TOOL_SERVER_UNAVAILABLE"


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"method": 5}', b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}'])
async def test_malformed_mcp_messages_are_refused(client, tools, body):
    token = await setup(client, tools=[])
    r = await client.post("/m/m1/mcp/echo-srv", content=body, headers={"Authorization": f"Bearer {token}", "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_REQUEST" and not tools.calls


async def test_admin_api_is_audited_and_generates_the_toolhive_config(client, owner):
    await make_agent(client, "agent-a")
    await admin(client, "POST", "/admin/tool-servers", {"id": "echo-srv", "name": "Echo", "url": TOOLS_URL}, 201)
    assert (await client.post("/admin/tool-servers", headers=ADMIN, json={"id": "echo-srv", "name": "x", "url": TOOLS_URL})).status_code == 409
    assert (await client.post("/admin/tool-servers", headers=ADMIN, json={"id": "x", "name": "x", "url": "file:///etc/passwd"})).status_code == 400
    await admin(client, "PUT", "/admin/mandates/m1/tool-servers/echo-srv", {"tools": ["echo", "time"]}, 200)
    assert (await client.put("/admin/mandates/m1/tool-servers/echo-srv", headers=ADMIN, json={"tools": ["bad name!"]})).status_code == 400
    [s] = (await admin(client, "GET", "/admin/tool-servers"))["servers"]
    assert s["mandates"] == [{"mandate_id": "m1", "tools": ["echo", "time"]}] and s["audience"] == "gateway-tools-echo-srv"
    th = s["toolhive"]
    assert "--oidc-audience gateway-tools-echo-srv" in th["command"] and f"--oidc-issuer {gw.GATEWAY_ISSUER}" in th["command"]
    policies = th["authz_config"]["cedar"]["policies"]
    assert th["authz_config"]["type"] == "cedarv1" and any('resource == Tool::"time"' in p for p in policies)
    assert all('principal.claim_mandate == "m1"' in p for p in policies)
    cedarpy.PolicySet.from_str("\n".join(policies))               # valid Cedar for ToolHive to load
    assert (await client.delete("/admin/tool-servers/echo-srv", headers=ADMIN)).status_code == 409   # still allowed under m1
    await admin(client, "DELETE", "/admin/mandates/m1/tool-servers/echo-srv", expect=200)
    await admin(client, "DELETE", "/admin/tool-servers/echo-srv", expect=200)
    trail = [e["decision"] for e in await admin(client, "GET", "/admin/audit")]
    assert {"tool_server_added", "tool_server_allowed", "tool_server_disallowed", "tool_server_removed"} <= set(trail)


async def test_signing_key_is_kept_across_restarts(client):
    assert (await gw.toolgate.load_signer(gw.state.pool)).jwk == gw.state.signer.jwk
    assert await gw.state.pool.fetchval("SELECT count(*) FROM signing_keys") == 1
