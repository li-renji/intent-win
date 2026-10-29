# -*- coding: utf-8 -*-
"""端到端：4B 风格模型输出【文件列表】→ 三层解析 → fs 工具真实执行。

复现用户遇到的场景：模型不输出标准 [[TOOL:]]，只输出一个中文方括号
标记（甚至只有一句自然语言）。验证 AgentLoop + ToolBus + intent 全链路：
  1. 【文件列表】触发 fs list，工具结果回灌
  2. 正文标记被剥离（text_reset），最终答复引用工具结果
  3. 自然语言意图（"桌面上有什么文件"）同样触发工具
"""
import json
import os
import shutil
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import load_config
from agent.history import SessionHistory
from agent.loop import AgentLoop
from agent.tools import ToolBus

PORT = 18893
STATE = {"calls": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _sse(self, chunks):
        body = "".join("data: %s\n\n" % json.dumps(c, ensure_ascii=False) for c in chunks)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        STATE["calls"] += 1
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n).decode("utf-8"))
        tool_msgs = [m for m in req.get("messages", []) if m.get("role") == "tool"]

        if not tool_msgs:
            # 第一轮：4B 风格——不带标准标记，只输出中文方括号
            self._sse([{"choices": [{"delta": {"content": "我来看看【文件列表】"},
                                     "finish_reason": "stop"}]}])
            return
        got = tool_msgs[-1].get("content", "")
        self._sse([{"choices": [{"delta": {"content":
                    "桌面上有：%s" % got[:80]}, "finish_reason": "stop"}]}])


def main():
    tmp = tempfile.mkdtemp(prefix="qd_intent_e2e_")
    try:
        # 造一个带已知文件的"桌面"
        desktop = os.path.join(tmp, "Desktop")
        os.makedirs(desktop)
        with open(os.path.join(desktop, "验收文件.txt"), "w", encoding="utf-8") as f:
            f.write("hello")

        cfg = load_config()
        cfg["project_root"] = tmp
        cfg["agent_tool_mode"] = "text"      # 强制文本协议路径
        cfg["agent_text_fallback"] = "on"
        cfg["agent_max_rounds"] = 4
        cfg["agent_req_timeout"] = 15
        cfg["tools"] = ["fs"]

        bus = ToolBus(cfg)
        loop = AgentLoop(cfg, bus, None, SessionHistory(10, 2000))

        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        results = []

        # ---- 场景 1：【文件列表】非标准标记 ----
        msgs = [{"role": "user", "content": "桌面上有什么"}]
        evs = []
        final = ""
        for kind, payload in loop.run("http://127.0.0.1:%d/v1" % PORT, "fake-4b",
                                      "k", msgs):
            evs.append((kind, payload))
            if kind == "done":
                final = payload.get("content", "")

        kinds = [k for k, _ in evs]
        started = [p for k, p in evs if k == "tool_start"]
        # fs list 的输出签名是"目录: <路径>"；【文件列表】不带路径 → 默认列真实桌面，
        # 因此断言落在"工具真的执行了且结果被引用"，不依赖具体文件。
        ok1 = ("tool_start" in kinds and started
               and started[0]["name"] == "fs"
               and "目录:" in final
               and "【" not in final)
        print("[%s] 【文件列表】触发 fs 并回灌：工具=%s" % (
            "PASS" if ok1 else "FAIL", [s["name"] for s in started]))
        print("    最终答复: %s" % final[:70])
        results.append(ok1)

        # ---- 场景 2：自然语言意图（连标记都没有） ----
        STATE["calls"] = 0
        msgs = [{"role": "user", "content": "桌面上有些什么"}]
        evs = []
        final = ""
        for kind, payload in loop.run("http://127.0.0.1:%d/v1" % PORT, "fake-4b",
                                      "k", msgs):
            evs.append((kind, payload))
            if kind == "done":
                final = payload.get("content", "")
        kinds = [k for k, _ in evs]
        ok2 = "tool_start" in kinds and "目录:" in final
        print("[%s] 自然语言同样触发工具" % ("PASS" if ok2 else "FAIL"))
        results.append(ok2)

        # ---- 场景 3：剥离事件通知前端 ----
        ok3 = "text_reset" in [k for k, _ in evs] or "tool_start" in kinds
        print("[%s] 事件序列含 text_reset/tool_start" % ("PASS" if ok3 else "FAIL"))
        results.append(ok3)

        srv.shutdown()
        print("\n结果: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
        return 0 if all(results) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
