# -*- coding: utf-8 -*-
"""验证桌面 UI 路径（app.py::_chat_stream）能跑通 agent 循环。

不启动真实窗口：直接构造 Api，替换 _push 为收集器，验证：
  1. 工具调用过程被推送（__onToolStart / __onToolEnd）
  2. 最终答复被推送（__onToken 累积 + __onDone）
  3. 会话被持久化，facts 里记录了工具调用痕迹

用法：python test_app_path.py
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MOCK_PORT = 18879
STATE = {"reject_tools": False}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        msgs = req.get("messages", [])
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        if STATE["reject_tools"] and req.get("tools"):
            # 上游拒绝 tools → 触发降级
            self.send_response(400)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if tool_msgs:
            content = "我查到了：「%s」" % tool_msgs[-1].get("content", "")[:30]
        else:
            content = '我先看看页面。[[TOOL: browser {"cmd": "SNAPSHOT"}]]'
        payload = ("data: " + json.dumps(
            {"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]},
            ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    from agent.app import Api
    from agent.browser.webview2 import WebView2Adapter
    from agent.config import load_config
    from agent.history import SessionHistory
    from agent.tools import ToolBus

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config()
    cfg["base_url"] = "http://127.0.0.1:%d/v1" % MOCK_PORT
    cfg["model"] = "mock-model"
    cfg["api_key"] = "sk-test"
    cfg["llm_mode"] = "cloud"   # 钉死云端模式：mock 只覆盖 base_url，避免读到本地引擎端点配置

    bus = ToolBus(cfg)
    browser = WebView2Adapter(cfg)
    browser.execute = lambda cmd, max_chars=None: "[假浏览器] %s → 标题:测试页" % cmd
    browser.available = lambda: True
    bus.attach_browser(browser.execute)
    hist = SessionHistory(cfg["history_facts_n"], cfg["summary_max_chars"])

    api = Api(cfg, bus, browser, hist)
    events = []
    api._push = lambda js, *args: events.append((js, args))

    conv = api.new_conversation()
    cid = conv["id"]

    # 直接跑流式聊天（同步等待，_chat_stream 是线程内跑完的）
    api._chat_stream("看看页面上有什么")
    time.sleep(0.5)

    kinds = [e[0] for e in events]
    results = []

    ok = any("__onToolStart" in k for k in kinds)
    print("[%s] 工具开始事件已推送" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    ok = any("__onToolEnd" in k for k in kinds)
    print("[%s] 工具结束事件已推送" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    ok = any("__onToken" in k for k in kinds)
    print("[%s] 正文 token 已推送" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    ok = any("__onDone" in k for k in kinds)
    print("[%s] 完成事件已推送" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    # 关键回归：notice 不能走 __onError（那会清空气泡，后续 token 全丢）
    ok = not any("__onError" in k for k in kinds)
    print("[%s] 未误用 __onError（气泡不会被销毁）" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    # 校验工具事件内容
    for js, args in events:
        if "__onToolEnd" in js:
            p = args[0]
            print("   工具: %s ok=%s 结果=%s" % (p.get("name"), p.get("ok"),
                                                str(p.get("result"))[:50]))
            if p.get("name") != "browser" or not p.get("ok"):
                results.append(False)

    # 会话持久化
    d = api._load(cid)
    ok = d is not None and len(d.get("messages", [])) == 2
    print("[%s] 会话已持久化（%d 条消息）" % ("PASS" if ok else "FAIL",
                                            len(d.get("messages", [])) if d else 0))
    results.append(ok)

    # facts 记录了工具调用痕迹
    ok = d is not None and any("已调用:browser" in f for f in d.get("facts", []))
    print("[%s] facts 记录了工具调用痕迹" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    # ---------- 降级场景：上游 400 拒绝 tools ----------
    print("\n--- 降级场景（上游 400 → 文本协议）---")
    STATE["reject_tools"] = True
    cfg["agent_tool_mode"] = "auto"
    api2 = Api(cfg, bus, browser, hist)
    ev2 = []
    api2._push = lambda js, *args: ev2.append((js, args))
    c2 = api2.new_conversation()
    api2._chat_stream("看看页面上有什么")
    time.sleep(0.5)
    k2 = [e[0] for e in ev2]

    ok = any("__onNotice" in k for k in k2)
    print("[%s] 降级提示走 __onNotice（非致命）" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    ok = not any("__onError" in k for k in k2)
    print("[%s] 降级时未触发 __onError（气泡保留）" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    # 关键：降级后仍应有 token 与 done —— 这正是截图里缺失的部分
    ok = any("__onToken" in k for k in k2)
    print("[%s] 降级后仍收到正文 token" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    ok = any("__onDone" in k for k in k2)
    print("[%s] 降级后仍收到完成事件" % ("PASS" if ok else "FAIL"))
    results.append(ok)

    try:
        os.remove(api2._conv_path(c2["id"]))
    except OSError:
        pass

    # 清理测试会话文件
    try:
        os.remove(api._conv_path(cid))
    except OSError:
        pass

    print("\n结果: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
    try:
        srv.shutdown()
    except Exception:
        pass
    os._exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
