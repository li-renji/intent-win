# -*- coding: utf-8 -*-
"""OpenAI 兼容客户端。云端 / 本地分流由 config.active_llm 决定，调用方透明。
只用标准库 urllib；支持流式（SSE）与非流式；支持 function calling（tools）。

事件协议（供 agent/loop.py 消费）：
    ("text", str)        文本增量
    ("tool_call", dict)  一条完整 tool_call（流式累积完毕后产出）
    ("done", dict)       {"content":str,"tool_calls":list,"finish_reason":str}
    ("error", str)       出错信息（后续不再有事件）
"""
import json
import urllib.error
import urllib.request

TIMEOUT = 40  # 对齐 ai_service 40s deadline


def _hdr_value(text):
    """HTTP 头值必须 latin-1 可编码，否则 urllib 抛 UnicodeEncodeError。

    含中文/emoji 的 API Key 会直接让请求崩掉（报 'latin-1' codec can't
    encode），这里把非 latin-1 字符按 UTF-8 字节原样塞进头里——
    主流服务端（含 OpenAI 兼容网关）都按 UTF-8 解析。
    """
    s = str(text or "")
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8").decode("latin-1")


def _post(url, payload, api_key, timeout=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _hdr_value(api_key or "sk-noauth"),
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout or TIMEOUT)


def _build_payload(model, messages, stream, tools=None):
    payload = {"model": model, "messages": messages, "stream": stream}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        # 让上游在最后一帧带上 usage（prompt/completion token），用于上下文用量显示
        payload["stream_options"] = {"include_usage": True}
    return payload


def _endpoint_url(endpoint):
    return endpoint.rstrip("/") + "/chat/completions"


def _explain_http_error(code, detail):
    """把上游 HTTP 错误翻译成用户能行动的中文提示（保留原始详情便于排查）。"""
    d = (detail or "").strip()[:300]
    low = d.lower()
    tail = ("；上游返回：" + d) if d else ""
    if code in (401, 403):
        return ("API Key 无效或没有权限（HTTP %d）：Key 可能复制不完整、已过期、"
                "或不属于该端点%s" % (code, tail))
    if code == 404:
        if "model" in low or "模型" in d:
            return "模型 ID 不被该端点支持（HTTP 404）：请把模型名改成平台提供的 ID%s" % tail
        return "端点路径不存在（HTTP 404）：请检查 base_url 是否以 /v1 结尾%s" % tail
    if code == 429:
        return "触发限流或额度不足（HTTP 429）%s" % tail
    if code == 413:
        return "请求体过大（HTTP 413）：请减少对话长度%s" % tail
    if code == 400:
        if (any(k in low for k in ("context", "length", "token", "too long", "too many"))
                or any(k in d for k in ("上下文", "容量", "过长", "超出"))):
            return "请求超出模型上下文容量（HTTP 400）%s" % tail
        return "上游拒绝请求（HTTP 400）%s" % tail
    if code >= 500:
        return "上游服务故障（HTTP %d）%s" % (code, tail)
    return "上游错误 HTTP %s%s" % (code, (" " + d) if d else "")


def _explain_net_error(e):
    """网络类错误：超时/解析失败/拒绝连接，给出可检查的点。"""
    s = str(e)
    low = s.lower()
    if "timed out" in low or "timeout" in low:
        return "连接超时：端点无响应。请检查地址是否正确、本机网络能否访问该域名"
    if "getaddrinfo" in low or "name or service not known" in low:
        return "域名无法解析：端点地址可能写错了"
    if "refused" in low:
        return "连接被拒绝：端点地址或端口有误，服务未在监听"
    return "网络错误：%s（请检查端点与网络）" % s


# ---------- 原有接口（保持兼容，行为不变） ----------

def chat(endpoint, model, api_key, messages, stream=False):
    """返回 (ok, content_or_error)；stream=True 时返回生成器逐块产出文本。"""
    payload = _build_payload(model, messages, stream)
    try:
        resp = _post(_endpoint_url(endpoint), payload, api_key)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        return False, _explain_http_error(e.code, detail)
    except Exception as e:  # 网络错误等
        return False, _explain_net_error(e)

    if not stream:
        try:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return True, content
        except Exception as e:
            return False, "响应解析失败: %s" % e
        finally:
            resp.close()

    def gen():
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    delta = json.loads(body)["choices"][0].get("delta", {})
                    if delta.get("content"):
                        yield delta["content"]
                except Exception:
                    continue
        finally:
            resp.close()
    return True, gen()


# ---------- 工具调用事件流 ----------

class _ToolCallAccum:
    """把流式 tool_calls 的 delta 累积成完整调用列表。"""

    def __init__(self):
        self.items = {}

    def feed(self, deltas):
        for d in deltas or []:
            if not isinstance(d, dict):
                continue
            idx = d.get("index", 0)
            slot = self.items.setdefault(
                idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if d.get("id"):
                slot["id"] = d["id"]
            if d.get("type"):
                slot["type"] = d["type"]
            fn = d.get("function")
            if isinstance(fn, dict):
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]

    def result(self):
        out = []
        for k in sorted(self.items):
            slot = self.items[k]
            if not slot["function"]["name"]:
                continue
            out.append(slot)
        return out


def chat_stream_events(base, model, api_key, messages, tools=None, timeout=None, stop_event=None):
    """发起一次流式请求，返回 (ok, generator)。

    generator 产出 ("text"|"tool_call"|"done"|"error", payload) 事件。
    ok=False 时 generator 只产出一条 ("error", 说明)。

    说明：payload 为字符串时表示普通错误；为 dict 时含 kind 字段：
        {"kind": "http", "code": 400, "msg": "..."}   上游明确拒绝（如不支持 tools）
        {"kind": "net",  "msg": "..."}                网络/超时等瞬时故障
    调用方据此判断「是否该降级」——超时绝不能当成「不支持 tools」。
    """
    payload = _build_payload(model, messages, True, tools)
    try:
        resp = _post(_endpoint_url(base), payload, api_key, timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        msg = _explain_http_error(e.code, detail)
        return False, iter([("error", {"kind": "http", "code": e.code, "msg": msg})])
    except Exception as e:
        return False, iter([("error", {"kind": "net", "msg": _explain_net_error(e)})])

    def gen():
        acc = _ToolCallAccum()
        content = []
        finish = ""
        usage = None
        stopped = False
        try:
            for raw in resp:
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    choice = json.loads(body)["choices"][0]
                except Exception:
                    # 有些帧只有 usage 没有 choices
                    try:
                        u = json.loads(body).get("usage")
                        if u: usage = u
                    except Exception:
                        pass
                    continue
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    yield ("text", delta["content"])
                # 思考过程 / 深度思考（DeepSeek 等放在 reasoning_content，Qwen 等在 reasoning）
                rb = delta.get("reasoning_content")
                if rb is None:
                    rb = delta.get("reasoning")
                if rb:
                    yield ("thinking", rb)
                if delta.get("tool_calls"):
                    acc.feed(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        except Exception as e:
            yield ("error", "流中断: %s" % e)
            return
        finally:
            try:
                resp.close()
            except Exception:
                pass
        calls = acc.result()
        for c in calls:
            yield ("tool_call", c)
        yield ("done", {"content": "".join(content), "tool_calls": calls,
                        "finish_reason": finish, "usage": usage, "stopped": stopped})

    return True, gen()
