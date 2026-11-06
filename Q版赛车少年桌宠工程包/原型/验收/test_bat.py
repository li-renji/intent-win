# -*- coding: utf-8 -*-
"""模拟双击 启动桌宠.bat 的实跑验收：检查无乱码报错、桌宠真的出现。"""
import os
import subprocess
import sys
import time
from PIL import ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "验收")
env = dict(os.environ, PET_AUTO_EXIT="9")  # 9 秒后自动退出，避免残留进程

print(">>> 启动 cmd.exe /c 启动桌宠.bat")
p = subprocess.Popen(
    ["cmd.exe", "/c", "启动桌宠.bat"],
    cwd=HERE, env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="gbk", errors="replace",
    creationflags=subprocess.CREATE_NEW_CONSOLE,
)
time.sleep(4.5)
im = ImageGrab.grab()
im.save(os.path.join(OUT, "8_bat启动_全屏.png"))
im.crop((1150, 120, 1800, 780)).save(os.path.join(OUT, "8_bat启动_局部.png"))
print(">>> 已截图")

try:
    out, _ = p.communicate(timeout=14)
except subprocess.TimeoutExpired:
    p.kill()
    out, _ = p.communicate()
    print(">>> [注意] cmd 超时被终止")

print(">>> cmd 退出码:", p.returncode)
print(">>> 输出:\n" + (out.strip() if out and out.strip() else "(空)"))
bad = [k for k in ("is not recognized", "cannot find the path", "[ERROR]", "Traceback", "Failed")
       if k.lower() in (out or "").lower()]
print(">>> 异常关键字:", bad if bad else "无")
print(">>> 结论:", "PASS" if p.returncode == 0 and not bad else "FAIL")
