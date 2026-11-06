# -*- coding: utf-8 -*-
"""阿觉：开机自启开关（**阿觉插件的一部分**）。

两种用法
--------
1. **桌宠界面**（推荐）：「设置 → 监听 → 开机自启」开关，直接调用本模块；
2. **命令行**：在项目根目录执行
       python plugins/ajue/enable_autostart.py install|uninstall|status

说明
----
- 写的是 **HKCU（当前用户）启动项**：不需要管理员权限，也不影响别的用户；
- 用 **pythonw.exe** 启动，**不弹任何黑窗口**，真正「后台隐身」；
- 顺带把 ``listen.enabled`` 打开——不然开机启动了它也不会真的在听；
- 监听默认「仅插电时开启」（省电），可在桌宠「设置 → 监听」里改。

为什么它待在插件内部
--------------------
「开机自启」是阿觉自己的部署方式，不是给模型用的技能，也不是一个独立扩展。
所以它只是本插件的一个模块：界面开关和命令行走的是同一套实现。
"""
import json
import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AjueListenDaemon"

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))
RUNNER = os.path.join(PLUGIN_DIR, "runner.py")
DATA_DIR = os.path.join(PROJECT_ROOT, "plugin_data", "ajue")
CONFIG_PATH = os.path.join(DATA_DIR, "ajue_config.json")


def _pythonw():
    """找 pythonw.exe（同目录的无窗口解释器），找不到就退回 python.exe。"""
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def _command():
    # 不传 --endpoint：runner 自己会去读宿主配置解析端点，宿主换端口也不会失效
    return '"%s" "%s" --listen-daemon --data-dir "%s"' % (
        _pythonw(), RUNNER, DATA_DIR)


def _read_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _listen_enabled():
    return bool((_read_config().get("listen") or {}).get("enabled"))


def _enable_listen():
    cfg = _read_config()
    lis = dict(cfg.get("listen") or {})
    lis["enabled"] = True
    cfg["listen"] = lis
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


# ------------------------------------------------------------ 对外 API
def install():
    """安装开机自启。返回 {ok, message, command}。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
        winreg.CloseKey(key)
    except Exception as e:
        return {"ok": False, "enabled": False,
                "message": "设置开机自启失败：%s" % e}
    _enable_listen()
    return {"ok": True, "enabled": True, "command": _command(),
            "message": "已设置开机自启。开机后阿觉会在后台无窗口地守着，"
                       "插着充电器时它会一直听你说话。"}


def uninstall():
    """取消开机自启。返回 {ok, message}。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, VALUE_NAME)
            msg = "已取消开机自启。"
        except FileNotFoundError:
            msg = "本来就没有设置开机自启。"
        winreg.CloseKey(key)
        return {"ok": True, "enabled": False, "message": msg}
    except Exception as e:
        return {"ok": False, "enabled": None,
                "message": "取消开机自启失败：%s" % e}


def status():
    """返回 {ok, enabled, command, listen_enabled, message}。"""
    enabled = False
    cmd = ""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        try:
            cmd, _ = winreg.QueryValueEx(key, VALUE_NAME)
            enabled = True
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except Exception:
        pass
    return {"ok": True, "enabled": enabled, "command": cmd,
            "listen_enabled": _listen_enabled(),
            "message": "开机自启：%s" % ("已开启" if enabled else "未设置")}


def set_enabled(on):
    """统一入口：on=True 安装，False 卸载；随后返回最新状态。"""
    r = install() if on else uninstall()
    st = status()
    st["ok"] = r.get("ok", True)
    st["message"] = r.get("message") or st.get("message")
    return st


if __name__ == "__main__":
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if action == "install":
        res = install()
    elif action == "uninstall":
        res = uninstall()
    else:
        res = status()
    print("[阿觉] %s" % res.get("message", ""))
    if res.get("command"):
        print("        %s" % res["command"])
