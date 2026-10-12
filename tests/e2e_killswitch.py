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
  6. LiteLLM's own spend log contains every allowed request id and none of the denied ones
  7. restoring A lets A work again
  8. deployment checks: LiteLLM/Postgres not published, no secrets tracked in git, gateway holds no provider secrets
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


def main() -> int:
    print("== starting isolated test stack (aigw-test)")
    sh("docker", "compose", "up", "-d", "--build", "db", "litellm", "mock-upstream", "migrate", "gateway")
    sh(str(ROOT / "scripts/litellm-setup.sh"))
    wait_ready()
    http("POST", MOCK + "/reset")

    print("== setup: mandate", M, "with agents", A, "and", B)
    admin("POST", "/admin/mandates", {"id": M, "name": "E2E mandate", "budget_usd": "5", "allowed_models": ["mock-model"]})
    tokens = {}
    for agent in (A, B):
        admin("POST", "/admin/agents", {"id": agent, "name": agent, "budget_usd": "1"})
        admin("PUT", f"/admin/agents/{agent}/mandates/{M}")
        tokens[agent] = admin("POST", f"/admin/agents/{agent}/token")["token"]
    allowed, denied = [], []

    print("== 1. both agents get answers")
    for agent in (A, B):
        status, data = chat(tokens[agent])
        check(f"{agent} allowed", status == 200 and data["choices"][0]["message"]["content"] == "mock reply", f"HTTP {status}")
    check("model called exactly twice", count() == 2, f"counter={count()}")
    audit = admin("GET", f"/admin/audit?mandate={M}&decision=settled")
    allowed += [e["request_id"] for e in audit]
    agents = {a["id"]: a for a in admin("GET", "/admin/agents")}
    mandate = {m["id"]: m for m in admin("GET", "/admin/mandates")}[M]
    check("spend attributed per agent", all(agents[x]["spent_micros"] > 0 for x in (A, B)),
          f"A={agents[A]['spent_usd']} B={agents[B]['spent_usd']}")
    check("mandate spend = A + B", mandate["spent_micros"] == agents[A]["spent_micros"] + agents[B]["spent_micros"],
          f"mandate={mandate['spent_usd']}")
    check("cost came from LiteLLM", all(e["cost_source"] == "litellm_header" for e in audit))

    print("== 2. withdraw A's budget")
    admin("PATCH", f"/admin/agents/{A}", {"budget_usd": "0"})
    before = count()
    status, data = chat(tokens[A])
    denied.append(data.get("error", {}).get("request_id"))
    check("A denied 402 AGENT_BUDGET_EXCEEDED", status == 402 and data["error"]["code"] == "AGENT_BUDGET_EXCEEDED", f"HTTP {status}")
    check("model NOT called for A", count() == before, f"counter={count()}")
    status, _ = chat(tokens[B])
    check("B still allowed", status == 200)
    check("model called once, for B", count() == before + 1, f"counter={count()}")
    admin("PATCH", f"/admin/agents/{A}", {"budget_usd": "1"})

    print("== 3. revoke A")
    admin("PATCH", f"/admin/agents/{A}", {"status": "revoked"})
    before = count()
    status, data = chat(tokens[A])
    denied.append(data.get("error", {}).get("request_id"))
    check("A denied 403 AGENT_REVOKED", status == 403 and data["error"]["code"] == "AGENT_REVOKED", f"HTTP {status}")
    check("model NOT called", count() == before, f"counter={count()}")
    trail = admin("GET", f"/admin/audit?agent={A}&limit=20")
    check("audit shows revocation then denial",
          any(e["decision"] == "agent_updated" and "status" in e["detail"].get("changed", {}) for e in trail)
          and any(e["decision"] == "deny" and e["reason_code"] == "AGENT_REVOKED" and e["policy_version"] for e in trail))

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
    denied.append(data.get("error", {}).get("request_id"))
    check("A still denied after restart", status == 403 and data["error"]["code"] == "AGENT_REVOKED", f"HTTP {status}")
    check("model NOT called", count() == before, f"counter={count()}")

    print("== 6. LiteLLM's own spend log agrees (waiting ~15s for its batch write)")
    allowed = [e["request_id"] for e in admin("GET", f"/admin/audit?mandate={M}&decision=settled")]
    time.sleep(15)
    ids = ",".join(f"'{x}'" for x in allowed + denied if x)
    logged = set(psql("litellm", f'SELECT request_id FROM "LiteLLM_SpendLogs" WHERE request_id IN ({ids}) '
                                 f'UNION SELECT litellm_call_id FROM "LiteLLM_SpendLogs" WHERE litellm_call_id IN ({ids})').split())
    check(f"all {len(allowed)} allowed requests are in LiteLLM_SpendLogs", set(allowed) <= logged, f"{len(logged & set(allowed))}/{len(allowed)}")
    check(f"none of the {len(denied)} denied requests reached LiteLLM", not (set(denied) & logged))

    print("== 7. restore A")
    admin("PATCH", f"/admin/agents/{A}", {"status": "active"})
    before = count()
    status, _ = chat(tokens[A])
    check("A allowed again", status == 200 and count() == before + 1, f"HTTP {status}, counter={count()}")

    print("== 8. deployment checks")
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
    check("gateway container has no master/salt/provider/owner secrets",
          not [n for n in env_names if n.split("=")[0] in ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "GEMINI_API_KEY",
                                                           "GATEWAY_OWNER_PASSWORD", "POSTGRES_PASSWORD")])

    sh("docker", "compose", "stop")
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}  (test stack stopped; data kept in project aigw-test)")
    return 1 if failures else 0


sys.exit(main())
