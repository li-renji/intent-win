# -*- coding: utf-8 -*-
"""长期记忆系统：跨会话持久化的结构化记忆（用户画像 / 偏好 / 事实 / 待办）。

与现有组件的分工
----------------
- ``history.py``  TurnFact → 单会话内的对话事实摘要（短期，切会话即换）
- ``memory.py``  本模块  → 跨会话的长期记忆（"我记得你是谁、你爱吃什么"）
- ``kb.py``               → 文档内容索引（外部知识，不是用户个人记忆）

设计要点
--------
1. **结构化而非一锅汤**：每条记忆带 kind（profile/preference/fact/task）与标签，
   便于按类型取用与界面分类展示；
2. **写入即去重**：新记忆与已有条目做 bigram 相似度比较，超过阈值视为同一条 →
   合并（更新原文、hits+1），避免"记住我喜欢深色"被反复写成好几条；
3. **纯本地、零依赖**：JSON 落盘（原子写：tmp + os.replace），不联网、不上云；
4. **召回省 token**：按提问关键词打分取 top-K，只回注几百字，
   不把整个记忆库灌进 system prompt；
5. **可控自动捕获**：只按明确语句模式（记住/我喜欢/我叫…）自动入库，
   宁缺毋滥，用户也能在界面里随时删。

存储：``memories/memory.json``，条目结构：
``{id, kind, text, tags, created_at, updated_at, hits, source}``
"""

import json
import os
import re
import threading
import time

MEM_DIR = "memories"
MEM_FILE = "memory.json"
MAX_ENTRIES = 2000          # 记忆条数上限（防无限膨胀）
MAX_TEXT = 400              # 单条记忆长度上限
SIM_THRESHOLD = 0.72        # bigram 相似度阈值：超过即视为同一条，合并而非新增
RECALL_MAX_CHARS = 700      # 单轮回注的记忆总字符预算

KIND_LABEL = {
    "profile": "档案",
    "preference": "偏好",
    "fact": "事实",
    "task": "待办",
}
KIND_ORDER = ["profile", "preference", "fact", "task"]

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9_]+")


# ---------------------------------------------------------------- 分词工具

def _terms(text):
    """中英混合分词：中文取 2-gram，英文/数字取整词。"""
    t = str(text or "").lower()
    terms = set(_WORD.findall(t))
    cjk = _CJK.findall(t)
    for i in range(len(cjk) - 1):
        terms.add(cjk[i] + cjk[i + 1])
    if len(cjk) == 1:
        terms.add(cjk[0])
    return terms


def _bigrams(text):
    """字符 bigram 集合（去空白），用于相似度比较。"""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t:
        return set()
    return set(t[i:i + 2] for i in range(len(t) - 1)) or set([t])


def _similar(a, b):
    """Jaccard 相似度（基于 bigram）。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


# ---------------------------------------------------------------- 自动捕获

# 只匹配明确的"要记住"语句，避免把普通闲聊全存成记忆
_CAPTURE_RULES = [
    (re.compile(r"(?:记住|记一下|记下|备忘一下|别忘了|不要忘)[:：,，]?\s*(.{2,200})"), "fact"),
    (re.compile(r"我(?:的名字)?叫\s*([^\s，。,.!！?？]{1,24})"), "profile"),
    (re.compile(r"我(?:的)?(?:生日|年龄|身高|体重|职业|邮箱|手机号|地址|公司|学校|团队)(?:是|为|：|:)\s*(.{1,60})"), "profile"),
    (re.compile(r"我(?:很|挺|比较)?喜欢\s*(.{1,60})"), "preference"),
    (re.compile(r"我(?:很|挺|比较)?不喜欢\s*(.{1,60})"), "preference"),
    (re.compile(r"我(?:讨厌|反感|受不了)\s*(.{1,60})"), "preference"),
    (re.compile(r"(?:以后|从今以后|今后|下次)(?:都|请)?\s*(.{2,80})"), "preference"),
]


_RECALL_ANY = re.compile(
    r"(记得|记住|我说过|之前|上次|还记得|我是谁|我叫什么|关于我|我的信息|我的偏好|我是怎样|我是谁)")

# 提问词 ↔ 记忆用词的轻量桥接（不依赖向量模型，避免额外依赖与体积）
# 例："我的名字是什么" → 命中"用户名叫任继"；"我的项目在哪" → 命中"目录/路径"
_QUERY_ALIASES = {
    "名字": ["名叫", "姓名", "称呼"],
    "叫什么": ["名叫", "姓名"],
    "哪里": ["目录", "路径", "位置", "地址", "在哪"],
    "在哪": ["目录", "路径", "位置"],
    "工作": ["职业", "公司", "团队", "岗位"],
    "生日": ["生日"],
    "喜欢": ["偏好", "喜欢", "喜爱"],
    "爱好": ["喜欢", "偏好"],
    "讨厌": ["不喜欢", "讨厌", "反感", "偏好"],
    "习惯": ["偏好", "喜欢", "习惯"],
    "是什么": ["是", "名叫", "叫"],
    "哪些": ["的"],
}


def _expand_query(query):
    """把提问词扩展成检索词集合（含别名桥接）。"""
    q = str(query or "")
    terms = _terms(q)
    for key, adds in _QUERY_ALIASES.items():
        if key in q:
            for a in adds:
                terms |= _terms(a)
    return terms


def auto_capture(text):
    """从用户话语里挑出值得长期记住的语句。

    返回 ``[(记忆内容, kind), ...]``；命中不了返回空列表（宁缺毋滥）。
    """
    s = str(text or "").strip()
    if not s or len(s) > 600:
        return []
    out = []
    for pat, kind in _CAPTURE_RULES:
        m = pat.search(s)
        if not m:
            continue
        body = m.group(1).strip().strip("，。,.!！?？;；")
        if len(body) < 2:
            continue
        # 过滤掉"记住这个""记一下笔记"这类无信息量的空指令
        if body in ("这个", "那个", "一下", "东西", "这件事", "它"):
            continue
        item = body if kind != "profile" else body
        out.append((item[:MAX_TEXT], kind))
        if len(out) >= 2:
            break
    return out


# ---------------------------------------------------------------- 记忆存储

class MemoryStore:
    """长期记忆仓库：JSON 落盘，线程安全。"""

    def __init__(self, project_root):
        self.root = project_root
        self.dir = os.path.join(project_root, MEM_DIR)
        self.path = os.path.join(self.dir, MEM_FILE)
        self._lock = threading.RLock()

    # ---------- 读写 ----------

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return []
        if isinstance(data, dict):
            items = data.get("items")
        else:
            items = data
        if not isinstance(items, list):
            return []
        return [i for i in items if isinstance(i, dict) and i.get("text")]

    def _save(self, items):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": items},
                      f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self.path)

    # ---------- 增删改查 ----------

    def add(self, text, kind="fact", tags=None, source="manual"):
        """写入一条记忆；与已有条目过于相似则合并。返回 (ok, 说明)。"""
        text = " ".join(str(text or "").split())[:MAX_TEXT]
        if not text:
            return False, "记忆内容为空"
        kind = kind if kind in KIND_LABEL else "fact"
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()][:8]
        now = int(time.time())
        with self._lock:
            items = self._load()
            best_i, best_sim = -1, 0.0
            for i, it in enumerate(items):
                s = _similar(text, it.get("text", ""))
                if s > best_sim:
                    best_i, best_sim = i, s
            if best_i >= 0 and best_sim >= SIM_THRESHOLD:
                it = items[best_i]
                it["text"] = text
                it["hits"] = int(it.get("hits", 0)) + 1
                it["updated_at"] = now
                if kind != it.get("kind"):
                    it["kind"] = kind
                if tags:
                    merged = list(dict.fromkeys(list(it.get("tags") or []) + tags))
                    it["tags"] = merged[:8]
                self._save(items)
                return True, "已合并到已有记忆（相似度 %.0f%%）" % (best_sim * 100)
            item = {"id": "m_%d_%d" % (now, len(items) + 1), "kind": kind,
                    "text": text, "tags": tags, "created_at": now,
                    "updated_at": now, "hits": 1, "source": source}
            items.append(item)
            if len(items) > MAX_ENTRIES:      # 超量丢最旧
                items = items[-MAX_ENTRIES:]
            self._save(items)
            return True, "已记住"

    def remove(self, mid):
        with self._lock:
            items = self._load()
            rest = [i for i in items if i.get("id") != mid]
            if len(rest) == len(items):
                return False, "未找到该记忆"
            self._save(rest)
            return True, "已删除"

    def clear(self, kind=None):
        with self._lock:
            items = self._load()
            if kind:
                rest = [i for i in items if i.get("kind") != kind]
            else:
                rest = []
            n = len(items) - len(rest)
            self._save(rest)
            return n

    def list(self, kind=None, limit=200):
        with self._lock:
            items = self._load()
        if kind:
            items = [i for i in items if i.get("kind") == kind]
        items.sort(key=lambda i: (int(i.get("updated_at", 0)), int(i.get("created_at", 0))),
                   reverse=True)
        return items[:max(1, min(int(limit or 200), 1000))]

    # ---------- 检索 ----------

    def search(self, kw, limit=20):
        """按关键词检索记忆（子串 + 分词命中，取更相关者）。"""
        kw = str(kw or "").strip()
        if not kw:
            return []
        low = kw.lower()
        terms = _terms(kw)
        scored = []
        with self._lock:
            items = self._load()
        for it in items:
            text = str(it.get("text", ""))
            tl = text.lower()
            score = 0.0
            if low in tl:
                score += 3.0
            hit = len(terms & _terms(text))
            score += hit
            if it.get("tags") and any(str(t).lower() in low for t in it.get("tags", [])):
                score += 2.0
            if score > 0:
                scored.append((score, int(it.get("updated_at", 0)), it))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [it for _s, _t, it in scored[:max(1, min(int(limit or 20), 100))]]

    def recall(self, query, limit=6, max_chars=RECALL_MAX_CHARS):
        """按当前提问召回最相关的记忆（用于回注 system prompt）。

        只回少量高价值内容（档案/偏好权重更高），并受总字符预算约束。
        """
        q_terms = _expand_query(query)
        if not q_terms:
            return []
        # 「你还记得我吗」这类提问：即使关键词没重合，也召回最近的记忆
        global_hit = bool(_RECALL_ANY.search(str(query or "")))
        kind_bonus = {"profile": 2.0, "preference": 1.5, "task": 0.6, "fact": 0.8}
        scored = []
        with self._lock:
            items = self._load()
        now = int(time.time())
        for it in items:
            text = str(it.get("text", ""))
            t_terms = _terms(text)
            if not t_terms:
                continue
            overlap = len(q_terms & t_terms)
            if overlap == 0 and not global_hit:
                continue        # 与提问无关的记忆不回注（省 token、避免干扰）
            score = overlap / float(len(q_terms))
            score += kind_bonus.get(it.get("kind"), 0.5) * 0.15
            age_days = max(0, (now - int(it.get("updated_at", 0)))) / 86400.0
            score += 0.15 / (1.0 + age_days / 30.0)     # 越新略微越优先
            score += min(float(it.get("hits", 1)), 5) * 0.02
            if score <= 0.12:
                continue
            scored.append((score, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        picked, used = [], 0
        for _s, it in scored[:max(1, min(int(limit or 6), 20))]:
            text = str(it.get("text", ""))
            if used + len(text) > max_chars:
                continue
            picked.append(it)
            used += len(text)
        return picked

    def inject(self, query, limit=6, max_chars=RECALL_MAX_CHARS):
        """生成注入 system prompt 的记忆文本（无命中返回空串）。"""
        picked = self.recall(query, limit=limit, max_chars=max_chars)
        if not picked:
            return ""
        lines = []
        for it in picked:
            label = KIND_LABEL.get(it.get("kind"), "事实")
            lines.append("- [%s] %s" % (label, str(it.get("text", ""))[:MAX_TEXT]))
        return "长期记忆（与本次提问相关的已知信息）：\n" + "\n".join(lines)

    def stats(self):
        items = self._load()
        by_kind = {}
        for it in items:
            k = it.get("kind", "fact")
            by_kind[k] = by_kind.get(k, 0) + 1
        return {"count": len(items), "by_kind": by_kind,
                "path": self.path,
                "chars": sum(len(str(i.get("text", ""))) for i in items)}


if __name__ == "__main__":
    import sys
    st = MemoryStore(os.getcwd())
    if "--add" in sys.argv:
        print(st.add(sys.argv[sys.argv.index("--add") + 1]))
    elif "--list" in sys.argv:
        for it in st.list():
            print("[%s] %s" % (KIND_LABEL.get(it.get("kind"), "?"), it.get("text")))
    else:
        print(json.dumps(st.stats(), ensure_ascii=False))
