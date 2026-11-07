# -*- coding: utf-8 -*-
"""把本体打包成一个「整个目录拷到 U 盘就能跑」的便携镜像。

产物：D:\\INTENT-win\\portable\\INTENT-Portable\\
把 INTENT-Portable 里的所有内容拷到 U 盘根目录，双击 START.cmd 即可。

空间技巧：runtime/（约 1.9GB）用 NTFS 硬链接，镜像在本机几乎不额外占空间；
拷到 U 盘时会自动实体化。数据类文件一律复制，避免镜像改配置改到本体。
"""
import os
import shutil
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DST = os.path.join(SRC, "portable", "INTENT-Portable")

# 随盘携带的目录（其余一律不带：备份、测试、截图、桌宠工程包、日志等）
ALLOW_DIRS = [
    "agent", "mybrowser", "ui", "skills", "plugins", "kb", "models",
    "会话", "memories", "security", "tools", "runtime", "docs", "market",
    "workbench", "extension-hub", "portable",
]
ALLOW_FILES = [
    "chat.py", "chat_cli.py", "find_python.ps1", "requirements.txt",
    "配置.qdai", "配置.example.qdai", "README.md",
]
# 镜像内不需要的工程文件
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "_empty_home", "backups",
             "browser-upgrade", "shots", "tests", "_publish", "logs",
             "Q版赛车少年桌宠工程包", "minimind_data", "验收",
             "_desktop_verify_tmp", "_lnk_probe", "_factory_tmp",
             "dist", "release",
             "INTENT-Portable"}   # 防止把产物自己递归拷进自己
SKIP_FILES = {"pc_bridge.token", "launch.log", "debug.log", "_probe_e2e.mp3",
              "PIN-TO-DESKTOP.cmd", "autorun.inf"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".tmp", ".bak"}
# 这些只属于构建/验收/发布工具，不进 U 盘镜像
BUILD_SKIP = {"build.py", "verify.py", "verify_desktop.py", "make_icon.py",
              "factory.py", "install.py", "push.py", "publish.py",
              "verify_factory.py", "_probe_lnk.py", "_probe_com.py",
              "scrub.py"}
PORTABLE_PORTS = {
    "port_6800": "16800", "port_6801": "16801", "http_port": "16802",
    "mybrowser_cdp_port": "19700", "local_engine_port": "18099",
    "mybrowser_serve_port": "19223", "pc_bridge_port": "18899",
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

GUIDE = """INTENT 便携版 · 使用说明
========================================

【怎么用】
  1. 把本文件夹里的所有内容拷到 U 盘（建议放 U 盘根目录）。
  2. 插到任意一台 Windows 电脑，双击 START.cmd。
     不用安装、不用管理员权限、不写注册表、不改这台电脑的 PATH。
  3. 第一次先双击 DOCTOR.cmd，看「这台电脑能不能跑」的结论。

【四个入口】
  START.cmd       终端对话（默认，只开一个窗口）
  WINDOW.cmd      桌面窗口版（豆包式聊天界面）
  DOCTOR.cmd      换机体检：缺什么、能不能跑、数据在哪
  SAFE-START.cmd  启动前临时输入 API Key，退出后从盘上抹掉

【什么东西跟着 U 盘走】
  配置.qdai        模型、API Key、所有设置
  会话\\            聊天记录与上下文
  memories\\        长期记忆
  kb\\              知识库索引与内容
  models\\          GGUF 本地模型（放这里，换电脑不丢）
  data\\home\\      浏览器登录态、缓存、工具默认输出目录
  runtime\\         Python 3.13 + WebView2 + Chromium（不借电脑上的环境）

  换句话说：会话、记忆、上下文、模型配置、登录态都存在盘里。
  插上的电脑只提供 CPU、内存、屏幕和（用云端模型时的）网络。

【端口】
  便携版用 16800 / 16801 / 16802 这一组独立端口，
  不会和这台电脑上已经装好的 INTENT（6800/6801/6802）抢。
  如果这组也被占，启动时会自动整组平移，并写回盘内配置。

【本地模型 / 离线可用】
  目前 models\\ 里还没有 GGUF 文件——这是如实状态，不是漏打包。
  要离线跑，做两件事：
    1. 把 .gguf 模型放进 models\\
    2. 把 llama-server.exe 放进 runtime\\llama\\，
       并在 配置.qdai 填 local_engine_binary=runtime/llama/llama-server.exe
  引擎二进制没有随盘携带（体积大且需自行下载官方发行包）。
  只放模型、不放引擎，DOCTOR.cmd 会明确提示「离线推理起不来」。

【能不能跑，取决于插上的电脑】
  必须：Windows 10 及以上、64 位、USB 口可写、至少 200MB 可用空间。
  建议：USB 3.0 / 固态 U 盘。1.9GB 运行时从普通 U 盘读会明显变慢，
        首次启动 Chromium / WebView2 可能要等十几秒。
  不支持：macOS、Linux、32 位 Windows、ARM 版 Windows。
  云端模型还需要那台电脑能联网；完全离线请用上面的本地模型方案。

【安全提醒（重要）】
  配置.qdai 里的 API Key 是明文。U 盘丢了 = Key 可能泄露。
  介意的话用 SAFE-START.cmd：盘上不存 Key，每次启动临时输入，退出清除。
  注意：闪存上的删除不等于物理擦除，极高安全要求请直接用临时 Key。
  另外 16800/16801 是本机端口且无鉴权，公用电脑上用完请关掉窗口。

【已知边界】
  · 知识库索引里存的是文件绝对路径，换电脑后旧索引会指向无效位置，
    重新扫描一次即可（便携模式只索引盘内 docs\\ 和 data\\资料\\，
    不会去扫插上的那台电脑的硬盘）。
  · 桌宠、插件市场、工作台等随盘携带，但默认都不自动启动。
  · 这是「免安装的绿色版」，不是编译后的单个 exe；目录结构请勿拆散。

【桌面快捷方式（本次新增）】
  Windows 7 起，U 盘插入后「自动运行程序」默认被禁用（防病毒），
  所以纯 U 盘做不到零操作自动执行；驱动需要微软签名，也不需要。
  给出两层可选项：

  A. 零安装，任意电脑
     插上 U 盘 -> 双击根目录 PIN-TO-DESKTOP.cmd（一次）
     -> 桌面出现「INTENT Portable」，以后双击它就能启动 Agent。
     会自动在当前电脑桌面建快捷方式；卸载运行
         PIN-TO-DESKTOP.cmd /U
     只写当前用户目录，不需要管理员、不改注册表、不常驻内存。

  B. 插入时自动（取决于这台电脑的 AutoPlay 策略）
     根目录 autorun.inf 带 action 与 shellexecute=PIN-TO-DESKTOP.cmd。
     在允许 U 盘 AutoRun 的电脑上，插入即自动建好桌面快捷方式；
     Windows 7 之后默认禁用 U 盘自动执行，此时右键 U 盘 -> AutoPlay
     也能一键完成。若组策略把 AutoPlay 整个关掉了，请用方案 A。

  刻意没做这几种「自动」方案：内核驱动（需微软签名）、常驻监听
  （占内存）、计划任务（要改宿主系统、且不少环境禁止创建）。
  它们都和「任意电脑免安装、不占资源」冲突，未提供也未交付。

  快捷方式不写死盘符：它调用宿主侧的 launch.cmd，后者扫描各盘找
  INTENT-PORTABLE.marker 再启动 START.cmd。U 盘换盘符、换一个 U 盘都能用。
  根目录的 autorun.inf 让「此电脑」里的 U 盘显示 INTENT 图标和名称；
  在系统允许 AutoPlay 策略时，右键 U 盘 -> AutoPlay 也能一键建快捷方式。

  注意：快捷方式属于「插上的那台电脑」，换一台新电脑要重新建一次；
        U 盘里的记忆、会话、模型始终是跟着盘走的，不受影响。

【盘内两个快捷方式（新增）】
  根目录自带「终端对话.lnk」「图形界面.lnk」，双击即用，不用装到桌面。
  .lnk 存不了可用的相对目标，所以它们固定指向每台 Windows 都有的
  %ComSpec%，参数按 INTENT-PORTABLE.marker 扫 C..Z 找 U 盘再启动。
  换电脑、换盘符、换一个 U 盘都不失效。

【软件更新（新增）】
  增量，只传 sha256 真的变了的文件；会话/记忆/模型/配置/登录态不动。
    UPDATE.cmd      U 盘联网从 GitHub 更新源拉程序文件（任意电脑）
    --check         只看有没有新版本
    --dry-run       预演不写盘
  开发机上还有「推送到U盘.cmd」（插盘增量推送，可更新 runtime 底座）
  和「发布新版本.cmd」（生成云端更新源并推到 GitHub）。
  runtime/ 约 1.9GB 不走云端；底座有新版时 UPDATE.cmd 会明确提示。
  清单：manifest.json（每个文件的 sha256 + 通道）与 version.json。
"""


def _skip_dir(name):
    return name in SKIP_DIRS


def _skip_file(name):
    return (name in SKIP_FILES or os.path.splitext(name)[1].lower() in SKIP_EXT
            or (name in BUILD_SKIP))


def _copy_tree(src, dst, hardlink=False):
    n = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
        rel = os.path.relpath(dirpath, src)
        out = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(out, exist_ok=True)
        for name in filenames:
            if _skip_file(name):
                continue
            s = os.path.join(dirpath, name)
            d = os.path.join(out, name)
            if os.path.exists(d):
                # 已存在时绝不 os.remove：既会触发删除保护，也可能误删。
                # 同一份（多为首次构建留下的硬链接）直接跳过；
                # 内容不同的，写临时文件再原子替换，不会写穿硬链接到本体。
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


def _preset_ports(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        k = s.partition("=")[0].strip() if ("=" in s and not s.startswith("#")) else None
        if k in PORTABLE_PORTS:
            out.append("%s=%s" % (k, PORTABLE_PORTS[k]))
            seen.add(k)
        else:
            out.append(line)
    out.append("")
    out.append("# ---- 便携版专用端口段（BOOT 每次启动会按占用情况自动校准）----")
    for k, v in PORTABLE_PORTS.items():
        if k not in seen:
            out.append("%s=%s" % (k, v))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


def _write(name, text, bom=False):
    path = os.path.join(DST, name)
    with open(path, "w", encoding="utf-8-sig" if bom else "ascii", newline="\r\n") as f:
        f.write(text)
    return path


def _crlf(path):
    """cmd.exe / wscript 在 LF-only 文本上容易出怪问题，统一转 CRLF。"""
    try:
        with open(path, "r", encoding="ascii") as f:
            text = f.read()
        with open(path, "w", encoding="ascii", newline="") as f:
            f.write(text.replace("\r\n", "\n").replace("\n", "\r\n"))
    except (OSError, UnicodeError):
        pass


def build():
    print("[构建] 目标：%s" % DST)
    os.makedirs(DST, exist_ok=True)
    total = 0

    # 1) runtime：硬链接（同盘 NTFS，不额外占 1.9GB）
    rt_src = os.path.join(SRC, "runtime")
    if os.path.isdir(rt_src):
        total += _copy_tree(rt_src, os.path.join(DST, "runtime"), hardlink=True)
        print("  runtime/ 已链接（Python + WebView2 + Chromium）")

    # 2) 代码与数据：真实复制，避免镜像改动影响本体
    for d in ALLOW_DIRS:
        if d == "runtime":
            continue
        s = os.path.join(SRC, d)
        if not os.path.isdir(s):
            if d in ("models", "memories"):
                os.makedirs(os.path.join(DST, d), exist_ok=True)
            continue
        total += _copy_tree(s, os.path.join(DST, d))
    for f in ALLOW_FILES:
        s = os.path.join(SRC, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(DST, f))
            total += 1

    # 3) 便携层文件（build.py / verify.py 不进镜像）
    pdir = os.path.join(DST, "portable")
    os.makedirs(pdir, exist_ok=True)
    for f in ("boot.py", "doctor.py"):
        shutil.copy2(os.path.join(SRC, "portable", f), os.path.join(pdir, f))
        total += 1

    # 4) 空目录：数据区与本地模型引擎位置
    for d in ("models", "memories", os.path.join("runtime", "llama"),
              os.path.join("data", "home", "Desktop"),
              os.path.join("data", "home", "Documents"),
              os.path.join("data", "资料"), os.path.join("data", "temp"),
              "会话", "logs"):
        os.makedirs(os.path.join(DST, d), exist_ok=True)

    # 5) 预置端口 + 启动器 + 说明
    _preset_ports(os.path.join(DST, "配置.qdai"))
    _write("START.cmd", START_CMD)
    _write("WINDOW.cmd", WINDOW_CMD)
    _write("DOCTOR.cmd", DOCTOR_CMD)
    _write("SAFE-START.cmd", SAFE_CMD)
    _write("UPDATE.cmd", UPDATE_CMD)
    _write("使用说明.txt", GUIDE, bom=True)

    # 6) 桌面快捷方式层：图标、标记、AutoRun、一键安装器
    sys.path.insert(0, os.path.join(SRC, "portable"))
    from make_icon import build as build_icon
    build_icon(os.path.join(DST, "intent.ico"))
    dsk = os.path.join(SRC, "portable", "desktop")
    for name in ("PIN-TO-DESKTOP.cmd", "autorun.inf"):
        shutil.copy2(os.path.join(dsk, name), os.path.join(DST, name))
        total += 1
    with open(os.path.join(DST, "INTENT-PORTABLE.marker"), "w",
              encoding="ascii", newline="\n") as f:
        f.write("INTENT Portable USB marker - do not delete\n")
    # 只规范化我们自己的脚本；绝不遍历 runtime/（那里是硬链接，
    # 原地改写会写穿到本体）。
    for name in ("START.cmd", "WINDOW.cmd", "DOCTOR.cmd", "SAFE-START.cmd",
                 "UPDATE.cmd", "PIN-TO-DESKTOP.cmd", "autorun.inf"):
        _crlf(os.path.join(DST, name))
    pdir2 = os.path.join(DST, "portable", "desktop")
    for name in (os.listdir(pdir2) if os.path.isdir(pdir2) else []):
        if os.path.splitext(name)[1].lower() in (".cmd", ".vbs", ".inf"):
            _crlf(os.path.join(pdir2, name))
    print("  桌面快捷方式层已生成（autorun / ico / marker / 安装器）")

    # 7) 盘内双快捷方式 + 增量清单（出厂包/推送/联网更新共用）
    import scrub
    scrub.scrub(DST)
    from shortcuts import make as make_links
    ok, msg = make_links(DST)
    if not ok:
        raise SystemExit("[构建] 盘内快捷方式生成失败：%s" % msg)
    import manifest as mf
    m = mf.make(DST)
    m["base_version"] = mf.base_sig(m["files"])
    mf.save(DST, m)
    mf.save_version(DST, m["version"], m["base_version"], "dev image build")
    print("  双快捷方式 + manifest.json 已生成（版本 %s）" % m["version"])

    logical = 0
    for dirpath, _dirs, files in os.walk(DST):
        for name in files:
            try:
                logical += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    print("[构建] 完成：%d 个文件，逻辑体积 %.2f GB" % (total, logical / 1024 ** 3))
    print("[构建] runtime 用硬链接，本机实际新增占用很小；拷到 U 盘会实体化。")
    return DST


if __name__ == "__main__":
    build()
    if "--path-only" in sys.argv:
        print(DST)
