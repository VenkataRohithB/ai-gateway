"""Tiny helper to call LiteLLM from inside its container using the master key from env.
Usage: docker compose exec -T litellm python3 - METHOD PATH [JSON] < scripts/litellm_api.py
Prints the response JSON; never prints the key."""
import json, os, sys, urllib.request, urllib.error
method, path = sys.argv[1], sys.argv[2]
body = sys.argv[3].encode() if len(sys.argv) > 3 else None
req = urllib.request.Request("http://localhost:4000" + path, data=body, method=method,
    headers={"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"], "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()); sys.exit(1)
