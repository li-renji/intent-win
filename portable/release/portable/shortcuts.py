# -*- coding: utf-8 -*-
"""在 U 盘根目录生成两个不写死盘符的快捷方式。

终端对话.lnk -> START.cmd
图形界面.lnk -> WINDOW.cmd

Windows 的 .lnk 存不了可用的相对目标：写绝对路径换盘符就断，
写相对路径 ShellExecute 直接解析失败（WinError 1223）。所以快捷方式固定
指向每台 Windows 都有的 %ComSpec%，参数按 INTENT-PORTABLE.marker 扫 C..Z
找到 U 盘再 call 对应脚本。换电脑、换盘符、换 U 盘都不失效。

生成用系统自带 cscript + 同目录 shortcuts.vbs，无第三方依赖、无需管理员。
"""
import os
import subprocess

LINKS = (("终端对话.lnk", "START.cmd"), ("图形界面.lnk", "WINDOW.cmd"))


def _vbs(root):
    return os.path.join(root, "portable", "desktop", "shortcuts.vbs")


def _cscript():
    p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "cscript.exe")
    return p if os.path.isfile(p) else "cscript"


def make(root):
    """返回 (ok, 说明)。root 是 U 盘镜像根目录。"""
    vbs = _vbs(root)
    if not os.path.isfile(vbs):
        return False, "缺少 portable\\desktop\\shortcuts.vbs"
    args = [_cscript(), "//nologo", vbs, "make", root]
    for name, target in LINKS:
        args += [name, target]
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           errors="replace", timeout=30)
    except OSError as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stdout + r.stderr).strip()[:200] or ("rc=%d" % r.returncode)
    missing = [n for n, _ in LINKS if not os.path.isfile(os.path.join(root, n))]
    if missing:
        return False, "未生成: %s" % missing
    return True, "2/2"


def read(root, name="终端对话.lnk"):
    """验收用：读回 .lnk 里存的目标与相对路径。"""
    p = os.path.join(root, name)
    try:
        r = subprocess.run([_cscript(), "//nologo", _vbs(root), "read", p],
                           capture_output=True, text=True, errors="replace", timeout=30)
    except OSError as e:
        return {"ERROR": str(e)}
    out = {}
    for line in (r.stdout or "").splitlines():
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out
