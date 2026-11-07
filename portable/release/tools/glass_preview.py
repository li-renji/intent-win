#!/usr/bin/env python3
"""玻璃效果快速预览：仅起一个透明 pywebview 窗口加载 ui/index.html，
复用 agent.app 的 DWM 整窗玻璃逻辑，避免启动完整 agent（本地引擎/知识库等）。

用法:
  python tools/glass_preview.py dark     # 黑玻璃（默认）
  python tools/glass_preview.py light    # 白玻璃
窗口标题固定为 "intent"，供 tools/截图.py 捕获。
"""
import os, sys, time, threading, ctypes

import webview

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = "file://" + os.path.join(ROOT, "ui", "index.html")

# ---------- DWM 整窗玻璃（与 agent.app 保持一致）----------
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_BORDER_COLOR = 34
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_NONE = 1
_DWMSBT_TRANSIENTWINDOW = 3   # Acrylic 亚克力（Win11）


def _dwm():
    d = ctypes.windll.dwmapi
    d.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.c_void_p, ctypes.c_uint32]
    d.DwmSetWindowAttribute.restype = ctypes.c_int
    if hasattr(d, "DwmEnableBlurBehindWindow"):
        d.DwmEnableBlurBehindWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        d.DwmEnableBlurBehindWindow.restype = ctypes.c_int
    return d


def _find_hwnd(title="intent"):
    u = ctypes.windll.user32
    pid_target = os.getpid()
    best = [0, 0]
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

    def _cb(hwnd, _):
        p = ctypes.c_ulong()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid_target:
            return True
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, buf, 512)
        if title.lower() not in (buf.value or "").lower():
            return True
        if not u.IsWindowVisible(hwnd):
            return True
        rc = ctypes.create_string_buffer(16)
        u.GetWindowRect(hwnd, rc)
        rect = ctypes.cast(rc, ctypes.POINTER(ctypes.c_long * 4)).contents
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        if area > best[1]:
            best[0] = hwnd
            best[1] = area
        return True

    u.EnumWindows(EnumProc(_cb), 0)
    if best[0]:
        return best[0]
    h = u.FindWindowW(None, title)
    return h or 0


def _set_form_transparent(hwnd, on, theme):
    form = None
    try:
        for w in webview.windows:
            nat = getattr(w, "native", None)
            if nat is not None:
                form = nat
                break
    except Exception:
        form = None
    if form is None:
        return
    try:
        import clr
        clr.AddReference('System.Drawing')
        from System.Drawing import Color
        from System import Action
    except Exception as e:
        print("[玻璃] 加载 System.Drawing 失败: %s" % e, flush=True)
        return
    for _ in range(8):
        try:
            def _apply():
                try:
                    if on:
                        form.BackColor = Color.Transparent
                    else:
                        bg = Color.FromArgb(13, 16, 23) if theme != "light" else Color.FromArgb(238, 241, 246)
                        form.BackColor = bg
                except Exception as e:
                    print("[玻璃] 设置 BackColor 失败: %s" % e, flush=True)

            if getattr(form, "InvokeRequired", False):
                form.Invoke(Action(_apply))
            else:
                _apply()
            print("[玻璃] 窗体透明=%s" % ("透明" if on else "不透明(%s)" % theme), flush=True)
            return
        except Exception as e:
            print("[玻璃] 窗体透明设置重试: %s: %s" % (type(e).__name__, e), flush=True)
            time.sleep(0.5)


def _apply_dwm_glass(hwnd, theme, glass_on=True):
    if not hwnd:
        return
    try:
        d = _dwm()
        dark = (theme != "light")
        if glass_on:
            sb = ctypes.c_int(_DWMSBT_TRANSIENTWINDOW)
            if d.DwmSetWindowAttribute(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
                                      ctypes.byref(sb), ctypes.sizeof(sb)) != 0:
                if hasattr(d, "DwmEnableBlurBehindWindow"):
                    class _BB(ctypes.Structure):
                        _fields_ = [("dwFlags", ctypes.c_uint32), ("fEnable", ctypes.c_int32),
                                    ("hRgnBlur", ctypes.c_void_p), ("fTransitionOnMaximized", ctypes.c_int32)]
                    d.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(_BB(1, 1, 0, 0)))
        else:
            sb = ctypes.c_int(_DWMSBT_NONE)
            d.DwmSetWindowAttribute(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
                                    ctypes.byref(sb), ctypes.sizeof(sb))
        v = ctypes.c_int(1 if dark else 0)
        d.DwmSetWindowAttribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE,
                                ctypes.byref(v), ctypes.sizeof(v))
        _set_form_transparent(hwnd, glass_on, theme)
    except Exception as e:
        print("[玻璃] DWM 应用失败: %s: %s" % (type(e).__name__, e), flush=True)


def _refresh_dwm(hwnd, theme, glass_on=True):
    if not hwnd:
        return
    try:
        d = _dwm()
        dark = (theme != "light")
        if glass_on:
            sb = ctypes.c_int(_DWMSBT_TRANSIENTWINDOW)
            if d.DwmSetWindowAttribute(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
                                      ctypes.byref(sb), ctypes.sizeof(sb)) != 0:
                if hasattr(d, "DwmEnableBlurBehindWindow"):
                    class _BB(ctypes.Structure):
                        _fields_ = [("dwFlags", ctypes.c_uint32), ("fEnable", ctypes.c_int32),
                                    ("hRgnBlur", ctypes.c_void_p), ("fTransitionOnMaximized", ctypes.c_int32)]
                    d.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(_BB(1, 1, 0, 0)))
        else:
            sb = ctypes.c_int(_DWMSBT_NONE)
            d.DwmSetWindowAttribute(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
                                    ctypes.byref(sb), ctypes.sizeof(sb))
        v = ctypes.c_int(1 if dark else 0)
        d.DwmSetWindowAttribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE,
                                ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


# ---------- 极简 js_api（仅满足 boot 必需调用；其余容错）----------
class _Api:
    def __init__(self, theme):
        self.theme = theme
        self.glass = True
        self._hwnd = 0

    def get_settings(self):
        return {"theme": self.theme, "configured": True,
                "glass_enabled": True, "glass_refract": True,
                "workbench_enabled": True, "profiles": []}

    def get_status(self):
        return {"ok": True, "models": [], "engine": "preview", "bridge": {"running": False}}

    def list_conversations(self):
        return []

    def new_conversation(self):
        return {"id": "preview"}

    def open_conversation(self, cid):
        return {}

    def save_settings(self, d):
        if "theme" in d:
            self.theme = d["theme"]
        return {}

    def win_set_dark(self, on):
        return {}

    def win_is_dragging(self):
        return {"dragging": False}

    def set_glass(self, on):
        hwnd = self._hwnd or _find_hwnd("intent")
        self._hwnd = hwnd
        self.glass = bool(on)
        _apply_dwm_glass(hwnd, self.theme, bool(on))
        return {"ok": True}

    # ---- frameless 自定义标题栏的窗口控制（与 app.py 对齐）----
    def _w(self):
        return self._hwnd or _find_hwnd("intent")

    def win_min(self):
        import ctypes
        try:
            hwnd = self._w()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 6)
        except Exception as e:
            print("[窗口] win_min 失败: %s" % e, flush=True)
        return {"ok": True}

    def win_max(self):
        import ctypes
        try:
            if not hasattr(self, "_maxed"):
                self._maxed = False
            hwnd = self._w()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 3 if not self._maxed else 9)
                self._maxed = not self._maxed
        except Exception as e:
            print("[窗口] win_max 失败: %s" % e, flush=True)
        return {"ok": True}

    def win_close(self):
        try:
            import webview as _wv
            _wv.windows[0].close()
        except Exception:
            pass
        return {"ok": True}

    def win_drag(self):
        import ctypes
        try:
            hwnd = self._w()
            if hwnd:
                ctypes.windll.user32.ReleaseCapture()
                ctypes.windll.user32.SendMessageW(hwnd, 0xA1, 2, 0)
        except Exception as e:
            print("[窗口] win_drag 失败: %s" % e, flush=True)
        return {"ok": True}

    def win_resize(self, region):
        import ctypes
        ht = {"left": 1, "right": 2, "top": 3, "topleft": 4, "topright": 5,
              "bottom": 6, "bottomleft": 7, "bottomright": 8}.get(region, 2)
        try:
            hwnd = self._w()
            if hwnd:
                ctypes.windll.user32.ReleaseCapture()
                ctypes.windll.user32.SendMessageW(hwnd, 0xA1, ht, 0)
        except Exception as e:
            print("[窗口] win_resize 失败: %s" % e, flush=True)
        return {"ok": True}

    # 其余按钮调用（聊天/知识库等）在预览中无需实现，JS 已 try/catch 容错
    def __getattr__(self, name):
        def _noop(*a, **k):
            return {}
        return _noop


def _init(api):
    hwnd = 0
    for _ in range(200):
        hwnd = _find_hwnd("intent")
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        print("[窗口] 未取得主窗口句柄", flush=True)
        return
    api._hwnd = hwnd
    _apply_dwm_glass(hwnd, api.theme, api.glass)
    print("[窗口] 主窗口句柄 %s，整窗玻璃=开 主题=%s" % (hwnd, api.theme), flush=True)
    # 周期轻量重刷 DWM 属性（不动窗体）
    def _loop():
        while True:
            time.sleep(2)
            _refresh_dwm(api._hwnd or _find_hwnd("intent"), api.theme, api.glass)
    threading.Thread(target=_loop, daemon=True).start()


def _boot(api, win=None):
    threading.Thread(target=_init, args=(api,), daemon=True).start()


def main():
    theme = (sys.argv[1] if len(sys.argv) > 1 else "dark").lower()
    if theme not in ("dark", "light"):
        theme = "dark"
    api = _Api(theme)
    w = webview.create_window(
        "intent", UI,
        js_api=api,
        width=1080, height=760,
        min_size=(820, 560),
        transparent=True,
        frameless=True,
        easy_drag=False,
    )
    webview.start(_boot, args=(api, w), gui="edgechromium")


if __name__ == "__main__":
    main()
