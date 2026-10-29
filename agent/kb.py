# -*- coding: utf-8 -*-
"""知识库：① 全机文件索引（按文件名秒查路径） + ② 文档内容索引（正文分块检索）。

诉求背景：手机端 Agent 首次启动会扫描电脑建知识库；PC 端同样需要，
且「记忆」要能在设置里直接查看。

设计
----
1. **只索引不读内容**（第一层）：扫描只记录 路径/文件名/大小/修改时间，
   不打开文件内容 —— 快（几十万文件秒级）、不占内存、隐私可控；
2. **后台增量**：首次启动后台线程扫描，UI 不卡；索引超 24 小时才重扫；
3. **省 token 检索**：Agent 用 kb 工具按文件名检索，一次只回 top N 条
   紧凑路径，比让模型翻目录列表省 90% 以上 token；
4. **两端共用**：索引落盘 kb/index.json，经 PC 文件桥 /kb/search 暴露给
   手机端 qidian编辑器，电脑手机同一份知识库。

5. **内容索引**（第二层，本模块新增）：第一层只能告诉你"文件在哪"，
   回答不了"文件里写了什么"。这里把可读文档（txt/md/code/json…）的正文
   切块后建倒排索引，支持中文 2-gram 检索，让 Agent 能直接引用文档内容：
   - 存储：kb/content.json（docs 元信息 + 分块正文 + 倒排表）
   - 增量：按 mtime/size 判断文档是否变化，只重建变化过的块
   - 体量上限：MAX_DOCS / MAX_CONTENT_CHARS，避免索引无限膨胀
"""

import json
import math
import os
import re
import threading
import time

INDEX_VERSION = 1
MAX_ENTRIES = 40000          # 索引条数上限（防超大磁盘写爆索引文件）
SCAN_STALE_SECONDS = 24 * 3600   # 索引超过一天才重扫
USER_DIR_NAMES = ("Desktop", "Documents", "Downloads", "Pictures",
                  "Music", "Videos", "桌面", "文档", "下载")
# 扫描时跳过的目录（系统/缓存/依赖，进去了只会拖慢且没有检索价值）
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
    "$recycle.bin", "system volume information", "appdata", "windows",
    "winsxs", "temp", "tmp", ".cache", ".idea", ".vscode", ".gradle",
    "program files", "program files (x86)", "programdata",
}
# 常见无检索价值的扩展名（安装包/缓存/媒体缩略图）
SKIP_EXTS = {
    ".tmp", ".temp", ".log", ".cache", ".bak", ".swp", ".dmp", ".etl",
    ".lnk", ".url",
}

# ======================= 内容索引（第二层）相关常量 =======================
CONTENT_VERSION = 2
CONTENT_FILE = "content.json"
DOC_EXTS = {".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json",
            ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml",
            ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css",
            ".sql", ".log"}
MAX_DOCS = 800               # 最多索引多少份文档
MAX_DOC_BYTES = 2 * 1024 * 1024      # 单文档最大读取字节
MAX_CONTENT_CHARS = 2_500_000        # 全部正文总字符上限
CHUNK_CHARS = 600                    # 每块目标长度
CHUNK_OVERLAP = 80                   # 相邻块重叠（避免答案被切断）
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9_]+")


def _fmt_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fPB" % n


def default_roots():
    """默认扫描范围：用户常用目录（深扫）+ 各磁盘根（浅扫）+ 当前项目。"""
    home = os.path.expanduser("~")
    roots = []
    for name in USER_DIR_NAMES:
        for base in (home, os.path.join(home, "OneDrive")):
            p = os.path.join(base, name)
            if os.path.isdir(p):
                roots.append((p, 6))      # (路径, 最大深度)
    # 各固定磁盘根目录浅扫（C:/D:/E:/… 顶层 3 层，避免钻进 Windows 系统目录）
    for letter in "CDEFGH":
        p = letter + ":\\"
        if os.path.isdir(p):
            roots.append((p, 3))
    return roots


class KnowledgeBase:
    """文件索引 + 检索。索引结构：{"version", "scanned_at", "roots", "count", "files"}。

    files 每项: [name, dir, size, mtime]（紧凑数组，索引文件更小）
    """

    def __init__(self, project_root):
        self.root = project_root
        self.dir = os.path.join(project_root, "kb")
        self.index_path = os.path.join(self.dir, "index.json")
        self._lock = threading.Lock()
        self._scanning = False
        self._progress = None        # {"done": n, "total_est": n, "root": str} | None

    # ---------- 索引读写 ----------

    def _load(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def stats(self):
        idx = self._load()
        if not idx:
            return {"indexed": False, "count": 0, "scanned_at": 0,
                    "scanning": self._scanning, "progress": self._progress}
        return {"indexed": True, "count": idx.get("count", 0),
                "scanned_at": idx.get("scanned_at", 0),
                "roots": idx.get("roots", []),
                "scanning": self._scanning, "progress": self._progress}

    def is_stale(self):
        idx = self._load()
        if not idx:
            return True
        return (time.time() - float(idx.get("scanned_at", 0) or 0)) > SCAN_STALE_SECONDS

    # ---------- 扫描 ----------

    def scan(self, roots=None, progress_cb=None):
        """同步扫描（后台线程里跑）。返回统计 dict。"""
        if self._scanning:
            return {"ok": False, "error": "正在扫描中"}
        self._scanning = True
        self._progress = {"done": 0, "total_est": 0, "root": ""}
        t0 = time.time()
        try:
            roots = roots or default_roots()
            files = []
            seen_dirs = set()
            for root_path, max_depth in roots:
                self._progress["root"] = root_path
                self._walk(root_path, 1, max_depth, files, seen_dirs)
                if progress_cb:
                    try:
                        progress_cb(self._progress)
                    except Exception:
                        pass
                if len(files) >= MAX_ENTRIES:
                    break
            os.makedirs(self.dir, exist_ok=True)
            idx = {"version": INDEX_VERSION, "scanned_at": time.time(),
                   "roots": [r[0] for r in roots], "count": len(files),
                   "files": files}
            tmp = self.index_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.index_path)     # 原子替换，防写一半
            self._progress = None
            return {"ok": True, "count": len(files),
                    "seconds": round(time.time() - t0, 1)}
        finally:
            self._scanning = False
            self._progress = None

    def _walk(self, path, depth, max_depth, files, seen_dirs):
        if len(files) >= MAX_ENTRIES or path.lower() in seen_dirs:
            return
        seen_dirs.add(path.lower())
        try:
            entries = os.scandir(path)
        except OSError:
            return
        with entries:
            for e in entries:
                if len(files) >= MAX_ENTRIES:
                    return
                try:
                    if e.is_dir(follow_symlinks=False):
                        if depth < max_depth and e.name.lower() not in SKIP_DIRS \
                                and not e.name.startswith("."):
                            self._walk(e.path, depth + 1, max_depth, files, seen_dirs)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext in SKIP_EXTS:
                        continue
                    st = e.stat(follow_symlinks=False)
                    files.append([e.name, os.path.dirname(e.path),
                                  st.st_size, int(st.st_mtime)])
                except OSError:
                    continue

    def scan_async(self, progress_cb=None):
        """后台线程扫描（首次启动用，不卡 UI）。"""
        if self._scanning or not self.is_stale():
            return False
        t = threading.Thread(target=self.scan, kwargs={"progress_cb": progress_cb},
                             daemon=True)
        t.start()
        return True

    def ensure_index_async(self, progress_cb=None):
        """启动时调用：无索引或过期才后台重建；否则什么都不做。"""
        return self.scan_async(progress_cb)

    # ---------- 检索 ----------

    def search(self, kw, limit=15):
        """按文件名检索，返回紧凑结果行（一次调用只回 top N，极度省 token）。"""
        kw = str(kw or "").strip().lower()
        idx = self._load()
        if not idx or not kw:
            return []
        words = [w for w in kw.split() if w]
        if not words:
            return []
        scored = []
        for name, d, size, mtime in idx.get("files", []):
            low = name.lower()
            if not all(w in low for w in words):
                continue
            if low.startswith(words[0]):
                rank = 0
            elif words[0] in low:
                rank = 1
            else:
                rank = 2
            scored.append((rank, -mtime, name, d, size))
        scored.sort()
        out = []
        for rank, _neg, name, d, size in scored[:max(1, min(int(limit or 15), 30))]:
            out.append("%s — %s (%s)" % (name, d, _fmt_size(size)))
        return out

    # ==================== 内容索引（第二层）：正文分块 + 倒排检索 ====================

    # ---------- 文本读取 ----------

    @staticmethod
    def _read_text(path):
        """读取文档正文（尽力解码 utf-8 / gbk / utf-16），超限截断。"""
        try:
            size = os.path.getsize(path)
            if size > MAX_DOC_BYTES:
                return None
            raw = open(path, "rb").read(MAX_DOC_BYTES)
        except OSError:
            return None
        for enc in ("utf-8", "gbk", "utf-16", "latin-1"):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return None

    @staticmethod
    def _chunk_text(text):
        """按段落聚合成块（目标 CHUNK_CHARS，相邻块重叠 CHUNK_OVERLAP）。"""
        text = re.sub(r"\r\n", "\n", str(text or ""))
        paras = [p.strip() for p in text.split("\n") if p.strip()]
        chunks, cur = [], ""
        for p in paras:
            if len(p) > CHUNK_CHARS * 2:      # 超长行（代码/表格）硬切
                if cur:
                    chunks.append(cur)
                    cur = ""
                for i in range(0, len(p), CHUNK_CHARS):
                    chunks.append(p[i:i + CHUNK_CHARS])
                continue
            if len(cur) + len(p) + 1 > CHUNK_CHARS and cur:
                chunks.append(cur)
                tail = cur[-CHUNK_OVERLAP:] if len(cur) > CHUNK_OVERLAP else cur
                cur = tail + "\n" + p
            else:
                cur = (cur + "\n" + p) if cur else p
        if cur:
            chunks.append(cur)
        return [c for c in chunks if c.strip()]

    @staticmethod
    def _terms(text):
        """中英混合分词：中文 2-gram，英文/数字整词。"""
        t = str(text or "").lower()
        terms = set(_WORD.findall(t))
        cjk = _CJK.findall(t)
        for i in range(len(cjk) - 1):
            terms.add(cjk[i] + cjk[i + 1])
        if len(cjk) == 1:
            terms.add(cjk[0])
        return terms

    # ---------- 索引读写 ----------

    def _content_path(self):
        return os.path.join(self.dir, CONTENT_FILE)

    def _load_content(self):
        try:
            with open(self._content_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {"version": CONTENT_VERSION, "docs": {}, "chunks": {}, "inverted": {}}
        if not isinstance(data, dict) or data.get("version") != CONTENT_VERSION:
            return {"version": CONTENT_VERSION, "docs": {}, "chunks": {}, "inverted": {}}
        data.setdefault("docs", {})
        data.setdefault("chunks", {})
        data.setdefault("inverted", {})
        return data

    def _save_content(self, data):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self._content_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self._content_path())

    # ---------- 入库 ----------

    def ingest(self, paths=None, progress_cb=None, max_docs=None):
        """把文档正文切块入库。paths 为空则按文件名索引自动挑候选文档。

        max_docs：限制本次最多处理的文档数（自动入库时用小上限，避免启动时吃满 CPU/IO）。
        返回 {"ok", "added", "updated", "skipped", "chars"}。
        """
        limit = MAX_DOCS if max_docs is None else max(1, min(int(max_docs), MAX_DOCS))
        with self._lock:
            data = self._load_content()
            docs = data["docs"]
            chunks = data["chunks"]
            inverted = data["inverted"]

            if paths:
                targets = [os.path.abspath(p) for p in paths
                           if p and os.path.isfile(p)]
            else:
                idx = self._load()
                targets = []
                for name, d, size, mtime in (idx or {}).get("files", []):
                    ext = os.path.splitext(name)[1].lower()
                    if ext in DOC_EXTS and size <= MAX_DOC_BYTES:
                        targets.append(os.path.abspath(os.path.join(d, name)))
                targets = targets[:limit]

            added = updated = skipped = 0
            total_chars = sum(len(c) for arr in chunks.values() for c in arr)
            for path in targets:
                if len(docs) >= limit and path not in docs:
                    skipped += 1
                    continue
                ext = os.path.splitext(path)[1].lower()
                if ext not in DOC_EXTS:
                    skipped += 1
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    skipped += 1
                    continue
                old = docs.get(path)
                if old and old.get("mtime") == int(st.st_mtime) \
                        and old.get("size") == int(st.st_size):
                    skipped += 1
                    continue
                text = self._read_text(path)
                if not text or not text.strip():
                    skipped += 1
                    continue
                blocks = self._chunk_text(text)
                if not blocks:
                    skipped += 1
                    continue
                # 超总字符预算 → 丢弃最旧文档
                new_chars = sum(len(b) for b in blocks)
                if total_chars + new_chars > MAX_CONTENT_CHARS:
                    skipped += 1
                    continue
                # 移除该文档旧块，重建倒排
                for term, postings in list(inverted.items()):
                    kept = [p for p in postings if p[0] != path]
                    if kept:
                        inverted[term] = kept
                    else:
                        del inverted[term]
                chunks[path] = blocks
                docs[path] = {"mtime": int(st.st_mtime), "size": int(st.st_size),
                              "n": len(blocks)}
                for i, b in enumerate(blocks):
                    for term in self._terms(b):
                        inverted.setdefault(term, []).append([path, i])
                total_chars += new_chars
                if old:
                    updated += 1
                else:
                    added += 1
                if progress_cb:
                    try:
                        progress_cb({"done": added + updated, "path": path})
                    except Exception:
                        pass
            self._save_content(data)
            return {"ok": True, "added": added, "updated": updated,
                    "skipped": skipped, "chars": total_chars,
                    "docs": len(docs)}

    def ingest_async(self, paths=None, progress_cb=None):
        t = threading.Thread(target=self.ingest, args=(paths, progress_cb), daemon=True)
        t.start()
        return True

    # ---------- 正文检索 ----------

    def grep(self, kw, limit=8):
        """在已入库文档正文中检索，返回带出处的片段列表。"""
        kw = str(kw or "").strip()
        if not kw:
            return []
        with self._lock:
            data = self._load_content()
        chunks = data.get("chunks") or {}
        inverted = data.get("inverted") or {}
        if not chunks:
            return []
        q_terms = self._terms(kw)
        if not q_terms:
            return []
        scores = {}
        for term in q_terms:
            postings = inverted.get(term)
            if not postings:
                continue
            idf = 1.0 + 0.5 * math.log(
                max(1, len(chunks)) / float(len(postings)))
            for path, idx in postings:
                key = (path, idx)
                scores[key] = scores.get(key, 0.0) + idf
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out = []
        for (path, idx), sc in ranked[:max(1, min(int(limit or 8), 30))]:
            blocks = chunks.get(path) or []
            if idx >= len(blocks):
                continue
            body = blocks[idx]
            pos = body.lower().find(kw.lower())
            if pos < 0:
                # 整词没命中（中文多字词）→ 用分词命中位置定位
                for term in sorted(q_terms, key=len, reverse=True):
                    i = body.lower().find(term)
                    if i >= 0:
                        pos = i
                        break
            if pos < 0:
                pos = 0
            start = max(0, pos - 90)
            snippet = body[start:start + 320].replace("\n", " ").strip()
            name = os.path.basename(path)
            out.append({"path": path, "name": name, "chunk": idx,
                        "score": round(sc, 2), "snippet": snippet})
        return out

    def content_stats(self):
        data = self._load_content()
        docs = data.get("docs") or {}
        chunks = data.get("chunks") or {}
        chars = sum(len(c) for arr in chunks.values() for c in arr)
        terms = len(data.get("inverted") or {})
        return {"docs": len(docs), "chunks": sum(len(v) for v in chunks.values()),
                "chars": chars, "terms": terms,
                "path": self._content_path()}

    def remove_doc(self, path):
        """从内容索引中移除某文档（不动原文件）。"""
        path = os.path.abspath(path)
        with self._lock:
            data = self._load_content()
            if path not in (data.get("docs") or {}):
                return {"ok": False, "error": "该文档不在内容索引中"}
            data["docs"].pop(path, None)
            data.get("chunks", {}).pop(path, None)
            for term, postings in list((data.get("inverted") or {}).items()):
                kept = [p for p in postings if p[0] != path]
                if kept:
                    data["inverted"][term] = kept
                else:
                    data["inverted"].pop(term, None)
            self._save_content(data)
            return {"ok": True}


if __name__ == "__main__":
    import sys
    kb = KnowledgeBase(os.getcwd())
    if "--scan" in sys.argv:
        print(json.dumps(kb.scan(), ensure_ascii=False))
    elif "--search" in sys.argv:
        kw = sys.argv[sys.argv.index("--search") + 1]
        for line in kb.search(kw):
            print(line)
    else:
        print(json.dumps(kb.stats(), ensure_ascii=False))
