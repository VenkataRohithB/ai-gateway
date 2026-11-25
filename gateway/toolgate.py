"""Tool gate: the gateway's signing key, short-lived passes for MCP tool servers, and the open MCP streams.

A pass is an RS256 JWT signed per forwarded MCP request: sub = agent, `mandate`, `server`, aud = the server's
audience, valid PASS_SECONDS. A tool server (ToolHive with --oidc-issuer = the gateway) verifies it against the
gateway's JWKS and applies its own per-tool Cedar rules to the claims. An agent's gw_ token never leaves the gateway.
"""
import asyncio, json, re, secrets, time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from policy import literal

PASS_SECONDS = 60
TOOL_NAME = r"^[A-Za-z0-9_.:/-]{1,128}$"


class Signer:
    def __init__(self, kid: str, private_pem: str):
        self.kid = kid
        self._key = serialization.load_pem_private_key(private_pem.encode(), None)
        self.jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key())), "kid": kid, "use": "sig", "alg": "RS256"}

    def mint(self, issuer: str, agent_id: str, mandate_id: str, server_id: str, audience: str) -> str:
        now = int(time.time())
        return jwt.encode({"iss": issuer, "sub": agent_id, "aud": audience, "mandate": mandate_id, "server": server_id,
                           "iat": now, "nbf": now, "exp": now + PASS_SECONDS, "jti": secrets.token_hex(12)},
                          self._key, "RS256", headers={"kid": self.kid})


async def load_signer(pool) -> Signer:
    """The oldest stored key; generated on first start. Two gateways starting at once still agree (oldest wins)."""
    if not await pool.fetchval("SELECT 1 FROM signing_keys LIMIT 1"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        await pool.execute("INSERT INTO signing_keys (kid, private_pem) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                           f"gw-{secrets.token_hex(4)}", pem)
    row = await pool.fetchrow("SELECT kid, private_pem FROM signing_keys ORDER BY created_at, kid LIMIT 1")
    return Signer(row["kid"], row["private_pem"])


def tool_calls(raw: bytes) -> list[str]:
    """Tool names of the tools/call requests in one MCP POST body (a JSON-RPC message or a batch).
    Raises ValueError for anything that isn't JSON-RPC, so nothing unparsed is forwarded."""
    try:
        body = json.loads(raw)
    except ValueError:
        raise ValueError("body is not valid JSON")
    msgs = body if isinstance(body, list) else [body]
    if not msgs or not all(isinstance(m, dict) and isinstance(m.get("method", ""), str) for m in msgs):
        raise ValueError("body must be a JSON-RPC message or a list of them")
    names = []
    for m in msgs:
        if m.get("method") == "tools/call":
            name = (m.get("params") or {}).get("name") if isinstance(m.get("params"), dict) else None
            if not isinstance(name, str) or not re.match(TOOL_NAME, name):
                raise ValueError("tools/call needs params.name")
            names.append(name)
    return names


def toolhive_config(server_id: str, grants: list[tuple[str, list[str]]]) -> dict:
    """ToolHive --authz-config for one server: each mandate allowed here may call its tools (all, when the list
    is empty) and read the server's prompts and resources. Everything else is denied by ToolHive's default."""
    policies = []
    for mandate_id, tools in grants:
        when = f"principal.claim_mandate == {literal(mandate_id)} && principal.claim_server == {literal(server_id)}"
        policies += [f'permit(principal, action == Action::"call_tool", resource == Tool::{literal(t)}) when {{ {when} }};'
                     for t in tools] or [f'permit(principal, action == Action::"call_tool", resource) when {{ {when} }};']
        policies.append(f'permit(principal, action in [Action::"get_prompt", Action::"read_resource"], resource) when {{ {when} }};')
    return {"version": "1.0", "type": "cedarv1", "cedar": {"policies": policies, "entities_json": "[]"}}


class Streams:
    """Open MCP responses. close() ends the matching ones at once; the agent's next message meets the gate again."""
    def __init__(self):
        self._open: dict[asyncio.Event, tuple[str, str, str]] = {}

    def open(self, agent_id: str, mandate_id: str, server_id: str) -> asyncio.Event:
        ev = asyncio.Event()
        self._open[ev] = (agent_id, mandate_id, server_id)
        return ev

    def done(self, ev: asyncio.Event) -> None:
        self._open.pop(ev, None)

    def close(self, agent: str | None = None, mandate: str | None = None, server: str | None = None) -> int:
        hits = [ev for ev, (a, m, s) in self._open.items()
                if (agent is None or a == agent) and (mandate is None or m == mandate) and (server is None or s == server)]
        for ev in hits:
            ev.set()
        return len(hits)
