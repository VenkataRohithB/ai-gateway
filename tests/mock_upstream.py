"""Fake OpenAI-compatible model server for the test stack (stdlib only).

LiteLLM (test config) routes `mock-model*` here. Counts every completion request so tests can
prove a denied request never reached the model.
  POST /v1/chat/completions  canned completion (10 prompt + 5 completion tokens)
  GET  /count                {"count": n, "last": <last request body>}
  POST /reset                count = 0, failures cleared
  POST /fail                 {"model", "status", "times"}: answer that model with `status` for the next `times` calls
Also a fake MCP tool server (streamable HTTP), the gateway's tool-server target in the e2e:
  POST /mcp                  initialize / tools/list / tools/call "echo" / notifications; records the Authorization header
  GET  /mcp                  an SSE stream that sends a ping every 0.2 s until the client hangs up
  GET  /mcp-count            {"count": n, "auth": <last Authorization header>, "streams": open GET streams}
"""
import json, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()
count, last = 0, None
failures = {}  # model -> [status, remaining]
mcp = {"count": 0, "auth": None, "streams": 0}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/count":
            with lock:
                return self._send(200, {"count": count, "last": last})
        if self.path == "/mcp-count":
            with lock:
                return self._send(200, mcp)
        if self.path == "/mcp":
            with lock:
                mcp["auth"], mcp["streams"] = self.headers.get("authorization"), mcp["streams"] + 1
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:  # the client hung up
                pass
            finally:
                with lock:
                    mcp["streams"] -= 1
            return
        self._send(404, {"error": "not found"})

    def mcp_post(self, body: bytes):
        msg = json.loads(body or b"{}")
        with lock:
            mcp["count"], mcp["auth"] = mcp["count"] + 1, self.headers.get("authorization")
        if "id" not in msg:  # a notification
            self.send_response(202)
            self.end_headers()
            return
        result = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock-mcp", "version": "1"}},
                  "tools/list": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]},
                  "tools/call": {"content": [{"type": "text", "text": json.dumps((msg.get("params") or {}).get("arguments"))}], "isError": False},
                  }.get(msg.get("method"))
        data = json.dumps({"jsonrpc": "2.0", "id": msg["id"], **({"result": result} if result is not None else
                                                                {"error": {"code": -32601, "message": "method not found"}})}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("mcp-session-id", "mock-session")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        global count, last
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path == "/mcp":
            return self.mcp_post(body)
        if self.path == "/reset":
            with lock:
                count, last = 0, None
                failures.clear()
            return self._send(200, {"count": 0})
        if self.path == "/fail":
            f = json.loads(body)
            with lock:
                failures[f["model"]] = [f["status"], f.get("times", 1)]
            return self._send(200, {"ok": True})
        if self.path.endswith("/chat/completions"):
            req = json.loads(body or b"{}")
            with lock:
                count += 1
                last = req
                fail = failures.get(req.get("model"))
                if fail and fail[1] > 0:
                    fail[1] -= 1
                    return self._send(fail[0], {"error": {"message": "mock failure", "type": "rate_limit_error"}})
            return self._send(200, {
                "id": f"mock-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()),
                "model": req.get("model", "mock-model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "mock reply"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
