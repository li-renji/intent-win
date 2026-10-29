# -*- coding: utf-8 -*-
"""WinForms / WebView2 宿主层的 CLR 初始化与组件加载。

这个模块集中处理 pythonnet + WebView2 的三个硬约束，都是踩过坑才总结出来的：

1. **COM 线程模型**
   WebView2 的 COM 初始化要求 STA (COINIT_APARTMENTTHREADED)。
   pythonnet 默认以 MTA 初始化 CLR，之后再改线程模式会抛
   `RPC_E_CHANGED_MODE (0x80010106)`。
   解法：在 `import clr` **之前**用 ctypes 调 ole32.CoInitializeEx。

2. **原生 Loader 路径**
   WebView2Loader.dll 必须能被找到（它在 pywebview 的 runtimes/win-x64/native 下）。

3. **Environment 同步获取**
   `CoreWebView2Environment.CreateAsync` 返回 Task，
   必须 `.GetAwaiter().GetResult()` 才能在 pythonnet 里拿到实体对象。
   只传 Task 不做同步等待，初始化回调不会正常触发。
"""
import ctypes
import os

_COINIT_APARTMENTTHREADED = 2
_initialized = False


def init_com_sta():
    """把当前线程初始化为 STA。必须在 import clr 之前调用。

    返回 True 表示成功（或已是 STA）。
    """
    global _initialized
    if _initialized:
        return True
    try:
        ole32 = ctypes.windll.ole32
        hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        # S_OK=0, S_FALSE=1 都算成功；RPC_E_CHANGED_MODE 才是失败
        if (hr & 0xFFFFFFFF) in (0, 1):
            _initialized = True
            return True
        return False
    except Exception:
        return False


def locate_webview2():
    """定位 WebView2 托管 DLL 与原生 Loader 目录。

    返回 (winforms_dll, core_dll, native_loader_dir)
    """
    candidates = []
    try:
        import webview
        candidates.append(os.path.join(os.path.dirname(webview.__file__), "lib"))
    except Exception:
        pass
    site = os.path.dirname(os.__file__)
    candidates.append(os.path.join(site, "webview", "lib"))

    for lib in candidates:
        dll = os.path.join(lib, "Microsoft.Web.WebView2.WinForms.dll")
        core = os.path.join(lib, "Microsoft.Web.WebView2.Core.dll")
        if os.path.exists(dll) and os.path.exists(core):
            loader = os.path.join(lib, "runtimes", "win-x64", "native")
            return dll, core, loader
    raise RuntimeError(
        "未找到 WebView2 托管组件（Microsoft.Web.WebView2.*.dll）。"
        "请安装 pywebview（自带该组件）或 pythonnet。")


def setup_clr(need_webview2=True):
    """完整初始化 CLR + WinForms + WebView2 引用。

    必须在主线程调用，且在任何 `from System...` 导入之前。
    """
    init_com_sta()

    import clr
    clr.AddReference("System.Drawing")
    clr.AddReference("System.Windows.Forms")

    if need_webview2:
        wv_dll, core_dll, loader_dir = locate_webview2()
        os.environ["PATH"] = loader_dir + os.pathsep + os.environ.get("PATH", "")
        clr.AddReference(wv_dll)
        clr.AddReference(core_dll)
    return clr


def create_environment(port, user_data_folder=None, extra_args=None):
    """创建带 CDP 调试端口的 CoreWebView2Environment。

    这是让 WebView2 打开 CDP 端口的正确方式 —— 比环境变量可靠。
    """
    from Microsoft.Web.WebView2.Core import (
        CoreWebView2Environment, CoreWebView2EnvironmentOptions)

    opts = CoreWebView2EnvironmentOptions()
    args = ["--remote-debugging-port=%d" % port]
    if user_data_folder:
        args.append("--user-data-dir=%s" % user_data_folder)
    if extra_args:
        args.extend(extra_args)
    opts.AdditionalBrowserArguments = " ".join(args)

    task = CoreWebView2Environment.CreateAsync(None, None, opts)
    # pythonnet 必须同步等待 Task，否则拿不到实体对象
    return task.GetAwaiter().GetResult()
