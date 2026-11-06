# -*- coding: utf-8 -*-
"""MyBrowser 适配器：把自研 MyBrowser 引擎接到 agent 的浏览器文本协议。

与 WebView2Adapter 暴露**完全相同**的接口（execute / available / start /
error），于是 agent 侧（app.py / main.py / tools.py / servers.py）零改动即可
切换浏览器引擎 —— 这正是 protocol.py 当初设计的「引擎无关文本协议」目标。

MyBrowser 借系统自带的 Edge WebView2（Chromium）渲染，但浏览器程序本身
（协议栈、语义快照层、动作层、AI 能力）全部自研，并参考 Lightpanda 新增了
DUMP / TRACE-RECORD-REPLAY / SERVE 三块 AI 浏览器能力。
"""
import os
import sys
import threading

# 让顶层 mybrowser 包可被导入（它在 INTENT-win/mybrowser）
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_INTENT_ROOT = os.path.dirname(_AGENT_DIR)
if _INTENT_ROOT not in sys.path:
    sys.path.insert(0, _INTENT_ROOT)

from mybrowser.agent_tool import MyBrowserTool  # noqa: E402


def _find_intent_hwnd():
    """找本进程、标题含 'intent' 的主窗口句柄（内嵌目标 / 兜底反查）。"""
    try:
        import ctypes
        import os
        u = ctypes.windll.user32
        pid = os.getpid()
        best = [0, 0]        # (hwnd, area)
        EnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

        def _cb(hwnd, _):
            p = ctypes.c_ulong()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value != pid:
                return True
            buf = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, buf, 512)
            if "intent" not in (buf.value or "").lower():
                return True
            if not u.IsWindowVisible(hwnd):
                return True
            rc = ctypes.create_string_buffer(16)
            u.GetWindowRect(hwnd, rc)
            rect = ctypes.cast(rc, ctypes.POINTER(ctypes.c_long * 4)).contents
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > best[1]:
                best[0], best[1] = hwnd, area
            return True

        u.EnumWindows(EnumProc(_cb), 0)
        return best[0]
    except Exception:
        return 0


class MyBrowserAdapter:
    """agent 浏览器工具 —— MyBrowser 实现。

    配置项（均带默认值，无需改 配置.qdai 也能跑）：
      browser_headless       bool  是否无头（默认 False，弹窗，用户能直接看到浏览器；
                                      设为 True 才隐藏窗口、仅供 AI 静默驱动）
      mybrowser_cdp_port    int   内部 CDP 端口（默认 9700）
      mybrowser_profile_dir str   用户数据目录（默认 %APPDATA%/INTENT-win/mybrowser_profile，
                                      持久化保留 cookie/登录态，避免每次都弹百度验证）
      mybrowser_search_engine str 搜索引擎（baidu/bing/sogou/duckduckgo/google，默认 None=内置）
      mybrowser_serve       bool  启动后是否同时起 CDP 服务器（对齐 Lightpanda serve，默认 False）
      mybrowser_serve_port  int   CDP 服务器端口（默认 9223）
      mybrowser_proxy       str  代理模式：'direct'(默认,直连,绕过本地开发代理) /
                                'system'(用系统代理) / 其它字符串作 --proxy-server 值
      mybrowser_preheat     bool  应用启动即预热浏览器(默认 True,让输入即跳转零冷启动；
                                False 则启动不弹窗,首次输入时再拉起)
      mybrowser_ui          bool  是否用「自研窗口」模式(默认 True)。
                                默认：启动 BrowserUI 这个自研窗口(界面给人看)，
                                AI 处理层通过 attach_to_ui 接管同一个可见页面
                                (DUMP/TRACE/SERVE 等处理给 AI 用) —— 即豆包式双层架构。
                                设为 False：退化成独立 Edge 渲染进程(纯 AI 静默驱动，
                                用户看不到界面，仅 agent 内部用)。
      mybrowser_embed      bool  内嵌进主窗口(默认 True，仅 mybrowser_ui=true 时生效)。
                                默认：把浏览器作为「INTENT 主窗口」内的一个子面板，
                                单窗口、聊天/浏览切换，像豆包那样集成；
                                设为 False：启动独立的「INTENT 浏览器」窗口。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.error = None
        self._err = None
        self.tool = None
        self._serve_port = None

        port = int(cfg.get("mybrowser_cdp_port", 9700))
        # 默认弹窗：用户要能直接看到内置浏览器（搜"豆包"时窗口应跳出来）。
        # 只有显式 browser_headless=true 才隐藏窗口（纯 AI 静默驱动场景）。
        headless = bool(cfg.get("browser_headless", False))
        # 持久化 profile：放到 %APPDATA%，跨应用重启保留 cookie/登录态。
        # 否则默认 %TEMP% 下每次都是全新 profile，百度会反复弹安全验证，
        # 用户体验就是「开了浏览器还得在里面再搜一遍」。
        profile_dir = cfg.get("mybrowser_profile_dir") or os.path.join(
            os.environ.get("APPDATA", os.environ.get("TEMP", ".")),
            "INTENT-win", "mybrowser_profile")
        engine = cfg.get("mybrowser_search_engine")
        proxy = cfg.get("mybrowser_proxy", "direct")
        self.tool = MyBrowserTool(port=port, headless=headless,
                                  profile_dir=profile_dir,
                                  search_engine=engine, proxy=proxy)

    # ---- 生命周期 ----

    def start(self, start_url=None):
        """拉起浏览器并建 CDP 控制面。返回是否成功。

        默认（mybrowser_ui=true）走「自研窗口」模式：
          · mybrowser_embed=true（默认）→ 把浏览器**内嵌进 INTENT 主窗口**
            （单窗口，聊天/浏览切换，像豆包那样集成）；
          · mybrowser_embed=false → 启动独立的「INTENT 浏览器」窗口。
        两种情况下，AI 处理层（DUMP/TRACE/SERVE + CDP 控制面）都接管同一个可见页面。
        mybrowser_ui=false → 退化成独立 Edge 渲染进程（纯 AI 静默驱动，无界面）。
        """
        self._err = None
        if self.tool is None:
            self.error = "MyBrowser 未初始化"
            return False
        if self.cfg.get("mybrowser_ui", True):
            ok = self._start_ui(start_url)
        else:
            ok = self.tool.start(start_url)
        if not ok:
            self._err = getattr(self.tool, "_error", None) or "MyBrowser 启动失败"
            self.error = self._err
            return False
        # 可选：同时起 CDP 服务器（对齐 Lightpanda serve），让 Puppeteer/Playwright 可驱动
        if self.cfg.get("mybrowser_serve"):
            try:
                self._serve_port = self.tool.serve(
                    int(self.cfg.get("mybrowser_serve_port", 9223)))
            except Exception as e:
                self._err = "CDP 服务器启动失败: %s" % e
        return True

    def _start_ui(self, start_url=None):
        """启动「自研浏览器」（界面给人看），AI 层接管同一可见页面。

        默认内嵌进主窗口（单窗口集成）；失败或 mybrowser_embed=false 时
        退化为独立窗口。
        """
        from mybrowser.ui.shell import BrowserUI

        # 已就绪（内嵌且可用，或独立窗口还开着）→ 直接复用
        if self.tool.available:
            if start_url:
                try:
                    self.tool.execute("NAVIGATE " + start_url)
                except Exception:
                    pass
            return True

        # 之前开过但被关掉/复位 → 重新拉起
        if getattr(self.tool, "_started", False):
            try:
                self.tool.reset_ui()
            except Exception:
                pass

        port = int(self.cfg.get("mybrowser_cdp_port", 9700))
        profile_dir = (self.cfg.get("mybrowser_profile_dir")
                       or os.path.join(
                           os.environ.get("APPDATA", os.environ.get("TEMP", ".")),
                           "INTENT-win", "mybrowser_profile"))
        proxy = self.cfg.get("mybrowser_proxy", "direct")
        engine_name = self.cfg.get("mybrowser_search_engine")

        # 与适配器同端口/同数据目录/同代理/同搜索引擎，保证窗口与 AI 控制面完全一致
        ui = BrowserUI(headless=False, search_engine=engine_name, proxy=proxy)
        ui.engine.port = port
        ui.engine.profile_dir = profile_dir

        if self.cfg.get("mybrowser_embed", True):
            ok = self._start_embedded(ui)
            if not ok:
                # 内嵌失败（主窗口找不到等）→ 回退独立窗口
                ok = self._start_standalone(ui)
        else:
            ok = self._start_standalone(ui)
        if not ok:
            return False

        # 等 UI 控制面就绪（control_loop 建好 session 后 attach 接管同页）
        ok2 = self.tool.attach_to_ui(ui, timeout=30.0)
        if not ok2:
            self._err = getattr(self.tool, "_error", None) or "浏览器控制面未就绪"
            self.error = self._err
            return False

        # 初始导航：标题栏搜/输网址 → 一步直达结果页（不先开主页再跳）
        if start_url:
            try:
                self.tool.execute("NAVIGATE " + start_url)
            except Exception:
                pass
        return True

    def _start_embedded(self, ui):
        """把浏览器内嵌进主窗口（单窗口集成）。返回是否成功。"""
        form = self._main_form()
        if form is None:
            return False
        try:
            import clr
            try:
                clr.AddReference("System")
            except Exception:
                pass
            from System import Action

            def _call():
                ui.host_into(form)

            # 在主窗口所在 STA 线程上构建控件（WinForms 控件必须在创建它的线程上建）
            if getattr(form, "InvokeRequired", False):
                form.Invoke(Action(_call))
            else:
                _call()
            return True
        except Exception as e:
            self._err = "内嵌失败: %s" % e
            self.error = self._err
            return False

    def _start_standalone(self, ui):
        """独立窗口模式（旧行为 / 内嵌失败回退）。"""
        import threading as _t

        def _runner():
            # 在 STA 线程跑 WinForms 消息循环（WebView2 要求 STA，
            # 否则 EnsureCoreWebView2Async 初始化会失败）。窗口关掉时 run() 自然退出。
            ui.run()

        launched = False
        try:
            import clr
            from System.Threading import (
                Thread as STAThread, ApartmentState, ThreadStart)
            th = STAThread(ThreadStart(_runner))
            th.SetApartmentState(ApartmentState.STA)
            th.Start()
            launched = True
        except Exception:
            pass
        if not launched:
            # 兜底：直接起 python 线程（不保证 STA，仅当 clr 不可用）
            _t.Thread(target=_runner, daemon=True).start()
        return True

    def _main_form(self, wait=12.0):
        """取 INTENT 主窗口的 WinForms.Form（内嵌目标）。轮询最多 wait 秒。"""
        import time
        end = time.time() + wait
        while time.time() < end:
            # 主路径：pywebview 暴露的 native 就是承载聊天 WebView2 的 Form
            try:
                import webview
                if webview.windows:
                    nat = getattr(webview.windows[0], "native", None)
                    if nat is not None:
                        return nat
            except Exception:
                pass
            # 兜底：按句柄反查本进程的 intent 窗口
            try:
                import clr
                clr.AddReference('System.Windows.Forms')
                from System.Windows.Forms import Control
                from System import IntPtr
                hwnd = _find_intent_hwnd()
                if hwnd:
                    c = Control.FromHandle(IntPtr(hwnd))
                    if c is not None:
                        return c
            except Exception:
                pass
            time.sleep(0.2)
        return None

    def show(self):
        """把内嵌浏览器显示出来（覆盖聊天）；独立窗口也尽量置顶。"""
        ui = getattr(self.tool, "_ui", None)
        if ui is not None:
            try:
                ui.show()
            except Exception:
                pass

    def stop(self):
        if self.tool is not None:
            try:
                self.tool.stop()
            except Exception:
                pass

    def available(self):
        """浏览器是否就绪（方法，兼容 WebView2Adapter 接口）。"""
        return bool(self.tool is not None and self.tool.available)

    # ---- 协议执行入口（与 WebView2Adapter 同签名）----

    def execute(self, cmd_text, max_chars=None):
        """执行一条浏览器文本指令，返回文本结果。

        支持 protocol.py 的基础指令（NAVIGATE/SNAPSHOT/CLICK/TYPE/SCROLL/
        BACK/FORWARD/RELOAD/READ），以及 MyBrowser 扩展的 DUMP/DIFF/EVAL/
        TRACE/RECORD/REPLAY/SERVE。
        """
        if self.tool is None:
            return self.error or "浏览器未启用"
        try:
            return self.tool.execute(cmd_text, max_chars=max_chars)
        except Exception as e:
            return "浏览器操作失败: %s" % e

    # ---- 可选：CDP 服务器（对齐 Lightpanda serve）----

    def serve(self, port=None):
        """手动起 CDP 服务器，返回 ws 端点；未起则 None。"""
        if self.tool is None:
            return None
        try:
            return self.tool.serve(
                int(port or self.cfg.get("mybrowser_serve_port", 9223)))
        except Exception as e:
            self._err = "CDP 服务器启动失败: %s" % e
            return None

    @property
    def cdp_endpoint(self):
        return self._serve_port
