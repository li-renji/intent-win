# -*- coding: utf-8 -*-
"""INTENT-win 自带运行时的唯一定位入口。

目标：把 D:/INTENT-win 整个目录拷到一台没有 WorkBuddy、没装 Edge 的
Windows 机器上，INTENT 依然能自己跑起来 —— 不借外部 Python、不借系统浏览器。

优先级统一为：本体内置 runtime/ -> 环境变量覆盖 -> 系统回退（仅兜底）。
所有需要找 Python / WebView2 / Chromium 的地方都应调这里，不要再写死绝对路径。
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)                      # mybrowser/ 的上一级 = 本体根
RUNTIME = os.path.join(ROOT, "runtime")


def _is_file(p):
    return bool(p) and os.path.isfile(p)


def python_exe():
    """本体内置 Python；找不到时返回空字符串（调用方自行兜底）。"""
    p = os.environ.get("INTENT_PYTHON") or os.path.join(RUNTIME, "python", "python.exe")
    return p if _is_file(p) else ""


def chromium_dir():
    return os.environ.get("INTENT_CHROMIUM_DIR") or os.path.join(RUNTIME, "chromium")


def browser_exe():
    """独立浏览器窗口用的 Chromium 可执行文件（可直接带 CDP 端口启动）。

    优先本体内置的 Chrome for Testing；本体内核缺失时才退回系统 Edge，
    保证老版本目录结构也能继续工作。
    """
    d = chromium_dir()
    for name in ("chrome.exe", "msedge.exe"):
        p = os.path.join(d, name)
        if _is_file(p):
            return p
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if _is_file(p):
            return p
    return ""


def browser_source(path=None):
    """返回 (是否本体内置, 说明)，用于自检报告，避免把系统 Edge 当成自有内核。"""
    p = path or browser_exe()
    if not p:
        return False, "缺失"
    try:
        owned = os.path.commonpath([os.path.abspath(p), os.path.abspath(RUNTIME)]) == os.path.abspath(RUNTIME)
    except ValueError:
        owned = False
    return owned, ("本体内置" if owned else "系统回退")


def webview2_runtime_dir():
    """pywebview 主窗口用的 WebView2 Fixed Version 运行时目录。

    目录必须含 msedgewebview2.exe，否则返回空字符串（让 pywebview 用系统 Evergreen）。
    """
    p = os.environ.get("INTENT_WEBVIEW2_DIR") or os.path.join(RUNTIME, "webview2")
    return p if _is_file(os.path.join(p, "msedgewebview2.exe")) else ""


def status():
    """给验收 / 排错用的一段可读快照（不含任何隐私内容）。"""
    py = python_exe()
    be = browser_exe()
    wv = webview2_runtime_dir()
    owned, src = browser_source(be)
    return {
        "root": ROOT,
        "python": py or "缺失",
        "python_owned": bool(py),
        "browser": be or "缺失",
        "browser_owned": owned,
        "browser_source": src,
        "webview2_runtime": wv or "未内置（回退系统）",
        "webview2_owned": bool(wv),
    }
