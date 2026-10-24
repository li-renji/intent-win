# -*- coding: utf-8 -*-
"""screen_ctl —— 本机屏幕截图与键鼠操控（纯 ctypes，零第三方依赖）。

- shoot()  ：GDI BitBlt 全屏截图 → 手写 PNG 编码，存到 shots/ 目录
- send_key()：组合键，如 "win+d" / "ctrl+shift+esc" / "alt+tab"
- type_text()：输入文本；ASCII 走键事件，非 ASCII（中文）走剪贴板粘贴
- click() / move()：鼠标

注意：ctypes 调用 Win32 必须显式声明 argtypes/restype，否则 64 位下句柄会被
截断成 32 位整数，导致访问违例。
"""

import ctypes
import os
import struct
import time
import zlib
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ---- 64 位安全：所有 Win32 调用必须显式声明 argtypes/restype ----
# 否则 ctypes 默认按 c_int 传句柄，64 位下句柄 > 2^31 会 OverflowError。
PVOID = ctypes.c_void_p
HANDLE = wintypes.HANDLE


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                               ctypes.c_void_p]
user32.keybd_event.restype = None
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_void_p]
user32.mouse_event.restype = None

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = PVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

VK = {
    "win": 0x5B, "lwin": 0x5B, "cmd": 0x5B,
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12,
    "shift": 0x10,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, " ": 0x20,
    "backspace": 0x08, "bs": 0x08, "del": 0x2E, "delete": 0x2E,
    "ins": 0x2D, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "prtsc": 0x2C, "printscreen": 0x2C,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
}


def _vk(token):
    """把 'a' / 'win' / 'F5' 之类的 token 转成虚拟键码。"""
    t = token.strip().lower()
    if not t:
        return None
    if t in VK:
        return VK[t]
    if len(t) == 1:
        o = ord(t.upper())
        if 0x30 <= o <= 0x39 or 0x41 <= o <= 0x5A:      # 0-9 A-Z
            return o
    return None


# ---------------------------------------------------------------- PNG 编码
def _png_chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _encode_png(width, height, rgba):
    """最小 PNG 编码器（RGBA8，无交错）。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride]
                   for y in range(height))
    idat = zlib.compress(raw, 6)
    return (b"\x89PNG\r\n\x1a\n" +
            _png_chunk(b"IHDR", ihdr) +
            _png_chunk(b"IDAT", idat) +
            _png_chunk(b"IEND", b""))


# ---------------------------------------------------------------- 截图
def shoot(root, region=None):
    """全屏（或区域）截图，存 PNG，返回描述文本（含路径与屏幕尺寸）。"""
    sw = user32.GetSystemMetrics(0)      # SM_CXSCREEN
    sh = user32.GetSystemMetrics(1)      # SM_CYSCREEN
    if sw <= 0 or sh <= 0:
        return "取屏幕尺寸失败"

    x0 = y0 = 0
    w, h = sw, sh
    if region:
        x0, y0, w, h = region
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            return "截图区域无效"

    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        return "GetDC 失败"
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    if not hdc_mem or not hbmp:
        user32.ReleaseDC(None, hdc_screen)
        return "创建兼容 DC/位图失败"
    old = gdi32.SelectObject(hdc_mem, hbmp)
    ok = gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x0, y0, SRCCOPY)

    buf = None
    if ok:
        # BITMAPINFOHEADER：32bpp 自底向上（负高度），BI_RGB
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = BI_RGB
        size = w * h * 4
        buf = ctypes.create_string_buffer(size)
        got = gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf,
                              ctypes.byref(bmi), DIB_RGB_COLORS)
        if got != h:
            buf = None

    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)

    if buf is None:
        return "取位图像素失败（GetDIBits）"

    # GetDIBits 给的是 BGRA，PNG 要 RGBA → 逐像素交换
    raw = buf.raw
    bgra = bytearray(raw)
    for i in range(0, len(bgra), 4):
        bgra[i], bgra[i + 2] = bgra[i + 2], bgra[i]
    png = _encode_png(w, h, bytes(bgra))

    out_dir = os.path.join(root, "shots")
    os.makedirs(out_dir, exist_ok=True)
    name = "shot_%s.png" % time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, name)
    with open(path, "wb") as f:
        f.write(png)
    return ("截图已保存: %s\n屏幕分辨率: %dx%d（左上角为原点，坐标范围 x∈[0,%d], y∈[0,%d]）"
            % (path, sw, sh, sw - 1, sh - 1))


# ---------------------------------------------------------------- 键鼠
def send_key(keys):
    """按下并松开一组组合键，如 'win+d'、'ctrl+shift+esc'。"""
    parts = [p for p in str(keys).split("+") if p.strip()]
    vks = []
    for p in parts:
        vk = _vk(p)
        if vk is None:
            return "无法识别的按键: %r（支持 win/ctrl/alt/shift/enter/esc/f1-f12/单个字母数字等）" % p
        vks.append(vk)
    if not vks:
        return "空按键序列"
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    return "已按键: %s" % "+".join(p.strip().lower() for p in parts)


def type_text(text):
    """输入文本。ASCII 走键事件；非 ASCII 走剪贴板粘贴（中文等）。"""
    text = str(text)
    if not text:
        return "空文本"
    for ch in text:
        o = ord(ch)
        if o < 128:
            vk = _vk(ch)
            if vk is None:
                continue
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.005)
        else:
            # 非 ASCII：用 Unicode 键事件直接送字符，不依赖剪贴板
            user32.keybd_event(o, 0, KEYEVENTF_UNICODE, 0)
            user32.keybd_event(o, 0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
            time.sleep(0.005)
    return "已输入 %d 个字符" % len(text)


def move(x, y):
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    x, y = int(x), int(y)
    if not (0 <= x < sw and 0 <= y < sh):
        return "坐标越界 (%d,%d)，屏幕 %dx%d" % (x, y, sw, sh)
    # 归一化到 0..65535（MOUSEEVENTF_ABSOLUTE 要求）
    nx = int(x * 65535 / (sw - 1)) if sw > 1 else 0
    ny = int(y * 65535 / (sh - 1)) if sh > 1 else 0
    user32.mouse_event(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, nx, ny, 0, 0)
    return "鼠标已移到 (%d,%d)" % (x, y)


def click(x, y, double=False):
    r = move(x, y)
    if "越界" in r:
        return r
    time.sleep(0.05)
    times = 2 if double else 1
    for _ in range(times):
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.05)
    return "已点击 (%d,%d)%s" % (x, y, "（双击）" if double else "")


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
