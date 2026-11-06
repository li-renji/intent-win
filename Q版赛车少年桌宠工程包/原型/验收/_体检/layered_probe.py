# -*- coding: utf-8 -*-
"""验证 Win32 分层窗口 + 色键穿透能否同时做到「背景真透明」和「角色点得到」。

背景统一用品红 #FF00FF，Win32 把该色抠成透明并让鼠标穿透；
红圆是角色区，点击应被 HTML 收到（JS 计数器 +1）。
"""
import os
import sys
import threading
import time
import ctypes
import webview

HERE = os.path.dirname(os.path.abspath(__file__))
X, Y, W, H = 300, 200, 240, 240

HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:100%;height:100%;background:#FF00FF;overflow:hidden}
#pet{position:absolute;left:20px;top:20px;width:200px;height:200px;border-radius:50%;
     background:#E8412E;cursor:pointer}
</style></head><body>
<div id="pet"></div>
<script>window.__clicks=0;document.getElementById('pet').addEventListener('click',()=>{window.__clicks++});</script>
</body></html>"""

u32 = ctypes.windll.user32
GWL_EXSTYLE, WS_EX_LAYERED, LWA_COLORKEY = -20, 0x00080000, 0x00000001


def setup_layered(win):
    for _ in range(200):
        native = getattr(win, "native", None)
        if native is not None:
            try:
                hwnd = int(native.Handle.ToInt32())
                ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
                ok = u32.SetLayeredWindowAttributes(hwnd, 0xFF00FF, 0, LWA_COLORKEY)
                print("[探针] layered 设置:", "OK" if ok else "FAIL",
                      "hwnd=", hwnd, "ex=", hex(ex), flush=True)
                return hwnd
            except Exception as e:
                print("[探针] layered 失败: %r" % (e,), flush=True)
                return None
        time.sleep(0.05)
    return None


def diag(win):
    time.sleep(8)
    from PIL import ImageGrab
    out = os.path.join(HERE, "probe_layered.png")
    ImageGrab.grab(bbox=(X - 20, Y - 20, X + W + 20, Y + H + 20)).save(out)
    print("[探针] 截图:", out, flush=True)
    # 模拟点击红圆中心，验证角色区能收到点击
    px, py = X + 120, Y + 120
    old = (ctypes.c_long * 2)()
    u32.GetCursorPos(ctypes.byref(old))
    u32.SetCursorPos(px, py)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    u32.mouse_event(0x0004, 0, 0, 0, 0)
    u32.SetCursorPos(old[0], old[1])
    time.sleep(0.6)
    try:
        print("[探针] 点击红圆后 JS 计数:", win.evaluate_js("window.__clicks"), flush=True)
    except Exception as e:
        print("[探针] 读取点击计数失败: %r" % (e,), flush=True)


if __name__ == "__main__":
    ctypes.windll.user32.SetProcessDPIAware()
    p = os.path.join(HERE, "probe_layered.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(HTML)
    win = webview.create_window(
        "分层窗口探针", p, width=W, height=H, x=X, y=Y,
        frameless=True, transparent=False, on_top=True,
        resizable=False, background_color="#FF00FF",
    )
    threading.Thread(target=lambda: setup_layered(win), daemon=True).start()
    threading.Thread(target=diag, args=(win,), daemon=True).start()
    webview.start()
