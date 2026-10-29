# -*- coding: utf-8 -*-
"""插件动作执行器（供 .qdplugin 的 run 字段调用）。

插件体系是「声明式」的：.qdplugin 只描述要跑什么命令，本文件就是那几个命令的落地实现。
用法：
    python tools/plugin_launch.py workbench            # 拉起设备工作台
    python tools/plugin_launch.py minimind start|stop|status
    python tools/plugin_launch.py ide on|off|status
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, ".plugin_state")


def _port_alive(host, port, timeout=0.6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _pid_file(name):
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, name + ".pid")


def _read_pid(name):
    p = _pid_file(name)
    try:
        with open(p, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _write_pid(name, pid):
    try:
        with open(_pid_file(name), "w", encoding="utf-8") as f:
            f.write(str(pid))
    except Exception:
        pass


def _alive_pid(pid):
    if not pid:
        return False
    try:
        import ctypes
        ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        return True
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _spawn(args, cwd, log_name):
    """后台拉起一个进程（不阻塞），日志写进 .plugin_state/<log_name>.log。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    logf = open(os.path.join(STATE_DIR, log_name + ".log"), "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    p = subprocess.Popen(args, cwd=cwd, stdout=logf, stderr=logf,
                         creationflags=flags)
    return p


def _wait_port(host, port, seconds=25):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if _port_alive(host, port):
            return True
        time.sleep(0.4)
    return False


# ---------------- 工作台 ----------------

def do_workbench():
    root = os.path.join(ROOT, "workbench")
    script = os.path.join(root, "server", "app.py")
    if not os.path.exists(script):
        print("未找到 workbench/server/app.py，工作台不可用")
        return 1
    if _port_alive("127.0.0.1", 3000):
        print("工作台已在运行：http://localhost:3000")
        return 0
    p = _spawn([sys.executable, script], root, "workbench")
    _write_pid("workbench", p.pid)
    if _wait_port("127.0.0.1", 3000, 20):
        print("工作台已启动（pid=%s）：http://localhost:3000" % p.pid)
        return 0
    print("工作台启动超时，请查看 .plugin_state/workbench.log")
    return 1


# ---------------- MiniMind 本地推理 ----------------

def _minimind_dir():
    for cand in (os.environ.get("MINIMIND_HOME"), os.path.join(ROOT, "vendor", "minimind"),
                 os.path.join(os.path.expanduser("~"), "minimind")):
        if cand and os.path.isdir(cand):
            return cand
    return None


def do_minimind(action):
    action = (action or "status").strip().lower()
    port = 8000
    d = _minimind_dir()
    if action in ("start", "status"):
        if not d:
            print("未找到 MiniMind 仓库。请克隆 https://github.com/jingyaogong/minimind 到 "
                  "%s，或设置环境变量 MINIMIND_HOME 指向它。" % os.path.join(ROOT, "vendor", "minimind"))
            return 1
        server = os.path.join(d, "api_server.py")
        if not os.path.exists(server):
            print("MiniMind 仓库里没有 api_server.py：%s" % server)
            return 1
    if action == "status":
        alive = _port_alive("127.0.0.1", port)
        print("MiniMind 服务 %s（http://127.0.0.1:%d/v1）" %
              ("运行中" if alive else "未运行", port))
        return 0 if alive else 1
    if action == "stop":
        pid = _read_pid("minimind")
        if not _alive_pid(pid):
            print("MiniMind 未在运行")
            return 0
        try:
            os.kill(pid, 15)
        except Exception as e:
            print("停止失败：%s" % e)
            return 1
        print("MiniMind 已停止")
        return 0
    # start
    if _port_alive("127.0.0.1", port):
        print("MiniMind 已在运行：http://127.0.0.1:%d/v1" % port)
        return 0
    p = _spawn([sys.executable, server], d, "minimind")
    _write_pid("minimind", p.pid)
    if _wait_port("127.0.0.1", port, 60):
        print("MiniMind 已启动（pid=%s）：http://127.0.0.1:%d/v1\n"
              "在 INTENT 的「模型 → 新建 API」里把接口地址填成上面的地址即可直接对话。"
              % (p.pid, port))
        return 0
    print("MiniMind 启动超时，请查看 .plugin_state/minimind.log")
    return 1


# ---------------- VS Code 风格编程面板 ----------------

def do_ide(action):
    action = (action or "status").strip().lower()
    flag = os.path.join(STATE_DIR, "ide_on")
    os.makedirs(STATE_DIR, exist_ok=True)
    if action == "on":
        open(flag, "w", encoding="utf-8").write("1")
        print("VS Code 风格编程面板已启用：AI 工作时点击聊天里的 AI 头像即可查看它正在改的文件。")
        return 0
    if action == "off":
        try:
            os.remove(flag)
        except Exception:
            pass
        print("VS Code 风格编程面板已关闭。")
        return 0
    on = os.path.exists(flag)
    print("VS Code 风格编程面板：%s" % ("已启用" if on else "未启用"))
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    what = sys.argv[1].strip().lower()
    args = sys.argv[2:]
    if what == "workbench":
        return do_workbench()
    if what == "minimind":
        return do_minimind(args[0] if args else "status")
    if what == "ide":
        return do_ide(args[0] if args else "status")
    print("未知动作: %s（可用 workbench / minimind / ide）" % what)
    return 1


if __name__ == "__main__":
    sys.exit(main())
