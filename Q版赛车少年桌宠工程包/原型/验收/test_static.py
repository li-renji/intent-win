# -*- coding: utf-8 -*-
"""验收：静态待机时角色完全不动（连拍对比，覆盖原 1.7s 呼吸周期的多个相位）。"""
import json
import os
import subprocess
import time
import numpy as np
from PIL import Image, ImageChops, ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "验收")
env = dict(os.environ, PET_AUTO_EXIT="16", PET_POS="400,200", PET_NO_IDLE="1")

p = subprocess.Popen(
    ["C:\\Python314\\python.exe", "desktop_pet.py"],
    cwd=HERE, env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
)
time.sleep(4.0)  # 等开场气泡消失

X, Y = 400, 200          # 与 PET_POS 一致，独立于用户正在运行的实例
box = (X, Y, X + 240, Y + 240)
print(">>> 桌宠位置:", X, Y, " 采样区:", box)

shots = []
for i in range(5):
    im = ImageGrab.grab().crop(box).convert("RGB")
    shots.append(im)
    im.save(os.path.join(OUT, f"9_静止_{i}.png"))
    time.sleep(2.2)          # 5 张跨 8.8s，远超原 bob 的 1.7s 周期

diffs = []
for i in range(1, 5):
    d = ImageChops.difference(shots[0], shots[i])
    diffs.append(int(np.count_nonzero(np.any(np.asarray(d), axis=2))))
    # 存一张可见的差异图（黑色=无差异），便于定位变化来源
    if diffs[-1] > 0:
        vis = Image.fromarray(np.where(np.any(np.asarray(d), axis=2)[..., None],
                                       [255, 0, 0], [0, 0, 0]).astype("uint8"))
        vis.save(os.path.join(OUT, f"9_差异_{i}.png"))
print(">>> 与首帧的差异像素数(共 57600):", diffs)

try:
    out, _ = p.communicate(timeout=9)
except subprocess.TimeoutExpired:
    p.kill()
    out, _ = p.communicate()
print(">>> 进程退出码:", p.returncode, " 输出:", (out or "").strip() or "(空)")

ok = max(diffs) < 200 and p.returncode == 0 and "Traceback" not in (out or "")
print(">>> 结论:", "PASS 静态完全静止" if ok else "FAIL 仍有位移/异常")
