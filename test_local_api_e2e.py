# -*- coding: utf-8 -*-
"""端到端：内置本地推理模型 → 通过对外 API（6802）提供服务。

这条链路是「手机端同款形态」的核心：模型跑在内置推理引擎进程里，
外部程序通过 OpenAI 兼容 API 访问，**看起来和调用云端 API 完全一样**。

验证顺序：
  1. 本地引擎进程启动并就绪（健康探测真实轮询）
  2. Context.endpoint() 解析到本地引擎（source=local-engine）
  3. 外部客户端 GET 6802 /v1/models → 拿到本地模型名
  4. 外部客户端 POST 6802 /v1/chat/completions → 内容来自本地引擎
  5. 引擎停止后 → 端点解析退回配置/CDN 降级路径，不静默失败
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.config import load_config
from agent.history import SessionHistory
from agent.local_engine import LocalEngine, resolve_llm
from agent.servers import Context, serve_6802
from agent.tools import ToolBus

ENGINE_PORT = 18973
HTTP_PORT = 18972
MARKER = "内置本地引擎回答"   # 伪引擎返回的独特标记，证明响应确实来自它

results = []


def check(name, ok, extra=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" | " + extra) if extra else ""))
    results.append(ok)


# 伪 llama-server：health + models + chat/completions（SSE）
FAKE_ENGINE = '''
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = 0
a = sys.argv[1:]
for i, x in enumerate(a):
    if x == "--port" and i + 1 < len(a):
        port = int(a[i + 1])
MODEL = "qwen2.5-1.5b-instruct-q4_k_m"
MARK = "__MARKER__"
class H(BaseHTTPRequestHandler):
    def log_message(self, *p): pass
    def do_GET(self):
        if self.path.startswith("/health"):
            b = b'{"status":"ok"}'
            self.send_response(200); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if self.path.startswith("/v1/models"):
            d = json.dumps({"object":"list","data":[{"id":MODEL,"object":"model","owned_by":"llama.cpp"}]}).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(d)))
            self.end_headers(); self.wfile.write(d); return
        self.send_response(404); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw)
        except Exception:
            req = {}
        stream = bool(req.get("stream"))
        msgs = req.get("messages") or []
        last = msgs[-1].get("content", "") if msgs else ""
        reply = "%s: 收到《%s》" % (MARK, last[:20])
        if stream:
            chunks = []
            for ch in reply:
                d = json.dumps({"choices":[{"delta":{"content":ch}}]}).encode()
                chunks.append(b"data: " + d + b"\\n\\n")
            chunks.append(b"data: [DONE]\\n\\n")
            body = b"".join(chunks)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        obj = {"choices":[{"message":{"role":"assistant","content":reply},"finish_reason":"stop"}]}
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''.replace("__MARKER__", MARKER)


def http_json(method, url, body=None, timeout=15):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return resp.status, json.loads(raw)
    except Exception:
        return resp.status, raw


async def boot_server(ctx, ready_flag):
    await serve_6802(ctx)
    ready_flag.set()
    # 永久挂起，模拟 app.py 里 loop.run_forever() 的保活行为；
    # 否则协程返回后 asyncio.run 会关闭事件循环，服务器随之消失。
    await asyncio.get_running_loop().create_future()


def main():
    root = tempfile.mkdtemp(prefix="qd_local_api_")
    try:
        script = os.path.join(root, "_engine.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(FAKE_ENGINE)

        cfg = load_config()
        cfg["project_root"] = root
        cfg["llm_mode"] = "local"
        cfg["local_engine_binary"] = script
        cfg["local_engine_port"] = ENGINE_PORT
        cfg["local_engine_model"] = "models/qwen2.5-1.5b/qwen.gguf"
        cfg["local_engine_ready_timeout"] = 20
        cfg["http_enabled"] = True
        cfg["http_host"] = "127.0.0.1"
        cfg["http_port"] = HTTP_PORT
        cfg["agent_enabled"] = False          # 本例只验证 API 转发，不开 agent 循环
        cfg["tools"] = []

        # 模型目录里放一个占位 gguf，仅让 probe 通过
        mdir = os.path.join(root, "models", "qwen2.5-1.5b")
        os.makedirs(mdir, exist_ok=True)
        mpath = os.path.join(mdir, "qwen.gguf")
        with open(mpath, "wb") as f:
            f.write(b"\x00" * 1024)

        engine = LocalEngine(cfg)
        engine.build_cmd = lambda model_path=None: [sys.executable, script,
                                                   "--port", str(ENGINE_PORT)]
        ok_start, msg_start = engine.start()
        check("1. 本地引擎启动就绪", ok_start and engine.status()["state"] == "running",
              msg_start)

        base, model, key, source = resolve_llm(cfg, engine)
        check("2. 端点解析到本地引擎",
              source == "local-engine" and base == "http://127.0.0.1:%d/v1" % ENGINE_PORT,
              "%s → %s" % (source, base))

        bus = ToolBus(cfg)
        ctx = Context(cfg, bus, None, SessionHistory(10, 2000))
        ctx.engine = engine

        ready = threading.Event()
        t = threading.Thread(target=lambda: asyncio.run(boot_server(ctx, ready)),
                             daemon=True)
        t.start()
        # 等端口可用
        deadline = __import__("time").time() + 10
        up = False
        while __import__("time").time() < deadline:
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % HTTP_PORT,
                                       timeout=2)
                up = True
                break
            except Exception:
                __import__("time").sleep(0.3)
        check("3. 对外 API(6802) 端口可用", up)

        if up:
            code, d = http_json("GET", "http://127.0.0.1:%d/v1/models" % HTTP_PORT)
            check("4a. 外部可见模型列表", code == 200 and d.get("data"),
                  str(d)[:60])
            code, d = http_json("POST", "http://127.0.0.1:%d/v1/chat/completions" % HTTP_PORT,
                                {"model": "qwen2.5-1.5b",
                                 "messages": [{"role": "user", "content": "你是谁"}]})
            got = d.get("choices", [{}])[0].get("message", {}).get("content", "")
            check("4b. 外部对话由本地引擎作答", code == 200 and MARKER in got, got[:60])

            # 流式
            url = "http://127.0.0.1:%d/v1/chat/completions" % HTTP_PORT
            data = json.dumps({"model": "qwen2.5-1.5b", "stream": True,
                               "messages": [{"role": "user", "content": "流式测试"}]}).encode()
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8")
            # SSE 逐字符发送且中文被 JSON 转义，必须先解析事件再还原文本
            got = []
            done_seen = False
            for line in raw.splitlines():
                if line.startswith("data: "):
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        done_seen = True
                        continue
                    try:
                        got.append(json.loads(payload)["choices"][0]["delta"]
                                   .get("content", ""))
                    except Exception:
                        pass
            text = "".join(got)
            check("4c. 流式 SSE 可用", MARKER in text and done_seen, text[:50])

        # 5. 停掉引擎 → 降级而非静默失败
        engine.stop()
        b2, m2, k2, src2 = resolve_llm(cfg, engine)
        check("5. 引擎停止后端点解析切换", src2 in ("llm-config", "cloud-fallback"), src2)

        print("\n结果: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
        return 0 if all(results) else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
