# -*- coding: utf-8 -*-
"""三层意图解析：把模型输出归一成工具调用。

为什么需要三层
--------------
小模型（1.5B~4B）受过 function calling 训练的极少，输出形态五花八门：
  Tier 1  标准文本协议  [[TOOL: fs {"op":"list"}]]
  Tier 2  变体标记      【fs】 / [TOOL: fs] / tool_call 标记 / 【文件列表】
  Tier 3  自然语言意图  「我看看桌面上有什么文件」（连标记都没有）
只认 Tier 1 会把 4B 模型的真实意图全部丢掉（模型以为自己在调工具，
其实什么都没发生）。本模块按置信度从高到低逐层解析，任一层命中即返回。

设计约束
--------
1. Tier 3 必须保守：意图词组命中才映射，且同一签名只触发一次
   （seen 去重），防止模型每句话都被误解析成工具调用。
2. Tier 1 保留模型给出的工具名（即使未启用也透传，由总线返回
   "工具不存在"错误回灌给模型——这是让它自我纠正的学习信号）；
   Tier 2/3 只映射 enabled 清单内的工具。
3. 返回值里的 calls 用 OpenAI tool_calls 结构，loop 无需区分来源。
"""
import json
import os
import re

# 项目根目录：按本文件位置推导（agent/ 的上两级）。
# 工作台等"打开XXX"类硬触发规则不再硬编码于此，改为从 skills/*.qdskill 的
# trigger/action/run 头动态加载（见 _load_skill_rules），实现"丢文件、重启即生效"。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKILLS_DIR = os.path.join(_PROJECT_ROOT, "skills")

# ---------- Tier 1：标准文本协议 [[TOOL: name {json}]] ----------
_T1_RE = re.compile(r"\[\[\s*TOOL\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\{.*?\})\s*\]\]",
                    re.DOTALL)

# ---------- Tier 2：变体标记 ----------
# （a）tool_call 开闭标记包裹的 JSON（Qwen / OpenAI 旧式）。
#     标记字面量拆开写，避免标记本身混入源码造成歧义。
_TC_OPEN = "<tool" + "_call>"
_TC_CLOSE = "</tool" + "_call>"
_T2_JSON_RE = re.compile(_TC_OPEN + r"\s*(\{.*?\})\s*" + _TC_CLOSE, re.DOTALL)
# （b）【fs】/【截图】/【文件列表】→ 中文工具名经别名表映射
_T2_BRACKET_RE = re.compile(r"【\s*([^】\s]{1,12})\s*】")
# （c）[TOOL: fs] 或 [TOOL:fs {"op":"list"}]（无 json 的残缺协议）
_T2_TOOL_RE = re.compile(r"\[\s*TOOL\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\{.*?\})?\s*\]",
                         re.DOTALL)

# 中文/别名 → 标准工具名（模型爱用中文名）
TOOL_ALIASES = {
    "文件": "fs", "文件列表": "fs", "列目录": "fs", "读取文件": "fs",
    "目录": "fs", "桌面文件": "fs",
    "截图": "screen", "屏幕": "screen", "截屏": "screen",
    "浏览器": "browser", "网页": "browser", "上网": "browser",
    "执行代码": "code", "代码": "code", "运行": "code", "计算": "code",
    "记忆": "memo", "记住": "memo",
    "技能": "skills",
    "手机": "phone",
    "fs": "fs", "screen": "screen", "browser": "browser",
    "code": "code", "memo": "memo", "skills": "skills", "phone": "phone",
}

# ---------- Tier 3：自然语言意图（保守映射） ----------
# (意图词组, 工具名, 默认参数) —— 一轮最多触发一个，见 _parse_t3
_INTENT_RULES = [
    (("桌面上有什么", "桌面有哪些", "列出桌面", "看看桌面", "桌面的文件", "有哪些文件"),
     "fs", {"op": "list", "path": "~/Desktop"}),
    (("有什么文件", "哪些文件", "列目录", "列出目录", "目录结构", "文件列表"),
     "fs", {"op": "list"}),
    (("读一下", "读取", "打开文件", "看看文件内容", "文件内容"),
     "fs", {"op": "read"}),
    (("查找文件", "搜索文件", "找一下文件", "找文件"),
     "fs", {"op": "search"}),
    (("截图", "截个屏", "看看屏幕", "屏幕上是什么", "屏幕内容"),
     "screen", {"op": "shot"}),
    (("打开网页", "访问网站", "看看百度", "浏览网页", "上网看看"),
     "browser", {"cmd": "SNAPSHOT"}),
    (("记住", "记一下", "帮我记"),
     "memo", {"op": "write"}),
    # 注：工作台等"打开XXX"类硬触发已外置到 skills/*.qdskill（trigger/action/run 头），
    # 由 _load_skill_rules 动态加载，不再硬编码于此。
]


# ---------- 从 skills/*.qdskill 加载"硬触发"规则（文件驱动，重启即生效） ----------
def _load_skill_rules(skills_dir, project_root):
    """扫描技能目录，把带 trigger + run 头的 .qdskill 注册成 Tier3 硬触发规则。

    头格式（均为可选，只有同时含 trigger 与 run 才生效）：
        trigger: 短语1,短语2,...        # 命中即触发（子串匹配，同 _INTENT_RULES）
        action:  code                  # 目前统一经 code 工具执行 run（默认 code）
        run:     cmd /c "@ROOT@/workbench/wb_start.cmd"   # 完整命令；@ROOT@ 替换为项目根
    没有这些头的技能文件（纯 recipe）不受影响，仍只经 skills 摘要机制生效。
    这样"打开工作台"之类集成变成：丢一个 .qdskill 文件 + 重启，零改 intent.py。
    """
    rules = []
    if not os.path.isdir(skills_dir):
        return rules
    for fn in sorted(os.listdir(skills_dir)):
        if not fn.endswith(".qdskill"):
            continue
        path = os.path.join(skills_dir, fn)
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        trigger = action = run = None
        # 只解析头部（前 30 行），遇到正文（非已知头且非空非注释行）即停止
        for line in lines[:30]:
            s = line.strip()
            if s.startswith("trigger:"):
                trigger = s[len("trigger:"):].strip()
            elif s.startswith("action:"):
                action = s[len("action:"):].strip()
            elif s.startswith("run:"):
                run = s[len("run:"):].strip()
            elif s and not s.startswith("#") and ":" in s and \
                 s.split(":", 1)[0].strip() in ("name", "version", "desc"):
                continue  # 已知头字段，继续读后续头部
            else:
                if s and not s.startswith("#"):
                    break  # 进入正文，停止头部解析
                # 空行或注释行：继续
        if not trigger or not run:
            continue
        phrases = [p.strip() for p in trigger.split(",") if p.strip()]
        if not phrases:
            continue
        run = run.replace("@ROOT@", project_root).replace("@root@", project_root)
        rules.append((tuple(phrases), "code", {"lang": "shell", "code": run}))
    return rules


_SKILL_RULES = _load_skill_rules(_SKILLS_DIR, _PROJECT_ROOT)


def _mk_call(name, args, seq):
    """构造 OpenAI tool_calls 结构。"""
    return {"id": "intent_%s_%d" % (name, seq), "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _valid(name, enabled):
    return name in enabled


def _load_json(raw, default=None):
    """宽容 JSON 解析，失败返回 default。"""
    if not raw:
        return default
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else default
    except Exception:
        return default


def _default_args(name):
    """Tier 2/Tier 3 命中时的最小可用参数。"""
    if name == "fs":
        return {"op": "list"}
    if name == "screen":
        return {"op": "shot"}
    if name == "browser":
        return {"cmd": "SNAPSHOT"}
    if name == "memo":
        return {"op": "search", "kw": ""}
    return {}


def _parse_t1(text, enabled):
    """Tier 1：标准 [[TOOL: name {json}]]。返回 (calls, spans)。

    不过滤 enabled：模型发明的工具名也透传，由 ToolBus 返回
    "工具未启用或不存在"错误回灌，让模型知道该工具不存在并自我纠正。
    """
    calls, spans = [], []
    for m in _T1_RE.finditer(text):
        name = m.group(1)
        args = _load_json(m.group(2)) or {}
        calls.append(_mk_call(name, args, len(calls)))
        spans.append(m.span())
    return calls, spans


def _parse_t2(text, enabled):
    """Tier 2：变体标记。返回 (calls, spans)。"""
    calls, spans = [], []

    # tool_call 标记包裹的 JSON
    for m in _T2_JSON_RE.finditer(text):
        d = _load_json(m.group(1))
        if not d:
            continue
        name = str(d.get("name", "")).strip()
        if not _valid(name, enabled):
            continue
        args = d.get("arguments")
        if args is None:
            args = d.get("args") or {}
        if isinstance(args, str):
            args = _load_json(args) or {}
        if not isinstance(args, dict):
            args = {}
        calls.append(_mk_call(name, args, len(calls)))
        spans.append(m.span())

    # [TOOL: fs] / [TOOL:fs {...}]
    for m in _T2_TOOL_RE.finditer(text):
        name = m.group(1)
        if not _valid(name, enabled):
            continue
        args = _load_json(m.group(2)) if m.group(2) else {}
        calls.append(_mk_call(name, args or {}, len(calls)))
        spans.append(m.span())

    # 【fs】/【截图】/【文件列表】
    for m in _T2_BRACKET_RE.finditer(text):
        raw = m.group(1).strip()
        name = TOOL_ALIASES.get(raw)
        if not name or not _valid(name, enabled):
            continue
        calls.append(_mk_call(name, _default_args(name), len(calls)))
        spans.append(m.span())

    return calls, spans


def _parse_t3(text, enabled, seen):
    """Tier 3：自然语言意图。同一工具整个任务只触发一次（seen 去重）。

    签名按工具名而非短语：措辞重叠（"桌面上有什么"/"有什么文件"）
    会绕过短语级签名，按工具去重才能保证一次任务只助推一次。
    """
    for phrases, name, args in _INTENT_RULES + _SKILL_RULES:
        if not _valid(name, enabled):
            continue
        sig = "t3:" + name
        if sig in seen:
            continue
        if any(p in text for p in phrases):
            seen.add(sig)
            return [_mk_call(name, dict(args), 0)]
    return []


def _strip_spans(text, spans):
    """按 span（从后往前）删除已解析标记，保留干净正文。"""
    for s, e in sorted(spans, reverse=True):
        text = text[:s] + text[e:]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract(text, enabled, seen=None):
    """解析模型输出，按置信度从高到低逐层尝试。

    text:    模型正文（done 时的完整 content）
    enabled: 已启用工具名列表
    seen:    Tier3 已触发签名集合（跨轮去重，调用方持有）

    返回 (calls, cleaned_text, tier)：
        calls        OpenAI tool_calls 结构列表（可能为空）
        cleaned_text 剥离调用标记后的正文（可直接展示）
        tier         0=无调用 1=标准协议 2=变体标记 3=自然语言意图
    """
    if not text:
        return [], "", 0
    enabled = set(enabled or [])
    seen = seen if seen is not None else set()

    calls, spans = _parse_t1(text, enabled)
    if calls:
        return calls, _strip_spans(text, spans), 1

    calls, spans = _parse_t2(text, enabled)
    if calls:
        return calls, _strip_spans(text, spans), 2

    calls = _parse_t3(text, enabled, seen)
    if calls:
        return calls, text.strip(), 3

    return [], text, 0
