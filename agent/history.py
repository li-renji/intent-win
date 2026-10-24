# -*- coding: utf-8 -*-
"""TurnFact 历史压缩（对齐 ai_service §5.2）。

多轮上下文按"事实摘要"而非"全量"注入：每轮结束后把该轮压缩成
1-3 条事实追加进事实表；下一轮只回注最近 N 条事实（history_facts_n），
不回灌完整历史数组。

压缩策略：按句子边界截断（保留语义完整），找不到边界才硬截——
直接 [:120] 会把长回答切断在半句话中间，模型读到的是残句。
"""

import re

_SENT_END = ("。", "！", "？", "；", "…", ". ", "! ", "? ", "; ", "\n")


def _clip(text, limit):
    """压缩到 limit 字符内，优先在句子边界收尾。"""
    t = " ".join(str(text).split())
    if len(t) <= limit:
        return t
    window = t[:limit]
    # 在后半段里找最后一个句末标点，避免截出太短的残句
    floor = max(10, int(limit * 0.5))
    best = -1
    for sep in _SENT_END:
        idx = window.rfind(sep)
        if idx >= floor and idx > best:
            best = idx + len(sep)
    if best > 0:
        return window[:best].strip()
    return window.rstrip() + "…"


class SessionHistory:
    def __init__(self, facts_n=10, summary_max_chars=2000):
        self.facts = []          # 事实摘要列表（最旧在前）
        self.facts_n = facts_n
        self.summary_max_chars = summary_max_chars

    def add_turn(self, user_text, reply_text, tools=None):
        """把一轮对话压缩为事实摘要入库。tools 为本轮调用的工具名列表（可选）。"""
        u = _clip(user_text, 120)
        r = _clip(reply_text, 160)
        if u:
            self.facts.append("用户:" + u)
        if r:
            self.facts.append("Agent:" + r)
        # 工具调用痕迹：让下一轮知道这轮动过什么，避免重复劳动
        if tools:
            self.facts.append("已调用:" + ",".join(str(t) for t in tools)[:80])
        # 总量守门：超预算时丢弃最旧事实
        budget = self.summary_max_chars
        total = sum(len(f) for f in self.facts)
        while self.facts and total > budget:
            total -= len(self.facts.pop(0))

    def inject(self):
        """返回注入本轮请求的事实摘要文本（只取最近 N 条）。"""
        recent = self.facts[-self.facts_n:]
        if not recent:
            return ""
        return "已知事实摘要（历史压缩，非全文）：\n- " + "\n- ".join(recent)

    # ---- 会话切换时的快照 / 恢复 ----
    def get_facts(self):
        return list(self.facts)

    def set_facts(self, facts):
        self.facts = [str(f) for f in (facts or [])]
