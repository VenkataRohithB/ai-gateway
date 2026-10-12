#!/usr/bin/env bash
# COSTS MONEY (~$0.0005): one real chat completion straight through LiteLLM (bypasses the gateway;
# admin/debug use only). Runs inside the container so the master key is never printed.
# Prefer tests/live_gemini.sh, which goes through the gateway. Asks for confirmation.
set -euo pipefail
cd "$(dirname "$0")/.."
MODEL="${1:-gemini-3.7-flash}"
read -r -p "This makes a REAL, paid Gemini call ($MODEL). Continue? [y/N] " ok; [ "$ok" = y ] || exit 1
docker compose exec -T litellm python3 - POST /chat/completions \
  "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: gateway-ok\"}]}" \
  < scripts/litellm_api.py | python3 -c '
import sys, json
status, body = sys.stdin.read().split(" ", 1)
d = json.loads(body)
if status != "200": sys.exit("FAILED " + status + ": " + str(d.get("error", d))[:300])
print("status:", status); print("model:", d["model"])
print("reply:", d["choices"][0]["message"]["content"].strip()); print("usage:", d.get("usage"))'
