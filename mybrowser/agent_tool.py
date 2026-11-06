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
from .core.dump import Dump
from .trace import Recorder, replay_script
from .net.cdpserver import CDPServer
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
                 search_engine=None, proxy="direct"):
        self.engine = WebView2Engine(port=port, headless=headless,
                                     profile_dir=profile_dir, proxy=proxy)
        # 代理模式：
        #   "direct"  -> --no-proxy-server（直连，绕过环境/系统里那个多半没在跑的
        #                本地开发代理 127.0.0.1:xxxx，避免百度等永远转圈）
        #   "system"  -> 不加任何参数，由 Chromium 用系统代理设置
        #   其他字符串 -> 作为 --proxy-server= 的值（自定义代理）
        self._proxy = proxy
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
        self._proc = None           # 我们自己拉起的 Edge 渲染进程（用于存活检测）
        self.recorder = Recorder()  # 会话录制（对齐 Lightpanda --trace）
        self.cdp_server = None       # CDP 服务器（对齐 Lightpanda serve）

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
                if self._proc_alive():
                    return True
                # 窗口被用户关掉 -> 渲染进程已死，彻底复位后重新拉起。
                # 否则下面会误判“已启动”而直接返回，导致再也不弹窗。
                self._reset()
            self._error = None
            if start_url is None:
                start_url = self.engine_search.homepage
            else:
                # 地址栏智能识别：URL/域名直接走，搜索词走搜索引擎，
                # 这样 start("豆包") 一开始就直达搜索结果，不再先开主页再跳。
                start_url = self.engine_search.normalize(start_url)

            # 渲染面：优先用系统 Edge 进程（headless 可控），
            # 有 UI 时由 ui/shell.py 提供 WebView2 控件
            proc = self._spawn_edge()
            self._proc = proc
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

            # 关键修复：接管「Edge 启动时自带的那个前台标签页」(about:blank)，
            # 而不是再 createTarget 新建一个隐藏标签——否则导航改的是隐藏标签，
            # 用户眼前的窗口永远停在 about:blank（这正是“输豆包不跳转”的根因）。
            launch_tid = None
            for _ in range(20):   # 短暂重试：启动标签可能稍后才注册进 Target.getTargets
                launch_tid = self.engine.find_page_target()
                if launch_tid:
                    break
                time.sleep(0.1)
            if launch_tid:
                # 接管启动时的前台标签页（可见的那个），而非新建隐藏标签
                sess = self.engine.attach_existing(launch_tid)
                try:
                    self.engine.client.send("Target.activateTarget",
                                            {"targetId": launch_tid})
                except Exception:
                    pass
            else:
                sess = self.engine.new_tab("about:blank")
            self.session = sess
            self.snapshotter = Snapshotter(sess)
            self.actions = Actions(sess, self.snapshotter)
            self._started = True

            if start_url:
                try:
                    sess.navigate(start_url)
                    time.sleep(0.6)
                except Exception:
                    pass
            return True
    def _spawn_edge(self):
        """起一个 Edge 渲染进程并开 CDP 端口。

        关键：把 WEBVIEW2_* 参数**只**注入这个子进程的环境变量（Popen env=），
        **不**写进程级 os.environ —— 否则会污染同进程里的主聊天窗口(pywebview)
        的 WebView2，导致两个 WebView2 抢 user-data-dir / 调试端口（UI 模式里
        这就是「浏览器纯白」的根因）。
        """
        import subprocess
        from .runtime_paths import browser_exe
        edge = browser_exe()          # 优先本体内置 Chromium，系统 Edge 仅兜底
        if not edge:
            self._error = "未找到浏览器内核（检查 runtime/chromium/chrome.exe）"
            return None
        args = [edge,
                "--remote-debugging-port=%d" % self.engine.port,
                "--user-data-dir=%s" % self.engine.profile_dir,
                "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,OptimizationHints",
                "--allow-file-access-from-files"]
        # 代理：默认直连，避开环境里那个多半没在跑的本地开发代理 127.0.0.1:xxxx
        # （它会导致百度等页面永远“转圈”加载不出）。需要代理时见 MyBrowserTool._proxy。
        if self._proxy == "direct":
            args.append("--no-proxy-server")
        elif self._proxy and self._proxy != "system":
            args.append("--proxy-server=" + self._proxy)
        if self.engine.headless:
            args.append("--headless=new")
        args.append("about:blank")
        # 仅对子进程注入 WebView2 环境变量，避免污染父进程（主窗口）
        child_env = dict(os.environ)
        child_env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = " ".join(args[1:])
        child_env["WEBVIEW2_USER_DATA_FOLDER"] = self.engine.profile_dir
        try:
            return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=child_env)
        except Exception as e:
            self._error = "渲染进程启动失败: %s" % e
            return None

    def serve(self, port=9223):
        """启动 CDP 服务器（对齐 Lightpanda 的 `serve`）。

        让 Puppeteer / Playwright / 任意 CDP 客户端能直接驱动 MyBrowser。
        """
        if self.cdp_server is None:
            self.cdp_server = CDPServer(self, port=port)
        self.cdp_server.start()
        return self.cdp_server.ws_endpoint

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

    def reset_ui(self):
        """仅复位 UI 模式下的会话状态（不销毁 engine，等下次重新接管窗口）。"""
        self._started = False
        self.session = None
        self.snapshotter = None
        self.actions = None
        self._ui = None
        self._last_snapshot = None
        self._ordinal_map = {}

    def stop(self):
        with self._lock:
            self._started = False
            # 先让浏览器通过 CDP 优雅退出：Edge 常把启动参数转交给新实例后
            # 让「启动进程」立刻结束，单靠 terminate(_proc) 关不掉真正带端口的
            # 渲染进程，会残留一串 msedge。Browser.close 只作用于我们自己的
            # 调试端口实例，不会碰到用户日常用的 Edge（不同 profile / 端口）。
            try:
                if self.engine and getattr(self.engine, "client", None):
                    self.engine.client.send("Browser.close", timeout=3.0)
            except Exception:
                pass
            if self._proc is not None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None
            # UI 模式：关掉可见窗口（在 UI 线程 Close，避免跨线程访问 COM）
            if self._ui is not None:
                ui = self._ui
                # 内嵌模式：只收起面板，【绝不】关闭主窗口（那是 INTENT 主程序）
                if getattr(ui, "_embedded", False):
                    try:
                        ui.hide()
                    except Exception:
                        pass
                    self._ui = None
                    return
                form = getattr(ui, "_form", None)
                if form is not None:
                    try:
                        form.Close()
                    except Exception:
                        pass
                self._ui = None
            try:
                self.engine.dispose()
            except Exception:
                pass

    @property
    def available(self):
        if not (self._started and self.session is not None):
            return False
        # UI 模式：窗口是否还开着（用户可能关掉了，需要重新拉起）
        if self._ui is not None:
            return bool(getattr(self._ui, "_running", False))
        # 独立 Edge 模式：看渲染进程是否还活着
        return self._proc_alive()

    def _proc_alive(self):
        """渲染进程是否还活着。_proc 为 None 表示进程不是我们拉起的
        （如 attach_to_ui 的自定义窗口），此时信任 session 状态。

        注意：Edge 会把启动参数转交给已有/新起的实例后让**启动进程立即退出**
        （poll() == 0），但真正的浏览器进程和 CDP 端口仍然活着。
        只看 poll() 会把一个完全可用的浏览器误判成「未启用」——
        表现为搜索打开了却停在初始页、SNAPSHOT 返回「浏览器未启用」。
        所以启动进程退出后再用 CDP 端口探活兜底，以真实连通性为准。
        """
        if self._proc is None:
            return True
        if self._proc.poll() is None:
            return True
        import socket
        try:
            s = socket.socket()
            s.settimeout(0.4)
            ok = s.connect_ex(("127.0.0.1", int(self.engine.port))) == 0
            s.close()
            return ok
        except Exception:
            return False

    def _reset(self):
        """彻底复位（用于窗口被用户关闭、进程已死后重新拉起）。"""
        self._started = False
        self.session = None
        self.snapshotter = None
        self.actions = None
        self._ui = None
        self._last_snapshot = None
        self._ordinal_map = {}
        old = self.engine
        try:
            if old is not None:
                old.dispose()
        except Exception:
            pass
        try:
            self.engine = WebView2Engine(port=old.port, headless=old.headless,
                                         profile_dir=old.profile_dir)
        except Exception:
            pass
        self._proc = None

    # ---------------- 协议执行 ----------------

    def _do_nav(self, url, prefix=None):
        """执行导航。prefix 非空时用它做返回前缀（搜索场景）。"""
        def _nav():
            # 优先用 UI 的 WebView2 控件导航（真导航，不经过 CDP）。
            # 注意：CoreWebView2.Navigate 是 COM 调用，必须在创建控件的 UI 线程上执行，
            # 所以用 BrowserUI.run_in_ui 派发到 UI 线程（open_url 是从 pywebview 的非
            # UI 线程进来的，直接调会抛 RPC_E_WRONG_THREAD 而静默失败）。
            if self._ui is not None:
                # 内嵌模式：任何导航都把浏览器面板显示出来（覆盖聊天）
                try:
                    self._ui.show()
                except Exception:
                    pass
                entry = self._ui._current()
                if entry is not None:
                    def _go():
                        entry["url"] = url
                        try:
                            entry["web"].CoreWebView2.Navigate(url)
                        except Exception:
                            pass
                    self._ui.run_in_ui(_go, timeout=20.0)
                    time.sleep(1.0)
                    return "%s | %s" % (self.session.title(),
                                       self.session.current_url())
            # 独立 Edge 进程路径：用 Page.navigate 真实导航，比 location.href 更稳。
            # （给 location.href 赋值会触发导航并销毁当前 JS 执行上下文，
            #  导致 Runtime.evaluate 抛错被静默吞掉，窗口停在原地、根本没跳转。）
            before = self.session.current_url()
            ok = False
            try:
                self.session.navigate(url, wait=False)
                ok = True
            except Exception:
                try:
                    self.session.navigate_js(url)
                    ok = True
                except Exception:
                    ok = False
            time.sleep(1.0)
            after = self.session.current_url()
            # 导航未生效（URL 没变化）时给出明确信号，便于上层判断是否失败。
            if not ok or (before and after and before == after
                          and url not in after):
                return "导航失败：页面未变化（%s）" % (after or before or "")
            return "%s | %s" % (self.session.title(), after)

        detail = self._in_ui(_nav, timeout=40.0)
        if prefix:
            return "%s: %s" % (prefix, detail)
        return "已导航: %s" % detail

    def execute(self, cmd_text, max_chars=None):
        """执行一条浏览器指令，返回文本结果。

        对外入口：先执行，再（若处于 trace 录制态）把这一步写入 trace。
        TRACE / RECORD / REPLAY / SERVE 自身不进入录制。
        """
        result = self._dispatch(cmd_text, max_chars=max_chars)
        parts = str(cmd_text).strip().split(" ", 1)
        action = parts[0].upper() if parts else ""
        if self.recorder.active and action not in (
                "TRACE", "RECORD", "REPLAY", "SERVE"):
            self.recorder.record(action, parts[1] if len(parts) > 1 else "",
                                 result)
        return result

    def _dispatch(self, cmd_text, max_chars=None):
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

            if action == "TRACE":
                # 对齐 Lightpanda 的 agent --trace：录制每步为 1 行 JSON
                a = (args[0].lower() if args else "status")
                if a == "on":
                    return self.recorder.start()
                if a == "off":
                    return self.recorder.stop()
                if a in ("toggle", "switch"):
                    return self.recorder.toggle()
                return self.recorder.status()

            if action == "RECORD":
                # 导出录制结果：RECORD SAVE <脚本> / RECORD TRACE <trace>
                if not args:
                    return self.recorder.status()
                sub = args[0].lower()
                if sub == "save":
                    path = (args[1] if len(args) > 1 else
                            os.path.join(os.environ.get("TEMP", "."),
                                         "mybrowser_session.js"))
                    try:
                        p = self.recorder.save_script(path)
                        return "已导出 PandaScript 脚本: %s（%d 步）" % (
                            p, len(self.recorder.steps))
                    except Exception as e:
                        return "导出脚本失败: %s" % e
                if sub == "trace":
                    path = (args[1] if len(args) > 1 else
                            os.path.join(os.environ.get("TEMP", "."),
                                         "mybrowser_trace.jsonl"))
                    try:
                        p = self.recorder.save_trace(path)
                        return "已导出 trace: %s（%d 步）" % (
                            p, len(self.recorder.steps))
                    except Exception as e:
                        return "导出 trace 失败: %s" % e
                if sub == "clear":
                    self.recorder.clear()
                    return "已清空录制"
                return "RECORD 需要 save/trace/clear 子命令"

            if action == "REPLAY":
                # 重放一段 PandaScript（确定性复现会话）
                if not args:
                    return "REPLAY 需要 脚本文件路径"
                path = args[0].strip()
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        text = f.read()
                except Exception as e:
                    return "读取脚本失败: %s" % e
                try:
                    results = replay_script(text, self.execute)
                except Exception as e:
                    return "重放失败: %s" % e
                return "已重放 %d 步:\n%s" % (
                    len(results), "\n".join(results)[:4000])

            if action == "SERVE":
                # 对齐 Lightpanda 的 serve：把 MyBrowser 暴露成 CDP 服务器
                if not args:
                    port = 9223
                else:
                    try:
                        port = int(args[0])
                    except ValueError:
                        return "SERVE 需要 端口号"
                try:
                    ep = self.serve(port)
                    return ("CDP 服务器已启动: %s\n"
                            "Puppeteer: puppeteer.connect({browserWSEndpoint:'%s'})"
                            % (ep, ep))
                except Exception as e:
                    return "SERVE 启动失败: %s" % e

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

            if action == "DUMP":
                # 对齐 Lightpanda 的 fetch --dump：把页面提取成多种格式
                if not self.session:
                    return "浏览器未就绪"
                fmt = (args[0] if args else "markdown").lower()
                n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 8000
                try:
                    d = Dump(self.session)
                    out = self._in_ui(lambda: d.dump(fmt, max_chars=n))
                    return out or "(空页面)"
                except Exception as e:
                    return "DUMP 失败: %s" % e

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
                    "DUMP/CLICK/CLICK_REF/TYPE/TYPE_REF/PRESS/HOVER/DRAG/SCROLL/"
                    "BACK/FORWARD/RELOAD/SHOT/EVAL/TRACE/RECORD/REPLAY/SERVE")

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
