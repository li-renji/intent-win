# -*- coding: utf-8 -*-
"""全局命令 intent 的自验收：python tests/verify_intent_cmd.py

模拟「新开的终端」：PATH 由注册表 User + Machine 重新拼出，再直接执行
裸命令 intent（不带路径、不带扩展名），验证 cmd 与 PowerShell 都能找到它。
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import winreg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = "D:\\INTENT-win"
BACKUP = os.path.join(ROOT, "logs", "user_path_backup_20261105.txt")
results = []
OUTPUTS = []


def reg_path(hive, sub):
    k = winreg.OpenKey(hive, sub)
    try:
        return winreg.QueryValueEx(k, "Path")[0]
    finally:
        k.Close()


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + ("  | " + detail if detail else ""), flush=True)


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


def dec(b):
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "replace")


def run(cmd, cwd, timeout=180, stdin=b"", shell=True, env=None):
    if env is None:
        env = NEW_ENV
    r = subprocess.run(cmd, cwd=cwd, env=env, shell=shell,
                       input=stdin, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    text = dec(r.stdout)
    OUTPUTS.append(text)
    return r.returncode, text


# ---- 1. 静态 + PATH ----
raw = open(os.path.join(ROOT, "intent.cmd"), "rb").read()
check("intent.cmd 纯 ASCII", all(c < 128 for c in raw))
check("intent.cmd CRLF", raw.count(b"\r\n") == raw.count(b"\n") and b"\n" in raw)

user = reg_path(winreg.HKEY_CURRENT_USER, "Environment")
machine = reg_path(winreg.HKEY_LOCAL_MACHINE,
                   r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
check("用户 PATH 含本体目录且仅一次",
      sum(1 for p in user.split(";") if p.rstrip("\\").lower() == ENTRY.lower()) == 1)
backup = open(BACKUP, encoding="utf-8-sig").read()
check("原 PATH 完整保留（前缀与备份一致）", user.startswith(backup.rstrip(";")))
NEW_ENV = dict(os.environ, PATH=os.path.expandvars(machine + ";" + user))
check("模拟新终端 PATH 已拼好（变量已展开）", ENTRY in NEW_ENV["PATH"] and "%" not in NEW_ENV["PATH"])
resolved = shutil.which("intent", path=NEW_ENV["PATH"]) or ""
check("裸命令 intent 解析到 intent.cmd", resolved.lower().endswith("intent.cmd"), resolved)
check("初始 6800 空闲", not port_open())

if not port_open():
    # ---- 2. cmd：裸命令 + 状态 ----
    code, out = run("intent --status", ROOT, timeout=90)
    check("cmd 输入 intent --status 能启动", "STATUS|" in out and code == 0,
          out.strip().replace("\n", " / ")[:170])
    check("单命令自动起停引擎", "[引擎] 正在后台启动" in out and "[引擎] 本次启动的引擎已关闭" in out)
    check("--status 后端口释放", wait_port(False))

    # ---- 3. 任意目录 + 不改动当前目录 ----
    other = os.environ.get("TEMP", "C:\\")
    code, out = run("intent --status && cd", other, timeout=90)
    check("任意目录都能用 intent", "STATUS|" in out and code == 0,
          out.strip().replace("\n", " / ")[:120])
    last = [l.strip() for l in out.splitlines() if l.strip()][-1]
    check("用完仍留在原目录", last.lower().rstrip("\\") == other.lower().rstrip("\\"), last)
    check("任意目录用例后端口释放", wait_port(False))

    # ---- 4. 中文单句 ----
    code, out = run("intent 用一句话说你好", ROOT, timeout=180)
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith("[引擎]")]
    check("中文单句提问有回答", code == 0 and lines and len(lines[-1]) > 2,
          (lines[-1][:120] if lines else out.strip()[:120]))
    check("单句后端口释放", wait_port(False))

    # ---- 5. 连续对话 ----
    code, out = run("intent", ROOT, timeout=180, stdin="你好\nexit\n".encode("utf-8"))
    check("不带参数直接进入连续对话", "> " in out and "[引擎] 就绪" in out)
    check("连续对话有回答且 exit 退出", out.count("> ") >= 2 and "[引擎] 本次启动的引擎已关闭" in out,
          out.strip().replace("\n", " / ")[:150])
    check("连续对话后端口释放", wait_port(False))

    # ---- 6. PowerShell ----
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "intent --status"],
                       cwd=ROOT, env=NEW_ENV, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=90)
    out = dec(r.stdout)
    OUTPUTS.append(out)
    check("PowerShell 输入 intent 也能启动", "STATUS|" in out and r.returncode == 0,
          out.strip().replace("\n", " / ")[:150])
    check("PowerShell 用例后端口释放", wait_port(False))

    # ---- 7. 已开终端（旧 PATH，无 D:\INTENT-win）经 .local\bin 转发器立即可用 ----
    old_env = dict(os.environ, PATH=os.path.expandvars(machine + ";" + backup))
    check("旧 PATH 不含本体目录", ENTRY not in old_env["PATH"])
    code, out = run("intent --status", ROOT, timeout=90, env=old_env)
    check("已开终端用转发器即刻可用", "STATUS|" in out and code == 0,
          out.strip().replace("\n", " / ")[:150])
    check("转发器用例后端口释放", wait_port(False))

    noise = [o for o in OUTPUTS if "is not recognized" in o or "Traceback" in o
             or "cannot find the path" in o or "[失败]" in o]
    check("所有用例输出干净无报错", not noise,
          (noise[0].strip().replace("\n", " / ")[:150] if noise else ""))

print("\n==== %s/%s PASS ====" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
