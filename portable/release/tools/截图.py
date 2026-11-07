#!/usr/bin/env python3
"""开发辅助：截取 intent 主窗口并保存为 BMP（无第三方依赖，纯 ctypes GDI）。

用法: python tools/截图.py [输出路径.bmp]
"""
import ctypes, struct, sys, time
from ctypes import wintypes

u = ctypes.windll.user32
g = ctypes.windll.gdi32
try:
    u.SetProcessDPIAware()
except Exception:
    pass
u.FindWindowW.restype = ctypes.c_void_p
hwnd = u.FindWindowW(None, "intent")
if not hwnd:
    print("找不到 intent 窗口")
    sys.exit(1)


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class RECT(ctypes.Structure):
    _fields_ = [("L", ctypes.c_long), ("T", ctypes.c_long), ("R", ctypes.c_long), ("B", ctypes.c_long)]


def capture(path, step=2, focus=True):
    if focus:
        # 先把窗口提到前台，否则截到的是压在上面的别的窗口
        try:
            u.ShowWindow(hwnd, 9)          # SW_RESTORE
            u.SetForegroundWindow(hwnd)
            u.BringWindowToTop(hwnd)
            time.sleep(0.6)
        except Exception:
            pass
    W = u.GetSystemMetrics(0); H = u.GetSystemMetrics(1)
    hdc = u.GetDC(0); memdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, W, H); g.SelectObject(memdc, bmp)
    g.BitBlt(memdc, 0, 0, W, H, hdc, 0, 0, 0x00CC0020)
    bi = BIH(); bi.biSize = 40; bi.biWidth = W; bi.biHeight = -H
    bi.biPlanes = 1; bi.biBitCount = 32; bi.biCompression = 0
    buf = ctypes.create_string_buffer(W * H * 4)
    g.GetDIBits(memdc, bmp, 0, H, buf, ctypes.byref(bi), 0)
    r = RECT(); u.GetWindowRect(hwnd, ctypes.byref(r))
    g.DeleteObject(bmp); g.DeleteDC(memdc); u.ReleaseDC(0, hdc)
    raw = buf.raw
    nraw = len(raw)
    # 窗口可能被拖到屏幕外 / 最小化 / 停在副屏，直接用窗口矩形去索引整屏缓冲会越界崩溃。
    # 这里先夹到屏幕范围内，再逐个像素做边界保护。
    x0 = max(0, min(W - 1, r.L)); y0 = max(0, min(H - 1, r.T))
    x1 = max(0, min(W, r.R));     y1 = max(0, min(H, r.B))
    cw, ch = x1 - x0, y1 - y0
    if cw <= 0 or ch <= 0:
        print("窗口不在可见屏幕范围内：rect=%s,%s,%s,%s 屏幕=%dx%d"
              % (r.L, r.T, r.R, r.B, W, H))
        sys.exit(1)
    ow, oh = max(1, cw // step), max(1, ch // step)
    rows = []
    for oy in range(oh):
        base = (y0 + oy * step) * W * 4
        line = bytearray()
        for ox in range(ow):
            i = base + (x0 + ox * step) * 4
            if i + 2 >= nraw:      # 越界就补黑，绝不让它崩
                line += b"\x00\x00\x00"
            else:
                line += bytes((raw[i + 2], raw[i + 1], raw[i]))     # BGR -> RGB
        rows.append(bytes(line))
    pad = (ow * 3 + 3) // 4 * 4
    data = bytearray()
    for line in rows:
        data += line + b"\x00" * (pad - len(line))
    with open(path, "wb") as f:
        f.write(struct.pack("<2sIHHI", b"BM", 14 + 40 + len(data), 0, 0, 14 + 40))
        f.write(struct.pack("<IiiHHIIiiII", 40, ow, -oh, 1, 24, 0, len(data), 0, 0, 0, 0))
        f.write(data)
    print("已保存", path, "%dx%d" % (ow, oh))


if __name__ == "__main__":
    capture(sys.argv[1] if len(sys.argv) > 1 else "win.bmp")
