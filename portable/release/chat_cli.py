# -*- coding: utf-8 -*-
"""在终端里跟 intent Agent 说话。

前提：另一个终端窗口已经运行  python -m agent.main --no-gui

用法：
    python chat_cli.py 你好             问一句，打印回答后退出
    python chat_cli.py                  进入连续对话，输入 exit 退出
    python chat_cli.py --status         查看引擎状态
"""
import socket
import sys

HOST, PORT = "127.0.0.1", 6800


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
            buf += chunk
            s.settimeout(0.8)          # 一问一答：静默 0.8 秒即认为说完
        return buf.decode("utf-8", "replace").strip()
    finally:
        s.close()


def main():
    args = sys.argv[1:]
    try:
        if args and args[0] == "--status":
            print(ask("STATUS"))
            return 0
        if args:
            print(ask("CHAT " + " ".join(args)))
            return 0
        print("intent 终端对话（输入 exit 退出）")
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
                print(ask("CHAT " + text), "\n")
            except OSError as e:
                print("[连接失败] 另一个窗口的 agent.main 还在跑吗？", e)
                return 1
    except OSError as e:
        print("[连接失败] 另一个窗口的 agent.main 还在跑吗？", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
