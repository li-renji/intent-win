# -*- coding: utf-8 -*-
"""自研语义快照层：把浏览器页面翻译成 LLM 可用的结构化文本。

这是 MyBrowser 的核心资产。设计目标：

1. **省 token**：只发语义节点（可交互元素 + 关键正文），不发完整 DOM、不发 HTML。
   参照业界实测：AX 树约 2k-5k token/页，纯 DOM 快照可达 50k+。

2. **稳定引用**：给元素分配 ref（如 `e12`），后续动作按 ref 定位，
   不依赖 DOM 下标 —— 页面刷新后 ref 依然有效，彻底解决「下标错位」问题。

3. **增量差分**：两次快照之间只报告变化，token 成本随交互步数近似线性而非平方。

4. **角色化描述**：用 role/name/value 而不是 CSS 选择器，模型判断更准，
   且不受前端重构影响。

对比 INTENT-win 现有的 protocol.py 快照（querySelectorAll 下标 + innerText 拼接）：
  - 下标 → ref：页面刷新不再错位
  - innerText → AX 树：噪声大幅减少，文本结构清晰
  - 全量 → 差分：多步交互的 token 成本骤降
"""
import json

# 无障碍角色 -> 中文说明 + 是否可交互
_ROLE_CN = {
    "button": ("按钮", True),
    "link": ("链接", True),
    "textbox": ("输入框", True),
    "searchbox": ("搜索框", True),
    "checkbox": ("复选框", True),
    "radio": ("单选框", True),
    "combobox": ("下拉框", True),
    "listbox": ("列表框", True),
    "option": ("选项", True),
    "menuitem": ("菜单项", True),
    "tab": ("标签页", True),
    "slider": ("滑块", True),
    "spinbutton": ("数字框", True),
    "switch": ("开关", True),
    "heading": ("标题", False),
    "paragraph": ("段落", False),
    "StaticText": ("文本", False),
    "InlineTextBox": ("文本", False),
    "image": ("图片", False),
    "table": ("表格", False),
    "row": ("行", False),
    "cell": ("单元格", False),
    "columnheader": ("列头", False),
    "rowheader": ("行头", False),
    "list": ("列表", False),
    "listitem": ("列表项", False),
    "banner": ("页眉", False),
    "navigation": ("导航", False),
    "main": ("主体", False),
    "contentinfo": ("页脚", False),
    "form": ("表单", False),
    "article": ("文章", False),
    "separator": ("分隔线", False),
    "RootWebArea": ("页面", False),
    "none": ("节点", False),
    "generic": ("容器", False),
    "group": ("分组", False),
    "region": ("区域", False),
}

# 这些角色即使没有名字也值得报告（结构性信息）
_STRUCTURAL = ("RootWebArea", "heading", "table", "row", "list", "listitem",
               "form", "navigation", "banner", "main", "contentinfo", "article")


def _node_name(n):
    """取节点的可访问名。"""
    name = n.get("name")
    if isinstance(name, dict):
        name = name.get("value")
    return (name or "").strip()


def _node_value(n):
    v = n.get("value")
    if isinstance(v, dict):
        v = v.get("value")
    return v


def _node_role(n):
    r = n.get("role")
    if isinstance(r, dict):
        r = r.get("value")
    return r or "none"


def _short_url(url, limit=90):
    """缩短 URL：中间省略。真实浏览器地址栏也是这么做的。

    带大量 query 的 URL（尤其含签名参数的验证码页）能轻易吃掉上千字符，
    而这些字符对模型判断页面毫无价值。
    """
    if not url or len(url) <= limit:
        return url
    head, sep, tail = url.partition("?")
    if sep and len(tail) > 20:
        # query 太长：只保留首个参数名
        first = tail.split("&", 1)[0]
        k = first.split("=", 1)[0]
        return "%s?%s=…" % (head, k)
    if len(url) > limit:
        return url[:limit - 1] + "…"
    return url


class Snapshot:
    """一次页面快照的结果。持有 ref -> backendNodeId 映射，供后续动作定位。"""

    def __init__(self, page_refs=None):
        self.refs = page_refs or {}      # ref -> backendDOMNodeId
        self.url = ""
        self.title = ""
        self.text = ""                   # 渲染后的紧凑文本
        self.elements = []               # [(ref, role_cn, name)]
        self.delta = None                # 相对上次的差异描述
        self.raw_node_count = 0

    def to_dict(self):
        return {
            "url": self.url,
            "title": self.title,
            "elements": len(self.elements),
            "delta": self.delta,
        }


class Snapshotter:
    """生成快照。持有上一次状态以计算差分。"""

    def __init__(self, session):
        self.session = session
        self._prev_refs = {}
        self._prev_text_hash = None
        self._prev_elements = {}

    def capture(self, max_chars=3500, max_elements=60, diff=True):
        """抓取当前页面快照。"""
        ax = self.session.send("Accessibility.getFullAXTree")
        nodes = ax.get("nodes", [])

        # 建立 nodeId -> node 索引，父子关系要靠 childIds
        by_id = {n["nodeId"]: n for n in nodes if "nodeId" in n}

        # 收集可交互 + 结构性节点
        picked = []
        for n in nodes:
            role = _node_role(n)
            cn, interactive = _ROLE_CN.get(role, (role, False))
            name = _node_name(n)
            value = _node_value(n)
            backend = n.get("backendDOMNodeId")
            if backend is None:
                continue
            if not interactive and role not in _STRUCTURAL:
                continue
            if not name and not interactive and role not in ("heading", "RootWebArea"):
                continue
            picked.append({
                "backend": backend,
                "role": role,
                "cn": cn,
                "name": name,
                "value": value,
                "interactive": interactive,
                "node": n,
            })

        # 文本节点：取 StaticText / InlineTextBox 的内容，去重后拼正文
        text_parts = []
        seen_text = set()
        for n in nodes:
            role = _node_role(n)
            if role not in ("StaticText", "InlineTextBox"):
                continue
            v = _node_value(n)
            if not isinstance(v, str):
                continue
            v = " ".join(v.split())
            if not v or v in seen_text:
                continue
            seen_text.add(v)
            text_parts.append(v)

        body = "\n".join(text_parts)

        # 分配 ref：优先用 backendNodeId 保持跨次稳定
        refs = {}
        for item in picked:
            backend = item["backend"]
            ref = "e%d" % (backend % 100000)
            # 同 backend 重复则加后缀
            r, n = ref, 1
            while r in refs and refs[r] != backend:
                n += 1
                r = "%s_%d" % (ref, n)
            refs[r] = backend
            item["ref"] = r

        # 组装元素表（只给交互元素编号，静态内容进正文）
        lines = []
        elements = []
        idx = 1
        for item in picked:
            if not item["interactive"]:
                continue
            desc = item["name"]
            if item["value"] and str(item["value"]) not in desc:
                desc = "%s = %s" % (desc, item["value"])
            lines.append("%d. [%s] %s  → %s" % (idx, item["cn"], desc, item["ref"]))
            elements.append((item["ref"], item["cn"], item["name"]))
            idx += 1
            if idx > max_elements:
                break

        # 差分计算
        delta = None
        if diff:
            delta = self._compute_delta(elements, body)

        url = self.session.current_url()
        title = self.session.title()

        header = "[页面] %s | %s" % (title, _short_url(url))
        if delta:
            header += "  (%s)" % delta
        out_lines = [header]
        if lines:
            out_lines.append("[可交互元素]")
            out_lines.extend(lines)
        if body:
            out_lines.append("[正文]")
            out_lines.append(body[:max_chars])

        text = "\n".join(out_lines)
        if len(text) > max_chars + 1500:
            text = text[:max_chars + 1500] + "...(truncated)"

        snap = Snapshot(refs)
        snap.url = url
        snap.title = title
        snap.text = text
        snap.elements = elements
        snap.delta = delta
        snap.raw_node_count = len(nodes)

        self._prev_refs = refs
        self._prev_elements = dict((e[0], e) for e in elements)
        return snap

    def _compute_delta(self, elements, body):
        """计算相对上次快照的变化。"""
        if not self._prev_elements:
            return None
        cur = dict((e[0], e) for e in elements)
        added = [k for k in cur if k not in self._prev_elements]
        removed = [k for k in self._prev_elements if k not in cur]
        changed = [k for k in cur
                   if k in self._prev_elements and cur[k][2] != self._prev_elements[k][2]]
        if not (added or removed or changed):
            return "页面无变化"
        parts = []
        if added:
            parts.append("新增%d项" % len(added))
        if removed:
            parts.append("移除%d项" % len(removed))
        if changed:
            parts.append("变更%d项" % len(changed))
        return "较上次快照：" + "、".join(parts)

    def resolve_ref(self, ref):
        """ref -> backendNodeId。找不到时返回 None。"""
        return self._prev_refs.get(ref)

    def describe_ref(self, ref):
        e = self._prev_elements.get(ref)
        if not e:
            return None
        return {"ref": ref, "role": e[1], "name": e[2]}
