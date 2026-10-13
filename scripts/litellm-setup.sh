#!/usr/bin/env bash
# Idempotent LiteLLM setup: ensure the scoped virtual key the gateway uses exists and may use every
# model in config/litellm.yaml. Re-run it after adding or removing models.
# Key value comes from LITELLM_GATEWAY_KEY in .env (passed via env, never printed or put on argv).
# Its max_budget is a gateway-wide backstop only; per-agent/per-mandate limits live in the gateway.
set -euo pipefail
cd "$(dirname "$0")/.."
BACKSTOP_USD="${1:-10}"
LITELLM_GATEWAY_KEY="$(grep '^LITELLM_GATEWAY_KEY=' .env | cut -d= -f2-)" BACKSTOP_USD="$BACKSTOP_USD" \
docker compose exec -T -e LITELLM_GATEWAY_KEY -e BACKSTOP_USD litellm python3 - <<'PY'
import json, os, urllib.request, urllib.error
def call(method, path, body=None):
    req = urllib.request.Request("http://localhost:4000" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return r.status, json.load(r)
    except urllib.error.HTTPError as e: return e.code, e.read().decode()[:300]
key = os.environ["LITELLM_GATEWAY_KEY"]
models = sorted(m["id"] for m in call("GET", "/v1/models")[1]["data"])
existing = call("GET", "/key/list?key_alias=gateway&return_full_object=true")[1]["keys"]
if existing and sorted(existing[0]["models"]) == models:
    print("gateway key up to date; models:", models, "max_budget:", existing[0]["max_budget"])
elif existing:
    status, out = call("POST", "/key/update", {"key": key, "models": models})
    assert status == 200, out
    print("gateway key updated; models:", models)
else:
    status, out = call("POST", "/key/generate", {"key": key, "key_alias": "gateway", "models": models,
        "max_budget": float(os.environ["BACKSTOP_USD"]), "metadata": {"purpose": "FastAPI gateway -> LiteLLM"}})
    assert status == 200, out
    print("created gateway key; models:", out["models"], "max_budget:", out["max_budget"])
PY
