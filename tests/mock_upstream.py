"""Fake OpenAI-compatible model server for the test stack (stdlib only).

LiteLLM (test config) routes `mock-model*` here. Counts every completion request so tests can
prove a denied request never reached the model.
  POST /v1/chat/completions  canned completion (10 prompt + 5 completion tokens)
  GET  /count                {"count": n, "last": <last request body>}
  POST /reset                count = 0, failures cleared
  POST /fail                 {"model", "status", "times"}: answer that model with `status` for the next `times` calls
"""
import json, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()
count, last = 0, None
failures = {}  # model -> [status, remaining]


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
        self._send(404, {"error": "not found"})

    def do_POST(self):
        global count, last
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
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
