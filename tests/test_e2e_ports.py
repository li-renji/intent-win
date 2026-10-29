# -*- coding: utf-8 -*-
"""真实三端口的端到端验证：起 mock 上游 + agent.main，起 6800/6801/6802，
用真实的 TCP 协议发 CHAT / BROWSER / 6801 工具调用，验证 agent 循环闭环。

用法：python test_e2e_ports.py
"""
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MOCK_PORT = 18878
STATE = {"calls": []}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        msgs = req.get("messages", [])
        stream = bool(req.get("stream"))
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        if tool_msgs:
            content = "最终答复：我已调用工具，看到「%s」" % tool_msgs[-1].get("content", "")[:30]
        else:
            content = '好的，我先看看。[[TOOL: browser {"cmd": "SNAPSHOT"}]]'

        if not stream:
            # 非流式：标准 OpenAI JSON 响应
            payload = json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}]}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        payload = ("data: " + json.dumps(
            {"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]},
            ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def line_cmd(port, cmd, wait=25):
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    s.sendall((cmd + "\n").encode("utf-8"))
    buf = b""
    s.settimeout(wait)
    try:
        while b"\n" not in buf:
            d = s.recv(4096)
            if not d:
                break
            buf += d
    except socket.timeout:
        pass
    s.close()
    return buf.decode("utf-8", "replace").strip()


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    try:
        from agent.config import load_config
        from agent.history import SessionHistory
        from agent.loop import AgentLoop
        from agent.servers import Context, serve_6800, serve_6801
        from agent.tools import ToolBus
        from agent.browser.webview2 import WebView2Adapter
        import asyncio

        # 不改配置文件，直接在内存里构造配置
        cfg = load_config()
        cfg["base_url"] = "http://127.0.0.1:%d/v1" % MOCK_PORT
        cfg["model"] = "mock-model"
        cfg["api_key"] = "sk-test"
        cfg["llm_mode"] = "cloud"   # 钉死云端模式：mock 只覆盖 base_url，避免读到本地引擎端点配置
        # 用高位端口，避免与正在运行的实例抢占 6800/6801
        cfg["port_6800"] = 16800
        cfg["port_6801"] = 16801
        cfg["http_port"] = 16802
        bus = ToolBus(cfg)
        browser = WebView2Adapter(cfg)

        # 用假浏览器适配器覆盖 execute（不依赖 pywebview / 真实窗口）
        def _fake_exec(cmd, max_chars=None):
            return "[E2E假浏览器] 执行 %s → 页面标题:测试页" % cmd
        browser.execute = _fake_exec
        browser.error = None
        browser.available = lambda: True
        bus.attach_browser(browser.execute)
        hist = SessionHistory(cfg["history_facts_n"], cfg["summary_max_chars"])

        from agent.app import _start_servers
        _start_servers(cfg, bus, browser, hist)
        time.sleep(1.5)

        P0, P1 = cfg["port_6800"], cfg["port_6801"]
        results = []

        # 1. STATUS
        r = line_cmd(P0, "STATUS")
        ok = "STATUS|" in r
        print("[%s] 6800 STATUS → %s" % ("PASS" if ok else "FAIL", r[:90]))
        results.append(ok)

        # 2. 6801 工具总线直调（原有能力不能坏）
        r = line_cmd(P1, json.dumps({"tool": "skills", "args": {}}))
        ok = '"ok": true' in r or '"ok":true' in r
        print("[%s] 6801 skills → %s" % ("PASS" if ok else "FAIL", r[:80]))
        results.append(ok)

        # 3. 6800 BROWSER 直通
        r = line_cmd(P0, "BROWSER SNAPSHOT")
        ok = "E2E假浏览器" in r
        print("[%s] 6800 BROWSER → %s" % ("PASS" if ok else "FAIL", r[:80]))
        results.append(ok)

        # 4. 6800 CHAT 走 agent 循环（核心）
        r = line_cmd(P0, "CHAT 看看页面上有什么", wait=40)
        ok = "最终答复" in r and "E2E假浏览器" in r
        print("[%s] 6800 CHAT(agent) → %s" % ("PASS" if ok else "FAIL", r[:120]))
        results.append(ok)

        # 5. agent_enabled=off 时退回原路径（原文直返，不执行工具）
        cfg["agent_enabled"] = False
        r = line_cmd(P0, "CHAT 再看看", wait=40)
        ok = "E2E假浏览器" not in r and len(r) > 0
        print("[%s] 6800 CHAT(纯对话，不执行工具) → %s" % ("PASS" if ok else "FAIL", r[:100]))
        results.append(ok)

        print("\n结果: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
        return 0 if all(results) else 1
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
        os._exit(0 if all(results) else 1)


if __name__ == "__main__":
    sys.exit(main())
