# -*- coding: utf-8 -*-
"""实测桌宠进程内存（空闲 / 播放动作 / 回到待机），并检查开机自启项。"""
import ctypes
import os
import subprocess
import time
import winreg
from ctypes import wintypes

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = r"C:\Python314\python.exe"


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


psapi = ctypes.WinDLL("psapi")
k32 = ctypes.WinDLL("kernel32")
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def mem(pid):
    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        return None
    p = PMC()
    p.cb = ctypes.sizeof(PMC)
    ok = psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb)
    k32.CloseHandle(h)
    if not ok:
        return None
    return (p.WorkingSetSize / 1048576, p.PeakWorkingSetSize / 1048576,
            p.PagefileUsage / 1048576)


def show(tag, m):
    if m:
        print(f"  {tag:16s} 工作集 {m[0]:6.1f} MB | 峰值 {m[1]:6.1f} MB | 提交 {m[2]:6.1f} MB")
    else:
        print(f"  {tag:16s} 读取失败")


print("=== 桌宠原型进程内存实测 ===")
env = dict(os.environ, PET_AUTO_EXIT="12", PET_POS="400,200", PET_NO_IDLE="1")
p = subprocess.Popen([PY, "desktop_pet.py"], cwd=HERE, env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(3)
show("待机 idle", mem(p.pid))

# 模拟单击，触发 wave 动作（懒加载 61 帧，用来测峰值）
u = ctypes.windll.user32
old = (ctypes.c_long * 2)()
u.GetCursorPos(ctypes.byref(old))
u.SetCursorPos(520, 320)
u.mouse_event(0x0002, 0, 0, 0, 0)
u.mouse_event(0x0004, 0, 0, 0, 0)
u.SetCursorPos(old[0], old[1])
time.sleep(2.5)
show("播放动作中", mem(p.pid))
time.sleep(3.5)
show("回到待机", mem(p.pid))

try:
    out, _ = p.communicate(timeout=6)
except subprocess.TimeoutExpired:
    p.kill()
    out, _ = p.communicate()
print("  进程退出码:", p.returncode, " 输出:", (out or b"").decode("utf-8", "replace").strip() or "(空)")

print()
print("=== 开机自启项检查 ===")
KEYWORDS = ("intent", "桌宠", "pet", "ajue", "阿觉")
for label, hive, path in [
    ("HKCU Run", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM Run", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
]:
    try:
        k = winreg.OpenKey(hive, path)
        total, hits = 0, []
        i = 0
        while True:
            try:
                name, val, _ = winreg.EnumValue(k, i)
            except OSError:
                break
            total += 1
            if any(s in (name + str(val)).lower() for s in KEYWORDS):
                hits.append(name)
            i += 1
        winreg.CloseKey(k)
        print(f"  {label}: 共 {total} 项, 与桌宠/INTENT 相关 {len(hits)} 项 {hits or ''}")
    except OSError as e:
        print(f"  {label}: 读取失败 {e}")

startup = os.path.join(os.environ.get("APPDATA", ""),
                       r"Microsoft\Windows\Start Menu\Programs\Startup")
try:
    fs = os.listdir(startup)
    hit = [f for f in fs if any(s in f.lower() for s in KEYWORDS)]
    print(f"  启动文件夹: 共 {len(fs)} 个文件, 相关 {len(hit)} 个 {hit or ''}")
except OSError as e:
    print(f"  启动文件夹: 读取失败 {e}")
