#!/usr/bin/env python3
"""End-to-end kill-switch proof (stdlib only, runs on the host).

Chain under test:  this script -> gateway (:18080) -> REAL LiteLLM -> tests/mock_upstream.py (counts calls)
Runs in the isolated compose project "aigw-test" (see scripts/test.sh). No real model provider is called.

Proves, for two agents A and B sharing one mandate:
  1. both get answers; spend is attributed per agent and per mandate
  2. withdrawing A's budget denies A (402) while B continues; the model counter moves only for B
  3. revoking A denies A (403 AGENT_REVOKED) before LiteLLM / the model
  4. while the gateway is down, requests fail closed (nothing reaches the model)
  5. after a gateway restart A is still revoked
  6. LiteLLM's own accounting agrees: the gateway key's spend rose by exactly what the gateway charged
  7. restoring A lets A work again
  8. a busy model (429) falls back to the next model in the mandate's list, through the real LiteLLM
  9. provider key + model added from the console API work end to end; the raw key is in no database
 10. deployment checks: LiteLLM/Postgres not published, no secrets tracked in git, gateway holds no provider secrets
"""
import json, os, pathlib, subprocess, sys, time, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = {**os.environ, "COMPOSE_PROJECT_NAME": "aigw-test", "COMPOSE_FILE": "docker-compose.yml:docker-compose.test.yml",
       "GATEWAY_PORT": os.environ.get("GATEWAY_PORT", "18080"), "MOCK_PORT": os.environ.get("MOCK_PORT", "19000")}
GW = f"http://127.0.0.1:{ENV['GATEWAY_PORT']}"
MOCK = f"http://127.0.0.1:{ENV['MOCK_PORT']}"
RUN = time.strftime("%H%M%S")
A, B, M = f"e2e-a-{RUN}", f"e2e-b-{RUN}", f"e2e-m-{RUN}"
failures = 0


def dotenv(name: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1]
    raise SystemExit(f"{name} missing from .env")


ADMIN_TOKEN = dotenv("ADMIN_TOKEN")


def sh(*args: str, check=True) -> str:
    return subprocess.run(args, cwd=ROOT, env=ENV, check=check, capture_output=True, text=True).stdout


def http(method: str, url: str, body=None, token=None) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def admin(method, path, body=None):
    status, data = http(method, GW + path, body, ADMIN_TOKEN)
    assert status < 300, (method, path, status, data)
    return data


def chat(token: str) -> tuple[int, dict]:
    return http("POST", f"{GW}/m/{M}/v1/chat/completions",
                {"model": "mock-model", "messages": [{"role": "user", "content": "e2e"}]}, token)


def count() -> int:
    return http("GET", MOCK + "/count")[1]["count"]


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def wait_ready(timeout=180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if http("GET", GW + "/health/ready")[0] == 200:
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(2)
    raise SystemExit("gateway not ready; see: docker compose -p aigw-test logs gateway")


def psql(db: str, sql: str) -> str:
    return sh("docker", "compose", "exec", "-T", "db", "psql", "-U", dotenv("POSTGRES_USER"), "-d", db, "-tAc", sql).strip()


def key_spend() -> float:
    return float(psql("litellm", "SELECT coalesce(sum(spend), 0) FROM \"LiteLLM_VerificationToken\" WHERE key_alias='gateway'") or 0)


def usage(agent: str | None = None, outcome: str | None = None) -> list[dict]:
    q = "&".join(f"{k}={v}" for k, v in (("mandate", M), ("agent", agent), ("outcome", outcome)) if v)
    return admin("GET", f"/admin/usage?limit=500&{q}")


def main() -> int:
    print("== starting isolated test stack (aigw-test)")
    sh("docker", "compose", "up", "-d", "--build", "db", "litellm", "mock-upstream", "migrate", "gateway")
    sh(str(ROOT / "scripts/litellm-setup.sh"))
    wait_ready()
    http("POST", MOCK + "/reset")
    key_spend_before = key_spend()

    print("== setup: mandate", M, "with agents", A, "and", B)
    admin("POST", "/admin/mandates", {"id": M, "name": "E2E mandate", "budget": "5", "allowed_models": ["mock-model", "mock-model-2"]})
    tokens = {}
    for agent in (A, B):
        admin("POST", "/admin/agents", {"id": agent, "name": agent, "budget": "1"})
        admin("PUT", f"/admin/agents/{agent}/mandates/{M}")
        tokens[agent] = admin("POST", f"/admin/agents/{agent}/token")["token"]

    print("== 1. both agents get answers")
    for agent in (A, B):
        status, data = chat(tokens[agent])
        check(f"{agent} allowed", status == 200 and data["choices"][0]["message"]["content"] == "mock reply", f"HTTP {status}")
    check("model called exactly twice", count() == 2, f"counter={count()}")
    allowed_rows = usage(outcome="ok")
    agents = {a["id"]: a for a in admin("GET", "/admin/agents")}
    mandate = {m["id"]: m for m in admin("GET", "/admin/mandates")}[M]
    check("spend attributed per agent", all(float(agents[x]["spent_usd"]) > 0 for x in (A, B)),
          f"A={agents[A]['spent_usd']} B={agents[B]['spent_usd']}")
    check("mandate spend = A + B", round(float(mandate["spent_usd"]), 6) == round(float(agents[A]["spent_usd"]) + float(agents[B]["spent_usd"]), 6),
          f"mandate={mandate['spent_usd']}")
    check("cost came from LiteLLM", len(allowed_rows) == 2 and all(r["cost_source"] == "litellm_header" for r in allowed_rows))

    print("== 2. withdraw A's budget")
    admin("PATCH", f"/admin/agents/{A}", {"budget": "0"})
    before = count()
    status, data = chat(tokens[A])
    check("A denied 402 AGENT_BUDGET_EXCEEDED", status == 402 and data["error"]["code"] == "AGENT_BUDGET_EXCEEDED", f"HTTP {status}")
    check("model NOT called for A", count() == before, f"counter={count()}")
    status, _ = chat(tokens[B])
    check("B still allowed", status == 200)
    check("model called once, for B", count() == before + 1, f"counter={count()}")
    admin("PATCH", f"/admin/agents/{A}", {"budget": "1"})

    print("== 3. revoke A")
    admin("PATCH", f"/admin/agents/{A}", {"status": "revoked"})
    before = count()
    status, data = chat(tokens[A])
    check("A denied 403 AGENT_REVOKED", status == 403 and data["error"]["code"] == "AGENT_REVOKED", f"HTTP {status}")
    check("model NOT called", count() == before, f"counter={count()}")
    trail = admin("GET", f"/admin/audit?agent={A}&limit=20")
    check("activity log shows the revocation, usage shows the denial",
          any(e["decision"] == "agent_updated" and "status" in e["detail"].get("changed", {}) for e in trail)
          and any(r["reason_code"] == "AGENT_REVOKED" and r["policy_version"] for r in usage(agent=A, outcome="denied")))

    print("== 4. gateway down: requests fail closed")
    sh("docker", "compose", "stop", "gateway")
    before = count()
    try:
        status = chat(tokens[B])[0]
    except (urllib.error.URLError, ConnectionError, OSError):
        status = "connection refused"
    check("request while gateway down did not succeed", status != 200, str(status))
    check("model NOT called", count() == before, f"counter={count()}")

    print("== 5. restart gateway: revocation persists")
    t0 = time.time()
    sh("docker", "compose", "start", "gateway")
    wait_ready()
    print(f"  (gateway ready {time.time() - t0:.0f}s after start)")
    status, data = chat(tokens[A])
    check("A still denied after restart", status == 403 and data["error"]["code"] == "AGENT_REVOKED", f"HTTP {status}")
    check("model NOT called", count() == before, f"counter={count()}")

    print("== 6. LiteLLM's own accounting agrees (waiting ~15s for its batch write)")
    time.sleep(15)
    charged = sum(float(r["cost_usd"]) for r in usage(outcome="ok"))
    delta = key_spend() - key_spend_before
    check("gateway key spend in LiteLLM == total charged by the gateway", abs(delta - charged) < 1e-6,
          f"litellm={delta:.6f} gateway={charged:.6f}")
    check("every denied request recorded, none forwarded", len(usage(outcome="denied")) == 3 and count() == before)

    print("== 7. restore A")
    admin("PATCH", f"/admin/agents/{A}", {"status": "active"})
    before = count()
    status, _ = chat(tokens[A])
    check("A allowed again", status == 200 and count() == before + 1, f"HTTP {status}, counter={count()}")

    print("== 8. fallback through the real LiteLLM")
    http("POST", MOCK + "/fail", {"model": "mock-model", "status": 429, "times": 1})
    before = count()
    req = urllib.request.Request(f"{GW}/m/{M}/v1/chat/completions", method="POST",
                                 data=json.dumps({"model": "mock-model", "messages": [{"role": "user", "content": "e2e"}]}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {tokens[B]}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        served, status = r.headers.get("x-gateway-model"), r.status
    check("busy mock-model -> answered by mock-model-2", status == 200 and served == "mock-model-2", f"HTTP {status}, served={served}")
    check("model called twice (busy + fallback)", count() == before + 2, f"counter={count() - before}")
    last = usage(agent=B)[0]
    check("one usage row, 2 attempts, fallback model recorded", last["attempts"] == 2 and last["model_served"] == "mock-model-2")

    print("== 9. provider key and model managed from the console API")
    sentinel = f"sk-e2e-sentinel-{RUN}-do-not-store-in-clear"
    keyname, model = f"e2e-key-{RUN}", f"console-mock-{RUN}"
    status, _ = http("POST", GW + "/admin/providers", {"name": keyname, "provider": "openai_compatible", "api_key": sentinel,
                                                       "api_base": "http://mock-upstream:9000/v1"}, ADMIN_TOKEN)
    check("key saved", status == 201, f"HTTP {status}")
    listed = admin("GET", "/admin/providers")
    hint = next((k["key_hint"] for k in listed["keys"] if k["name"] == keyname), None)
    check("key is never returned in full", hint is not None and sentinel not in json.dumps(listed), f"hint={hint}")
    status, body = http("POST", GW + "/admin/models", {"name": model + "-x", "provider_key": keyname,
                                                       "provider_model": "no-such-model-anywhere"}, ADMIN_TOKEN)
    check("model with no known price is refused and not left behind",
          status == 400 and body["error"]["code"] == "PRICE_UNKNOWN" and model + "-x" not in json.dumps(admin("GET", "/admin/providers")), f"HTTP {status}")
    status, _ = http("POST", GW + "/admin/models", {"name": model, "provider_key": keyname, "provider_model": "console-mock",
                                                    "input_usd_per_1m": "1", "output_usd_per_1m": "2"}, ADMIN_TOKEN)
    check("model added with manual prices", status == 201, f"HTTP {status}")
    admin("PATCH", f"/admin/mandates/{M}", {"allowed_models": ["mock-model", "mock-model-2", model]})
    before = count()
    status, data = http("POST", f"{GW}/m/{M}/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": "e2e"}]}, tokens[B])
    check("agent call through the console-added model", status == 200 and count() == before + 1, f"HTTP {status}")
    status, _ = http("DELETE", f"{GW}/admin/models/{model}", None, ADMIN_TOKEN)
    check("model on a mandate can't be removed", status == 409, f"HTTP {status}")
    admin("PATCH", f"/admin/mandates/{M}", {"allowed_models": ["mock-model", "mock-model-2"]})
    status, _ = http("DELETE", f"{GW}/admin/providers/{keyname}", None, ADMIN_TOKEN)
    check("key used by a model can't be removed", status == 409, f"HTTP {status}")
    dumps = sh("docker", "compose", "exec", "-T", "db", "pg_dump", "-U", dotenv("POSTGRES_USER"), "-d", "litellm") + \
        sh("docker", "compose", "exec", "-T", "db", "pg_dump", "-U", dotenv("POSTGRES_USER"), "-d", "gateway")
    check("raw key is in neither database (stored encrypted)", sentinel not in dumps and len(dumps) > 10_000)
    logs = sh("docker", "compose", "logs", "gateway", "litellm", check=False)
    check("raw key is in no container log", sentinel not in logs)
    ok = all(http("DELETE", f"{GW}{path}", None, ADMIN_TOKEN)[0] == 200 for path in (f"/admin/models/{model}", f"/admin/providers/{keyname}"))
    check("model and key removed", ok and keyname not in json.dumps(admin("GET", "/admin/providers")))

    print("== 10. deployment checks")
    cfg = json.loads(subprocess.run(["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
                                    cwd=ROOT, capture_output=True, text=True, check=True).stdout)
    published = {s: v.get("ports") for s, v in cfg["services"].items() if v.get("ports")}
    check("only the gateway is published (localhost)", list(published) == ["gateway"]
          and all(p.get("host_ip") == "127.0.0.1" for p in published["gateway"]), str(published))
    tracked = sh("git", "ls-files").split()
    check("no .env / .secrets tracked in git", not [f for f in tracked if f.startswith((".env", ".secrets")) and f != ".env.example"])
    secrets = [line.split("=", 1)[1] for line in (ROOT / ".env").read_text().splitlines()
               if "=" in line and not line.startswith("POSTGRES_USER") and len(line.split("=", 1)[1]) >= 16]
    leaked = [f for f in tracked if (ROOT / f).is_file()
              and any(s in (ROOT / f).read_text(errors="ignore") for s in secrets)]
    check("no .env secret value appears in any tracked file", not leaked, ", ".join(leaked))
    env_names = sh("docker", "compose", "exec", "-T", "gateway", "env").split("\n")
    check("gateway container has no salt/provider/owner secrets",
          not [n for n in env_names if n.split("=")[0] in ("LITELLM_SALT_KEY", "GEMINI_API_KEY",
                                                           "GATEWAY_OWNER_PASSWORD", "POSTGRES_PASSWORD")])

    sh("docker", "compose", "stop")
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}  (test stack stopped; data kept in project aigw-test)")
    return 1 if failures else 0


sys.exit(main())
