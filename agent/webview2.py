# -*- coding: utf-8 -*-
"""WebView2 适配器（Windows 端内置浏览器）。

pywebview 起一个 WebView2 窗口，AI 侧所有操作经 evaluate_js 桥执行。
将来 Linux 端以 QtWebEngine.runJavaScript 实现同一 protocol.py 即可对齐。
"""
import json
import threading

from . import protocol

try:
    import webview  # pywebview
    _HAVE_WEBVIEW = True
except ImportError:
    _HAVE_WEBVIEW = False

_SCROLL_PX = {"down": 600, "up": -600, "top": -999999, "bottom": 999999}


class WebView2Adapter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.window = None
        self._ready = threading.Event()
        self.error = None

    # ---- 生命周期 ----
    def start(self, start_url="https://www.bing.com"):
        if not _HAVE_WEBVIEW:
            self.error = "浏览器未启用：缺 pywebview（pip install pywebview）"
            return False
        t = threading.Thread(target=self._run, args=(start_url,), daemon=True)
        t.start()
        return self._ready.wait(timeout=25) or False

    def _run(self, url):
        try:
            self.window = webview.create_window(
                "奇点浏览器 · qd-agent",
                url,
                width=self.cfg["browser_width"],
                height=self.cfg["browser_height"],
            )
            self.window.events.loaded += lambda: self._ready.set()
            # 独立模式(main.py)才启动事件循环；GUI 应用模式(app.py)下循环已运行
            if not webview.windows or all(w is self.window for w in webview.windows):
                webview.start(gui="edgechromium")
        except Exception as e:
            self.error = "WebView2 启动失败: %s" % e
            self._ready.set()

    def available(self):
        return self.window is not None and self._ready.is_set() and not self.error

    # ---- JS 桥 ----
    def _js(self, script, timeout=15):
        if not self.available():
            return None
        done = threading.Event()
        box = {}

        def _task():
            try:
                box["r"] = self.window.evaluate_js(script)
            except Exception as e:
                box["err"] = str(e)
            done.set()

        threading.Thread(target=_task, daemon=True).start()
        if not done.wait(timeout):
            return None
        return box.get("r") if "err" not in box else None

    # ---- 协议执行入口（browser 文本协议 → 动作）----
    def execute(self, cmd_text, max_chars=None):
        if not _HAVE_WEBVIEW:
            return self.error or "浏览器未启用"
        if not self.available():
            return self.error or "浏览器窗口未就绪"
        max_chars = max_chars or self.cfg["snapshot_max_chars"]
        action, args = protocol.parse(cmd_text)
        if action is None:
            return "非法BROWSER指令（支持 NAVIGATE/SNAPSHOT/CLICK/TYPE/SCROLL/BACK/FORWARD/RELOAD/READ）"

        if action == "NAVIGATE":
            url = (args[0] if args else "").strip()
            if not url:
                return "NAVIGATE 需要 url"
            if "://" not in url:
                url = "https://" + url
            self.window.load_url(url)
            return "已导航: %s" % url
        if action == "SNAPSHOT":
            data = self._js(protocol._SNAPSHOT_JS)
            if data is None:
                return "快照失败"
            try:
                return protocol.format_snapshot(json.loads(data), max_chars)
            except Exception:
                return str(data)[:max_chars]
        if action == "READ":
            text = self._js(protocol._TEXT_JS) or ""
            n = max_chars
            if args and args[0].isdigit():
                n = min(int(args[0]), 8000)
            return text[:n] + ("...(truncated)" if len(text) > n else "")
        if action == "CLICK":
            if not args or not args[0].lstrip("-").isdigit():
                return "CLICK 需要 元素编号"
            return str(self._js(protocol._CLICK_JS % int(args[0])) or "超时")
        if action == "TYPE":
            if len(args) < 2 or not args[0].isdigit():
                return "TYPE 需要 元素编号 + 文本"
            return str(self._js(protocol._TYPE_JS % (int(args[0]), json.dumps(args[1], ensure_ascii=False))) or "超时")
        if action == "SCROLL":
            d = (args[0].lower() if args else "down")
            if d not in _SCROLL_PX:
                return "SCROLL 方向: down/up/top/bottom"
            return str(self._js(protocol._SCROLL_JS % _SCROLL_PX[d]) or "超时")
        if action == "BACK":
            return "OK" if self._js("history.back(); 'OK'") else "超时"
        if action == "FORWARD":
            return "OK" if self._js("history.forward(); 'OK'") else "超时"
        if action == "RELOAD":
            self.window.load_url(self.window.get_current_url())
            return "已刷新"
        return "未知动作"
