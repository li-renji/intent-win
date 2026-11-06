# -*- coding: utf-8 -*-
"""MyBrowser 内核引擎：WebView2 进程的生命周期与控制面。

职责边界：
  · 拉起 WebView2 渲染进程（借用 Chromium 内核）
  · 打开 CDP 调试端口，供自研 CDP 客户端接管
  · 维护浏览器级 / 标签页级会话

设计要点：WebView2 控件跑在 UI 线程（STA），而 CDP 控制面跑在后台线程。
两者通过 Session 对象解耦，UI 线程只管画和收事件，控制面只管发命令。
"""
import os
import threading
import time
import urllib.request

from ..net.cdp import CDPClient, CDPError


class EngineError(Exception):
    pass


class WebView2Engine:
    """WebView2 渲染引擎 + CDP 控制面。

    用法：
        eng = WebView2Engine(port=9555)
        eng.attach_ui(webview2_control)   # 由 UI 线程调用
        eng.start_cdp()                    # 由后台线程调用
        sess = eng.new_tab()              # 新标签页
    """

    def __init__(self, port=9555, profile_dir=None, headless=False, proxy="direct"):
        self.port = port
        self.profile_dir = profile_dir or os.path.join(
            os.environ.get("TEMP", "."), "mybrowser_profile")
        self.headless = headless
        # 代理模式：direct=直连（绕过环境/系统里多半没在跑的本地开发代理，
        #   否则百度等页面会永远转圈）；system=用系统代理；其它字符串=自定义 --proxy-server
        self.proxy = proxy
        self.client = None          # CDPClient（浏览器级）
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._err = None
        self._tabs = {}
        self._tab_seq = 0

    # ---------------- 环境准备 ----------------

    def _core_args(self):
        """除「端口 / 用户数据目录」之外的浏览器启动参数（两类场景共用）。

        注意：--remote-debugging-port 与 --user-data-dir 由调用方单独加，
        这里不要重复，否则 Chromium 会出现两份同义参数、行为不可预期。
        """
        args = ["--disable-features=Translate,OptimizationHints",
                "--no-first-run", "--no-default-browser-check",
                # 允许我们自己写的扩展/自动化脚本在页面上跑
                "--allow-file-access-from-files"]
        # 代理：默认直连，避开环境里那个多半没在跑的本地开发代理 127.0.0.1:xxxx
        if self.proxy == "direct":
            args.append("--no-proxy-server")
        elif self.proxy and self.proxy != "system":
            args.append("--proxy-server=" + self.proxy)
        if self.headless:
            args.append("--headless=new")
        return args

    def prepare_env(self):
        """【仅独立 Edge 模式用】把启动参数写成全局环境变量。

        必须仅在「自己拉起 msedge.exe」的独立渲染进程场景调用 —— 因为
        子进程（msedge）只读环境变量、不读我们构造的 CoreWebView2Environment。

        ⚠️ 绝不能在「自研窗口（UI）模式」调用：它会把 WEBVIEW2_USER_DATA_FOLDER
        与调试端口写成**进程级全局变量**，污染同进程里的主聊天窗口（pywebview）
        的 WebView2，导致两个 WebView2 抢同一个 user-data-dir / 调试端口、
        内核初始化失败 → 浏览器纯白。UI 模式请改用 build_environment()。
        """
        os.makedirs(self.profile_dir, exist_ok=True)
        args = ["--remote-debugging-port=%d" % self.port] + self._core_args()
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = " ".join(args)
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = self.profile_dir

    def build_environment(self):
        """【UI / 自研窗口模式专用】构建带 CDP 端口的 CoreWebView2Environment。

        端口与 user-data-dir 由 host.create_environment 单独注入，**不写全局
        环境变量**，从而与同进程的主聊天窗口 WebView2 彻底隔离 —— 这是修掉
        「浏览器纯白」的关键：两个 WebView2 不再争抢 user-data-dir / 调试端口。
        """
        os.makedirs(self.profile_dir, exist_ok=True)
        from ..ui import host
        return host.create_environment(
            self.port, user_data_folder=self.profile_dir,
            extra_args=self._core_args())

    def wait_cdp(self, timeout=30.0):
        """等 CDP 调试端口就绪，返回 /json/version 的内容。

        注意：必须绕过 HTTP 代理。环境里常设了 HTTP_PROXY/HTTPS_PROXY 指向
        本地代理，urllib 会把 127.0.0.1 的请求也发过去，导致 502。
        """
        import json
        import urllib.request
        # 构建一个完全不走代理的 opener
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = "http://127.0.0.1:%d/json/version" % self.port
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                with opener.open(url, timeout=2) as r:
                    info = json.loads(r.read().decode("utf-8"))
                self._ready.set()
                return info
            except Exception as e:
                last = e
                time.sleep(0.3)
        self._err = last
        raise EngineError("CDP 端口 %d 未就绪: %s" % (self.port, last))

    def connect(self, timeout=30.0):
        """建立 CDP 连接（浏览器级）。"""
        info = self.wait_cdp(timeout)
        self.client = CDPClient(info["webSocketDebuggerUrl"], timeout=30)
        return self.client

    # ---------------- 标签页 ----------------

    def new_tab(self, url="about:blank"):
        """新建标签页并附着，返回 Session。"""
        if not self.client:
            raise EngineError("CDP 未连接")
        with self._lock:
            tgt = self.client.send("Target.createTarget", {"url": url})
            tid = tgt["targetId"]
            att = self.client.send("Target.attachToTarget",
                                   {"targetId": tid, "flatten": True})
            sid = att["sessionId"]
            self._tab_seq += 1
            sess = Session(self, tid, sid, self._tab_seq)
            self._tabs[tid] = sess
        sess.enable()
        return sess

    def attach_existing(self, target_id):
        """附着到已存在的 target（用于接管 UI 里已经打开的页面）。"""
        if not self.client:
            raise EngineError("CDP 未连接")
        with self._lock:
            att = self.client.send("Target.attachToTarget",
                                   {"targetId": target_id, "flatten": True})
            sid = att["sessionId"]
            self._tab_seq += 1
            sess = Session(self, target_id, sid, self._tab_seq)
            self._tabs[target_id] = sess
        sess.enable()
        return sess

    def find_page_target(self):
        """找出当前已打开的 page 类型 target id。"""
        tgts = self.client.send("Target.getTargets")
        for t in tgts.get("targetInfos", []):
            if t.get("type") == "page":
                return t["targetId"]
        return None

    def list_tabs(self):
        return list(self._tabs.values())

    def close_tab(self, target_id):
        sess = self._tabs.pop(target_id, None)
        if sess and self.client:
            try:
                self.client.send("Target.closeTarget", {"targetId": target_id})
            except CDPError:
                pass

    def dispose(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None


class Session:
    """一个标签页 = 一个 CDP session。"""

    def __init__(self, engine, target_id, session_id, index):
        self.engine = engine
        self.target_id = target_id
        self.session_id = session_id
        self.index = index
        self._enabled = set()

    @property
    def cdp(self):
        return self.engine.client

    def send(self, method, params=None, timeout=30.0):
        return self.cdp.send(method, params, session_id=self.session_id,
                             timeout=timeout)

    def evaluate(self, expr, await_promise=False, timeout=30.0):
        return self.cdp.evaluate(expr, session_id=self.session_id,
                                 await_promise=await_promise, timeout=timeout)

    def enable(self):
        """按需开启 CDP 域（开启后才收得到事件）。"""
        for domain in ("Page", "Runtime", "DOM", "Network", "Accessibility"):
            if domain in self._enabled:
                continue
            try:
                self.send("%s.enable" % domain)
                self._enabled.add(domain)
            except CDPError:
                pass

    def navigate(self, url, wait=True):
        """导航。

        Page.navigate 在 WebView2 上会阻塞到导航开始，若目标站点慢，
        在 UI 线程里会卡住消息循环。用 wait=False 时立即返回。
        """
        if "://" not in url and not url.startswith("about:"):
            url = "https://" + url
        self.send("Page.navigate", {"url": url})
        if wait:
            self.wait_ready()
        return url

    def wait_ready(self, timeout=15.0):
        """等导航完成。

        不能用 wait_event（它会阻塞调用线程）；在 UI 线程里阻塞会卡住
        消息循环，导致所有 CDP 命令超时。改用轮询 URL 稳定性判断。

        注意：调用方若在 UI 线程，请传 timeout 较小值或改用 wait=False。
        """
        import time as _t
        end = _t.time() + timeout
        stable = 0
        last = None
        while _t.time() < end:
            try:
                cur = self.current_url()
            except CDPError:
                cur = None
            if cur and cur == last and cur != "about:blank":
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last = cur
            _t.sleep(0.25)
        return self.current_url()

    def navigate_js(self, url):
        """用 JS 跳转页面。

        Page.navigate 在 WebView2 上会阻塞（要等渲染进程确认），
        在 UI 线程里会卡死消息循环。用 location.href 赋值等效且不阻塞。
        """
        import json
        self.evaluate("location.href = %s" % json.dumps(url))
        return url

    def current_url(self):
        try:
            return self.evaluate("location.href") or ""
        except CDPError:
            return ""

    def title(self):
        try:
            return self.evaluate("document.title") or ""
        except CDPError:
            return ""

    def back(self):
        try:
            self.evaluate("history.back()")
            return True
        except CDPError:
            return False

    def forward(self):
        try:
            self.evaluate("history.forward()")
            return True
        except CDPError:
            return False

    def reload(self):
        try:
            self.send("Page.reload", {"ignoreCache": False})
            return True
        except CDPError:
            return False

    def screenshot(self, fmt="png"):
        import base64
        res = self.send("Page.captureScreenshot", {"format": fmt})
        return base64.b64decode(res.get("data", ""))

    def close(self):
        self.engine.close_tab(self.target_id)
