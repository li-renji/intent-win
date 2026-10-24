# -*- coding: utf-8 -*-
"""Agent 循环：让模型能真正调用工具（原生 function calling + 文本协议兜底）。

设计目标
--------
1. **原生优先**：向 OpenAI 兼容端点传 tools=[...]，模型返回 tool_calls 时执行并回灌。
2. **文本兜底**：很多小模型/自研推理服务（MNN、llama.cpp 老版本、部分手机端 App）
   不支持 tools 参数，或训练时没学过 function calling。此时退化为「文本协议」：
   模型在正文里输出 `[[TOOL: name {json args}]]`，循环照样识别并执行。
   小模型输出形态杂乱（【文件列表】、[TOOL: fs]、甚至只有一句自然语言），
   由 agent/intent.py 做三层意图解析（标准协议/变体标记/自然语言）兜底。
   两种模式可同时开启，原生失败自动降级，不丢用户请求。
3. **不绕过 6801**：工具执行一律走 ToolBus，外部调用方（6801）与模型调用共用同一出口。
4. **可控**：最大轮数、总时长、单次工具结果长度均可配；危险工具需确认。

事件（供 UI 展示执行过程）
--------------------------
    ("text", str)          模型正文增量
    ("tool_start", dict)   开始执行工具 {"name","args"}
    ("tool_end", dict)     工具结束 {"name","ok","result"}
    ("done", dict)         {"content":str,"rounds":int,"tools":list}
    ("error", str)
"""
import json
import time

from . import intent, llm

# 模型正文里常见的"口头计划"（小模型幻觉：说自己要调工具但没调）
_VERBAL = ("我要调用", "我将调用", "接下来调用", "现在调用", "让我调用",
           "正在调用", "我将使用", "我要使用", "调用工具")


def _args_of(call):
    """从 tool_call 结构里取出参数字典（宽容解析）。"""
    fn = call.get("function") or {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {"_raw": raw}
    except Exception:
        return {"_raw": str(raw)}


class AgentLoop:
    """把「一问一答」升级为「会调工具的自主循环」。"""

    def __init__(self, cfg, bus, browser=None, history=None):
        self.cfg = cfg
        self.bus = bus
        self.browser = browser
        self.history = history
        self.max_rounds = int(cfg.get("agent_max_rounds", 6))
        self.max_seconds = int(cfg.get("agent_max_seconds", 180))
        self.text_fallback = str(cfg.get("agent_text_fallback", "on")).lower() in (
            "on", "true", "1", "yes")
        self.max_tool_result = int(cfg.get("agent_max_tool_result", 1500))
        # 单次模型请求超时（秒）。手机端推理可能很慢，默认给 90s。
        self.req_timeout = int(cfg.get("agent_req_timeout", 90))
        self.confirm_required = set(
            x.strip() for x in str(cfg.get("agent_confirm_tools", "")).split(",")
            if x.strip())
        self.on_event = None      # 外部注入：用于审批危险工具（可选）
        # 上游能力缓存：按 (base, model) 记住该端点是否接受原生 tools。
        # 避免每轮对话都先撞一次 400 再降级（手机端小模型尤其明显）。
        self._native_ok_cache = {}
        # 工具协议模式：
        #   auto   先试原生 tools，被拒再降级文本协议（默认）
        #   text   直接用文本协议（1.5B 等未训练过 function calling 的小模型用这个）
        #   native 只用原生 function calling
        self.tool_mode = str(cfg.get("agent_tool_mode", "auto")).lower()
        # Tier3 自然语言意图去重集合（同一意图整个任务只触发一次）
        self._seen_intents = set()

    # ---------- 入口 ----------

    def _inject_text_protocol(self, messages):
        """注入文本协议说明 + 工具参数 + few-shot 示例。

        小模型（1.5B~4B）光靠规则说明学不会工具格式，必须给：
        1) 每个工具的参数名与取值（从 schema 动态生成，加工具自动生效）
        2) 完整对话示例（覆盖最常用工具），让模型照抄结构
        """
        tools = self.bus.enabled()
        names = "、".join(tools) if tools else "（无）"

        # 从 schema 动态生成参数说明，避免硬编码与 schema 脱节
        detail_lines = []
        for s in self.bus.openai_schemas():
            fn = s.get("function", {})
            nm = fn.get("name", "")
            desc = (fn.get("description") or "").split("。")[0]
            props = (fn.get("parameters") or {}).get("properties", {}) or {}
            req = (fn.get("parameters") or {}).get("required", []) or []
            if not props:
                detail_lines.append("- %s（无参数）" % nm)
                continue
            ps = []
            for pn, pv in props.items():
                mark = "*" if pn in req else ""
                ps.append("%s%s" % (pn, mark))
            detail_lines.append("- %s: %s | 参数: %s" % (nm, desc, ", ".join(ps)))
        detail = "\n".join(detail_lines)

        # few-shot：只演示已启用的工具，避免教模型调不存在的工具
        ex = []
        if "fs" in tools:
            ex.append('用户：桌面上有些什么文件\n助手：[[TOOL: fs {"op": "list", "path": "~/Desktop"}]]\n'
                      '助手：桌面有 qd-agent-win 文件夹、截图.png 等。')
        if "screen" in tools:
            ex.append('用户：现在屏幕上是什么\n助手：[[TOOL: screen {"op": "shot"}]]\n'
                      '助手：已截图，屏幕 1920x1080，中间是一个浏览器窗口。')
        if "browser" in tools:
            ex.append('用户：帮我看看百度首页上有什么\n助手：[[TOOL: browser {"cmd": '
                      '"NAVIGATE https://www.baidu.com"}]]\n助手：[[TOOL: browser {"cmd": '
                      '"SNAPSHOT"}]]\n助手：百度首页上有搜索框、新闻分类和几个热搜词。')
        if "code" in tools:
            ex.append('用户：帮我算一下 1 到 100 的和\n助手：[[TOOL: code {"lang": "python", '
                      '"code": "print(sum(range(101)))"}]]\n助手：结果是 5050。')
        if "memo" in tools:
            ex.append('用户：记住我喜欢用深色主题\n助手：[[TOOL: memo {"op": "write", '
                      '"text": "用户喜欢深色主题"}]]\n助手：已记住。')
        examples = "\n".join(ex)

        tip = """

【工具调用协议】
你可以调用以下工具：%s

各工具参数（* 为必填）：
%s

调用格式（必须严格照此格式，单独成行）：
[[TOOL: 工具名 {"参数名": "参数值"}]]

示例对话：
%s

规则：
1. 需要获取文件/网页/屏幕/计算结果等外部信息时，必须实际调用工具，不要凭空猜测，也不要描述你"将要"做什么。
2. 一次只调用一个工具，等系统回灌结果后再决定下一步。
3. 拿到工具结果后，用自然语言给出最终答复，答复里不要出现 [[TOOL: 字样。
4. 只调用上面列出的工具，不要发明新工具名。
5. 不需要工具的问题，直接回答即可。""" % (names, detail, examples)

        new_msgs = list(messages)
        if new_msgs and new_msgs[0].get("role") == "system":
            new_msgs[0] = {"role": "system",
                           "content": new_msgs[0].get("content", "") + tip}
        else:
            new_msgs.insert(0, {"role": "system", "content": tip.strip()})
        return new_msgs

    def run(self, base, model, key, messages, stream=True):
        """执行一轮完整 agent 对话。

        messages: 已含 system 与本轮 user 的消息列表（函数内部会追加）。
        返回 (ok: bool, final_text: str, trace: list)。
        trace 记录每一步工具调用，供持久化与复盘。
        """
        t0 = time.time()
        trace = []
        schemas = self.bus.openai_schemas()
        # 读取该端点的能力缓存：None=未知，True=支持原生 tools，False=不支持
        cache_key = (base, model)
        cached = self._native_ok_cache.get(cache_key)
        if self.tool_mode == "text":
            native_ok = False
        elif self.tool_mode == "native":
            native_ok = True
        else:
            native_ok = True if cached is None else cached

        for rnd in range(1, self.max_rounds + 1):
            if time.time() - t0 > self.max_seconds:
                yield ("error", "超过最大执行时长（%ds），已中止。" % self.max_seconds)
                return

            # --- 发起一次模型请求 ---
            use_native = native_ok and bool(schemas)
            tools = schemas if use_native else None
            # 走文本协议时，首轮就带上协议说明 + few-shot 示例
            if not use_native and self.text_fallback and rnd == 1:
                messages = self._inject_text_protocol(messages)
            ok, gen = llm.chat_stream_events(base, model, key, messages,
                                             tools=tools, timeout=self.req_timeout)

            content_parts = []
            calls = []
            got_error = None
            emitted_text = False
            for kind, payload in gen:
                if kind == "text":
                    content_parts.append(payload)
                    emitted_text = True
                    yield ("text", payload)
                elif kind == "tool_call":
                    calls.append(payload)
                elif kind == "error":
                    got_error = payload
                    break
                elif kind == "done":
                    done = payload
                    content_parts = [done.get("content", "")]
                    if done.get("tool_calls"):
                        calls = done["tool_calls"]
                    break

            if got_error:
                # 错误分类：
                #   http 4xx → 上游明确拒绝（如不认识 tools 参数）→ 可降级
                #   http 5xx / net → 瞬时故障（超时、休眠）→ 不可降级，直接报错
                err = got_error
                err_kind = err.get("kind") if isinstance(err, dict) else "net"
                err_code = err.get("code") if isinstance(err, dict) else 0
                err_msg = err.get("msg") if isinstance(err, dict) else str(err)
                reject_tools = (err_kind == "http" and 400 <= err_code < 500)

                if use_native and self.text_fallback and not emitted_text and reject_tools:
                    native_ok = False
                    self._native_ok_cache[cache_key] = False
                    # 追加文本协议说明，让模型知道降级后怎么调工具
                    messages = self._inject_text_protocol(messages)
                    # notice：非致命提示，调用方应继续等待后续内容
                    yield ("notice", "上游不支持原生 tools，已切换文本协议重试。")
                    continue
                yield ("error", err_msg)
                return

            text = "".join(content_parts).strip()

            # --- 文本协议兜底：三层意图解析（标准协议/变体标记/自然语言）---
            # Tier1 [[TOOL:]] / Tier2 【fs】[TOOL:]等变体 / Tier3 自然语言意图，
            # 覆盖 4B 小模型输出【文件列表】这类非标准格式的情况。
            if not calls and self.text_fallback:
                tcalls, clean, tier = intent.extract(
                    text, self.bus.enabled(), self._seen_intents)
                if tcalls:
                    calls = tcalls
                    text = clean
                    # 正文里的调用标记已剥离，通知前端把气泡重置为干净文本
                    if emitted_text:
                        yield ("text_reset", clean)

            # --- 没有工具调用 → 模型给出了最终答案 ---
            if not calls:
                final = text or ""
                # 小模型"只说不做"：说要用工具却没调 → 提示它真正调用
                if not final and any(v in text for v in _VERBAL):
                    final = "（模型声称要调用工具但未实际调用）"
                yield ("done", {"content": final, "rounds": rnd,
                                "tools": [t["name"] for t in trace]})
                return

            # --- 执行工具 ---
            assistant_msg = {"role": "assistant", "content": text or None}
            if use_native:
                assistant_msg["tool_calls"] = calls
            messages.append(assistant_msg)

            for call in calls:
                name = (call.get("function") or {}).get("name", "")
                args = _args_of(call)
                cid = call.get("id") or ("call_%d" % len(trace))

                # 危险工具需确认
                if name in self.confirm_required and self.on_event:
                    approved = self.on_event(("need_confirm", name, args))
                    if not approved:
                        ok_t, res = False, "用户拒绝了该操作。"
                else:
                    ok_t, res = self.bus.invoke(name, args)

                if len(res) > self.max_tool_result:
                    res = res[: self.max_tool_result] + "...(truncated)"

                step = {"name": name, "args": args, "ok": ok_t, "result": res, "round": rnd}
                trace.append(step)
                yield ("tool_start", {"name": name, "args": args, "round": rnd})
                yield ("tool_end", step)

                messages.append({"role": "tool", "tool_call_id": cid,
                                 "name": name, "content": res})

            # --- 最后一轮后仍无最终答案 → 让模型收尾 ---
            if rnd == self.max_rounds:
                messages.append({"role": "user",
                                 "content": "请基于以上工具结果给出最终答复，不要再调用工具。"})
                ok, gen = llm.chat_stream_events(base, model, key, messages,
                                                 tools=None, timeout=self.req_timeout)
                final_text = ""
                for kind, payload in gen:
                    if kind == "text":
                        final_text += payload
                        yield ("text", payload)
                    elif kind == "error":
                        yield ("error", payload)
                        return
                    elif kind == "done":
                        final_text = payload.get("content", "") or final_text
                        break
                yield ("done", {"content": final_text, "rounds": rnd,
                                "tools": [t["name"] for t in trace]})
                return

        yield ("done", {"content": "", "rounds": self.max_rounds,
                        "tools": [t["name"] for t in trace]})
