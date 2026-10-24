# -*- coding: utf-8 -*-
"""新工具（fs / code / screen）端到端测试。

覆盖：
  1. 工具注册与 schema 导出
  2. fs 各 op 直调（list/read/search/grep/info/异常输入）
  3. code 直调（python/shell/超时/异常输入）
  4. screen 直调（shot/越界/按键校验）
  5. 文本协议注入包含新工具的参数说明
  6. agent 循环：模型发 [[TOOL: fs ...]] → 工具执行 → 结果回灌 → 最终答复
  7. UI 事件流：tool_start / tool_end / done
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.config import load_config
from agent.history import SessionHistory
from agent.loop import AgentLoop
from agent.tools import ToolBus

MOCK_PORT = 18891
STATE = {"mode": "fs_tool", "calls": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _sse(self, chunks):
        body = b""
        for c in chunks:
            body += ("data: " + json.dumps(c, ensure_ascii=False) + "\n\n").encode("utf-8")
        body += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        msgs = req.get("messages", [])
        STATE["calls"] += 1
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        mode = STATE["mode"]

        # 已回灌工具结果 → 给出最终答复（必须引用工具结果）
        # multi_tool 模式例外：它要模拟"拿到结果后再决定调第二个工具"
        if tool_msgs and mode != "multi_tool":
            got = tool_msgs[-1].get("content", "")
            self._sse([{"choices": [{"delta": {"content":
                "查到了：%s" % got[:60]}, "finish_reason": "stop"}]}])
            return

        if mode == "fs_tool":
            self._sse([{"choices": [{"delta": {"content":
                '我来看看桌面。[[TOOL: fs {"op": "list", "path": "~/Desktop"}]]'},
                "finish_reason": "stop"}]}])
        elif mode == "code_tool":
            self._sse([{"choices": [{"delta": {"content":
                '[[TOOL: code {"lang": "python", "code": "print(sum(range(101)))"}]]'},
                "finish_reason": "stop"}]}])
        elif mode == "screen_tool":
            self._sse([{"choices": [{"delta": {"content":
                '[[TOOL: screen {"op": "shot"}]]'}, "finish_reason": "stop"}]}])
        elif mode == "multi_tool":
            # 连续两轮：先 fs，再 code；第三轮才给最终答复
            if STATE["calls"] <= 1:
                self._sse([{"choices": [{"delta": {"content":
                    '[[TOOL: fs {"op": "info", "path": "README.md"}]]'},
                    "finish_reason": "stop"}]}])
            elif STATE["calls"] == 2:
                self._sse([{"choices": [{"delta": {"content":
                    '[[TOOL: code {"lang": "shell", "code": "echo ok"}]]'},
                    "finish_reason": "stop"}]}])
            else:
                got = tool_msgs[-1].get("content", "") if tool_msgs else ""
                self._sse([{"choices": [{"delta": {"content":
                    "两步都完成了，最后结果：%s" % got[:50]},
                    "finish_reason": "stop"}]}])
        elif mode == "bad_tool":
            # 模型发明了一个不存在的工具
            self._sse([{"choices": [{"delta": {"content":
                '[[TOOL: fly {"where": "moon"}]]'}, "finish_reason": "stop"}]}])
        else:
            self._sse([{"choices": [{"delta": {"content": "纯文本答复"},
                         "finish_reason": "stop"}]}])


def make_cfg(**over):
    cfg = load_config()
    cfg["base_url"] = "http://127.0.0.1:%d/v1" % MOCK_PORT
    cfg["model"] = "test-model"
    cfg["api_key"] = "test"
    cfg["agent_tool_mode"] = "text"
    cfg["agent_max_rounds"] = 6
    cfg["agent_max_seconds"] = 60
    cfg["agent_max_tool_result"] = 1500
    cfg.update(over)
    return cfg


def make_bus(cfg):
    bus = ToolBus(cfg)
    # 注入假浏览器，避免依赖 pywebview
    bus.attach_browser(lambda cmd: "[假浏览器] %s" % cmd)
    return bus


def run_agent(mode, user_text="桌面上有些什么文件", cfg=None):
    """跑一轮完整 agent，返回 (final, tool_names, events)。"""
    STATE["mode"] = mode
    STATE["calls"] = 0
    cfg = cfg or make_cfg()
    bus = make_bus(cfg)
    hist = SessionHistory(cfg["history_facts_n"], cfg["summary_max_chars"])
    loop = AgentLoop(cfg, bus, None, hist)
    msgs = [{"role": "system", "content": "你是奇点Agent"},
            {"role": "user", "content": user_text}]
    final, tools, events = "", [], []
    for kind, payload in loop.run(cfg["base_url"], cfg["model"], cfg["api_key"], msgs):
        events.append((kind, payload))
        if kind == "done":
            final = payload.get("content", "")
        elif kind == "tool_end":
            tools.append(payload.get("name"))
        elif kind == "error":
            final = "[错误] %s" % payload
    return final, tools, events


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    results = []
    try:
        # ---- 1. 注册与 schema ----
        cfg = make_cfg()
        bus = make_bus(cfg)
        names = bus.enabled()
        ok = all(n in names for n in ("fs", "code", "screen"))
        print("[%s] 新工具已注册: %s" % ("PASS" if ok else "FAIL", names))
        results.append(ok)

        # ---- 2. fs 直调 ----
        ok1 = "目录:" in bus.invoke("fs", {"op": "list", "path": "~/Desktop"})[1]
        ok2 = "README" in bus.invoke("fs", {"op": "read", "path": "README.md"})[1]
        ok3 = "loop.py" in bus.invoke("fs", {"op": "search", "path": ".", "pattern": "loop"})[1]
        ok4 = "未知 op" in bus.invoke("fs", {"op": "delete"})[1]
        ok5 = "路径不存在" in bus.invoke("fs", {"op": "list", "path": "Z:/nope"})[1]
        ok = all([ok1, ok2, ok3, ok4, ok5])
        print("[%s] fs 直调（list/read/search/未知op/不存在路径）" % ("PASS" if ok else "FAIL"))
        if not ok:
            print("   %s %s %s %s %s" % (ok1, ok2, ok3, ok4, ok5))
        results.append(ok)

        # ---- 3. code 直调 ----
        r = bus.invoke("code", {"lang": "python", "code": "print(sum(range(101)))"})[1]
        ok1 = "5050" in r
        r2 = bus.invoke("code", {"lang": "shell", "code": "echo hi"})[1]
        ok2 = "hi" in r2
        cfg2 = make_cfg(code_timeout=3)
        bus2 = make_bus(cfg2)
        r3 = bus2.invoke("code", {"lang": "python",
                                  "code": "import time; time.sleep(20)"})[1]
        ok3 = "超时" in r3
        ok4 = "缺少 code" in bus.invoke("code", {})[1]
        ok = all([ok1, ok2, ok3, ok4])
        print("[%s] code 直调（python/shell/超时保护/缺参）" % ("PASS" if ok else "FAIL"))
        if not ok:
            print("   %s %s %s %s" % (ok1, ok2, ok3, ok4))
        results.append(ok)

        # ---- 4. screen 直调 ----
        r = bus.invoke("screen", {"op": "shot"})[1]
        ok1 = "截图已保存" in r and "屏幕分辨率" in r
        ok2 = "越界" in bus.invoke("screen", {"op": "click", "x": 99999, "y": 99999})[1]
        ok3 = "无法识别" in bus.invoke("screen", {"op": "key", "keys": "ctrl+zzz"})[1]
        ok4 = "未知 op" in bus.invoke("screen", {"op": "fly"})[1]
        ok = all([ok1, ok2, ok3, ok4])
        print("[%s] screen 直调（shot/越界/按键校验/未知op）" % ("PASS" if ok else "FAIL"))
        if not ok:
            print("   %s %s %s %s" % (ok1, ok2, ok3, ok4))
        results.append(ok)

        # ---- 5. 文本协议注入含新工具参数 ----
        loop = AgentLoop(cfg, bus, None, None)
        injected = loop._inject_text_protocol(
            [{"role": "system", "content": "基础"}])
        sysmsg = injected[0]["content"]
        ok = all(k in sysmsg for k in ("fs", "code", "screen", "op", "[[TOOL:"))
        print("[%s] 文本协议说明含全部新工具参数" % ("PASS" if ok else "FAIL"))
        results.append(ok)

        # ---- 6. agent 循环：fs ----
        final, tools, evs = run_agent("fs_tool")
        ok = ("fs" in tools) and ("查到了" in final) and ("Desktop" in final)
        print("[%s] agent 循环 fs：工具=%s" % ("PASS" if ok else "FAIL", tools))
        print("    答复: %s" % final[:80])
        results.append(ok)

        # ---- 7. agent 循环：code ----
        final, tools, evs = run_agent("code_tool", user_text="1加到100等于多少")
        ok = ("code" in tools) and "5050" in final
        print("[%s] agent 循环 code：工具=%s" % ("PASS" if ok else "FAIL", tools))
        print("    答复: %s" % final[:80])
        results.append(ok)

        # ---- 8. agent 循环：screen ----
        final, tools, evs = run_agent("screen_tool", user_text="现在屏幕上是什么")
        ok = ("screen" in tools) and "截图" in final
        print("[%s] agent 循环 screen：工具=%s" % ("PASS" if ok else "FAIL", tools))
        print("    答复: %s" % final[:80])
        results.append(ok)

        # ---- 9. 连续多工具 ----
        final, tools, evs = run_agent("multi_tool", user_text="看看README多大再跑个命令")
        ok = ("fs" in tools) and ("code" in tools)
        print("[%s] 连续多工具：工具=%s" % ("PASS" if ok else "FAIL", tools))
        results.append(ok)

        # ---- 10. 模型发明不存在的工具 → 应报错而非崩溃 ----
        final, tools, evs = run_agent("bad_tool", user_text="飞到月球")
        kinds = [k for k, _ in evs]
        ok = ("tool_start" in kinds) and ("error" not in kinds)
        print("[%s] 未知工具名不崩溃（返回错误结果给模型）" % ("PASS" if ok else "FAIL"))
        print("    工具=%s 答复=%s" % (tools, final[:60]))
        results.append(ok)

        # ---- 11. UI 事件流 ----
        final, tools, evs = run_agent("fs_tool")
        kinds = [k for k, _ in evs]
        ok = ("tool_start" in kinds) and ("tool_end" in kinds) and ("done" in kinds)
        print("[%s] UI 事件流完整（start/end/done）" % ("PASS" if ok else "FAIL"))
        results.append(ok)

    finally:
        srv.shutdown()

    print("\n结果: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
