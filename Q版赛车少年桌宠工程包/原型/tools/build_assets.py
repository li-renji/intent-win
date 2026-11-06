# -*- coding: utf-8 -*-
"""把工程包现有素材处理成原型可用的透明素材。

输入（工程包/素材-图片视频/）：
  pet_main_transparent.png  实为 JPEG 白底「白衣版」立绘
  wave/jump/think/sit.mp4   白底动作视频（HEVC 720x720 / 24fps / 121帧）

输出（原型/assets/）：
  idle.png                 真透明静态立绘（脚底对齐、居中）
  frames/<动作>/000.png... 真透明帧序列（12fps，用于 tkinter 桌宠）
  <动作>.webm              带 Alpha 的 WebM（用于浏览器预览，若编码可用）

去背原理：角色有完整深色描边，内部白色（帽/脸/衣/鞋）不与画面边界
连通；因此只去掉「与四边连通的浅灰白区域」，而不是全局色键抠白。
"""
import os
import shutil
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.dirname(ROOT)
SRC = os.path.join(PKG, "素材-图片视频")
OUT = os.path.join(ROOT, "assets")
CANVAS = 240          # 统一画布边长
FEET_Y = 232          # 脚底对齐线
BODY_H = 214          # 角色统一高度
ACTIONS = ["wave", "jump", "think", "sit"]
FRAME_STEP = 2        # 24fps -> 12fps


def remove_white_bg(bgr):
    """连通域去白底，返回 RGBA（BGRA 顺序的 4 通道数组）。"""
    b, g, r = cv2.split(bgr.astype(np.int16))
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    light = (mn > 195) & ((mx - mn) < 32)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(light.astype(np.uint8), 8)
    edges = set(np.unique(np.concatenate(
        [lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]])).tolist())
    bg = np.isin(lab, list(edges)) & (lab != 0)
    a = np.where(bg, 0, 255).astype(np.uint8)
    a = cv2.morphologyEx(a, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = a
    return rgba


def to_pil(bgra):
    return Image.fromarray(cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA))


def normalize(im, bbox_union=None):
    """裁到角色边界，统一高度，脚底对齐、水平居中贴到 CANVAS 画布。"""
    a = im.getchannel("A")
    bb = a.point(lambda v: 255 if v > 128 else 0).getbbox()
    if bb is None:
        return Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    crop = im.crop(bb)
    w, h = crop.size
    nh = BODY_H
    nw = max(1, int(round(w * nh / h)))
    crop = crop.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    canvas.paste(crop, ((CANVAS - nw) // 2, FEET_Y - nh), crop)
    return canvas


def build_idle():
    """待机态用 wave 的第 0 帧，而不是那张静态 JPEG 立绘。

    原因：工程包里的静态立绘（实为 JPEG）与 4 段动作视频不是同一批画面——
    静态图是睁眼插兜、角色宽 99px，视频帧宽 118px 且表情不同。直接用静态图
    做 idle，动作切换时会明显"变脸 + 跳大小"。取视频首帧可保证形象/尺寸/
    姿态完全同源，切换无跳变。静态立绘另存为参考，不进入运行时。
    """
    cap = cv2.VideoCapture(os.path.join(SRC, "wave.mp4"))
    ok, bgr = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("读不到 wave.mp4 首帧")
    out = normalize(to_pil(remove_white_bg(bgr)))
    p = os.path.join(OUT, "idle.png")
    out.save(p)

    # 静态立绘留作参考（去背后），不参与运行时，避免混淆
    jpg = os.path.join(SRC, "pet_main_transparent.png")
    bgr2 = cv2.imread(jpg, cv2.IMREAD_COLOR)
    if bgr2 is not None:
        ref = normalize(to_pil(remove_white_bg(bgr2)))
        ref.save(os.path.join(OUT, "idle_static_reference.png"))
    a = out.getchannel("A")
    print("[待机] 取自 wave 首帧 ->", p,
          "透明占比 %.1f%%" % ((np.array(a) == 0).mean() * 100))
    return p


def build_action(name):
    src = os.path.join(SRC, name + ".mp4")
    cap = cv2.VideoCapture(src)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames_dir = os.path.join(OUT, "frames", name)
    os.makedirs(frames_dir, exist_ok=True)
    # 帧名固定 000..060，直接覆盖写入即可，不做批量删除（避免误删/安全拦截）

    # 第一遍：去背 + 记录每帧角色 bbox，求并集，保证跨动作/跨帧位置稳定
    raws = []
    idx = 0
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        if idx % FRAME_STEP:
            idx += 1
            continue
        raws.append(remove_white_bg(bgr))
        idx += 1
    cap.release()
    print("[%s] 抽取 %d 帧 (共 %d)" % (name, len(raws), total))

    outs = []
    for i, bgra in enumerate(raws):
        im = normalize(to_pil(bgra))
        p = os.path.join(frames_dir, "%03d.png" % i)
        im.save(p)
        outs.append(p)
    return outs


def find_ffmpeg():
    """ffmpeg 由 imageio-ffmpeg 提供；脚本可能跑在没有该包的解释器下，
    所以也支持 FFMPEG_EXE 环境变量指定路径。"""
    ff = os.environ.get("FFMPEG_EXE", "").strip('"')
    if ff and os.path.isfile(ff):
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def build_webm(name, frames):
    ff = find_ffmpeg()
    if not ff:
        print("[%s] 跳过 WebM：找不到 ffmpeg（可设 FFMPEG_EXE）" % name)
        return None
    out = os.path.join(OUT, name + ".webm")
    cmd = [ff, "-y", "-loglevel", "error", "-framerate", "12",
           "-i", os.path.join(OUT, "frames", name, "%03d.png"),
           "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
           "-auto-alt-ref", "0", "-b:v", "0", "-crf", "32",
           "-an", out]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    if r.returncode == 0 and os.path.exists(out):
        print("[%s] WebM -> %s (%.0f KB)" % (name, os.path.basename(out),
                                             os.path.getsize(out) / 1024))
        return out
    print("[%s] WebM 编码失败，跳过；帧序列仍可用: %s" %
          (name, (r.stderr or "").strip()[:160]))
    return None


def main():
    os.makedirs(OUT, exist_ok=True)
    webm_only = "--webm-only" in sys.argv
    if not webm_only:
        build_idle()
    for n in ACTIONS:
        frames_dir = os.path.join(OUT, "frames", n)
        frames = sorted(
            os.path.join(frames_dir, f)
            for f in os.listdir(frames_dir) if f.endswith(".png")
        ) if os.path.isdir(frames_dir) else []
        if not frames:
            frames = build_action(n)
        elif not webm_only:
            print("[%s] 帧序列已存在 %d 张，复用" % (n, len(frames)))
        build_webm(n, frames)
    print("\n[完成] 输出目录:", OUT)


if __name__ == "__main__":
    sys.exit(main())
