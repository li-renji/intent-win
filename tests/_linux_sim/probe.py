# -*- coding: utf-8 -*-
"""用「模拟 Linux」环境跑完整无界面核心链路（STATUS + 一问一答）。

模拟层 sitecustomize.py 会删掉 ctypes.windll / 屏蔽 pywin32、pythonnet，
等价真实 Linux 上"这些 Windows API 根本不存在"的事实。

用法：
    python tests/_linux_sim/probe.py            # STATUS + 一问一答
    python tests/_linux_sim/probe.py --status   # 只测 STATUS
"""
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"
PORT = 6800
LOG = os.path.join(ROOT, "logs", "linux_probe.log")


def recv_line(line, timeout=90):
    s = socket.create_connection((HOST, PORT), timeout=5)
    try:
        s.settimeout(timeout)
        s.sendall((line.strip() + "\n").encode("utf-8"))
        buf = b""
        while True:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            s.settimeout(0.8)
            buf += chunk
        return buf.decode("utf-8", "replace").strip()
    finally:
        s.close()


def status_ok():
    try:
        return recv_line("STATUS", timeout=3).startswith("STATUS|")
    except OSError:
        return False


def tail(n=1200):
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-n:]
    except OSError:
        return ""


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    env = dict(os.environ)
    env["INTENT_LINUX_SIM"] = "1"
    env["PYTHONPATH"] = SIM_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(LOG, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "agent.main", "--no-gui"],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT,
    )
    ok_status = False
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                print("[FAIL] 引擎提前退出，退出码=%s" % proc.returncode)
                break
            if status_ok():
                ok_status = True
                break
            time.sleep(0.4)
        if not ok_status:
            print("[FAIL] 引擎未能就绪")
            return 1
        print("[OK] STATUS:", recv_line("STATUS", timeout=5)[:200])
        if "--status" in sys.argv:
            return 0
        print("[..] 发起一问一答（CHATS）...")
        ans = recv_line("CHATS 请只回复两个字：收到", timeout=120)
        print("[OK] CHATS 响应:", (ans[:400] or "（空响应）"))
        if not ans:
            return 1
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()
        t = tail()
        if t:
            print("---- 引擎日志（尾部）----\n" + t)


if __name__ == "__main__":
    sys.exit(main())
