# -*- coding: utf-8 -*-
"""生成随盘的 intent.ico（32x32 / 32bpp，纯标准库，不依赖 Pillow）。

用于 autorun.inf 的盘符图标和桌面快捷方式图标：深蓝圆角块 + 白色 I。
"""
import struct
import os

W = H = 32


def _pixel(x, y):
    # 圆角矩形：角心 (9,9)..(22,22)，半径 8
    cx = min(max(x, 9), 22)
    cy = min(max(y, 9), 22)
    inside = (x - cx) ** 2 + (y - cy) ** 2 <= 64
    if not inside:
        return (0, 0, 0, 0)                      # 透明
    # 竖向渐变：B 通道由深到亮，做出一点体积感
    t = y / float(H - 1)
    b = int(120 + 90 * t)
    g = int(70 + 50 * t)
    r = int(38 + 30 * t)
    # 白色 "I"：上下横 + 中间竖
    is_i = ((9 <= y <= 11 or 20 <= y <= 22) and 10 <= x <= 21) or \
           (11 <= y <= 20 and 14 <= x <= 17)
    if is_i:
        r = g = b = 245
    return (b, g, r, 255)                        # BGRA


def build(path):
    xor = bytearray()
    for y in range(H - 1, -1, -1):               # BMP 是自下而上
        for x in range(W):
            xor.extend(_pixel(x, y))
    and_mask = b"\x00" * (4 * H)                 # 1bpp，每行补齐 4 字节
    dib = struct.pack("<IiiHHIIiiII", 40, W, H * 2, 1, 32, 0,
                      len(xor) + len(and_mask), 0, 0, 0, 0)
    img = dib + bytes(xor) + and_mask
    head = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, len(img), 6 + 16)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(head + entry + img)
    return path


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "INTENT-Portable", "intent.ico")
    print(build(out))
