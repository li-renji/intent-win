# -*- coding: utf-8 -*-
"""三端口服务（语义钉死，对齐 ai_service §4.4.1）：

  6800 裸文本行   人/程序对话入口（STATUS/HELLO/ECHO/HELP/SKILLS/CHAT/BROWSER）
  6801 JSON 行    内部工具转发（消息总线起点：唯一工具执行通道）
  6802 HTTP       对外 OpenAI 兼容 API（/v1/models、/v1/chat/completions SSE）
"""
import asyncio
import json
import os
import time
import urllib.parse

from . import llm
from .local_engine import LocalEngine, resolve_llm
from .loop import AgentLoop
from .prompts import build_system_prompt

_LOOPBACK = ("127.0.0.1", "::1")


class Context:
    """装配好的运行时（config + ToolBus + 浏览器 + 会话历史）。"""

    def __init__(self, cfg, bus, browser, history):
        self.cfg = cfg
        self.bus = bus
        self.browser = browser
        self.history = history
        self.agent = AgentLoop(cfg, bus, browser, history)
        self.engine = LocalEngine(cfg)

    def endpoint(self):
        """解析本次请求实际端点：本地引擎优先，不可用时按策略降级。"""
        base, model, key, source = resolve_llm(self.cfg, self.engine)
        self._last_source = source
        return base, model, key


# ---------- 6800 ----------

async def serve_6800(ctx):
    host, port = ctx.cfg["host"], ctx.cfg["port_6800"]

    async def handle(reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode("utf-8", "replace").strip()
                if not cmd:
                    continue
                reply = await _dispatch_6800(ctx, cmd)
                writer.write((reply + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    print("[6800] 对话入口就绪 %s:%s" % (host, port))
    return server


async def _dispatch_6800(ctx, cmd):
    head, _, rest = cmd.partition(" ")
    head = head.upper()
    if head == "STATUS":
        eng = ctx.cfg["llm_mode"]
        ok = bool(ctx.endpoint()[0])
        return "STATUS|引擎=%s|端点=%s|浏览器=%s|工具=%s" % (
            eng, "已配置" if ok else "未配置",
            "就绪" if ctx.browser.available() else ("未启用" if ctx.browser.error else "未启动"),
            ",".join(ctx.bus.enabled()) or "无")
    if head == "HELLO":
        return "你好，这里是 intent（Windows Agent）。能力摘要：%s" % ", ".join(ctx.bus.enabled())
    if head == "ECHO":
        return rest
    if head == "HELP":
        return "STATUS|HELLO|ECHO <文本>|HELP|SKILLS LIST|CHAT <消息>|BROWSER <指令>|QUIT"
    if head == "SKILLS":
        if rest.strip().upper() == "LIST":
            return ctx.bus.dispatch(json.dumps({"tool": "skills", "args": {}}))
        return "用法: SKILLS LIST"
    if head == "BROWSER":
        if "browser" not in ctx.bus.enabled():
            return "浏览器工具未启用（配置 tools 未含 browser）"
        return await asyncio.to_thread(ctx.browser.execute, rest)
    if head == "CHAT":
        if not rest:
            return "用法: CHAT <消息>"
        return await chat_once(ctx, rest)
    return "未知指令（HELP 查看列表）"


async def chat_once(ctx, user_text, scenario="chat"):
    """一次完整对话：最小 system 片段 + 事实摘要注入 + LLM 调用 + 轮末压缩。

    agent_enabled=on 时走 AgentLoop：模型可自主调用工具，多轮直到给出最终答复。
    """
    base, model, key = ctx.endpoint()
    if not base:
        return "LLM 未配置（检查 配置.qdai 的 base_url / api_key）"
    system = build_system_prompt(
        ctx.bus.enabled(), scenario=scenario,
        skills_dir=os.path.join(ctx.cfg["project_root"], "skills"))
    facts = ctx.history.inject()
    messages = [{"role": "system", "content": (system + ("\n" + facts if facts else ""))},
                {"role": "user", "content": user_text}]

    if not ctx.cfg.get("agent_enabled", True):
        ok, out = await asyncio.to_thread(llm.chat, base, model, key, messages, False)
        if not ok:
            return "[错误] " + out
        ctx.history.add_turn(user_text, out)   # TurnFact：只留摘要，不留全量
        return out

    # agent 模式：多轮工具调用循环
    final = ""
    tool_names = []
    loop = ctx.agent

    def _run():
        nonlocal final, tool_names
        for kind, payload in loop.run(base, model, key, messages):
            if kind == "done":
                final = payload.get("content", "")
                tool_names = payload.get("tools", [])
            elif kind == "error":
                # payload 可能是 dict（含 kind/msg）或纯字符串
                if isinstance(payload, dict):
                    final = "[错误] " + str(payload.get("msg", payload))
                else:
                    final = "[错误] " + str(payload)

    await asyncio.to_thread(_run)
    ctx.history.add_turn(user_text, final, tool_names)
    return final


# ---------- 6801 ----------

async def serve_6801(ctx):
    host, port = ctx.cfg["host"], ctx.cfg["port_6801"]

    async def handle(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                out = await asyncio.to_thread(ctx.bus.dispatch, line.decode("utf-8", "replace"))
                writer.write((out + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    print("[6801] 内部工具总线就绪 %s:%s（总线起点）" % (host, port))
    return server


# ---------- 6802 ----------

async def serve_6802(ctx):
    host = ctx.cfg["http_host"]
    port = ctx.cfg["http_port"]
    if not ctx.cfg["http_enabled"]:
        print("[6802] HTTP API 已关闭（http_enabled=off）")
        return None
    external = host not in _LOOPBACK

    async def handle(reader, writer):
        try:
            req_line = await reader.readline()
            if not req_line:
                return
            method, path, _ = req_line.decode("latin1").split(" ", 2)
            headers = {}
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin1").partition(":")
                headers[k.strip().lower()] = v.strip()
            body = b""
            n = int(headers.get("content-length", 0) or 0)
            if n:
                body = await reader.readexactly(n)

            # 对外监听强制 Bearer 鉴权（对齐 §4.4.3）
            if external:
                auth = headers.get("authorization", "")
                if auth != "Bearer " + ctx.cfg.get("api_key", "\x00"):
                    await _http(writer, 401, {"error": "unauthorized"})
                    return

            parsed = urllib.parse.urlparse(path)
            if method == "GET" and parsed.path == "/v1/models":
                _, model, _ = ctx.endpoint()
                await _http(writer, 200, {"object": "list", "data": [
                    {"id": model, "object": "model", "owned_by": "qd"}]}, method="GET")
            elif method == "POST" and parsed.path == "/v1/chat/completions":
                await _chat_completions(ctx, writer, body)
            else:
                await _http(writer, 404, {"error": "not found"})
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    print("[6802] OpenAI兼容API就绪 %s:%s%s" % (host, port, "（对外：强制Bearer）" if external else ""))
    return server


async def _chat_completions(ctx, writer, body):
    try:
        req = json.loads(body.decode("utf-8"))
        messages = req.get("messages")
        if not req.get("model") or not messages:
            return await _http(writer, 400, {"error": "缺 model 或 messages"})
    except Exception:
        return await _http(writer, 400, {"error": "请求体非法"})

    base, model, key = ctx.endpoint()
    if not base:
        return await _http(writer, 502, {"error": "上游未配置"})

    stream = bool(req.get("stream"))
    # 对外 API 透传给模型；内部工具调用不经 6802（那是 6801 的职责）
    ok, out = await asyncio.to_thread(llm.chat, base, req.get("model", model), key, messages, stream)
    if not ok:
        code = 504 if "timed out" in str(out).lower() else 502
        return await _http(writer, code, {"error": out})

    cid = "chatcmpl-qd-%d" % int(time.time())
    if not stream:
        return await _http(writer, 200, {
            "id": cid, "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": out},
                         "finish_reason": "stop"}]})

    # SSE 流式
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                 b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
    try:
        for chunk in out:
            data = json.dumps({"id": cid, "object": "chat.completion.chunk",
                               "choices": [{"index": 0, "delta": {"content": chunk}}]},
                              ensure_ascii=False)
            writer.write(("data: %s\n\n" % data).encode("utf-8"))
            await writer.drain()
        writer.write(b"data: [DONE]\n\n")
        await writer.drain()
    except ConnectionError:
        pass


async def _http(writer, code, obj, method="POST"):
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
              404: "Not Found", 502: "Bad Gateway", 504: "Gateway Timeout"}[code]
    head = ("HTTP/1.1 %d %s\r\nContent-Type: application/json\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n") % (code, reason, len(payload))
    writer.write(head.encode("latin1") + payload)
    await writer.drain()
