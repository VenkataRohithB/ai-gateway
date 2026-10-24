"""Tiny demo agent: asks the gateway one question per run, like a real agent would.
Usage: python scripts/demo_agent.py ["your question"] [mandate=demo] [token_file=.secrets/demo-a.token] [model=gemini-3.7-flash]
COSTS MONEY when GEMINI_API_KEY is set (a few hundredths of a cent per call).
"""
import json, pathlib, sys, urllib.error, urllib.request

args = sys.argv[1:] + [None] * 4
question = args[0] or "In one sentence, what is an AI gateway?"
mandate, token_file, model = args[1] or "demo", args[2] or ".secrets/demo-a.token", args[3] or "gemini-3.7-flash"
token = pathlib.Path(token_file).read_text().strip()

body = {"model": model, "max_tokens": 256, "messages": [{"role": "user", "content": question}]}
req = urllib.request.Request(f"http://127.0.0.1:8080/m/{mandate}/v1/chat/completions", json.dumps(body).encode(),
                             {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
        print("ALLOWED", r.status, "| model:", r.headers["x-gateway-model"], "| request:", r.headers["x-request-id"])
        print("reply:", d["choices"][0]["message"]["content"].strip())
        print("usage:", d["usage"])
except urllib.error.HTTPError as e:
    err = json.load(e).get("error", {})
    print("BLOCKED", e.code, err.get("code"), "-", err.get("message"))
