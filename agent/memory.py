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
``{id, kind, text, tags, created_at, updated_at, hits, source, layer}``

分层（借鉴 TencentDB Agent Memory 的 L1/L2/L3 抽象，纯本地实现）
----------------------------------------------------------------
- layer=1（L1 Atom）：原子事实/偏好，由 auto_capture 或显式 add 写入；
  对应 TencentDB 的 L1（从对话抽取的事实、偏好、约束）。
- layer=2（L2 Scenario）：任务 / 项目级场景卡，由 synthesize() 把同 tag
  的多条 L1 聚合而成；对应 TencentDB 的 L2（围绕场景组织的知识块）。
- layer=3（L3 Persona）：长期画像，由高频命中 / 强偏好演化而来；
  对应 TencentDB 的 L3（长期用户画像）。

由此形成 L1→L2→L3 的生长骨架：原子事实沉淀为场景，场景稳定后
收敛为画像。recall 时按 L3→L2→L1 的优先级注入，保持字符预算可控。

召回排序采用 RRF（Reciprocal Rank Fusion，k=60）：将“关键词匹配”
与“bigram 语义相似度”两路独立排序后做倒数排名融合，消除词频饱和
导致的长尾压制——记忆量越大越明显（见 TencentDB 的 search-utils.ts）。
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
RRF_K = 60                  # RRF 融合常数（与 TencentDB 一致，越高越平滑）
SYNTHESIZE_TAG_MIN = 3      # L1 聚合为 L2 所需的最小同 tag 命中次数

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

    def add(self, text, kind="fact", tags=None, source="manual", layer=1):
        """写入一条记忆；与已有条目过于相似则合并。返回 (ok, 说明)。

        layer：1=L1 原子事实 / 2=L2 场景卡 / 3=L3 画像（默认 1）。
        """
        text = " ".join(str(text or "").split())[:MAX_TEXT]
        if not text:
            return False, "记忆内容为空"
        kind = kind if kind in KIND_LABEL else "fact"
        layer = layer if layer in (1, 2, 3) else 1
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
                if layer > int(it.get("layer", 1)):   # 仅允许向上聚合层级
                    it["layer"] = layer
                if tags:
                    merged = list(dict.fromkeys(list(it.get("tags") or []) + tags))
                    it["tags"] = merged[:8]
                self._save(items)
                return True, "已合并到已有记忆（相似度 %.0f%%）" % (best_sim * 100)
            item = {"id": "m_%d_%d" % (now, len(items) + 1), "kind": kind,
                    "text": text, "tags": tags, "created_at": now,
                    "updated_at": now, "hits": 1, "source": source,
                    "layer": layer}
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

    def list(self, kind=None, layer=None, limit=200):
        with self._lock:
            items = self._load()
        if kind:
            items = [i for i in items if i.get("kind") == kind]
        if layer is not None:
            items = [i for i in items if int(i.get("layer", 1)) == int(layer)]
        items.sort(key=lambda i: (int(i.get("updated_at", 0)), int(i.get("created_at", 0))),
                   reverse=True)
        return items[:max(1, min(int(limit or 200), 1000))]

    def l3_view(self, limit=20, max_chars=500):
        """取出全部 L3 画像条目，拼成可注入 system prompt 的文本。无则返回空串。"""
        items = self.list(layer=3, limit=limit)
        if not items:
            return ""
        lines = ["[用户画像 L3]"]
        used = 0
        for i in items:
            t = str(i.get("text", ""))
            if used + len(t) > max_chars:
                break
            lines.append("- " + t)
            used += len(t)
        return "\n".join(lines)

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

    # ---------- RRF 融合 ----------

    @staticmethod
    def _rrf_merge(ranked_lists, k=RRF_K, top=None):
        """倒数排名融合：把多路排序结果融成一路，消除词频饱和。

        每个 item 的 RRF 分 = Σ 1/(k + rank + 1)；出现在多路的分数累加。
        返回 [(rrf_score, item), ...] 按分降序。
        """
        pool = {}
        for rlist in ranked_lists:
            for rank, it in enumerate(rlist):
                uid = it.get("id") or id(it)
                sc = 1.0 / (k + rank + 1)
                if uid in pool:
                    pool[uid][0] += sc
                else:
                    pool[uid] = [sc, it]
        merged = sorted(pool.values(), key=lambda x: x[0], reverse=True)
        if top:
            merged = merged[:top]
        return merged

    def recall(self, query, limit=6, max_chars=RECALL_MAX_CHARS):
        """按当前提问召回最相关的记忆（用于回注 system prompt）。

        两路独立排序后用 RRF 融合：
          - 路 A：关键词/别名命中（原有的 overlap + tag + 别名桥接）
          - 路 B：bigram 语义相似度（Jaccard，抗错别字 / 同义改写）
        再按 layer 优先级（L3→L2→L1）微调，最后受字符预算约束。
        """
        q_terms = _expand_query(query)
        if not q_terms:
            return []
        # 「你还记得我吗」这类提问：即使关键词没重合，也召回全部候选
        global_hit = bool(_RECALL_ANY.search(str(query or "")))
        kind_bonus = {"profile": 2.0, "preference": 1.5, "task": 0.6, "fact": 0.8}
        now = int(time.time())
        with self._lock:
            items = self._load()

        # 路 A：关键词打分（与现网逻辑一致）
        a_ranked, b_ranked = [], []
        a_scored = []
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
            a_scored.append((score, it))
        a_scored.sort(key=lambda x: x[0], reverse=True)
        a_ranked = [it for _s, it in a_scored[:max(1, min(int(limit * 3 or 18), 60))]]  # 放大候选集供融合

        # 路 B：bigram 语义相似度（不依赖关键词是否重叠，覆盖改写 / 错别字）
        if not global_hit:
            b_scored = []
            for it in items:
                sim = _similar(query, str(it.get("text", "")))
                if sim < 0.18:
                    continue
                b_scored.append((sim, it))
            b_scored.sort(key=lambda x: x[0], reverse=True)
            b_ranked = [it for _s, it in b_scored[:max(1, min(int(limit * 3 or 18), 60))]]

        if not a_ranked and not b_ranked:
            return []

        fused = self._rrf_merge([r for r in (a_ranked, b_ranked) if r], top=max(1, min(int(limit * 2), 20)))

        # layer 优先级微调：L3→L2→L1 整体前置（高层记忆更值得回注）
        def _layer_key(x):
            it = x[1]
            lyr = int(it.get("layer", 1))
            return (2 if lyr >= 3 else 1 if lyr == 2 else 0, x[0])
        fused.sort(key=_layer_key, reverse=True)

        picked, used = [], 0
        for _rrf, it in fused:
            text = str(it.get("text", ""))
            if used + len(text) > max_chars:
                continue
            picked.append(it)
            used += len(text)
            if len(picked) >= limit:
                break
        return picked

    def synthesize(self, min_hits=SYNTHESIZE_TAG_MIN):
        """L1→L2 聚合：把同 tag、高频命中的多条 L1 自动提炼为一条 L2 场景卡。

        设计意图（对应 TencentDB 的 L2 Scenario）：原子事实沉淀成场景知识块，
        下次相关任务直接加载场景而非逐条事实。幂等：已存在的同 tag L2 会跳过。
        返回本次新生成的 L2 条目数。
        """
        with self._lock:
            items = self._load()
            l1 = [it for it in items if int(it.get("layer", 1)) == 1 and it.get("tags")]
            by_tag = {}
            for it in l1:
                for tg in it.get("tags", []):
                    by_tag.setdefault(tg, []).append(it)
            # 已存在的 L2 覆盖的源记忆集合（跨调用去重，避免重复造卡）
            existing_heads = set()
            for it in items:
                if int(it.get("layer", 1)) == 2 and it.get("sources"):
                    existing_heads.add(frozenset(it["sources"]))
            seen_heads = set()      # 同一次调用内去重
            now = int(time.time())
            made = 0
            for tg, group in by_tag.items():
                if len(group) < min_hits:
                    continue
                # 命中频次高 / 更新近的优先汇总
                group = sorted(group, key=lambda g: (int(g.get("hits", 1)),
                                                    int(g.get("updated_at", 0))),
                             reverse=True)
                head = group[:5]
                head_ids = frozenset(h.get("id") for h in head)
                # 已被已有 L2（子集）覆盖，或本次调用已汇过，跳过
                if head_ids in seen_heads or any(head_ids <= e for e in existing_heads):
                    continue
                seen_heads.add(head_ids)
                existing_heads.add(head_ids)
                summary = "；".join(str(g.get("text", ""))[:80] for g in head)
                item = {"id": "m_%d_l2_%d" % (now, made + 1),
                        "kind": "fact", "text": "[场景:%s] %s" % (tg, summary),
                        "tags": [tg], "created_at": now, "updated_at": now,
                        "hits": sum(int(g.get("hits", 1)) for g in head),
                        "source": "synthesized", "layer": 2,
                        "sources": list(head_ids)}
                items.append(item)
                made += 1
            if made:
                self._save(items)
            return made

    def synthesize_l3(self, min_hits=4):
        """L2→L3 收敛：把高频命中 / 强偏好的 L1/L2 提炼为「用户画像」（L3）。

        设计意图（对应 TencentDB 的 L3 Persona）：原子事实沉淀成场景（L2），
        场景稳定后再收敛为「你是谁」的画像（L3）。这是阿觉「通过记忆认识你」
        的关键一层。

        实现（纯本地、无 LLM 依赖、幂等）：
          - 候选：L2 全部 + L1 中 hits>=min_hits 的强记忆；
          - 按主导 tag 聚类，每组聚合前若干条成一条画像陈述；
          - 经 add() 写入 layer=3（相似度>=0.72 自动合并，不重复造像）。

        返回本次新生成的 L3 条数。
        """
        with self._lock:
            items = self._load()
            cands = [it for it in items
                     if int(it.get("layer", 1)) >= 2
                     or int(it.get("hits", 1)) >= min_hits]
            if not cands:
                return 0
            # 按主导 tag 聚类
            by_tag = {}
            for it in cands:
                tags = it.get("tags") or []
                key = tags[0] if tags else "_general"
                by_tag.setdefault(key, []).append(it)
            # 已有 L3 的文本集合（去重，避免重复造像）
            existing = set(str(i.get("text", "")) for i in items
                           if int(i.get("layer", 1)) == 3)
            now = int(time.time())
            made = 0
            for tg, group in by_tag.items():
                if len(group) < 2 and tg != "_general":
                    continue
                group = sorted(group, key=lambda g: (int(g.get("hits", 1)),
                                                    int(g.get("updated_at", 0))),
                              reverse=True)
                head = group[:6]
                summary = "；".join(str(g.get("text", ""))[:80] for g in head)
                text = ("[画像:%s] %s" % (tg, summary))[:MAX_TEXT]
                if text in existing:
                    continue
                # 经 add 写入 L3：相似则合并，不重复
                ok, _ = self.add(text, kind="profile", tags=[tg],
                                 source="synthesized", layer=3)
                if ok:
                    made += 1
                    existing.add(text)
            return made

    def inject(self, query, limit=6, max_chars=RECALL_MAX_CHARS):
        """生成注入 system prompt 的记忆文本（无命中返回空串）。"""
        picked = self.recall(query, limit=limit, max_chars=max_chars)
        if not picked:
            return ""
        lines = []
        for it in picked:
            label = KIND_LABEL.get(it.get("kind"), "事实")
            lyr = int(it.get("layer", 1))
            tag = (" L%d" % lyr) if lyr >= 2 else ""
            lines.append("- [%s%s] %s" % (label, tag, str(it.get("text", ""))[:MAX_TEXT]))
        return "长期记忆（与本次提问相关的已知信息）：\n" + "\n".join(lines)

    def stats(self):
        items = self._load()
        by_kind = {}
        by_layer = {1: 0, 2: 0, 3: 0}
        for it in items:
            by_kind[it.get("kind", "fact")] = by_kind.get(it.get("kind", "fact"), 0) + 1
            by_layer[int(it.get("layer", 1))] = by_layer.get(int(it.get("layer", 1)), 0) + 1
        return {"count": len(items), "by_kind": by_kind, "by_layer": by_layer,
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
