# -*- coding: utf-8 -*-
"""chat.bat 单入口端到端自验收。运行：python tests/verify_chat_one.py"""
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT = os.path.join(ROOT, "chat.bat")
PY = sys.executable
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
results = []


def port_open():
    s = socket.socket()
    try:
        return s.connect_ex(("127.0.0.1", 6800)) == 0
    finally:
        s.close()


def wait_port(want, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if port_open() == want:
            return True
        time.sleep(0.2)
    return False


def run(args, stdin=None, timeout=180):
    return subprocess.run([BAT] + args, cwd=ROOT, env=ENV,
                          stdin=subprocess.DEVNULL if stdin is None else stdin,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, shell=False)


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + ("  | " + detail if detail else ""), flush=True)


def clean():
    """确认没有别的引擎残留；有则报错，不擅自杀用户的进程。"""
    if port_open():
        print("!! 6800 已被占用，跳过会冲突的用例", flush=True)
        return False
    return True


# ---- 1. 静态检查 ----
raw = open(BAT, "rb").read()
check("bat 纯 ASCII", all(c < 128 for c in raw))
check("bat CRLF 换行", b"\n" in raw and raw.count(b"\r\n") == raw.count(b"\n"))
check("新增文件都在本体目录", os.path.isfile(os.path.join(ROOT, "chat.py")) and os.path.isfile(BAT))
check("初始 6800 空闲", not port_open())

if clean():
    # ---- 2. 双击等价：cmd /c chat.bat --status ----
    t0 = time.time()
    r = run(["--status"], timeout=90)
    out = (r.stdout or "") + (r.stderr or "")
    check("单入口 --status 返回引擎状态", "STATUS|" in out, out.strip().replace("\n", " / ")[:180])
    check("--status 自行拉起引擎", "[引擎] 正在后台启动" in out)
    check("--status 退出码 0", r.returncode == 0, "code=%s" % r.returncode)
    check("--status 退出后自动关引擎", "[引擎] 本次启动的引擎已关闭" in out)
    check("--status 退出后端口释放", wait_port(False))
    log_text = open(os.path.join(ROOT, "logs", "chat_engine.log"), encoding="utf-8", errors="replace").read()
    check("引擎日志已落盘且可读", "[6800]" in log_text and "就绪" in log_text,
          log_text.strip().replace("\n", " / ")[:160])

    # ---- 3. 单句提问 ----
    r = run(["用一句话说你好"], timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    answer = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("[引擎]")]
    check("单句提问有回答", r.returncode == 0 and len(answer) > 0 and len(answer[0]) > 2,
          (answer[0][:120] if answer else out.strip()[:120]))
    check("单句提问后端口释放", wait_port(False))

    # ---- 4. 连续对话（管道喂两轮 + exit）----
    r = subprocess.run([PY, "chat.py"], cwd=ROOT, env=ENV, input="你好\nexit\n",
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    check("连续对话进入循环", "> " in out and "[引擎] 就绪" in out)
    check("连续对话有回答", "CHAT" not in out and len(out.split("> ")) >= 2,
          out.strip().replace("\n", " / ")[:160])
    check("exit 后自动关引擎", "[引擎] 本次启动的引擎已关闭" in out)
    check("连续对话后端口释放", wait_port(False))

    # ---- 5. 已有引擎在跑：复用且不误关 ----
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    p = subprocess.Popen([PY, "-m", "agent.main", "--no-gui"], cwd=ROOT,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    ready = wait_port(True, seconds=30)
    check("外部引擎启动成功", ready)
    if ready:
        r = run(["--status"], timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        check("检测到已有引擎并复用", "直接复用" in out and "STATUS|" in out,
              out.strip().replace("\n", " / ")[:160])
        check("复用时退出不关别人引擎", "已关闭" not in out and port_open())
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    check("外部引擎清理后端口释放", wait_port(False))

print("\n==== %s/%s PASS ====" % (sum(1 for _, ok, _ in results if ok), len(results)))
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
