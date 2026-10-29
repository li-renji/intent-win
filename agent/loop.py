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

from . import econ, intent, llm
from .config import MODEL_CTX_LIMITS, model_ctx_limit

# 模型正文里常见的"口头计划"（小模型幻觉：说自己要调工具但没调）
_VERBAL = ("我要调用", "我将调用", "接下来调用", "现在调用", "让我调用",
           "正在调用", "我将使用", "我要使用", "调用工具")

# 上游「上下文/容量超限」的典型特征（HTTP 400/413 最常见，小模型网关也用中文提示）
_CTX_PATTERNS = ("context", "上下文", "容量", "过长", "too long", "too many",
                 "maximum context", "maximum token", "token 上限", "超过最大",
                 "超出容量", "超出上下文", "exceed")


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
        # 上下文超限压缩重试：每个任务只允许一次（防止压缩-重试死循环）
        self._ctx_retried = False
        # 上下文经济性（对齐 pi）：主动压缩每任务只做一次
        self.compact_ratio = float(cfg.get("agent_compact_ratio", 0.8) or 0)
        self.keep_tokens = int(cfg.get("agent_keep_tokens", 8000))
        self._compacted = False
        # 停止信号（由 UI 触发，request_stop / clear_stop）
        self._stop_event = None
        try:
            import threading
            self._stop_event = threading.Event()
        except Exception:
            self._stop_event = None

    def request_stop(self):
        if self._stop_event:
            self._stop_event.set()

    def clear_stop(self):
        if self._stop_event:
            self._stop_event.clear()

    @property
    def stop_event(self):
        return self._stop_event

    # ---------- 上下文超限自救 ----------

    @staticmethod
    def _is_ctx_error(err):
        """判断上游错误是否为「上下文/容量超限」——这类错误压缩历史就能救。"""
        if isinstance(err, dict):
            if err.get("kind") == "net":
                return False        # 网络超时绝不是容量问题
            msg = str(err.get("msg") or "")
        else:
            msg = str(err)
        low = msg.lower()
        return any(p in low for p in _CTX_PATTERNS)

    @staticmethod
    def _compress_for_ctx(messages):
        """上下文超限时压缩对话：保留系统提示与本轮任务，过程压成简短摘要。

        丢弃中间轮的 assistant/tool 消息（同时避免悬空的 tool_call_id），
        只把最近的工具结果折叠成一条摘要——小上下文模型只剩这条路。
        """
        sys_msg, last_user, tool_results = None, None, []
        for m in messages:
            role = m.get("role")
            if role == "system":
                sys_msg = m
            elif role == "user":
                last_user = m
            elif role == "tool":
                tool_results.append(str(m.get("content") or ""))
        out = []
        if sys_msg:
            out.append(sys_msg)
        if tool_results:
            parts, total = [], 0
            for c in reversed(tool_results[-4:]):      # 只保留最近 4 条工具结果
                piece = c[:300]
                if total + len(piece) > 1200:          # 摘要总量封顶 1200 字
                    break
                parts.append(piece)
                total += len(piece)
            out.append({"role": "user",
                        "content": "（上下文已满，早期过程已压缩）此前工具结果摘要：\n"
                                   + "\n---\n".join(reversed(parts))})
        if last_user:
            out.append(last_user)
        return out

    @staticmethod
    def _compact_messages(messages, keep_tokens):
        """主动压缩（对齐 pi 的 context 预算管理）：接近容量上限前瘦身。

        保留：系统提示 + 首轮任务（主线）+ 最后一个 user 之后的整段当前轮次
        （从 user 起整段保留，天然成轮，不会出现裸 tool 消息被部分端点拒绝）。
        中段历史免费折叠成一条本地结构化摘要（不花 token 调模型）。
        """
        sys_msg = next((m for m in messages if m.get("role") == "system"), None)
        first_user = next((m for m in messages if m.get("role") == "user"), None)
        user_idx = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        if not user_idx:
            return list(messages)
        last_user_idx = user_idx[-1]
        head = [m for m in (sys_msg, first_user) if m is not None]
        tail = messages[last_user_idx:]
        kept = set(id(m) for m in head) | set(id(m) for m in tail)
        dropped = [m for m in messages if id(m) not in kept]
        if not dropped:
            return list(messages)
        # 中段 → 免费摘要（本地生成）：用户说过什么、调过哪些工具、结果要点
        lines = []
        for m in dropped:
            role = m.get("role")
            if role == "user":
                lines.append("· 用户: " + str(m.get("content") or "")[:80])
            elif role == "assistant":
                names = [((tc.get("function") or {}).get("name") or "")
                         for tc in (m.get("tool_calls") or [])]
                names = [n for n in names if n]
                if names:
                    lines.append("· 调用工具: " + ", ".join(names))
            elif role == "tool":
                lines.append("· 工具结果: " + str(m.get("content") or "")[:80])
        out = list(head)
        if lines:
            out.append({"role": "user",
                        "content": "（为节省上下文，早期过程摘要）\n"
                                   + "\n".join(lines[:40])})
        out.extend(tail)
        return out

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
                      '助手：桌面有 intent 文件夹、截图.png 等。')
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
        if "web" in tools:
            ex.append('用户：帮我查一下今天有什么科技新闻\n助手：[[TOOL: web {"op": "search", '
                      '"q": "今日科技新闻"}]]\n助手：查到几条，其中…')
        if "organize" in tools:
            ex.append('用户：把我下载文件夹整理一下\n助手：[[TOOL: organize {"op": "plan", '
                      '"path": "~/Downloads"}]]\n助手：计划如下：.zip 5 个、.pdf 3 个…（确认后 op=run 执行）')
        if "remind" in tools:
            ex.append('用户：30 分钟后提醒我开会\n助手：[[TOOL: remind {"op": "add", '
                      '"text": "开会", "when": "in 30m"}]]\n助手：已设提醒。')
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
        # 每个任务重置：上下文超限压缩重试每任务只允许一次
        self._ctx_retried = False
        self._compacted = False
        self.clear_stop()   # 新一轮：清除上一轮的停止信号
        schemas = self.bus.openai_schemas()
        # 当前模型的上下文容量（tok）：型号查表优先，其次按配置推断
        ctx_limit = MODEL_CTX_LIMITS.get(model) or model_ctx_limit(self.cfg)
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
            # 停止信号：UI 点了「停止」→ 立即收尾，不再发新一轮模型请求
            if self._stop_event and self._stop_event.is_set():
                yield ("notice", "已停止生成。")
                yield ("done", {"content": "".join(content_parts).strip(),
                                "rounds": rnd - 1, "tools": [t["name"] for t in trace]})
                return
            # max_seconds<=0 表示不限时（长任务拆解不被强杀）；>0 才做软上限提醒
            if self.max_seconds > 0 and time.time() - t0 > self.max_seconds:
                yield ("notice", "已运行 %ds，超过建议时长但继续执行（长任务不限时）。" % self.max_seconds)
                self.max_seconds = 0  # 只提醒一次，之后不再打断

            # --- 上下文经济性：接近容量上限就主动压缩（对齐 pi 预算管理）---
            # 不是等上游报「超限」才救，而是用掉 80% 就先瘦身，任务不中断
            if (not self._compacted and self.compact_ratio > 0
                    and econ.messages_tokens(messages) > ctx_limit * self.compact_ratio):
                self._compacted = True
                messages = self._compact_messages(messages, self.keep_tokens)
                yield ("notice",
                       "上下文用量已较高，已自动压缩早期历史（任务主线与最近对话保留，不影响当前任务）。")

            # --- 发起一次模型请求 ---
            use_native = native_ok and bool(schemas)
            tools = schemas if use_native else None
            # 走文本协议时，首轮就带上协议说明 + few-shot 示例
            if not use_native and self.text_fallback and rnd == 1:
                messages = self._inject_text_protocol(messages)
            t_req = time.time()
            ok, gen = llm.chat_stream_events(base, model, key, messages,
                                             tools=tools, timeout=self.req_timeout,
                                             stop_event=self._stop_event)

            content_parts = []
            calls = []
            got_error = None
            emitted_text = False
            req_usage = None
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
                    req_usage = done.get("usage")
                    if done.get("stopped"):
                        # 用户在流式过程中点了「停止」：把已生成的部分作为最终答复收尾
                        yield ("notice", "已停止生成。")
                        yield ("done", {"content": "".join(content_parts).strip(),
                                        "rounds": rnd, "tools": [t["name"] for t in trace]})
                        return
                    break
            # 上报 token 用量（上下文用量条用）
            if req_usage:
                yield ("usage", req_usage)
            # 慢请求提醒：本轮模型响应太慢（>20s 才有内容）时提示一次
            dt = time.time() - t_req
            if not got_error and dt > 20 and not emitted_text:
                yield ("notice", "本轮模型响应较慢（%.0fs），本地引擎可能正忙于加载或上下文过长。" % dt)

            if got_error:
                # 错误分类：
                #   http 4xx → 上游明确拒绝（如不认识 tools 参数）→ 可降级
                #   http 5xx / net → 瞬时故障（超时、休眠）→ 不可降级，直接报错
                err = got_error
                err_kind = err.get("kind") if isinstance(err, dict) else "net"
                err_code = err.get("code") if isinstance(err, dict) else 0
                err_msg = err.get("msg") if isinstance(err, dict) else str(err)

                # ① 上下文/容量超限 → 压缩历史后重试一次（降级协议救不了容量问题）
                if self._is_ctx_error(err):
                    if not self._ctx_retried:
                        self._ctx_retried = True
                        messages = self._compress_for_ctx(messages)
                        # notice：非致命提示，调用方应继续等待后续内容
                        yield ("notice", "模型上下文已满，已自动压缩历史后重试…")
                        continue
                    yield ("error",
                           "模型上下文已满（%s）。已自动压缩重试仍超限：请点「新对话」重新开始、"
                           "把任务拆小，或换上下文更大的模型。" % err_msg)
                    return

                # ② 上游 4xx 拒绝 tools 参数 → 降级文本协议重试
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

                # 工具结果截断：头尾保留、中间打洞（错误信息通常在末尾），
                # 并告诉模型原结果有多大，让它决定是否缩小范围续读
                _res_full = res
                res, cut = econ.truncate_middle(res, self.max_tool_result)
                if cut:
                    res += "\n（以上为截断视图，原结果共 %d 字符；可缩小范围或分页读取）" % len(_res_full)

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
                                                 tools=None, timeout=self.req_timeout,
                                                 stop_event=self._stop_event)
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
