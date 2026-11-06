# -*- coding: utf-8 -*-
"""把带 Alpha 的 PNG 帧序列打包成 animated WebP（浏览器原生支持透明动画）。

本机 ffmpeg 9.0.2 的 VP9 编码会丢掉 alpha（实测输出 yuv420p），
所以不采用 WebM；animated WebP 经抽帧验证四角 alpha=0，通道完整。
"""
import os
import sys
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets")
ACTIONS = ["wave", "jump", "think", "sit"]


def build(name):
    d = os.path.join(OUT, "frames", name)
    files = sorted(f for f in os.listdir(d) if f.endswith(".png"))
    ims = [Image.open(os.path.join(d, f)).convert("RGBA") for f in files]
    p = os.path.join(OUT, name + ".webp")
    ims[0].save(p, save_all=True, append_images=ims[1:],
                duration=83, loop=0, lossless=True, method=4)
    chk = Image.open(p).convert("RGBA").getchannel("A")
    corners = [chk.getpixel(q) for q in [(0, 0), (chk.width - 1, 0),
                                         (0, chk.height - 1), (chk.width - 1, chk.height - 1)]]
    ok = Image.open(p).n_frames == len(files) and all(c == 0 for c in corners)
    print("[%s] %d 帧 -> %s (%.0f KB) 四角alpha=%s %s" %
          (name, len(files), os.path.basename(p),
           os.path.getsize(p) / 1024, corners, "OK" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    results = {n: build(n) for n in ACTIONS}
    print("\n[完成]" if all(results.values()) else "\n[有失败项]")
    sys.exit(0 if all(results.values()) else 1)
