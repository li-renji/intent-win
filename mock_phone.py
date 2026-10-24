"""Mock 手机服务：记录收到的路径，用于验证网关的路径重写与重试逻辑。"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19999
FAIL_FIRST = int(sys.argv[2]) if len(sys.argv) > 2 else 0
hits = []


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        hits.append(("GET", self.path))
        print("MOCK HIT", self.path, flush=True)
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "qwen2.5-1.5b-mnn"}]})
        elif self.path == "/health":
            self._send(200, {"ok": True, "mock": True})
        else:
            self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(n) if n else b""
        hits.append(("POST", self.path))
        print("MOCK HIT", self.path, "body:", body[:120].decode("utf-8", "ignore"), flush=True)
        if len(hits) <= FAIL_FIRST:
            # 模拟手机冻结：接受连接但不返回
            print("MOCK simulating freeze (no reply)", flush=True)
            time.sleep(30)
            return
        self._send(200, {"id": "chatcmpl-mock", "object": "chat.completion",
                         "model": "qwen2.5-1.5b-mnn",
                         "choices": [{"index": 0, "message": {"role": "assistant",
                                                              "content": "MOCK OK"}, "finish_reason": "stop"}]})


print("mock phone on 127.0.0.1:%d" % PORT, flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
