# -*- coding: utf-8 -*-
"""intent 一键终端：双击 chat.bat（或 python chat.py）即可，只开一个窗口。

本脚本会在后台静默拉起 agent.main（三端口引擎，不弹新窗口），等引擎就绪
后直接进入对话；退出时自动关掉「自己拉起的」引擎，不残留进程和端口。
若引擎本来就在跑（图形版或其它终端），直接复用，退出时绝不动它。

用法：
    chat.bat                   连续对话（输入 exit 退出）
    chat.bat 你好              问一句，打印回答后退出
    chat.bat --status          只看引擎状态
    chat.bat --keep            退出时保留本次拉起的后台引擎
"""
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("INTENT_HOST", "127.0.0.1")
# U 盘便携版用独立端口段，避免和宿主电脑上已装的 INTENT 抢 6800。
# 普通桌面版不设该变量，行为完全不变。
try:
    PORT = int(os.environ.get("INTENT_PORT_6800", 6800))
except (TypeError, ValueError):
    PORT = 6800
LOG_DIR = os.path.join(ROOT, "logs")
LOG_PATH = os.path.join(LOG_DIR, "chat_engine.log")
START_TIMEOUT = 30


def alive():
    """端口能连上、且 6800 正确应答 STATUS，才算引擎真的就绪。"""
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


def ask(line, timeout=120):
    """往 6800 发一行指令，收完回答返回文本。"""
    s = socket.create_connection((HOST, PORT), timeout=5)
    try:
        s.settimeout(timeout)
        s.sendall((line.strip() + "\n").encode("utf-8"))
        buf = b""
        while True:
            try:
                chunk = s.recv(8192)
            except socket.timeout:
                break
            if not chunk:
                break
            s.settimeout(0.8)          # 一问一答：静默 0.8 秒即认为说完
            buf += chunk
        return buf.decode("utf-8", "replace").strip()
    finally:
        s.close()


def log_tail(n=1500):
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-n:]
    except OSError:
        return ""


def start_engine():
    """静默启动引擎；返回 (进程, 错误信息)，成功时错误信息为 None。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    log = open(LOG_PATH, "w", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    # -u + PYTHONIOENCODING：日志实时落盘且统一 UTF-8，退出时也查得到原因
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "agent.main", "--no-gui"],
            cwd=ROOT, stdin=subprocess.DEVNULL, env=env,
            stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
        )
    except OSError as e:
        log.close()
        return None, "无法启动 Python 引擎：%s" % e
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            log.close()
            return proc, "引擎进程提前退出（退出码 %s）" % proc.returncode
        if alive():
            log.close()
            return proc, None
        time.sleep(0.3)
    log.close()
    return proc, "等待引擎就绪超时（%s 秒）" % START_TIMEOUT


def stop_engine(proc):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=3)
        except OSError:
            return
    # 确保端口确实释放，不然就是残留
    for _ in range(20):
        if not alive():
            break
        time.sleep(0.2)
    print("[引擎] 本次启动的引擎已关闭。")


def ask_stream(line, timeout=120):
    """往 6800 发 CHATS 命令，逐块实时打印（打字机效果），读到 EOF 即结束。

    直接写字节到 stdout.buffer，避免 UTF-8 多字节在 4096 边界被截断导致乱码。
    """
    s = socket.create_connection((HOST, PORT), timeout=5)
    try:
        s.settimeout(timeout)
        s.sendall((line.strip() + "\n").encode("utf-8"))
        while True:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                sys.stdout.write("\n[响应超时]\n")
                break
            if not chunk:                      # 连接关闭 = 流式结束
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()
    finally:
        s.close()


def main():
    args = [a for a in sys.argv[1:] if a != "--keep"]
    keep = "--keep" in sys.argv[1:]
    proc = None

    # 安全/隐私报告是纯本地只读操作，不该为了看它把引擎拉起来。
    if args and args[0] in ("--security", "--privacy"):
        try:
            from agent import safety
            md, where = safety.build_report(save=True)
            passed, total, _ = safety.selftest()
            lines = md.splitlines()
            i = next((n for n, l in enumerate(lines) if l.startswith("## 二、")), 0)
            print("隐私与安全校验报告：%s" % where)
            print("模式：%s ｜ 自检：%d/%d ｜ 审计日志：%s" %
                  (safety._mode(safety.current_config()), passed, total, safety.LOG_PATH))
            print()
            print("\n".join(lines[:4]))
            print()
            print("\n".join(lines[i:i + 9]))
            print("\n全文见上述文件；看审计记录：python -m agent.safety --log 20")
            return 0
        except Exception as e:
            print("[失败] 生成报告失败：%s" % e)
            return 1

    # 运行时自检：纯本地，不为看一眼依赖就把引擎拉起来。
    if args and args[0] == "--runtime":
        try:
            from mybrowser.runtime_paths import status
            s = status()
            print("INTENT 自带运行时自检（纯本地，未启动引擎）")
            print("  本体目录       :", s["root"])
            print("  Python         :", s["python"],
                  "[本体内置]" if s["python_owned"] else "[缺失]")
            print("  浏览器内核     :", s["browser"], "[%s]" % s["browser_source"])
            print("  WebView2 运行时:", s["webview2_runtime"],
                  "[本体内置]" if s["webview2_owned"] else "[系统回退]")
            print("  当前解释器     :", sys.executable)
            ok = s["python_owned"] and s["browser_owned"] and s["webview2_owned"]
            print("  结论           :",
                  "完全自主运行（不借 WorkBuddy Python / 系统浏览器）" if ok
                  else "仍有外部依赖，见上")
            return 0 if ok else 1
        except Exception as e:
            print("[失败] 运行时自检失败：", e)
            return 1

    if alive():
        print("[引擎] 已在运行，直接复用（退出时不会关它）。", flush=True)
    else:
        print("[引擎] 正在后台启动（不弹新窗口）...", flush=True)
        proc, err = start_engine()
        if err:
            print("[失败] " + err)
            tail = log_tail()
            if tail:
                print("---- 引擎日志 ----\n" + tail)
            if proc and not keep:
                stop_engine(proc)
            return 1
        print("[引擎] 就绪。", flush=True)

    try:
        if args and args[0] == "--status":
            print(ask("STATUS"))
            return 0
        if args:
            ask_stream("CHATS " + " ".join(args))
            return 0
        print("intent 终端对话（输入 exit 退出；退出会自动关闭本次启动的引擎）")
        while True:
            try:
                text = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text:
                continue
            if text.lower() in ("exit", "quit", "q"):
                return 0
            try:
                ask_stream("CHATS " + text)
            except OSError as e:
                print("[连接失败] 引擎不可用了：", e)
                return 1
    except OSError as e:
        print("[连接失败] 引擎不可用了：", e)
        return 1
    finally:
        if proc and not keep:
            stop_engine(proc)


if __name__ == "__main__":
    sys.exit(main())
