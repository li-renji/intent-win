# -*- coding: utf-8 -*-
"""Linux 无界面核心移植验收（自验收脚本）。

分三层：
  A. 交付物检查：启动脚本 / 依赖清单 / 代码改动点是否到位
  B. 跨平台链路：严格模拟 Linux（无 windll / 无 GUI 库）下 STATUS + 一问一答
  C. Windows 回归：源码语法编译通过（不破坏原平台）

真机 Linux 本机无法直接验证（WSL 被安全策略拦、Docker daemon 未运行），
故用 sitecustomize 屏蔽 Windows 专有 API 与 GUI 库来等价暴露跨平台问题。
"""
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM_DIR = os.path.join(ROOT, "tests", "_linux_sim")
PY = sys.executable
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def section_a():
    print("\n=== A. 交付物与代码改动点 ===")
    check("start-linux.sh 存在", os.path.isfile(os.path.join(ROOT, "start-linux.sh")))
    sh = read("start-linux.sh") if os.path.isfile(os.path.join(ROOT, "start-linux.sh")) else ""
    check("start-linux.sh 走 chat.py", "chat.py" in sh)
    check("start-linux.sh 关回环代理", "no_proxy" in sh and "127.0.0.1" in sh)
    check("requirements-linux.txt 存在",
          os.path.isfile(os.path.join(ROOT, "requirements-linux.txt")))
    req = read("requirements-linux.txt") if os.path.isfile(os.path.join(ROOT, "requirements-linux.txt")) else ""
    # 只看真正的依赖声明行，忽略注释里的「不需要 pywebview」等说明
    dep_lines = [l for l in req.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    deps = "\n".join(dep_lines).lower()
    check("依赖清单不含 pywebview", "pywebview" not in deps)
    check("依赖清单不含 pythonnet", "pythonnet" not in deps)

    tools = read("agent/tools.py")
    check("shell 工具有 /bin/sh 分支", '"/bin/sh", "-c"' in tools)
    check("shell 工具按 os.name 分流", 'os.name == "nt"' in tools)
    check("桌面路径有 home 回退", 'os.path.isdir(desktop)' in tools)

    main = read("agent/main.py")
    check("启动文案去掉 Windows 限定", "Windows Agent" not in main)


def recv(line, timeout=120):
    s = socket.create_connection(("127.0.0.1", 6800), timeout=5)
    try:
        s.settimeout(timeout)
        s.sendall((line.strip() + "\n").encode("utf-8"))
        buf = b""
        while True:
            try:
                c = s.recv(4096)
            except socket.timeout:
                break
            if not c:
                break
            s.settimeout(0.8)
            buf += c
        return buf.decode("utf-8", "replace").strip()
    finally:
        s.close()


def status_ok():
    try:
        return recv("STATUS", timeout=3).startswith("STATUS|")
    except OSError:
        return False


def section_b():
    print("\n=== B. 严格模拟 Linux 核心链路（无 windll / 无 GUI 库）===")
    logpath = os.path.join(ROOT, "logs", "verify_linux_core.log")
    os.makedirs(os.path.dirname(logpath), exist_ok=True)
    env = dict(os.environ)
    env["INTENT_LINUX_SIM"] = "1"
    env["PYTHONPATH"] = SIM_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(logpath, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, "-u", "-m", "agent.main", "--no-gui"],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if status_ok():
                ready = True
                break
            time.sleep(0.4)
        check("严格模拟下引擎就绪（三端口起来）", ready,
              "退出码=%s" % proc.returncode if proc.poll() is not None else "")
        if ready:
            st = recv("STATUS", timeout=5)
            check("STATUS 正确应答", st.startswith("STATUS|"))
            check("浏览器降级为未启用", "浏览器=未启用" in st)
            ans = recv("CHATS 请只回复两个字：收到", timeout=120)
            check("一问一答有响应（链路通）", bool(ans), repr(ans[:120]))
        else:
            log.seek(0)
            print("  ---- 引擎日志 ----\n" + log.read()[-800:])
            check("STATUS 正确应答", False)
            check("浏览器降级为未启用", False)
            check("一问一答有响应（链路通）", False)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()


def section_c():
    print("\n=== C. Windows 回归（语法编译）===")
    r = subprocess.run(
        [PY, "-m", "py_compile", "agent/tools.py", "agent/main.py", "chat.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    check("改动文件语法编译通过", r.returncode == 0, r.stderr.strip()[:300])


def main():
    print("INTENT Linux 无界面核心移植 · 自验收")
    section_a()
    section_b()
    section_c()
    print("\n=== 结果：%d 通过 / %d 失败 ===" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
