"""模拟一个支持 Range 的模型镜像源，用于验证 ModelProvisioner 的
多镜像切换、断点续传、sha256 校验逻辑。

用法: python mock_mirror.py <port> <file1> [file2...]
行为:
  - 支持 Range: bytes=N-  (返回 206)
  - 可用 --fail-first N 让前 N 个文件请求直接 500（测试镜像切换）
  - 可用 --truncate N 把响应截断 N 字节（测试大小校验）
"""
import http.server
import os
import socketserver
import sys
import hashlib

ROOT = None
FAIL_FIRST = 0
TRUNCATE = 0
STATE = {"hits": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        rel = self.path.lstrip("/").split("?")[0]
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        STATE["hits"] += 1
        if FAIL_FIRST > 0 and STATE["hits"] <= FAIL_FIRST:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0])

        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
        if TRUNCATE > 0:
            data = data[:-TRUNCATE]

        if start > 0:
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(sys.argv[1])
    ROOT = sys.argv[2]
    srv = socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler)
    srv.allow_reuse_address = True
    print("mock mirror on %d root=%s" % (port, ROOT), flush=True)
    srv.serve_forever()
