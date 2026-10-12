#!/usr/bin/env bash
# OPT-IN, COSTS MONEY (~$0.0005): one real model call through the full chain
#   agent token -> gateway :8080 -> LiteLLM -> Gemini
# then prints the gateway's audit rows for it (cost source should be litellm_header).
# Usage: tests/live_gemini.sh [mandate=demo] [token_file=.secrets/demo-a.token] [model=gemini-3.7-flash]
set -euo pipefail
cd "$(dirname "$0")/.."
MANDATE="${1:-demo}"; TOKEN_FILE="${2:-.secrets/demo-a.token}"; MODEL="${3:-gemini-3.7-flash}"
TOKEN="$(cat "$TOKEN_FILE")"
RESP="$(curl -s -D - "http://127.0.0.1:${GATEWAY_PORT:-8080}/m/$MANDATE/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":1024,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: gateway-ok\"}]}")"
RID="$(printf '%s' "$RESP" | grep -i '^x-request-id:' | tr -d '\r' | cut -d' ' -f2)"
printf '%s' "$RESP" | sed -n '/^\r$/,$p' | python3 -c '
import sys, json
d = json.loads(sys.stdin.read())
if "error" in d: sys.exit("DENIED/FAILED: " + json.dumps(d["error"]))
print("reply:", d["choices"][0]["message"]["content"].strip(), "| finish:", d["choices"][0]["finish_reason"], "| usage:", d["usage"])'
echo "request_id: $RID"
scripts/gw_admin.py GET "/admin/audit?request_id=$RID" | python3 -c '
import sys, json
s, b = sys.stdin.read().split(" ", 1)
for e in reversed(json.loads(b)):
    print("  audit:", *(f"{k}={e[k]}" for k in ("decision", "reserved_usd", "cost_usd", "cost_source", "prompt_tokens", "completion_tokens")))'
