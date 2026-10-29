#!/usr/bin/env python3
"""开发辅助：按「截图裁剪坐标」点击 intent 窗口（裁剪坐标 = 截图上的位置，step=2 缩放）。

用法: python tools/点击.py <x> <y> [说明]
坐标取自 tools/截图.py 输出的截图（810x570 裁剪坐标系）。
"""
import ctypes, sys, time
from ctypes import wintypes

u = ctypes.windll.user32
try:
    u.SetProcessDPIAware()
except Exception:
    pass
u.FindWindowW.restype = ctypes.c_void_p
hwnd = u.FindWindowW(None, "intent")
if not hwnd:
    print("找不到 intent 窗口")
    sys.exit(1)
u.ShowWindow(hwnd, 9)
u.SetForegroundWindow(hwnd)
time.sleep(0.7)


class RECT(ctypes.Structure):
    _fields_ = [("L", ctypes.c_long), ("T", ctypes.c_long), ("R", ctypes.c_long), ("B", ctypes.c_long)]


r = RECT()
u.GetWindowRect(hwnd, ctypes.byref(r))
STEP = 2                      # 与 tools/截图.py 的 step 一致
x = int(sys.argv[1]) * STEP + r.L
y = int(sys.argv[2]) * STEP + r.T
u.SetCursorPos(x, y)
time.sleep(0.25)
u.mouse_event(0x0002, 0, 0, None)     # LEFTDOWN
time.sleep(0.08)
u.mouse_event(0x0004, 0, 0, None)     # LEFTUP
time.sleep(1.2)
print("已点击裁剪坐标 (%s,%s) → 屏幕 (%d,%d)  %s" % (sys.argv[1], sys.argv[2], x, y,
      sys.argv[3] if len(sys.argv) > 3 else ""))
