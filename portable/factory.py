# -*- coding: utf-8 -*-
"""构建「能直接装到 U 盘的全新出厂包」。

产物：portable\\dist\\INTENT-Portable-Setup\\
    安装到U盘.cmd      双击；自动找 U 盘，或传 --drive E:
    使用说明.txt
    tools\\install.py
    payload\\           <- 全新镜像，零个人数据

与 build.py（开发镜像，带本机会话/记忆/Key）的区别：
  * 配置.qdai 由 配置.example.qdai 生成，api_key 为空
  * 会话 / memories / kb / logs / data / models 一律只建空目录
  * 不带任何测试、验收、备份、构建脚本；不带真实 Key 与本机绝对路径
  * 根目录带「终端对话.lnk」「图形界面.lnk」两个换盘符不失效的快捷方式
  * 带 manifest.json / version.json / UPDATE.cmd，支持后续增量更新

空间技巧：runtime/ 用 NTFS 硬链接，出厂包在本机几乎不额外占空间；
装到 U 盘时 install.py 会实体化。
"""
import os
import shutil
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.path.join(SRC, "portable")
PKG = os.path.join(PORT, "dist", "INTENT-Portable-Setup")
PAYLOAD = os.path.join(PKG, "payload")
TOOLS = os.path.join(PKG, "tools")

CODE_DIRS = ["agent", "mybrowser", "ui", "skills", "plugins", "security",
             "tools", "docs", "market", "workbench", "extension-hub"]
TOP_FILES = ["chat.py", "chat_cli.py", "find_python.ps1", "requirements.txt",
             "配置.example.qdai", "README.md"]
PORTABLE_FILES = ["boot.py", "doctor.py", "manifest.py", "sync.py",
                  "shortcuts.py", "update.py"]
DESKTOP_FILES = ["desktop.vbs", "launch.cmd", "read_shortcut.vbs",
                 "shortcuts.vbs", "PIN-TO-DESKTOP.cmd", "autorun.inf"]
# 数据区：只建空目录，绝不拷任何内容
EMPTY_DIRS = ["会话", "memories", "models", "kb", "logs",
              os.path.join("data", "home", "Desktop"),
              os.path.join("data", "home", "Documents"),
              os.path.join("data", "资料"), os.path.join("data", "temp"),
              os.path.join("runtime", "llama")]
EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", "backups",
                "browser-upgrade", "shots", "tests", "_publish",
                "Q版赛车少年桌宠工程包", "minimind_data", "验收",
                "INTENT-Portable", "dist", "release", "_lnk_probe",
                "_factory_tmp", "_desktop_verify_tmp"}
# 只放「任何位置都不该随盘」的运行时垃圾。绝不能按文件名放构建脚本：
# runtime/python/Lib/site-packages 里真有 install.py / publish.py / factory.py，
# 按名排除会把 pip、openpyxl 的模块一起删掉。
EXCLUDE_FILES = {"pc_bridge.token", "launch.log", "debug.log",
                 "_probe_e2e.mp3"}
EXCLUDE_REL = {os.path.join("tools", "api_push.py"),
               os.path.join("tools", "api_wiz_test.py"),
               os.path.join("security", "verify_safety.py"),
               os.path.join("portable", "build.py"),
               os.path.join("portable", "factory.py"),
               os.path.join("portable", "install.py"),
               os.path.join("portable", "push.py"),
               os.path.join("portable", "publish.py"),
               os.path.join("portable", "verify.py"),
               os.path.join("portable", "verify_desktop.py"),
               os.path.join("portable", "verify_factory.py"),
               os.path.join("portable", "make_icon.py"),
               os.path.join("portable", "scrub.py"),
               # 工作台：验收脚本 + 运行时数据（含用户 Key / 节点 / 登录态，出厂包必须干净）
               os.path.join("workbench", "verify.py"),
               os.path.join("workbench", "_probe_chrome.py"),
               os.path.join("workbench", "nodes.json"),
               os.path.join("workbench", "usage.json"),
               os.path.join("workbench", "quota_report.json"),
               os.path.join("workbench", "providers.json")}
# 按完整相对路径排除的目录（不能按名字：runtime 里也有叫 data 的目录）
EXCLUDE_DIR_REL = {os.path.join("workbench", "data")}
EXCLUDE_EXT = {".pyc", ".pyo", ".log", ".tmp", ".bak", ".new", ".lnk"}
# 反复重建时，早先版本可能留下的开发文件；每次构建前精确清掉（只删这些）
MUST_NOT_EXIST = [
    "pc_bridge.token", "tools/api_push.py", "tools/api_wiz_test.py",
    "security/verify_safety.py",
    "portable/build.py", "portable/factory.py", "portable/install.py",
    "portable/push.py", "portable/publish.py", "portable/verify.py",
    "portable/verify_desktop.py", "portable/verify_factory.py",
    "portable/make_icon.py", "portable/scrub.py",
    "portable/_probe_lnk.py", "portable/_probe_com.py",
    "workbench/verify.py", "workbench/_probe_chrome.py",
    "workbench/nodes.json", "workbench/usage.json",
    "workbench/quota_report.json", "workbench/providers.json",
]
# 出厂默认值：修掉之前实测出的浏览器 UI 死锁 / 桌宠自启，并保持安全闸门开启
FACTORY_DEFAULTS = {
    "mybrowser_ui": "false",
    "companion_autostart": "false",
    "safety_mode": "enforce",
}

START_CMD = """@echo off
chcp 65001 >nul
title INTENT Portable (USB)
cd /d "%~dp0"
set "PY=%~dp0runtime\\python\\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\\boot.py" %*
if errorlevel 1 ( echo. & echo [FAILED] See the message above. & pause >nul )
"""
WINDOW_CMD = """@echo off
chcp 65001 >nul
title INTENT Portable - Window
cd /d "%~dp0"
set "PY=%~dp0runtime\\python\\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\\boot.py" --gui
if errorlevel 1 pause
"""
DOCTOR_CMD = """@echo off
chcp 65001 >nul
title INTENT Portable - Check
cd /d "%~dp0"
set "PY=%~dp0runtime\\python\\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\\boot.py" --doctor
echo.
pause
"""
SAFE_CMD = """@echo off
chcp 65001 >nul
title INTENT Portable - No key on disk
cd /d "%~dp0"
set "PY=%~dp0runtime\\python\\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\\boot.py" --safe
if errorlevel 1 pause
"""
UPDATE_CMD = """@echo off
chcp 65001 >nul
title INTENT Portable - Update
cd /d "%~dp0"
set "PY=%~dp0runtime\\python\\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\\update.py" %*
echo.
pause
"""
INSTALL_CMD = """@echo off
chcp 65001 >nul
title INTENT Portable - Install to USB
set "PY=%~dp0payload\\runtime\\python\\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0tools\\install.py" --payload "%~dp0payload" %*
echo.
pause
"""
PUSH_CMD = """@echo off
chcp 65001 >nul
title INTENT Portable - Push update to USB
cd /d "%~dp0.."
set "PY=%~dp0..\\runtime\\python\\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\\push.py" %*
echo.
pause
"""
PUBLISH_CMD = """@echo off
chcp 65001 >nul
title INTENT Portable - Publish release
cd /d "%~dp0.."
set "PY=%~dp0..\\runtime\\python\\python.exe"
if not exist "%PY%" set "PY=python"
if "%1"=="" ("%PY%" "%~dp0portable\\publish.py" --stage) else ("%PY%" "%~dp0portable\\publish.py" %*)
echo.
pause
"""
MAKE_CMD = """@echo off
chcp 65001 >nul
title INTENT Portable - Build factory package
cd /d "%~dp0.."
set "PY=%~dp0..\\runtime\\python\\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\\factory.py"
echo.
pause
"""

GUIDE = """INTENT 便携版 · 全新出厂包
========================================

【这是什么】
  一个可以直接装到 U 盘的安装包。装上去的是全新实例：
  没有会话、没有记忆、没有聊天记录、没有浏览器登录态、没有 API Key、
  没有本机文件路径。第一次用需要自己填 Key（或用 SAFE-START.cmd 临时输入）。

【怎么装】
  1. 插上 U 盘（建议空盘，至少 4GB，NTFS 或 exFAT 均可）。
  2. 双击本目录的「安装到U盘.cmd」。
     它会自动找到可移动盘并把 payload 装到 U 盘根目录。
     有多个 U 盘时会列出来让你选；也可以命令行指定：
         安装到U盘.cmd --drive E:
     预演不写盘：安装到U盘.cmd --dry-run
  3. 装完在 U 盘根目录会看到两个快捷方式：
         终端对话.lnk    -> 终端聊天
         图形界面.lnk    -> 桌面窗口聊天
     双击任意一个即可。不需要管理员、不写注册表、不改这台电脑的 PATH。

【快捷方式为什么不写死盘符】
  Windows 的 .lnk 存不了可用的相对目标。这里固定指向每台 Windows 都有的
  %ComSpec%，参数按 INTENT-PORTABLE.marker 扫 C..Z 找到 U 盘再启动。
  所以 U 盘从 E: 变 F:、换一台电脑、换一个 U 盘，图标照样能用。

【软件更新了怎么办】
  两条通道，都只传变化的文件，你的数据一个字节都不动：
    A. 本机插盘推送（最快，可更新 runtime 底座）
       在开发机运行：portable\\推送到U盘.cmd --drive E:
       或：python portable\\push.py --drive E:
    B. U 盘联网拉取（任意电脑，只更新程序文件）
       双击 U 盘根目录 UPDATE.cmd，从 GitHub 更新源拉增量。
       runtime 底座有新版时会提示回开发机走 A，不会假装更新了。
  发布新版本（开发者）：portable\\发布新版本.cmd
       生成 portable\\release\\ 更新源并提交到 GitHub，所有 U 盘
       下次点 UPDATE.cmd 就能拿到。

  被保护、永不覆盖/删除的数据：会话\\、memories\\、models\\、kb\\、
  logs\\、data\\home\\、data\\资料\\、配置.qdai、pc_bridge.token。

【目录里还有什么】
  DOCTOR.cmd      换机体检：这台电脑能不能跑、缺什么
  SAFE-START.cmd  启动前临时输入 API Key，退出后从盘上抹掉
  PIN-TO-DESKTOP.cmd  可选：把「INTENT Portable」放到这台电脑桌面
  autorun.inf     让「此电脑」里 U 盘显示 INTENT 名称与图标
  配置.example.qdai   最新默认配置模板（联网更新会同步它）

【边界（如实说明）】
  · 仅 Windows 10+ 64 位；建议 USB 3.0 / 固态 U 盘，首次启动要读 1.9GB。
  · models\\ 是空的，本地 GGUF 模型与 llama-server.exe 需自行放入。
  · 云端模型要求能联网；完全离线请配本地模型。
  · 配置.qdai 里 Key 是明文（出厂为空）；介意请用 SAFE-START.cmd。
  · 桌面快捷方式属于「插上的那台电脑」，换电脑要重新 PIN 一次；
    U 盘里的两个 .lnk 不受影响。
"""

PACKAGE_README = """INTENT Portable 出厂安装包
==========================

安装：双击「安装到U盘.cmd」，插上 U 盘即可。
      或命令行：安装到U盘.cmd --drive E:
内容：payload\\ 是全新镜像（零个人数据），tools\\ 是安装器。
说明：见 payload\\使用说明.txt

装好后 U 盘根目录会有「终端对话.lnk」「图形界面.lnk」两个快捷方式。
更新：开发机用 portable\\推送到U盘.cmd，或 U 盘上双击 UPDATE.cmd 联网拉取。
"""


def _w(path, text, bom=False, crlf=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    s = text.replace("\r\n", "\n").replace("\n", "\r\n") if crlf else text
    with open(path, "w", encoding="utf-8-sig" if bom else "ascii", newline="") as f:
        f.write(s)


def _crlf(path):
    try:
        with open(path, "r", encoding="ascii") as f:
            t = f.read()
        with open(path, "w", encoding="ascii", newline="") as f:
            f.write(t.replace("\r\n", "\n").replace("\n", "\r\n"))
    except (OSError, UnicodeError):
        pass


def _copy_tree(src, dst, hardlink=False, top=""):
    """top: 该目录在 payload 里的名字，用于按完整相对路径排除开发脚本。"""
    n = 0
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        here = top if rel == "." else os.path.normpath(os.path.join(top, rel))
        dirnames[:] = [d for d in dirnames
                       if d not in EXCLUDE_DIRS
                       and not any(os.path.normpath(os.path.join(here, d)) == x
                                   or os.path.normpath(os.path.join(here, d)).startswith(x + os.sep)
                                   for x in EXCLUDE_DIR_REL)]
        out = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(out, exist_ok=True)
        for name in filenames:
            if (name in EXCLUDE_FILES or name in EXCLUDE_DIRS
                    or os.path.splitext(name)[1].lower() in EXCLUDE_EXT):
                continue
            r = os.path.normpath(os.path.join(top, rel, name)) if rel != "." \
                else os.path.normpath(os.path.join(top, name))
            if r in EXCLUDE_REL:
                continue
            s = os.path.join(dirpath, name)
            d = os.path.join(out, name)
            if os.path.isfile(d):
                try:
                    if os.path.samefile(s, d):
                        n += 1
                        continue
                except OSError:
                    pass
                tmp = d + ".new"
                shutil.copy2(s, tmp)
                os.replace(tmp, d)
                n += 1
                continue
            done = False
            if hardlink:
                try:
                    os.link(s, d)
                    done = True
                except OSError:
                    done = False
            if not done:
                shutil.copy2(s, d)
            n += 1
    return n


def _clean_config(dst):
    """由公开模板生成配置：Key 为空 + 出厂默认开关 + 便携端口段。"""
    from build import PORTABLE_PORTS
    tpl = os.path.join(SRC, "配置.example.qdai")
    with open(tpl, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    out, seen = [], set()
    for line in lines:
        s = line.strip()
        k = s.partition("=")[0].strip() if ("=" in s and not s.startswith("#")) else None
        if k in PORTABLE_PORTS:
            out.append("%s=%s" % (k, PORTABLE_PORTS[k]))
            seen.add(k)
        elif k in FACTORY_DEFAULTS:
            out.append("%s=%s" % (k, FACTORY_DEFAULTS[k]))
            seen.add(k)
        else:
            out.append(line)
    out.append("")
    out.append("# ---- 出厂默认 / 便携端口（BOOT 启动时按占用自动校准）----")
    for k, v in PORTABLE_PORTS.items():
        if k not in seen:
            out.append("%s=%s" % (k, v))
    for k, v in FACTORY_DEFAULTS.items():
        if k not in seen:
            out.append("%s=%s" % (k, v))
    with open(os.path.join(dst, "配置.qdai"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


def _prune_stale():
    n = 0
    for rel in MUST_NOT_EXIST:
        p = os.path.join(PAYLOAD, os.path.normpath(rel))
        if os.path.isfile(p):
            os.remove(p)
            n += 1
    return n


def build():
    print("[出厂包] 目标：%s" % PKG)
    os.makedirs(PAYLOAD, exist_ok=True)
    os.makedirs(TOOLS, exist_ok=True)
    stale = _prune_stale()
    if stale:
        print("  已清除 %d 个旧版遗留的开发文件" % stale)

    # 1) runtime：硬链接（同盘 NTFS，不额外占 1.9GB）
    n = _copy_tree(os.path.join(SRC, "runtime"),
                   os.path.join(PAYLOAD, "runtime"), hardlink=True, top="runtime")
    print("  runtime/ 已链接（%d 个文件）" % n)

    # 2) 程序代码：真实复制，逐目录排除测试/凭据/开发脚本
    for d in CODE_DIRS:
        s = os.path.join(SRC, d)
        if os.path.isdir(s):
            n += _copy_tree(s, os.path.join(PAYLOAD, d), top=d)

    # 3) 顶层文件
    for f in TOP_FILES:
        s = os.path.join(SRC, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(PAYLOAD, f))
            n += 1

    # 4) 便携层与桌面层
    pdir = os.path.join(PAYLOAD, "portable")
    ddir = os.path.join(pdir, "desktop")
    os.makedirs(ddir, exist_ok=True)
    for f in PORTABLE_FILES:
        shutil.copy2(os.path.join(PORT, f), os.path.join(pdir, f))
        n += 1
    for f in DESKTOP_FILES:
        shutil.copy2(os.path.join(PORT, "desktop", f), os.path.join(ddir, f))
        n += 1
    shutil.copy2(os.path.join(PORT, "install.py"), os.path.join(TOOLS, "install.py"))

    # 5) 数据区：只建空目录
    for d in EMPTY_DIRS:
        os.makedirs(os.path.join(PAYLOAD, d), exist_ok=True)

    # 6) 干净配置 + 启动器 + 说明
    _clean_config(PAYLOAD)
    _w(os.path.join(PAYLOAD, "START.cmd"), START_CMD)
    _w(os.path.join(PAYLOAD, "WINDOW.cmd"), WINDOW_CMD)
    _w(os.path.join(PAYLOAD, "DOCTOR.cmd"), DOCTOR_CMD)
    _w(os.path.join(PAYLOAD, "SAFE-START.cmd"), SAFE_CMD)
    _w(os.path.join(PAYLOAD, "UPDATE.cmd"), UPDATE_CMD)
    _w(os.path.join(PAYLOAD, "使用说明.txt"), GUIDE, bom=True)
    _w(os.path.join(PKG, "安装到U盘.cmd"), INSTALL_CMD)
    _w(os.path.join(PKG, "使用说明.txt"), PACKAGE_README, bom=True)
    # 开发机侧三个一键命令（不属于出厂包，留在本体 portable\\ 里）
    for name, text in (("制作出厂包.cmd", MAKE_CMD),
                       ("推送到U盘.cmd", PUSH_CMD),
                       ("发布新版本.cmd", PUBLISH_CMD)):
        _w(os.path.join(PORT, name), text)
        _crlf(os.path.join(PORT, name))

    # 7) 图标 / AutoRun 入口 / 标记
    sys.path.insert(0, PORT)
    from make_icon import build as icon
    icon(os.path.join(PAYLOAD, "intent.ico"))
    shutil.copy2(os.path.join(PORT, "desktop", "autorun.inf"),
                 os.path.join(PAYLOAD, "autorun.inf"))
    shutil.copy2(os.path.join(PORT, "desktop", "PIN-TO-DESKTOP.cmd"),
                 os.path.join(PAYLOAD, "PIN-TO-DESKTOP.cmd"))
    with open(os.path.join(PAYLOAD, "INTENT-PORTABLE.marker"), "w",
              encoding="ascii", newline="\n") as f:
        f.write("INTENT Portable USB marker - do not delete\n")
    for name in ("START.cmd", "WINDOW.cmd", "DOCTOR.cmd", "SAFE-START.cmd",
                 "UPDATE.cmd", "PIN-TO-DESKTOP.cmd", "autorun.inf"):
        _crlf(os.path.join(PAYLOAD, name))
    for name in os.listdir(ddir):
        if os.path.splitext(name)[1].lower() in (".cmd", ".vbs", ".inf"):
            _crlf(os.path.join(ddir, name))
    _crlf(os.path.join(PKG, "安装到U盘.cmd"))

    # 8) 把文档/技能里写死的开发机路径换成随盘可用的 %INTENT_ROOT%
    import scrub
    nfix = scrub.scrub(PAYLOAD)
    print("  已清理 %d 个文件里的开发机绝对路径" % nfix)

    # 9) 两个快捷方式
    from shortcuts import make as make_links
    ok, msg = make_links(PAYLOAD)
    if not ok:
        raise SystemExit("[出厂包] 快捷方式生成失败：%s" % msg)
    print("  快捷方式：终端对话.lnk / 图形界面.lnk（%s）" % msg)

    # 10) 清单（runtime 只记指纹，不进云端更新源）
    import manifest as mf
    m = mf.make(PAYLOAD)
    m["base_version"] = mf.base_sig(m["files"])
    mf.save(PAYLOAD, m)
    mf.save_version(PAYLOAD, m["version"], m["base_version"], "factory build")
    print("[出厂包] 完成：%d 个文件，版本 %s，底座 %s"
          % (m["count"], m["version"], m["base_version"]))
    print("[出厂包] 通道分布：%s" % mf.stats(m["files"]))
    return PAYLOAD


if __name__ == "__main__":
    build()
    if "--path-only" in sys.argv:
        print(PAYLOAD)
