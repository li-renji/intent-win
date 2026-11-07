# -*- coding: utf-8 -*-
"""INTENT U 盘便携启动层。

只做三件事，绝不改插上的那台电脑（不写注册表、不改系统 PATH、不装服务）：

1. 把 USERPROFILE / APPDATA / LOCALAPPDATA / TEMP 这些「会写宿主电脑」的位置
   全部重定向到 U 盘的 data\\ 目录。于是 os.path.expanduser("~")、浏览器
   登录态 profile、缓存、工具默认输出目录，全都跟着 U 盘走。
2. 挑一组没被占用的端口，幂等写进随盘的 配置.qdai。便携版用 168xx 段，
   不会和这台电脑上已经装好的 INTENT（6800/6801/6802）打架。
3. 用 U 盘自带的 python.exe 启动引擎 / 聊天 / 桌面窗口。

用法（一般由根目录的 START.cmd 调用）：
    python portable\\boot.py              终端对话（默认）
    python portable\\boot.py --gui        桌面窗口版
    python portable\\boot.py --doctor     只做换机体检
    python portable\\boot.py --safe       启动前临时输入 API Key，退出即抹掉
    python portable\\boot.py --print-env  验收用：打印重定向后的所有路径
"""
import json
import os
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
HOME = os.path.join(DATA, "home")
CONFIG = os.path.join(ROOT, "配置.qdai")
PY = os.path.join(ROOT, "runtime", "python", "python.exe")

# 便携版专用端口段（整组平移，避免半组冲突）
BASE_PORTS = {
    "port_6800": 16800,
    "port_6801": 16801,
    "http_port": 16802,
    "mybrowser_cdp_port": 19700,
    "local_engine_port": 18099,
    "mybrowser_serve_port": 19223,
    "pc_bridge_port": 18899,
}


def _port_busy(port):
    s = socket.socket()
    s.settimeout(0.15)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        s.close()


def _our_engine(port):
    """6800 上是自己人（STATUS 应答）就复用，不要重复拉起第二份引擎。"""
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=0.4)
        try:
            s.settimeout(0.8)
            s.sendall(b"STATUS\n")
            return s.recv(256).startswith(b"STATUS|")
        finally:
            s.close()
    except OSError:
        return False


def pick_ports():
    if _our_engine(BASE_PORTS["port_6800"]):
        return dict(BASE_PORTS)
    for off in range(0, 21):
        cand = {k: v + off for k, v in BASE_PORTS.items()}
        if not any(_port_busy(p) for p in cand.values()):
            return cand
    return dict(BASE_PORTS)


def _rewrite_config(updates):
    """只替换指定键，保留注释、顺序和其余所有设置；内容无变化就不写盘。"""
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        key = s.partition("=")[0].strip() if ("=" in s and not s.startswith("#")) else None
        if key in updates:
            out.append("%s=%s" % (key, updates[key]))
            seen.add(key)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append("%s=%s" % (k, v))
    text = "\n".join(out) + "\n"
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            if f.read() == text:
                return True
    except OSError:
        pass
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, CONFIG)
    return True


def set_api_key(value):
    return _rewrite_config({"api_key": value})


def _assert_portable_root():
    """只认 U 盘镜像根目录。防止有人从源码工程里误跑便携层、把桌面版配置改坏。"""
    if not os.path.isfile(os.path.join(ROOT, "START.cmd")):
        raise SystemExit(
            "[便携版] 当前目录不是 U 盘镜像（根目录缺 START.cmd），已拒绝改写配置。\n"
            "请在 portable\\INTENT-Portable\\ 里双击 START.cmd。")


def prepare():
    """建立便携环境，返回 (env, ports)。"""
    _assert_portable_root()
    for p in (DATA, HOME,
              os.path.join(HOME, "AppData", "Roaming"),
              os.path.join(HOME, "AppData", "Local"),
              os.path.join(HOME, "Desktop"),
              os.path.join(HOME, "Documents"),
              os.path.join(DATA, "temp"),
              os.path.join(DATA, "资料")):
        try:
            os.makedirs(p, exist_ok=True)
        except OSError:
            pass

    ports = pick_ports()
    _rewrite_config({k: str(v) for k, v in ports.items()})

    env = dict(os.environ)
    drive, tail = os.path.splitdrive(HOME)
    env["USERPROFILE"] = HOME
    env["HOME"] = HOME
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail
    env["APPDATA"] = os.path.join(HOME, "AppData", "Roaming")
    env["LOCALAPPDATA"] = os.path.join(HOME, "AppData", "Local")
    env["TEMP"] = os.path.join(DATA, "temp")
    env["TMP"] = env["TEMP"]
    # tempfile 的候选顺序是 TMPDIR -> TEMP -> TMP；宿主常把 TMPDIR 设成 D:\Temp，
    # 少设这一个，缓存就会悄悄写回插上的那台电脑。
    env["TMPDIR"] = env["TEMP"]
    env["INTENT_PORTABLE"] = "1"
    env["INTENT_ROOT"] = ROOT
    env["INTENT_PORT_6800"] = str(ports["port_6800"])
    env["INTENT_PYTHON"] = PY
    env["INTENT_WEBVIEW2_DIR"] = os.path.join(ROOT, "runtime", "webview2")
    env["INTENT_CHROMIUM_DIR"] = os.path.join(ROOT, "runtime", "chromium")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PATH"] = (os.path.dirname(PY) + os.pathsep
                   + os.path.join(os.path.dirname(PY), "Scripts") + os.pathsep
                   + env.get("PATH", ""))
    # 宿主电脑上残留的覆盖变量不能把运行时指回 C 盘
    env.pop("INTENT_CONFIG", None)
    return env, ports


def _python():
    return PY if os.path.isfile(PY) else sys.executable


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env, ports = prepare()

    if "--print-env" in argv:
        # 用「带这套 env 的真实子进程」验证重定向生效，而不是只看变量值
        try:
            probe = subprocess.check_output(
                [_python(), "-c",
                 "import os,sys,tempfile;print(os.path.expanduser('~'));"
                 "print(tempfile.gettempdir());print(sys.executable)"],
                cwd=ROOT, env=env, stderr=subprocess.STDOUT
            ).decode("utf-8", "replace").splitlines()
        except Exception:
            probe = []
        print(json.dumps({
            "root": ROOT,
            "home_actual": probe[0] if len(probe) > 0 else "",
            "temp_actual": probe[1] if len(probe) > 1 else "",
            "python_actual": probe[2] if len(probe) > 2 else "",
            "userprofile": env["USERPROFILE"],
            "appdata": env["APPDATA"],
            "localappdata": env["LOCALAPPDATA"],
            "temp": env["TEMP"],
            "python": env["INTENT_PYTHON"],
            "webview2": env["INTENT_WEBVIEW2_DIR"],
            "chromium": env["INTENT_CHROMIUM_DIR"],
            "portable": env["INTENT_PORTABLE"],
            "ports": ports,
        }, ensure_ascii=False, indent=1))
        return 0

    # 每次插上新电脑先体检；有致命项时不悄悄硬跑，也不让窗口一闪而过。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from doctor import collect, print_report
    report = collect(ROOT, env, ports, network="--doctor" in argv)
    fatal = [r for r in report if r["level"] == "FAIL"]
    print_report(report)
    if fatal and "--force" not in argv:
        print("\n[便携版] 这台电脑缺少必要组件，无法完整运行。按回车仍要试一次，或直接关掉窗口。")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            return 2
    if "--doctor" in argv:
        return 0

    safe = "--safe" in argv
    key_was = ""
    if safe:
        try:
            with open(CONFIG, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("api_key="):
                        key_was = line.strip().partition("=")[2]
                        break
        except OSError:
            pass
        set_api_key("")
        try:
            import getpass
            entered = getpass.getpass("请输入 API Key（不回显，只用于本次运行）：").strip()
        except (EOFError, KeyboardInterrupt):
            entered = ""
        if not entered:
            print("[便携版] 未输入 Key，已取消启动（配置里的 Key 未被改动）。")
            if key_was:
                set_api_key(key_was)
            return 2
        set_api_key(entered)

    passthrough = [a for a in argv if a not in ("--gui", "--safe", "--doctor", "--force")
                   and not a.startswith("--print-env")]
    if "--gui" in argv:
        cmd = [_python(), "-m", "agent.app"]
    else:
        cmd = [_python(), os.path.join(ROOT, "chat.py")] + passthrough

    code = 1
    try:
        code = subprocess.call(cmd, cwd=ROOT, env=env)
    except OSError as e:
        print("[便携版] 启动失败：%s" % e)
    finally:
        if safe:
            set_api_key("")
            print("[便携版] 已清除本次临时写入的 API Key。")
    return code


if __name__ == "__main__":
    sys.exit(main())
