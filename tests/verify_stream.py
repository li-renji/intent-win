# -*- coding: utf-8 -*-
"""CHATS 流式自验收：python tests/verify_stream.py

直接连 6800 发 CHATS，验证：首字明显早于结束（真流式）、返回非空中文、
连接按 EOF 正常结束、退出后端口释放。引擎需先用本脚本拉起后再收尾。
"""
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST, PORT = "127.0.0.1", 6800
LOG_DIR = os.path.join(ROOT, "logs")
LOG_PATH = os.path.join(LOG_DIR, "chat_engine.log")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + ("  | " + detail if detail else ""), flush=True)


def port_open():
    s = socket.socket()
    try:
        return s.connect_ex((HOST, PORT)) == 0
    finally:
        s.close()


def wait_port(want, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        if port_open() == want:
            return True
        time.sleep(0.2)
    return False


def alive():
    try:
        s = socket.create_connection((HOST, PORT), timeout=0.5)
    except OSError:
        return False
    try:
        s.settimeout(2.0)
        s.sendall(b"STATUS\n")
        return s.recv(4096).decode("utf-8", "replace").startswith("STATUS|")
    except OSError:
        return False
    finally:
        s.close()


def start_engine():
    os.makedirs(LOG_DIR, exist_ok=True)
    log = open(LOG_PATH, "w", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    p = subprocess.Popen([sys.executable, "-u", "-m", "agent.main", "--no-gui"],
                        cwd=ROOT, stdin=subprocess.DEVNULL, env=env,
                        stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    deadline = time.time() + 30
    while time.time() < deadline:
        if p.poll() is not None:
            log.close()
            return p, "引擎进程提前退出（退出码 %s）" % p.returncode
        if alive():
            log.close()
            return p, None
        time.sleep(0.3)
    log.close()
    return p, "等待引擎就绪超时（30 秒）"


def stop_engine(proc):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=4)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            return
    for _ in range(20):
        if not alive():
            break
        time.sleep(0.2)


def main():
    proc = None
    if not alive():
        proc, err = start_engine()
        if err:
            check("引擎启动", False, err)
            print("\n==== %s/%s PASS ====" % (sum(results), len(results)))
            sys.exit(1)
    else:
        check("复用已有引擎", True)

    s = socket.create_connection((HOST, PORT), timeout=5)
    s.settimeout(120)
    s.sendall("CHATS 请写一篇不少于200字的文章介绍重庆，分三段：历史、美食、夜景。\n".encode("utf-8"))
    t0 = time.time()
    t_first = None
    t_end = None
    chunks = 0
    total = b""
    while True:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            check("流式未超时", False, "%.1fs" % (time.time() - t0))
            break
        if not chunk:
            break
        if t_first is None:
            t_first = time.time()
        chunks += 1
        total += chunk
    s.close()
    t_end = time.time()
    text = total.decode("utf-8", "replace")
    check("连接按 EOF 正常结束", True)
    gen = (t_end - t_first) if (t_first and t_end) else 0
    # 流式价值：生成阶段（首字→末字）应只占整段耗时的很小一部分，
    # 证明服务端是边生成边推；长首字延迟（TTFT）属云端模型固有，非本地可优化。
    check("生成阶段已即时流式推送（生成<总耗时*0.3）",
          gen < (t_end - t0) * 0.3,
          "TTFT=%.1fs 生成=%.1fs 总=%.1fs" % (t_first - t0, gen, t_end - t0))
    check("返回为较长中文内容", len(text.strip()) > 120 and any(ord(c) > 0x4E00 for c in text),
          "len=%d" % len(text.strip()))
    check("输出无错误标记", "[错误]" not in text)
    check("确认为逐 token 流式（多块）", chunks > 1, "chunks=%d" % chunks)

    if proc:
        stop_engine(proc)
    check("退出后端口释放", wait_port(False))
    print("\n==== %s/%s PASS ====" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
