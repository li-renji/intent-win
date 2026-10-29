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

    def __init__(self, engine=None, headless=False, search_engine=None):
        self.engine = engine or WebView2Engine(headless=headless)
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

    def build(self):
        """构造完全自研的浏览器界面。"""
        from System.Windows.Forms import (
            Form, Panel, FlowLayoutPanel, Button, TextBox, Label,
            DockStyle, FormBorderStyle, FlowDirection, BorderStyle, FlatStyle,
        )
        from System.Drawing import Font, Color, ContentAlignment

        form = Form()
        form.Text = "MyBrowser"
        form.Width = 1200
        form.Height = 820
        form.FormBorderStyle = FormBorderStyle.Sizable
        form.BackColor = Color.FromArgb(32, 33, 36)
        self._form = form

        # ---------- 顶部工具栏（自研 UI）----------
        top = FlowLayoutPanel()
        top.Dock = DockStyle.Top
        top.Height = 46
        top.FlowDirection = FlowDirection.LeftToRight
        top.BackColor = Color.FromArgb(48, 49, 53)
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
        status = Label()
        status.Text = "就绪"
        status.Width = 210
        status.ForeColor = Color.FromArgb(150, 152, 158)
        status.TextAlign = ContentAlignment.MiddleLeft

        for c in (btn_back, btn_fwd, btn_reload, btn_home, addr, btn_go,
                  btn_star, status):
            top.Controls.Add(c)

        # ---------- 标签栏（自研 UI）----------
        tabbar = FlowLayoutPanel()
        tabbar.Dock = DockStyle.Top
        tabbar.Height = 36
        tabbar.FlowDirection = FlowDirection.LeftToRight
        tabbar.BackColor = Color.FromArgb(40, 41, 45)
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
        content.BackColor = Color.FromArgb(24, 25, 28)

        form.Controls.Add(content)
        form.Controls.Add(tabbar)
        form.Controls.Add(top)

        self._ui = {
            "form": form, "top": top, "tabbar": tabbar, "content": content,
            "addr": addr, "status": status, "tab_label": tab_label,
            "btn_back": btn_back, "btn_fwd": btn_fwd,
            "btn_reload": btn_reload, "btn_home": btn_home,
            "btn_go": btn_go, "btn_star": btn_star, "btn_newtab": btn_newtab,
        }

        # ---------- 事件接线 ----------
        btn_go.Click += lambda s, e: self._on_go()
        btn_reload.Click += lambda s, e: self._on_reload()
        btn_home.Click += lambda s, e: self._on_home()
        btn_back.Click += lambda s, e: self._on_back()
        btn_fwd.Click += lambda s, e: self._on_fwd()
        btn_star.Click += lambda s, e: self._on_star()
        btn_newtab.Click += lambda s, e: self._on_new_tab()
        form.FormClosing += lambda s, e: self._on_close()
        return form

    # ---------------- 标签页（主线程）----------------

    def _make_tab(self, url=None):
        from Microsoft.Web.WebView2.WinForms import WebView2
        from System.Windows.Forms import DockStyle

        content = self._ui["content"]
        web = WebView2()
        web.Dock = DockStyle.Fill
        web.Visible = False
        content.Controls.Add(web)

        url = url or self.engine_search.homepage
        entry = {"web": web, "title": "新标签页", "url": url}
        self._tabs[id(web)] = entry

        def on_init(sender, args):
            if not args.IsSuccess:
                self._ui["status"].Text = "内核初始化失败"
                return
            try:
                web.CoreWebView2.Navigate(url)
            except Exception:
                pass
            self._queue_ui(lambda: self._activate(entry))

        web.CoreWebView2InitializationCompleted += on_init
        # 用带 CDP 端口的 Environment
        web.EnsureCoreWebView2Async(self._env)
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

    def run(self):
        """主入口：在主线程跑 WinForms 消息循环，控制线程跑 CDP。"""
        import threading as _t

        # 1) COM/CLR 初始化（必须在 import clr 之前完成 STA 设置）
        host.setup_clr(need_webview2=True)

        from System.Windows.Forms import Application, Timer

        # 2) 创建带 CDP 端口的 Environment
        self.engine.prepare_env()
        self._env = host.create_environment(
            self.engine.port, user_data_folder=self.engine.profile_dir)

        self._running = True

        # 3) 构建 UI 并跑消息循环（主线程）
        form = self.build()
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
