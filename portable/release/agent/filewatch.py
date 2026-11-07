# -*- coding: utf-8 -*-
"""文件改动监听：给「VS Code 风格编程面板」提供实时数据流。

为什么要它：Agent 改文件的路径不止一条（code 工具、organize 工具、插件命令、
用户手动编辑……），在每个工具里单独埋点容易漏。监听器在后台按固定节奏扫描
受监控目录，发现 mtime/大小变化就把新内容推给前端，天然覆盖所有来源。

代价与约束：
  · 只扫「受监控根目录」，默认项目根 + 桌面（深度受限）；
  · 跳过 node_modules/.git/venv 等重目录；
  · 单次扫描文件数设上限，避免大目录拖慢 UI；
  · 只推送文本类文件（二进制不推，前端也用不上）；
  · 轮询间隔默认 0.8 秒，够实时又不至于烧 CPU。
"""
import os
import threading
import time

# 这些目录名一律跳过（重且无关）
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".workbuddy",
    "dist", "build", ".next", ".cache", "site-packages", ".tox", ".gradle",
}

# 这些后缀视为文本，可推送内容
TEXT_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".less", ".json", ".json5", ".yml",
    ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".md", ".markdown",
    ".txt", ".rst", ".csv", ".tsv", ".sql", ".sh", ".bash", ".zsh", ".bat",
    ".cmd", ".ps1", ".xml", ".svg", ".go", ".rs", ".java", ".kt", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".rb", ".php", ".lua", ".swift", ".gradle", ".dockerfile",
    ".gitignore", ".editorconfig",
}

MAX_FILES = 6000        # 单次扫描上限
MAX_CONTENT = 300_000   # 单文件推送内容上限（字节）
MAX_DEPTH = 4           # 受监控目录的最大递归深度


def _is_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        return True
    name = os.path.basename(path).lower()
    return name in ("dockerfile", "makefile", "readme", "license", ".gitignore")


class FileWatcher:
    """轮询式文件监听器。on_change(path, content) 回调在 watcher 线程里触发。"""

    def __init__(self, roots=None, on_change=None, interval=0.8):
        self.roots = []
        self.on_change = on_change
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._snap = {}        # path -> (mtime, size)
        for r in (roots or []):
            self.add_root(r)

    # ---- 对外接口 ----
    def add_root(self, path):
        if not path:
            return
        p = os.path.normpath(os.path.abspath(str(path)))
        if os.path.isdir(p):
            with self._lock:
                if p not in self.roots:
                    self.roots.append(p)

    def roots_list(self):
        with self._lock:
            return list(self.roots)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- 内部 ----
    def _run(self):
        # 首轮只建立基线，不推送（避免启动时把整个仓库都推一遍）
        first = True
        while not self._stop.is_set():
            try:
                changed = self._scan()
                if not first:
                    for path, content in changed:
                        try:
                            if self.on_change:
                                self.on_change(path, content)
                        except Exception:
                            pass
                first = False
            except Exception:
                pass
            self._stop.wait(self.interval)

    def _scan(self):
        cur = {}
        changed = []
        for root in self.roots_list():
            self._walk(root, 0, cur)
        old = self._snap
        self._snap = cur
        for path, sig in cur.items():
            prev = old.get(path)
            if prev is None:
                # 新出现的文件：只在内容可读且不像刚生成的临时文件时推送
                if self._readable_new(path):
                    changed.append((path, self._read(path)))
            elif prev != sig:
                changed.append((path, self._read(path)))
        return changed

    def _walk(self, root, depth, out):
        if depth > MAX_DEPTH or len(out) >= MAX_FILES:
            return
        try:
            entries = list(os.scandir(root))
        except OSError:
            return
        for e in entries:
            if len(out) >= MAX_FILES:
                return
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in SKIP_DIRS or e.name.startswith("."):
                        continue
                    self._walk(e.path, depth + 1, out)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat()
                    out[e.path] = (st.st_mtime, st.st_size)
            except OSError:
                continue

    def _readable_new(self, path):
        try:
            name = os.path.basename(path)
            if name.startswith("~$") or name.endswith((".tmp", ".swp", ".part")):
                return False
            return _is_text(path) and os.path.getsize(path) <= MAX_CONTENT
        except OSError:
            return False

    def _read(self, path):
        try:
            if not _is_text(path):
                return ""
            if os.path.getsize(path) > MAX_CONTENT:
                return ""
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""
