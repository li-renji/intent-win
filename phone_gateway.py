"""
奇点OS 手机网关 —— 让 PC 端的 WorkBuddy 稳定调用手机上的本地 AI。

为什么需要它（两层问题）：
  1. 路径问题：WorkBuddy 自定义模型的 URL 拼接行为有 "URL 直通" 和 "自动补 /chat/completions"
     两种模式，直通模式下会把请求打到裸地址上，手机返回 404。
     本网关接受任意路径，统一重写为 /v1/chat/completions，WorkBuddy 怎么拼都能命中。
  2. 冻结问题：vivo/OriginOS 会让手机进入深度睡眠，此时 TCP 能连上但服务不回包（请求超时）。
     本网关对超时做自动重试 + 退避，扛过短暂的冻结窗口。

用法：
  python phone_gateway.py [--port 9997] [--phone <手机IP>:9999] [--retries 3]
  WorkBuddy 自定义模型配置：
      url  = http://127.0.0.1:9997/v1
      apiKey = <手机 App 的 key>（网关会替换成手机真实 key）
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PHONE_HOST = "<手机IP>"
PHONE_PORT = 9999
PHONE_KEY = "<你的API Key>"
LISTEN_PORT = 9997
RETRIES = 3
RETRY_BACKOFF = 2.0
PHONE_TIMEOUT = 25.0  # 单次请求超时：手机正常响应 0.05~3s，25s 足够判死并进入重试
# 手机地址候选（DHCP 会变，逐个探测挑活的）
PHONE_CANDIDATES = ["<手机IP>:9999", "<备用手机IP>:9999"]
_active_phone = {"host": PHONE_HOST, "port": PHONE_PORT, "ts": 0.0}
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway.log")

# ---- prompt 裁剪参数 ----
# 手机端 MNN 引擎实测：prompt ≤2496 字符正常，约 4976 字符时原生崩溃（App 闪退）；
# 且 prefill 耗时与长度线性相关（约 4.4ms/字符）。所以这里在 PC 侧先把 prompt 压到安全区。
PROMPT_BUDGET = 1400          # 送进手机的总字符预算
SYSTEM_TEXT = "你是运行在手机上的本地 AI 助手，用中文简洁回答。"  # 替换 Agent 那套几千字的系统提示词
KEEP_ORIGINAL_SYSTEM = False  # True = 保留原 system（截断到 400 字符）


def trim_body(body):
    """把请求体裁剪到手机能安全处理的规模。返回 (裁剪后body, 原始字符数, 裁剪后字符数)。"""
    if not body:
        return body, 0, 0
    try:
        data = json.loads(body)
    except Exception:
        return body, len(body), len(body)

    # 简单 /chat 格式：{"prompt": "..."}
    if isinstance(data, dict) and isinstance(data.get("prompt"), str):
        raw = data["prompt"]
        data["prompt"] = raw[:PROMPT_BUDGET]
        return json.dumps(data, ensure_ascii=False), len(raw), len(data["prompt"])

    # OpenAI 兼容格式：{"messages": [...]}
    msgs = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(msgs, list) or not msgs:
        return body, len(body), len(body)

    raw_len = sum(len(m.get("content") or "") for m in msgs if isinstance(m, dict))
    has_tools = bool(data.get("tools"))

    # 工具调用模式：必须保留 tools 定义 + 上一步的工具调用与执行结果，
    # 否则模型看不到「观察」，agent 循环就断了。
    if has_tools:
        out = [{"role": "system", "content": SYSTEM_TEXT}]
        used = len(SYSTEM_TEXT)
        # 上一步 assistant 的 tool_calls（还原成模型看得懂的 DSL）
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("tool_calls"):
                fn = (m["tool_calls"][0] or {}).get("function") or {}
                fname = fn.get("name", "")
                fargs = fn.get("arguments", "")
                arg_text = ""
                try:
                    ao = json.loads(fargs) if isinstance(fargs, str) else {}
                    if isinstance(ao, dict) and ao:
                        k = list(ao.keys())[0]
                        arg_text = "%s=%s" % (k, ao[k])
                except Exception:
                    pass
                line = "【工具】%s|%s" % (fname, arg_text)
                out.append({"role": "assistant", "content": line})
                used += len(line)
                break
        # 工具执行结果（role=tool）—— 最重要，必须留
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "tool":
                c = (m.get("content") or "")[:600]
                out.append({"role": "tool", "content": c})
                used += len(c)
        # 最后一轮用户输入
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
                room = max(PROMPT_BUDGET - used, 200)
                c = m["content"][:room]
                out.append({"role": "user", "content": c})
                used += len(c)
                break
        data["messages"] = out
        data.pop("tool_choice", None)
        data.pop("temperature", None)
        data.pop("top_p", None)
        data.pop("max_tokens", None)
        # 工具描述也占 token，压缩一下 description
        tools = data.get("tools")
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, dict):
                    fn = t.get("function")
                    if isinstance(fn, dict) and isinstance(fn.get("description"), str):
                        fn["description"] = fn["description"][:120]
        return json.dumps(data, ensure_ascii=False), raw_len, used

    out = []
    if KEEP_ORIGINAL_SYSTEM:
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "system":
                out.append({"role": "system", "content": (m.get("content") or "")[:400]})
                break
    else:
        out.append({"role": "system", "content": SYSTEM_TEXT})

    used = len(out[0]["content"])
    # 上一步工具的执行结果（即使没有 tools 字段也要保留，否则模型看不到观察）
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "tool":
            c = (m.get("content") or "")[:600]
            if used + len(c) < PROMPT_BUDGET - 200:
                out.append({"role": "tool", "content": c})
                used += len(c)
    # 最近一条助手回复
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content"):
            c = m["content"][:200]
            if used + len(c) < PROMPT_BUDGET - 200:
                out.append({"role": "assistant", "content": c})
                used += len(c)
            break
    # 最后一条用户消息（最不能丢）
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
            room = PROMPT_BUDGET - used
            out.append({"role": "user", "content": m["content"][:max(room, 100)]})
            used += min(len(m["content"]), max(room, 100))
            break
    # 若最后一条是 system/其它角色，兜底放最后一条有内容的
    if len(out) <= 1:
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("content") and m.get("role") != "system":
                out.append({"role": "user", "content": m["content"][:PROMPT_BUDGET - used]})
                used += len(m["content"])
                break

    data["messages"] = out
    # 手机端不认 tools/tool_choice 等字段，去掉能省一点传输和解析
    for k in ("tools", "tool_choice", "functions", "temperature", "top_p", "max_tokens"):
        data.pop(k, None)
    return json.dumps(data, ensure_ascii=False), raw_len, used

_lock = threading.Lock()


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with _lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def resolve_phone():
    """在候选地址里挑一个活的（2s 快速探测），结果缓存 60s，避免每次都全扫。"""
    now = time.time()
    if now - _active_phone["ts"] < 60 and _active_phone["host"]:
        return _active_phone["host"], _active_phone["port"]
    for cand in PHONE_CANDIDATES:
        host, _, port_s = cand.partition(":")
        port = int(port_s or 9999)
        url = "http://%s:%d/health" % (host, port)
        try:
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + PHONE_KEY})
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    _active_phone.update({"host": host, "port": port, "ts": now})
                    log("手机地址已锁定 %s:%d" % (host, port))
                    return host, port
        except Exception:
            continue
    # 都探测不到：退回首选地址（可能只是手机睡死了，地址没错）
    return PHONE_HOST, PHONE_PORT


def phone_request(path, body=None, method="GET", timeout=None):
    """向手机发起一次请求，返回 (ok, status, bytes)。"""
    host, port = resolve_phone()
    url = "http://%s:%d%s" % (host, port, path)
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + PHONE_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout or PHONE_TIMEOUT) as r:
            return True, r.status, r.read()
    except urllib.error.HTTPError as e:
        return False, e.code, e.read()
    except Exception as e:
        return False, 0, ("[%s] %s" % (type(e).__name__, e)).encode("utf-8")


def call_with_retry(path, body=None, method="GET"):
    last = (False, 0, b"")
    for attempt in range(1, RETRIES + 1):
        t0 = time.time()
        ok, status, data = phone_request(path, body, method)
        ms = int((time.time() - t0) * 1000)
        if ok:
            log("%s %s -> %s %dms (attempt %d)" % (method, path, status, ms, attempt))
            return True, status, data
        last = (ok, status, data)
        head = data[:120].decode("utf-8", "ignore")
        log("%s %s -> %s %dms (attempt %d) %s" % (method, path, status or "ERR", ms, attempt, head))
        if status and status != 0:
            # 手机明确回了错误码（如 401/404/503），重试无意义
            break
        if attempt < RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    return last


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 自己记日志，避免 stderr 噪音

    def _reply(self, status, payload, content_type="application/json"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/gateway/health"):
            # 监控用途：只做一次 3s 快速探测，不走重试链（否则一次要 75s+）
            t0 = time.time()
            host, port = resolve_phone()
            ok, status, data = phone_request("/health", timeout=3)
            self._reply(200 if ok else 502,
                        {"ok": ok, "phone_status": status, "phone": "%s:%d" % (host, port),
                         "ms": int((time.time() - t0) * 1000),
                         "hint": "手机可能在深度睡眠——解锁并打开起点OS mini 即可恢复"})
            return
        if path.endswith("/models"):
            ok, status, data = call_with_retry("/v1/models")
            self._reply(status or 502, data)
            return
        ok, status, data = call_with_retry("/health")
        self._reply(200 if ok else 502, data)

    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
        path = self.path.split("?")[0]

        # 手机端 Agent 端点：原样转发，不裁剪（手机侧自己会限长）
        if path.endswith("/agent/run"):
            ok, status, data = call_with_retry("/agent/run", body, "POST")
            if ok:
                self._reply(status, data)
            else:
                self._reply(status or 504, {"error": "phone_unreachable",
                                            "detail": data[:200].decode("utf-8", "ignore")})
            return

        # 统一重写：不管 WorkBuddy 拼的是什么路径，都打到 OpenAI 兼容端点
        if path.endswith("/chat/completions") or path == "/v1" or path == "/":
            target = "/v1/chat/completions"
        elif path.endswith("/chat"):
            target = "/chat"
        else:
            target = "/v1/chat/completions"

        # 关键：先把 prompt 压到手机能安全处理的规模，避免 MNN 越界闪退 + prefill 巨慢
        body, raw_len, new_len = trim_body(body)
        if raw_len > new_len:
            log("prompt 裁剪 %d -> %d 字符（防闪退+降延迟）" % (raw_len, new_len))

        ok, status, data = call_with_retry(target, body, "POST")
        if ok:
            self._reply(status, data)
        else:
            self._reply(status or 504, {"error": "phone_unreachable",
                                        "detail": data[:200].decode("utf-8", "ignore")})


def main():
    global PHONE_HOST, PHONE_PORT, PHONE_KEY, LISTEN_PORT, RETRIES, PHONE_TIMEOUT, PHONE_CANDIDATES
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=LISTEN_PORT)
    ap.add_argument("--phone", default=",".join(PHONE_CANDIDATES),
                    help="手机地址候选，逗号分隔，如 <手机IP>:9999,<备用手机IP>:9999")
    ap.add_argument("--key", default=PHONE_KEY)
    ap.add_argument("--retries", type=int, default=RETRIES)
    ap.add_argument("--timeout", type=float, default=PHONE_TIMEOUT)
    args = ap.parse_args()
    PHONE_CANDIDATES = [x.strip() for x in args.phone.split(",") if x.strip()]
    PHONE_HOST, _, p = PHONE_CANDIDATES[0].partition(":")
    PHONE_PORT = int(p or 9999)
    PHONE_KEY = args.key
    LISTEN_PORT = args.port
    RETRIES = args.retries
    PHONE_TIMEOUT = args.timeout

    srv = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    log("网关已启动 http://127.0.0.1:%d/v1  ->  手机 %s  (重试 %d 次, 单次超时 %ss)"
        % (LISTEN_PORT, ", ".join(PHONE_CANDIDATES), RETRIES, PHONE_TIMEOUT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("网关已停止")


if __name__ == "__main__":
    main()
