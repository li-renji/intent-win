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
    return payload


def _endpoint_url(endpoint):
    return endpoint.rstrip("/") + "/chat/completions"


# ---------- 原有接口（保持兼容，行为不变） ----------

def chat(endpoint, model, api_key, messages, stream=False):
    """返回 (ok, content_or_error)；stream=True 时返回生成器逐块产出文本。"""
    payload = _build_payload(model, messages, stream)
    try:
        resp = _post(_endpoint_url(endpoint), payload, api_key)
    except urllib.error.HTTPError as e:
        return False, "上游错误 HTTP %s" % e.code
    except Exception as e:  # 网络错误等
        return False, "上游失败: %s" % e

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


def chat_stream_events(base, model, api_key, messages, tools=None, timeout=None):
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
        msg = "上游错误 HTTP %s" % e.code
        if detail:
            msg += " " + detail
        return False, iter([("error", {"kind": "http", "code": e.code, "msg": msg})])
    except Exception as e:
        return False, iter([("error", {"kind": "net", "msg": "上游失败: %s" % e})])

    def gen():
        acc = _ToolCallAccum()
        content = []
        finish = ""
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    choice = json.loads(body)["choices"][0]
                except Exception:
                    continue
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    yield ("text", delta["content"])
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
                        "finish_reason": finish})

    return True, gen()
