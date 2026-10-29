# -*- coding: utf-8 -*-
"""MyBrowser -> Windows Agent 的 BROWSER 工具适配器。

与 INTENT-win 现有 agent/browser/protocol.py 保持**协议兼容**，
但底层实现完全不同：真实事件、真实等待、稳定 ref、增量快照。

现有协议（保持不变，Agent 侧零改动）：
  NAVIGATE <url>          导航
  SNAPSHOT [max_chars]    页面语义快照（AX 树 + 可交互元素 ref + 正文）
  CLICK <i>               点击第 i 号元素
  TYPE <i> <文本>         向第 i 号输入框填入文本
  SCROLL <down|up|top|bottom>
  BACK / FORWARD / RELOAD
  READ [max_chars]        只取正文纯文本（最省 token）

新增（向后兼容，Agent 可选使用）：
  CLICK_REF <ref>         直接按 ref 点击（比序号更稳）
  TYPE_REF <ref> <文本>
  PRESS <key>             按键（Enter/Tab/Escape/…）
  HOVER <i|ref>
  DRAG <ref>
  SHOT [path]             截图（AX 树看不到的视觉内容）
  DIFF [max_chars]        增量快照（只报相对上次的变化）
"""
import json
import os
import threading
import time

from .core.engine import WebView2Engine, EngineError
from .core.snapshot import Snapshotter
from .core.actions import Actions
from .core.search import SearchEngine, get_engine
from .net.cdp import CDPError


class MyBrowserTool:
    """给 Agent 用的浏览器工具。

    生命周期：
        tool = MyBrowserTool()
        tool.start()          # 起渲染进程 + 建 CDP
        tool.execute("NAVIGATE " + tool.engine_search.homepage)
        tool.execute("SNAPSHOT")
        tool.stop()
    """

    def __init__(self, port=9610, headless=False, profile_dir=None,
                 search_engine=None):
        self.engine = WebView2Engine(port=port, headless=headless,
                                     profile_dir=profile_dir)
        self.engine_search = (SearchEngine(search_engine)
                               if search_engine else get_engine())
        self.snapshotter = None
        self.actions = None
        self.session = None
        self._lock = threading.RLock()
        self._started = False
        self._error = None
        self._last_snapshot = None
        self._ordinal_map = {}     # 序号 -> ref（供 CLICK i / TYPE i 使用）
        self._ui = None             # 接管 BrowserUI 时非空

    # ---------------- UI 模式下的线程调度 ----------------

    def _in_ui(self, fn, timeout=30.0):
        """执行 fn。

        实测结论：WebView2 的 CDP 命令在**任意线程**都能正常执行
        （Runtime.evaluate / Input / Accessibility 全部秒回），
        唯一会阻塞的是 Page.navigate —— 它要等渲染进程确认，
        在 UI 线程里会卡死消息循环。

        因此策略是：
          · 普通 CDP 命令 → 直接在当前线程执行
          · 导航 → 优先走 WebView2 控件 API（UI 线程），否则用 JS 跳转
        """
        return fn()

    # ---------------- 生命周期 ----------------

    def start(self, start_url=None, timeout=40.0):
        """启动渲染进程并建立控制面。

        start_url 为 None 时用当前搜索引擎的首页。
        """
        with self._lock:
            if self._started:
                return True
            self._error = None
            if start_url is None:
                start_url = self.engine_search.homepage
            self.engine.prepare_env()

            # 渲染面：优先用系统 Edge 进程（headless 可控），
            # 有 UI 时由 ui/shell.py 提供 WebView2 控件
            proc = self._spawn_edge()
            try:
                self.engine.connect(timeout=timeout)
            except EngineError as e:
                self._error = str(e)
                if proc:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                return False

            sess = self.engine.new_tab("about:blank")
            self.session = sess
            self.snapshotter = Snapshotter(sess)
            self.actions = Actions(sess, self.snapshotter)
            self._started = True

            if start_url:
                try:
                    sess.navigate(start_url)
                    time.sleep(1.2)
                except Exception:
                    pass
            return True
    def _spawn_edge(self):
        """起一个 Edge 渲染进程并开 CDP 端口。"""
        import subprocess
        edge = None
        for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
            if os.path.exists(p):
                edge = p
                break
        if not edge:
            self._error = "未找到 Edge 可执行文件"
            return None
        args = [edge,
                "--remote-debugging-port=%d" % self.engine.port,
                "--user-data-dir=%s" % self.engine.profile_dir,
                "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,OptimizationHints",
                "--allow-file-access-from-files"]
        if self.engine.headless:
            args.append("--headless=new")
        args.append("about:blank")
        try:
            return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except Exception as e:
            self._error = "渲染进程启动失败: %s" % e
            return None

    def attach_to_ui(self, ui, timeout=30.0):
        """接管一个已在运行的 BrowserUI（带界面的自研窗口）。

        与 start() 的区别：start() 自己拉一个 headless 渲染进程；
        attach_to_ui() 接管 ui/shell.py 里的 WebView2 窗口，
        这样用户能看着 Agent 操作自己写的浏览器界面。

        ui 需已提供 engine（且 engine.client 已连上）。
        """
        with self._lock:
            if self._started:
                return True
            self.engine = ui.engine
            self._error = None

            # 等 UI 的 control_loop 建好 session
            end = time.time() + timeout
            while time.time() < end:
                if getattr(ui, "session", None) is not None:
                    break
                time.sleep(0.2)
            else:
                self._error = "UI 控制面未就绪"
                return False

            self.session = ui.session
            self.snapshotter = ui.snapshotter
            self.actions = ui.actions
            self._ui = ui
            self._started = True
            return True

    def stop(self):
        with self._lock:
            self._started = False
            self.engine.dispose()

    @property
    def available(self):
        return self._started and self.session is not None

    # ---------------- 协议执行 ----------------

    def _do_nav(self, url, prefix=None):
        """执行导航。prefix 非空时用它做返回前缀（搜索场景）。"""
        def _nav():
            # 优先用 UI 的 WebView2 控件导航（真导航，不经过 CDP）
            if self._ui is not None:
                entry = self._ui._current()
                if entry is not None:
                    entry["url"] = url
                    try:
                        entry["web"].CoreWebView2.Navigate(url)
                    except Exception:
                        pass
                    time.sleep(1.5)
                    return "%s | %s" % (self.session.title(),
                                       self.session.current_url())
            # 退化路径：JS 跳转（Page.navigate 在 WebView2 上会阻塞）
            self.session.navigate_js(url)
            time.sleep(1.5)
            return "%s | %s" % (self.session.title(), self.session.current_url())

        detail = self._in_ui(_nav, timeout=40.0)
        if prefix:
            return "%s: %s" % (prefix, detail)
        return "已导航: %s" % detail

    def execute(self, cmd_text, max_chars=None):
        """执行一条浏览器指令，返回文本结果。"""
        if not self.available:
            return self._error or "浏览器未启用"

        parts = str(cmd_text).strip().split(" ", 1)
        action = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""
        args = rest.split(" ", 1) if rest else []

        try:
            if action == "SEARCH":
                kw = rest.strip()
                if not kw:
                    return "SEARCH 需要 关键词"
                url = self.engine_search.search_url(kw)
                return self._do_nav(url, "已搜索 '%s'（%s）" % (
                    kw, self.engine_search.display_name))

            if action == "ENGINE":
                if not rest:
                    return ("当前搜索引擎: %s（可用: baidu/bing/sogou/"
                            "duckduckgo/google）"
                            % self.engine_search.display_name)
                ok, msg = self.engine_search.set_engine(rest.strip())
                return msg if ok else "切换失败: %s" % msg

            if action == "NAVIGATE":
                raw = rest.strip()
                if not raw:
                    return "NAVIGATE 需要 url"
                # 支持直接给搜索词（走地址栏同款识别逻辑）
                url = self.engine_search.normalize(raw)
                return self._do_nav(url)

            if action == "SNAPSHOT":
                n = int(args[0]) if args and args[0].isdigit() else (
                    max_chars or 3500)
                snap = self._in_ui(lambda: self.snapshotter.capture(max_chars=n))
                self._last_snapshot = snap
                self._build_ordinal_map(snap)
                return snap.text

            if action == "DIFF":
                n = int(args[0]) if args and args[0].isdigit() else (
                    max_chars or 3500)
                snap = self._in_ui(lambda: self.snapshotter.capture(max_chars=n))
                self._last_snapshot = snap
                self._build_ordinal_map(snap)
                return "较上次：%s\n%s" % (snap.delta or "首次快照", snap.text)

            if action == "READ":
                n = int(args[0]) if args and args[0].isdigit() else (
                    max_chars or 4000)
                return self._in_ui(lambda: self.actions.read_text(n))

            if action == "CLICK":
                if not args or not args[0].isdigit():
                    return "CLICK 需要 元素编号"
                ref = self._ref_by_ordinal(int(args[0]))
                if not ref:
                    return "编号 %s 不存在，请先 SNAPSHOT" % args[0]
                return self._in_ui(lambda: self.actions.click(ref))

            if action == "CLICK_REF":
                if not args:
                    return "CLICK_REF 需要 ref"
                r = args[0].strip()
                return self._in_ui(lambda: self.actions.click(r))

            if action == "TYPE":
                if len(args) < 2 or not args[0].isdigit():
                    return "TYPE 需要 元素编号 + 文本"
                ref = self._ref_by_ordinal(int(args[0]))
                if not ref:
                    return "编号 %s 不存在，请先 SNAPSHOT" % args[0]
                text = args[1]
                return self._in_ui(lambda: self.actions.type_text(text, ref))

            if action == "TYPE_REF":
                if len(args) < 2:
                    return "TYPE_REF 需要 ref + 文本"
                r, text = args[0].strip(), args[1]
                return self._in_ui(lambda: self.actions.type_text(text, r))

            if action == "PRESS":
                if not args:
                    return "PRESS 需要 按键名"
                k = args[0].strip().upper()
                return self._in_ui(lambda: self.actions.press_key(k))

            if action == "HOVER":
                if not args:
                    return "HOVER 需要 编号或ref"
                t = args[0].strip()
                if t.isdigit():
                    t = self._ref_by_ordinal(int(t))
                return self._in_ui(lambda: self.actions.hover(t))

            if action == "DRAG":
                if not args:
                    return "DRAG 需要 ref"
                r = args[0].strip()
                return self._in_ui(lambda: self.actions.drag(r))

            if action == "SCROLL":
                d = args[0].lower() if args else "down"
                px = {"down": 600, "up": -600, "top": -2000, "bottom": 2000}.get(d)
                if px is None:
                    return "SCROLL 方向: down/up/top/bottom"
                if d in ("top", "bottom"):
                    expr = "window.scrollTo({top:%s,behavior:'smooth'})" % (
                        "0" if d == "top" else "999999")
                    self._in_ui(lambda: self.session.evaluate(expr))
                    return "已滚动到%s" % d
                    return "已滚动到%s" % d
                self.actions.scroll(px)
                return "已滚动 %s" % d

            if action == "BACK":
                self._in_ui(lambda: self.session.back())
                time.sleep(0.6)
                return "已后退"

            if action == "FORWARD":
                self._in_ui(lambda: self.session.forward())
                time.sleep(0.6)
                return "已前进"

            if action == "RELOAD":
                self._in_ui(lambda: self.session.reload())
                time.sleep(0.8)
                return "已刷新"

            if action == "SHOT":
                path = args[0].strip() if args else os.path.join(
                    os.environ.get("TEMP", "."), "mybrowser_shot.png")
                self._in_ui(lambda: self.actions.screenshot(path))
                return "已截图: %s" % path

            if action == "EVAL":
                if not rest:
                    return "EVAL 需要 JS 表达式"
                return str(self._in_ui(lambda: self.actions.eval_js(rest)))

            return ("未知动作。支持: NAVIGATE/SEARCH/ENGINE/SNAPSHOT/DIFF/READ/"
                    "CLICK/CLICK_REF/TYPE/TYPE_REF/PRESS/HOVER/DRAG/SCROLL/"
                    "BACK/FORWARD/RELOAD/SHOT/EVAL")

        except CDPError as e:
            return "浏览器操作失败: %s" % e
        except Exception as e:
            return "浏览器异常: %s" % e

    def _build_ordinal_map(self, snap):
        """把快照里的元素按显示顺序编成 1-based 序号，供 CLICK i / TYPE i 使用。"""
        self._ordinal_map = {}
        for i, (ref, role, name) in enumerate(snap.elements):
            self._ordinal_map[i + 1] = ref

    def _ref_by_ordinal(self, ordinal):
        return self._ordinal_map.get(ordinal)


# ---------------- 供 INTENT-win agent 直接挂载 ----------------

def make_dispatch(tool):
    """返回一个符合 agent/tools.py attach_browser 签名的分发函数。"""
    def dispatch(cmd):
        return tool.execute(cmd)
    return dispatch
