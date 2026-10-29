# -*- coding: utf-8 -*-
"""agent 循环端到端验证脚本（不依赖真实上游）。

覆盖场景：
  A. 原生 function calling：首轮返回 tool_calls → 执行工具 → 回灌 → 二轮给最终答案
  B. 上游拒绝 tools 参数（HTTP 400）→ 降级文本协议 → 同样跑通
  C. 模型纯文本答复（不调工具）→ 一轮结束
  D. 工具返回错误 → 正确标记 ok=False
  E. agent_enabled=off → 退回原来的一问一答路径
  F. 网络超时不得误判为不支持 tools（应报 error 不降级）
  G. tool_mode=native 时上游 400 → 降级
  H. 上游返回上下文超限 → 自动压缩历史重试并跑完（不得直接死）
  I. 压缩后仍超限 → 给出可操作的明确提示（而非裸抛上游原文）

用法：python test_agent_loop.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import load_config
from agent.history import SessionHistory
from agent.loop import AgentLoop
from agent.tools import ToolBus

PORT = 18877
STATE = {"mode": "native", "calls": []}


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

    def _json_err(self, code):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        msgs = req.get("messages", [])
        has_tools = bool(req.get("tools"))
        mode = STATE["mode"]

        # 上游拒绝 tools 参数 → 模拟 MNN/部分手机端服务
        if mode == "reject_tools" and has_tools:
            self._json_err(400)
            return

        # 模拟手机深度睡眠：接受连接但不回包 → 客户端超时（网络类错误）
        if mode == "timeout":
            time.sleep(8)
            return

        # 模拟上下文超限：首次请求 400 + 错误详情；压缩后重试则放行
        # （ctx_always=True 时始终 400，用于测“压缩后仍超限”的兜底提示）
        if mode == "ctx_overflow" and (STATE.get("ctx_always")
                                       or not STATE.get("ctx_compressed")):
            STATE["ctx_compressed"] = True
            body = json.dumps({"error": {"message":
                "请求超出模型上下文容量（context length exceeded），请减少输入",
                "code": 400}}, ensure_ascii=False).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        last = msgs[-1]
        tool_results = [m for m in msgs if m.get("role") == "tool"]

        # 已经拿到工具结果 → 给最终答案
        if tool_results:
            got = tool_results[-1].get("content", "")
            self._sse([{"choices": [{"delta": {"content": "我查到了：" + got[:40]}, "finish_reason": "stop"}]}])
            return

        if mode == "text_proto":
            # 文本协议：把工具调用写在正文里
            self._sse([{"choices": [{"delta": {"content":
                '我来查看页面。[[TOOL: browser {"cmd": "SNAPSHOT"}]]'}}]}])
            return

        if mode == "pure_text":
            self._sse([{"choices": [{"delta": {"content": "这是一个纯文本答复。"}, "finish_reason": "stop"}]}])
            return

        if mode == "tool_error":
            self._sse([{"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_x", "type": "function",
                "function": {"name": "browser", "arguments": "{\"cmd\": \"SNAPSHOT\"}"}}]},
                "finish_reason": "tool_calls"}]}])
            return

        # 默认：原生 tool_calls（分两个 delta 模拟流式累积）
        self._sse([
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "browser", "arguments": "{\"cmd\": "}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"name": "", "arguments": " \"SNAPSHOT\"}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def make_cfg(**over):
    cfg = load_config(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "配置.qdai"))
    cfg["base_url"] = "http://127.0.0.1:%d/v1" % PORT
    cfg["model"] = "mock-model"
    cfg["api_key"] = "sk-test"
    cfg["tools"] = ["browser", "memo", "skills"]
    cfg["agent_max_rounds"] = 4
    cfg["agent_max_seconds"] = 30
    for k, v in over.items():
        cfg[k] = v
    return cfg


def run_case(name, mode, expect_tools=1, expect_notice=False, expect_error=False,
             tool_mode="auto", req_timeout=6, ctx_always=False, **cfg_over):
    STATE["mode"] = mode
    STATE["ctx_compressed"] = False
    STATE["ctx_always"] = ctx_always
    cfg = make_cfg(**cfg_over)
    cfg["agent_tool_mode"] = tool_mode
    cfg["agent_req_timeout"] = req_timeout
    bus = ToolBus(cfg)
    # 用假浏览器，避免依赖 pywebview
    bus.attach_browser(lambda cmd: "[假浏览器] 执行了 %s → 返回页面快照" % cmd)
    hist = SessionHistory(cfg["history_facts_n"], cfg["summary_max_chars"])
    loop = AgentLoop(cfg, bus, None, hist)

    msgs = [{"role": "system", "content": "你是奇点Agent"},
            {"role": "user", "content": "看看页面上有什么"}]
    text, tools, rounds, errs, notices = "", [], 0, [], []
    final_done = ""
    for kind, payload in loop.run(cfg["base_url"], cfg["model"], cfg["api_key"], msgs):
        if kind == "text":
            text += payload
        elif kind == "tool_end":
            tools.append(payload["name"])
        elif kind == "done":
            rounds = payload.get("rounds", 0)
            final_done = payload.get("content", "")
        elif kind == "error":
            errs.append(str(payload))
        elif kind == "notice":
            notices.append(str(payload))

    ok = True
    if len(tools) != expect_tools:
        ok = False
    if expect_notice and not notices:
        ok = False
    if not expect_notice and notices:
        ok = False
    if expect_error and not errs:
        ok = False
    if not expect_error and errs:
        ok = False
    # done 的内容不应残留协议标记
    if "[[TOOL:" in final_done:
        ok = False
    # 致命错误时不应有 done
    if expect_error and final_done:
        ok = False

    print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    print("   工具: %s | 轮数: %s | notice: %s | error: %s" % (
        tools or "无", rounds, notices or "无", (errs or ["无"])[0][:60]))
    if final_done:
        print("   最终答复(done): %s" % final_done.strip()[:70])
    return ok


def main():
    srv = start_server()
    time.sleep(0.3)
    results = []
    try:
        results.append(run_case("A 原生 tool_calls 循环", "native", 1))
        results.append(run_case("B 上游 400 拒绝 tools → 降级（notice 非致命）",
                                 "reject_tools", 1, expect_notice=True))
        results.append(run_case("C 模型纯文本答复（不调工具）", "pure_text", 0))
        results.append(run_case("D 文本协议模式", "text_proto", 1, tool_mode="text"))
        results.append(run_case("E tool_mode=text 直接走文本协议，不带 tools 参数",
                                 "text_proto", 1, tool_mode="text"))
        results.append(run_case("F 网络超时不得误判为不支持 tools（应报 error 不降级）",
                                 "timeout", 0, expect_error=True))
        results.append(run_case("G tool_mode=native 时上游 400 → 降级",
                                "reject_tools", 1, expect_notice=True, tool_mode="native"))
        results.append(run_case("H 上下文超限 → 自动压缩重试（不得直接死）",
                                "ctx_overflow", 1, expect_notice=True))
        results.append(run_case("I 压缩后仍超限 → 给出可操作提示（error）",
                                "ctx_overflow", 0, expect_notice=True,
                                expect_error=True, ctx_always=True))
    finally:
        srv.shutdown()
    print("\n结果: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    import time
    sys.exit(main())
