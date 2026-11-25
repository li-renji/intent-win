# -*- coding: utf-8 -*-
"""模拟 Linux 解释器环境：仅当 INTENT_LINUX_SIM=1 时生效。

被 Python 启动时自动加载（放在 PYTHONPATH 首位）。作用是模拟真实 Linux 上
"没有 ctypes.windll / 没有 pywin32 / 没有 pythonnet" 的事实，让核心链路的
跨平台问题在当前 Windows 机器上也能暴露出来。

用法：
    INTENT_LINUX_SIM=1 PYTHONPATH=<本目录> python -m agent.main --no-gui
"""
import os
import sys

if os.environ.get("INTENT_LINUX_SIM") == "1":
    import ctypes

    # 真实 Linux 的 ctypes 没有这些 Windows 专有属性
    for _name in ("windll", "oledll", "WINFUNCTYPE", "WINFUNC", "c_wchar"):
        try:
            delattr(ctypes, _name)
        except (AttributeError, TypeError):
            pass

    # 屏蔽 Windows 专有 + 所有 GUI 第三方模块：置 None 会让 import 抛 ImportError。
    # 真实 Linux 干净 Python3 里这些都不存在，必须确保核心链路不依赖它们。
    for _mod in ("clr", "pythonnet", "win32api", "win32com", "win32con",
                 "win32gui", "win32process", "pywintypes",
                 "webview", "bottle", "proxy_tools"):
        sys.modules[_mod] = None

    # 让 sys.platform 表现为 linux（可选）。默认不开：在 Windows 解释器上强改
    # sys.platform 会让 selectors 误选 epoll，制造与真实 Linux 无关的假故障。
    # 真实 Linux 上 sys.platform 本来就是 linux，无需此处模拟。
    if os.environ.get("INTENT_LINUX_SIM_PLATFORM") == "1":
        sys.platform = "linux"

    if os.environ.get("INTENT_LINUX_SIM_VERBOSE") == "1":
        print("[linux_sim] ctypes.windll=%s sys.platform=%s" %
              (hasattr(ctypes, "windll"), sys.platform), flush=True)
