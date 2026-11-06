# -*- coding: utf-8 -*-
"""试验：从四边连通域去掉白底，保留角色内部的白色（帽/脸/衣/鞋）。

角色有完整深色描边，内部白色不与图像边界连通，所以按连通域而非全局色键
去背可以保住它们。输出一张 RGBA 结果图供肉眼验收。
"""
import os
import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "wave_frame.png")
OUT = os.path.join(HERE, "matte_wave.png")

bgr = cv2.imread(SRC, cv2.IMREAD_COLOR)
h, w = bgr.shape[:2]
b, g, r = cv2.split(bgr.astype(np.int16))
mx = np.maximum(np.maximum(r, g), b)
mn = np.minimum(np.minimum(r, g), b)
# 浅灰白候选：亮、低饱和
light = (mn > 195) & ((mx - mn) < 32)
# 只保留与四条边界连通的候选块（真正的背景）
n, lab, stats, _ = cv2.connectedComponentsWithStats(light.astype(np.uint8), 8)
edge_labels = set(np.unique(np.concatenate([
    lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]
])).tolist())
bg = np.isin(lab, list(edge_labels)) & (lab != 0)
alpha = np.where(bg, 0, 255).astype(np.uint8)
# 边缘轻微收缩+羽化，去掉白底残留的亮边
alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
alpha = cv2.GaussianBlur(alpha, (3, 3), 0.6)

rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
rgba[:, :, 3] = alpha
cv2.imwrite(OUT, rgba)
print("[去背] 输入:", SRC)
print("[去背] 输出:", OUT)
print("[去背] 透明像素占比: %.1f%%" % ((alpha == 0).mean() * 100))
print("[去背] 中心像素 alpha(应为255):", alpha[h // 2, w // 2])
print("[去背] 左上角 alpha(应为0):", alpha[2, 2])
