#!/usr/bin/env python3
"""Call the gateway admin API. Reads ADMIN_TOKEN (and optional GATEWAY_URL) from env or ./.env.

  scripts/gw_admin.py GET  /admin/summary
  scripts/gw_admin.py POST /admin/agents '{"id":"demo-a","name":"Demo A","budget_usd":"1.00"}'
  scripts/gw_admin.py POST /admin/agents/demo-a/token --save-token .secrets/demo-a.token
  scripts/gw_admin.py PATCH /admin/agents/demo-a '{"status":"revoked"}'

--save-token FILE writes the returned agent token to FILE (mode 0600) instead of printing it.
"""
import json, os, pathlib, sys, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent

def env(name: str, default: str | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1]
    return default

def main(argv: list[str]) -> int:
    save = None
    if "--save-token" in argv:
        i = argv.index("--save-token")
        save = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__)
        return 2
    method, path, body = argv[0].upper(), argv[1], argv[2] if len(argv) > 2 else None
    base = env("GATEWAY_URL") or f"http://127.0.0.1:{env('GATEWAY_PORT', '8080')}"
    req = urllib.request.Request(base + path, method=method, data=body.encode() if body else None,
                                 headers={"Authorization": f"Bearer {env('ADMIN_TOKEN')}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, data = r.status, json.load(r)
    except urllib.error.HTTPError as e:
        status, data = e.code, json.loads(e.read() or b"{}")
    if save and isinstance(data, dict) and "token" in data:
        path_ = pathlib.Path(save)
        path_.parent.mkdir(parents=True, exist_ok=True)
        path_.touch(mode=0o600)
        path_.write_text(data["token"] + "\n")
        data["token"] = f"<saved to {save}>"
    print(status, json.dumps(data, indent=2, default=str))
    return 0 if status < 400 else 1

sys.exit(main(sys.argv[1:]))
