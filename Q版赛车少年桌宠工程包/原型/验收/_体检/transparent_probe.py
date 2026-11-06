# -*- coding: utf-8 -*-
"""最小验证：pywebview 在这台机器上能否做出真透明窗口。

用法: python transparent_probe.py [--opaque]
起一个 240x240 无边框窗口，里面只画一个红圆；延迟 8 秒后枚举窗口状态并
用 ImageGrab 抓取窗口所在屏幕区域。透明生效时截图应为「桌面 + 红圆」。
"""
import os
import sys
import threading
import time
import ctypes
import webview

HERE = os.path.dirname(os.path.abspath(__file__))
X, Y, W, H = 300, 200, 240, 240
OPAQUE = "--opaque" in sys.argv
TAG = "不透明窗口对照" if OPAQUE else "透明窗口探针"

HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:100%;height:100%;background:transparent;overflow:hidden}
#pet{position:absolute;left:20px;top:20px;width:200px;height:200px;border-radius:50%;
     background:#E8412E;box-shadow:inset 0 -20px 0 rgba(0,0,0,.15);}
</style></head><body><div id="pet" class="pywebview-drag-region"></div></body></html>"""


def diag():
    time.sleep(8)
    u = ctypes.windll.user32
    found = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if "探针" in buf.value or "对照" in buf.value:
                r = (ctypes.c_long * 4)()
                u.GetWindowRect(hwnd, ctypes.byref(r))
                found.append((buf.value, bool(u.IsWindowVisible(hwnd)),
                              tuple(r), hex(u.GetWindowLongW(hwnd, -20))))
        return True

    u.EnumWindows(EnumProc(cb), None)
    print("[探针] 窗口枚举:", found or "未找到", flush=True)
    try:
        from PIL import ImageGrab
        out = os.path.join(HERE, "probe_%s.png" % ("opaque" if OPAQUE else "transparent"))
        ImageGrab.grab(bbox=(X - 20, Y - 20, X + W + 20, Y + H + 20)).save(out)
        print("[探针] 截图已保存:", out, flush=True)
    except Exception as e:
        print("[探针] 截图失败: %r" % (e,), flush=True)


if __name__ == "__main__":
    ctypes.windll.user32.SetProcessDPIAware()
    p = os.path.join(HERE, "probe.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(HTML)
    webview.create_window(
        TAG, p, width=W, height=H, x=X, y=Y,
        frameless=True, transparent=not OPAQUE, on_top=True, resizable=False,
    )
    threading.Thread(target=diag, daemon=True).start()
    webview.start()
