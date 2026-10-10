#!/usr/bin/env bash
# Milestone 1 check: one real chat completion through LiteLLM (inside the container,
# so the master key is never printed). Prints model, reply and token usage only.
set -euo pipefail
cd "$(dirname "$0")/.."
MODEL="${1:-gemini-2.5-flash}"
docker compose exec -T litellm python3 - POST /chat/completions \
  "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: gateway-ok\"}]}" \
  < scripts/litellm_api.py | python3 -c '
import sys, json
status, body = sys.stdin.read().split(" ", 1)
d = json.loads(body)
if status != "200": sys.exit("FAILED " + status + ": " + str(d.get("error", d))[:300])
print("status:", status); print("model:", d["model"])
print("reply:", d["choices"][0]["message"]["content"].strip()); print("usage:", d.get("usage"))'
