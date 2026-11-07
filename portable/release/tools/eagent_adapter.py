# -*- coding: utf-8 -*-
r"""EAgent (yaogaochaole) 适配器：在 intent-win 里作为"启动器"插件。

EAgent 是易语言写的 GUI 程序（D:\yaogaochaole），需要：
  - 本机装了 Edge 浏览器
  - 以管理员身份运行
  - 首次需手动在弹出的 Edge 里登录 DeepSeek 网页版
因此无法作为"返回文本的命令行工具"直接被 Agent 调用，这里做成"拉起 + 状态检查"的桥。

接口（与 plugins/yaogaochaole.qdplugin 对应）：
    python eagent_adapter.py <action> <arg>
      action = launch | status
      arg    = 工作目录路径（launch 时使用）
"""
import json
import os
import subprocess
import sys

PROJECT_DIR = r"D:\yaogaochaole"
RELEASE_URL = "https://github.com/YaoGaoChaoLe/EAgent/releases"
SEARCH_DIRS = [
    PROJECT_DIR,
    os.path.join(PROJECT_DIR, "dist"),
    os.path.join(PROJECT_DIR, "build"),
    r"D:\Downloads",
    r"D:\Desktop",
    r"D:\Documents",
    os.path.expanduser(r"~\Downloads"),
    os.path.expanduser(r"~\Desktop"),
]


def _find_exe():
    hits = []
    for d in SEARCH_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().startswith("eagent") and f.lower().endswith(".exe"):
                    hits.append(os.path.join(root, f))
            if len(hits) >= 5:
                break
    return hits


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


def do_status():
    hits = _find_exe()
    if hits:
        _emit({"status": "ok", "found": True, "paths": hits,
               "message": "找到 EAgent.exe，可用 launch 拉起。"})
    else:
        _emit({"status": "ok", "found": False,
               "message": ("未找到 EAgent.exe。请从 releases 下载并放到 D:\\yaogaochaole：\n"
                           + RELEASE_URL)})


def do_launch(arg):
    hits = _find_exe()
    if not hits:
        _emit({"status": "error", "code": 1, "found": False,
               "message": ("未找到 EAgent.exe，无法启动。请先下载：\n" + RELEASE_URL
                           + "\n并放到 D:\\yaogaochaole 或 Downloads。")})
        return
    exe = hits[0]
    workdir = arg.strip() if arg and arg.strip() else os.path.expanduser("~")
    if not os.path.isdir(workdir):
        _emit({"status": "error", "code": 2,
               "message": "工作目录不存在: %s" % workdir})
        return
    # 以管理员身份拉起（EAgent 需要管理员权限才能操控电脑）
    ps = (
        'Start-Process -FilePath "%s" -Verb RunAs -ArgumentList "%s"'
        % (exe, workdir)
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=30)
        _emit({"status": "ok",
               "message": ("已请求以管理员身份启动 EAgent（%s），工作目录=%s。"
                           "请在弹出的窗口选择工作目录并登录 DeepSeek 网页版，"
                           "之后用 EAgent 发消息即可。" % (exe, workdir))})
    except Exception as e:  # noqa: BLE001
        _emit({"status": "error", "code": 3, "message": "启动失败: %s" % e})


def main():
    action = (sys.argv[1].strip().lower() if len(sys.argv) > 1 else "status")
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if action == "launch":
        do_launch(arg)
    elif action == "status":
        do_status()
    else:
        _emit({"status": "error", "code": 4,
               "message": "action 必须是 launch 或 status，收到: %s" % action})


if __name__ == "__main__":
    main()
