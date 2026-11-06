# -*- coding: utf-8 -*-
"""MyBrowser 自研 UI 外壳。

这是「浏览器程序」的自研部分：窗口、标签栏、地址栏、导航按钮、
历史记录、书签，全部自己实现。渲染面借 WebView2 内核。

线程模型（WinForms 约束）：
  · **主线程**跑 WinForms 消息循环，拥有所有控件。
    WebView2 控件必须在主线程创建（COM STA，见 ui/host.py）。
  · **控制线程**跑 CDP，负责快照 / 动作。
  · 两者通过 self.ui_queue 传递「要在 UI 线程执行」的回调。
"""
import os
import queue
import threading
import time

from ..core.engine import WebView2Engine, EngineError
from ..core.snapshot import Snapshotter
from ..core.actions import Actions
from ..core.search import SearchEngine, get_engine
from . import host


class BrowserUI:
    """自研浏览器窗口。"""

    def __init__(self, engine=None, headless=False, search_engine=None, proxy="direct"):
        self.engine = engine or WebView2Engine(headless=headless, proxy=proxy)
        if engine is not None:
            engine.proxy = proxy
        self.engine_search = (SearchEngine(search_engine)
                               if search_engine else get_engine())
        self.snapshotter = None
        self.actions = None
        self.session = None
        self.ui_queue = queue.Queue()
        self._tabs = {}            # id(web) -> {"web":ctrl, "title":str, "url":str}
        self._active = None
        self._running = False
        self._form = None
        self._ui = {}
        self._browser_root = None     # 内嵌模式：承载浏览器的 root 面板（None=独立窗口）
        self._embedded = False       # 是否内嵌进主窗口（单窗口集成）
        self._env = None
        self._bookmarks_file = os.path.join(
            os.environ.get("APPDATA", "."), "MyBrowser", "bookmarks.json")

    # ---------------- 书签 ----------------

    def load_bookmarks(self):
        import json
        try:
            with open(self._bookmarks_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def save_bookmark(self, title, url):
        import json
        marks = self.load_bookmarks()
        if not any(m.get("url") == url for m in marks):
            marks.append({"title": title, "url": url})
        try:
            os.makedirs(os.path.dirname(self._bookmarks_file), exist_ok=True)
            with open(self._bookmarks_file, "w", encoding="utf-8") as f:
                json.dump(marks, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return len(marks)

    # ---------------- UI 构建（主线程）----------------

    def _build_toolbar_and_content(self):
        """构造浏览器工具栏 + 内容区，并把引用存进 self._ui。

        既给独立窗口(build)用，也给内嵌进主窗口(host_into)用 —— 后者把它们套进
        一个 root 面板再挂到主窗口下，实现「单窗口 / 聊天·浏览切换」。
        """
        from System.Windows.Forms import (
            Panel, FlowLayoutPanel, Button, TextBox, Label,
            DockStyle, FlowDirection, BorderStyle, FlatStyle,
        )
        from System.Drawing import Font, Color, ContentAlignment

        # ---------- 顶部工具栏（自研 UI）----------
        top = FlowLayoutPanel()
        top.Dock = DockStyle.Top
        top.Height = 46
        top.FlowDirection = FlowDirection.LeftToRight
        top.BackColor = Color.FromArgb(20, 22, 28)
        top.WrapContents = False

        def mk_btn(text, width=40):
            b = Button()
            b.Text = text
            b.Width = width
            b.Height = 30
            b.FlatStyle = FlatStyle.Flat
            b.ForeColor = Color.FromArgb(228, 230, 234)
            b.BackColor = Color.FromArgb(64, 66, 70)
            b.FlatAppearance.BorderColor = Color.FromArgb(88, 90, 95)
            return b

        btn_back = mk_btn("←")
        btn_fwd = mk_btn("→")
        btn_reload = mk_btn("⟳")
        btn_home = mk_btn("⌂")
        btn_star = mk_btn("☆")

        addr = TextBox()
        addr.Width = 620
        addr.Height = 26
        addr.Text = self.engine_search.homepage
        addr.Font = Font("Segoe UI", 10)
        addr.ForeColor = Color.FromArgb(236, 237, 239)
        addr.BackColor = Color.FromArgb(56, 58, 62)
        addr.BorderStyle = BorderStyle.FixedSingle

        btn_go = mk_btn("Go", 48)
        # ✕：内嵌模式 = 收起浏览器回到聊天；独立窗口 = 关闭本窗口
        btn_hide = mk_btn("✕", 40)
        status = Label()
        status.Text = "就绪"
        status.Width = 200
        status.ForeColor = Color.FromArgb(150, 152, 158)
        status.TextAlign = ContentAlignment.MiddleLeft

        for c in (btn_back, btn_fwd, btn_reload, btn_home, addr, btn_go,
                  btn_star, btn_hide, status):
            top.Controls.Add(c)

        # ---------- 标签栏（自研 UI）----------
        tabbar = FlowLayoutPanel()
        tabbar.Dock = DockStyle.Top
        tabbar.Height = 36
        tabbar.FlowDirection = FlowDirection.LeftToRight
        tabbar.BackColor = Color.FromArgb(16, 18, 22)
        tabbar.WrapContents = False

        btn_newtab = mk_btn("+", 34)
        btn_newtab.Height = 26
        tabbar.Controls.Add(btn_newtab)

        tab_label = Label()
        tab_label.Text = "  标签 1  "
        tab_label.Width = 240
        tab_label.ForeColor = Color.FromArgb(220, 222, 226)
        tab_label.TextAlign = ContentAlignment.MiddleLeft
        tabbar.Controls.Add(tab_label)

        # ---------- 内容区 ----------
        content = Panel()
        content.Dock = DockStyle.Fill
        content.BackColor = Color.FromArgb(13, 16, 23)

        # ---------- 事件接线 ----------
        btn_go.Click += lambda s, e: self._on_go()
        btn_reload.Click += lambda s, e: self._on_reload()
        btn_home.Click += lambda s, e: self._on_home()
        btn_back.Click += lambda s, e: self._on_back()
        btn_fwd.Click += lambda s, e: self._on_fwd()
        btn_star.Click += lambda s, e: self._on_star()
        btn_newtab.Click += lambda s, e: self._on_new_tab()
        btn_hide.Click += lambda s, e: self._on_hide()

        self._ui = {
            "top": top, "tabbar": tabbar, "content": content,
            "addr": addr, "status": status, "tab_label": tab_label,
            "btn_back": btn_back, "btn_fwd": btn_fwd,
            "btn_reload": btn_reload, "btn_home": btn_home,
            "btn_go": btn_go, "btn_star": btn_star, "btn_newtab": btn_newtab,
            "btn_hide": btn_hide,
        }
        return top, tabbar, content

    def build(self):
        """【独立窗口模式】构造完全自研的浏览器窗口。"""
        from System.Windows.Forms import Form, FormBorderStyle
        from System.Drawing import Color

        top, tabbar, content = self._build_toolbar_and_content()
        form = Form()
        form.Text = "INTENT 浏览器"
        form.Width = 1200
        form.Height = 820
        form.FormBorderStyle = FormBorderStyle.Sizable
        # 与 INTENT 主窗口同色（深色 13,16,23），不再有「外来窗口」的既视感
        form.BackColor = Color.FromArgb(13, 16, 23)
        self._form = form

        form.Controls.Add(content)
        form.Controls.Add(tabbar)
        form.Controls.Add(top)
        form.FormClosing += lambda s, e: self._on_close()
        return form

    # ---------------- 标签页（主线程）----------------

    def _make_tab(self, url=None):
        from Microsoft.Web.WebView2.WinForms import WebView2
        from System.Windows.Forms import DockStyle
        from System.Drawing import Color

        content = self._ui["content"]
        web = WebView2()
        web.Dock = DockStyle.Fill
        # 初始化完成前先给深色底，避免刺眼的纯白（与 INTENT 主窗口同色）
        try:
            web.BackColor = Color.FromArgb(13, 16, 23)
        except Exception:
            pass
        content.Controls.Add(web)

        url = url or self.engine_search.homepage
        entry = {"web": web, "title": "新标签页", "url": url}
        self._tabs[id(web)] = entry

        def on_init(sender, args):
            if not args.IsSuccess:
                exc = getattr(args, "InitializationException", None)
                self._ui["status"].Text = "内核初始化失败：%s" % (
                    exc or "未知（多半是 WebView2 运行时缺失，或与主窗口冲突）")
                return
            try:
                # 让内核默认底色也是 INTENT 深色，空白期不再闪白
                web.CoreWebView2.DefaultBackgroundColor = Color.FromArgb(13, 16, 23)
                web.CoreWebView2.Navigate(url)
            except Exception as e:
                self._ui["status"].Text = "导航失败: %s" % e
                return
            self._queue_ui(lambda: self._activate(entry))

        def on_nav(sender, args):
            try:
                if getattr(args, "IsSuccess", True):
                    self._ui["status"].Text = "就绪"
                else:
                    self._ui["status"].Text = "加载失败（检查网络 / 代理设置）"
            except Exception:
                pass

        web.CoreWebView2InitializationCompleted += on_init
        try:
            web.NavigationCompleted += on_nav
        except Exception:
            pass
        # 用「自定义 CoreWebView2Environment」（端口/数据目录已隔离，不污染主窗口）
        try:
            web.EnsureCoreWebView2Async(self._env)
        except Exception as e:
            self._ui["status"].Text = "启动失败: %s" % e
        return entry

    def _activate(self, entry):
        for e in self._tabs.values():
            try:
                e["web"].Visible = (e is entry)
            except Exception:
                pass
        self._active = entry
        self._ui["addr"].Text = entry.get("url", "")
        self._ui["status"].Text = "就绪"

    def _on_new_tab(self, sender=None, args=None):
        entry = self._make_tab()
        self._activate(entry)

    def _current(self):
        return self._active

    # ---------------- 导航 ----------------

    def _normalize(self, q):
        """地址栏输入 -> URL（识别网址 / 本地地址 / 搜索词）。"""
        return self.engine_search.normalize(q)

    def _on_go(self):
        entry = self._current()
        if not entry:
            return
        url = self._normalize(self._ui["addr"].Text)
        if not url:
            return
        entry["url"] = url
        self._ui["status"].Text = "加载中…"
        try:
            entry["web"].CoreWebView2.Navigate(url)
        except Exception as e:
            self._ui["status"].Text = "导航失败: %s" % e

    def _on_reload(self):
        entry = self._current()
        if entry:
            try:
                entry["web"].CoreWebView2.Reload()
            except Exception:
                pass

    def _on_home(self):
        self._ui["addr"].Text = self.engine_search.homepage
        self._on_go()

    def _on_back(self):
        entry = self._current()
        if entry:
            try:
                entry["web"].CoreWebView2.GoBack()
            except Exception:
                pass

    def _on_fwd(self):
        entry = self._current()
        if entry:
            try:
                entry["web"].CoreWebView2.GoForward()
            except Exception:
                pass

    def _on_star(self):
        entry = self._current()
        if entry:
            n = self.save_bookmark(entry.get("title", ""), entry.get("url", ""))
            self._ui["status"].Text = "已收藏 (%d)" % n

    def _on_close(self):
        self._running = False

    @property
    def is_alive(self):
        """窗口是否还开着（供 adapter 判断「关掉后再输入要不要重新弹」）。"""
        return bool(self._running and self._form is not None)

    # ---------------- 控制面 ----------------

    def control_loop(self):
        """控制面初始化 + 命令泵。

        重要约束：WebView2 的 CDP 命令（Page/Runtime/Input/DOM）必须在
        **创建 WebView2 控件的那个线程**（即 UI 主线程）上执行，
        否则命令会挂起直到超时 —— 实测确认过。

        因此这里的分工是：
          · 后台线程：负责建立 CDP 连接（纯 socket，跨线程安全）
          · UI 线程（pump_ui）：负责执行所有 CDP 命令
          · Agent 线程：通过 run_in_ui() 提交命令，等结果
        """
        try:
            self.engine.connect()
        except EngineError as e:
            self._queue_ui(lambda: self._ui["status"].__setattr__(
                "Text", "CDP 未就绪: %s" % e))
            return

        target_id = None
        for _ in range(60):
            target_id = self.engine.find_page_target()
            if target_id:
                break
            time.sleep(0.4)

        if not target_id:
            self._queue_ui(lambda: self._ui["status"].__setattr__(
                "Text", "未找到页面 target"))
            return

        sess = self.engine.attach_existing(target_id)
        self.session = sess
        self.snapshotter = Snapshotter(sess)
        self.actions = Actions(sess, self.snapshotter)
        self._queue_ui(lambda: self._ui["status"].__setattr__(
            "Text", "MyBrowser 就绪"))

        def on_nav(sid, params, _s):
            try:
                self._queue_ui(self._sync_address_now)
            except Exception:
                pass

        self.engine.client.on_any(on_nav)

        while self._running:
            time.sleep(0.2)

    def run_in_ui(self, fn, timeout=30.0):
        """在 UI 线程执行 fn 并返回结果。可从任意线程调用。

        这是让 Agent 线程安全驱动 WebView2 的唯一正确方式。
        """
        box = {}
        done = threading.Event()

        def _wrapper():
            try:
                box["r"] = fn()
            except Exception as e:
                box["e"] = e
            finally:
                done.set()

        self.ui_queue.put(_wrapper)
        if not done.wait(timeout):
            raise EngineError("UI 线程超时: %s" % fn)
        if "e" in box:
            raise box["e"]
        return box.get("r")

    def _sync_address_now(self):
        """在 UI 线程读取当前页信息并刷新地址栏。"""
        try:
            sess = self.session
            if sess is None:
                return
            url = sess.current_url()
            title = sess.title()
            self._ui["addr"].Text = url or ""
            self._ui["tab_label"].Text = "  %s  " % (title or "标签 1")[:36]
        except Exception:
            pass

    def _sync_address(self, url, title):
        try:
            self._ui["addr"].Text = url or ""
            self._ui["tab_label"].Text = "  %s  " % (title or "标签 1")[:36]
        except Exception:
            pass

    def _queue_ui(self, fn):
        self.ui_queue.put(fn)

    def pump_ui(self):
        """在 UI 线程执行排队的回调。由 Timer 驱动。"""
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass

    # ---------------- 启动 ----------------

    def _find_intent_hwnd(self):
        """找本进程、标题含 'intent' 的主窗口句柄（用于把浏览器挂到主窗口之下）。

        必须按 PID 过滤：多个实例并存时 FindWindowW 按标题会返回「别人家」的窗口。
        因此枚举本进程顶层窗口，取标题匹配且可见、面积最大的那个。
        """
        try:
            import ctypes
            import os
            u = ctypes.windll.user32
            pid_target = os.getpid()
            best = [0, 0]        # (hwnd, area)
            EnumProc = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

            def _cb(hwnd, _):
                p = ctypes.c_ulong()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value != pid_target:
                    return True
                buf = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(hwnd, buf, 512)
                if "intent" not in (buf.value or "").lower():
                    return True
                if not u.IsWindowVisible(hwnd):
                    return True
                rc = ctypes.create_string_buffer(16)
                u.GetWindowRect(hwnd, rc)
                rect = ctypes.cast(
                    rc, ctypes.POINTER(ctypes.c_long * 4)).contents
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                if area > best[1]:
                    best[0], best[1] = hwnd, area
                return True

            u.EnumWindows(EnumProc(_cb), 0)
            return best[0]
        except Exception:
            return 0

    def _attach_to_intent(self, form):
        """把浏览器窗口挂到主窗口(intent)之下：作为其子窗口(Owner)并贴在主窗口右侧，
        视觉上就是「同一套软件」而不是一个外来窗口。任一步失败都安全回退成独立窗口。

        主窗口可能在预热(preature)时还没建好，所以这里会轮询最多数秒等它出现。
        """
        import time as _t
        hwnd = 0
        for _ in range(60):          # 最多等 ~6 秒
            hwnd = self._find_intent_hwnd()
            if hwnd:
                break
            _t.sleep(0.1)
        if not hwnd:
            return
        try:
            from System.Windows.Forms import Form, FormStartPosition
            from System.Drawing import Point
            owner = Form.FromHandle(hwnd)
            if owner is not None:
                # Owner 让浏览器窗口：随主窗口最小化/关闭、在任务栏与主窗口归为一组、
                # 始终浮在主窗口之上 —— 观感上「就是 INTENT 的一部分」。
                form.Owner = owner
            # 贴在主窗口右侧，像一个集成面板而非并排的两个程序
            try:
                import ctypes
                import struct
                rc = ctypes.create_string_buffer(16)
                ctypes.windll.user32.GetWindowRect(hwnd, rc)
                x, y, r, b = struct.unpack("llll", rc.raw)
                form.StartPosition = FormStartPosition.Manual
                form.Location = Point(r + 8, y)
            except Exception:
                pass
        except Exception:
            pass

    def run(self):
        """主入口：在主线程跑 WinForms 消息循环，控制线程跑 CDP。"""
        import threading as _t

        # 1) COM/CLR 初始化（必须在 import clr 之前完成 STA 设置）
        host.setup_clr(need_webview2=True)

        from System.Windows.Forms import Application, Timer, FormStartPosition

        # 2) 用「自定义 CoreWebView2Environment」限定 CDP 端口与 user-data-dir。
        #    ⚠️ 这里【绝不】调用 prepare_env()：它会把 WEBVIEW2_USER_DATA_FOLDER /
        #    调试端口写成**进程级全局环境变量**，污染同进程里的主聊天窗口
        #    （pywebview）的 WebView2，导致两个 WebView2 抢同一个 user-data-dir /
        #    调试端口、内核初始化失败 → 浏览器纯白。隔离环境是修白屏的关键。
        self._env = self.engine.build_environment()

        self._running = True

        # 3) 构建 UI 并跑消息循环（主线程）
        form = self.build()
        # 挂到主窗口(intent)之下：作为其子窗口 + 贴在主窗口右侧，视觉上就是一套软件
        self._attach_to_intent(form)

        entry = self._make_tab()
        self._activate(entry)

        timer = Timer()
        timer.Interval = 50
        timer.Tick += lambda s, e: self.pump_ui()
        timer.Start()

        # 4) 控制线程
        ctl = _t.Thread(target=self.control_loop, daemon=True)
        ctl.start()

        try:
            Application.Run(form)
        finally:
            self._running = False
            self.engine.dispose()

    # ---------------- 内嵌进主窗口（单窗口集成）----------------

    def host_into(self, main_form):
        """【内嵌模式】把浏览器作为主窗口(intent)内的一个子面板，单窗口集成。

        必须在主窗口所在的 STA 线程上调用（适配层用 form.Invoke 派发过来）。
        **不**调用 Application.Run —— 主窗口自己的消息循环已经驱动一切；
        我们只是往里塞一个面板 + 起一个后台 CDP 控制线程。

        关键：浏览器 WebView2 用「隔离的自定义 CoreWebView2Environment」
        （独立端口 + 独立 user-data-dir），和主窗口的聊天 WebView2 互不干扰，
        这也是「白屏」修复的核心。
        """
        import threading as _t
        host.setup_clr(need_webview2=True)
        from System.Windows.Forms import Timer, Panel, DockStyle
        from System.Drawing import Color

        # 隔离环境（端口 / user-data-dir 只在本实例，不污染主窗口的聊天 WebView2）
        self._env = self.engine.build_environment()
        self._embedded = True
        self._form = main_form
        self._running = True

        top, tabbar, content = self._build_toolbar_and_content()
        # root 面板：套住工具栏 + 内容区，整体作为主窗口的一个子控件
        root = Panel()
        root.Dock = DockStyle.Fill
        root.BackColor = Color.FromArgb(13, 16, 23)
        root.Visible = False          # 初始隐藏，聊天可见；打开浏览器时才显示
        root.Controls.Add(content)
        root.Controls.Add(tabbar)
        root.Controls.Add(top)
        main_form.Controls.Add(root)
        self._browser_root = root

        entry = self._make_tab()
        self._activate(entry)

        # 在主线程上跑 UI 命令泵（WebView2 的 COM 调用必须在创建它的线程上执行）
        timer = Timer()
        timer.Interval = 50
        timer.Tick += lambda s, e: self.pump_ui()
        timer.Start()

        # 后台 CDP 控制线程：连上本实例的页面，供 AI 处理层接管（DUMP/TRACE/SERVE）
        ctl = _t.Thread(target=self.control_loop, daemon=True)
        ctl.start()

    def show(self):
        """显示浏览器面板（内嵌模式覆盖聊天；独立窗口则置顶）。"""
        def _s():
            if self._browser_root is not None:
                self._browser_root.Visible = True
                self._browser_root.BringToFront()
        self.run_in_ui(_s)

    def hide(self):
        """收起浏览器面板，回到聊天（仅内嵌模式有意义）。"""
        def _h():
            if self._browser_root is not None:
                self._browser_root.Visible = False
        self.run_in_ui(_h)

    def _on_hide(self):
        """工具栏 ✕ 按钮：内嵌模式收起回聊天；独立窗口则关闭本窗口。"""
        if getattr(self, "_embedded", False):
            self.hide()
        else:
            try:
                if self._form is not None:
                    self._form.Close()
            except Exception:
                pass
