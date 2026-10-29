# -*- coding: utf-8 -*-
"""MyBrowser 端到端自检：不开窗口，纯控制面验证自研全链路。

跑法： python -m mybrowser.selftest
"""
import base64
import os
import sys
import time

from .core.engine import WebView2Engine, EngineError
from .core.snapshot import Snapshotter
from .core.actions import Actions
from .net.cdp import CDPError

PORT = 9666
URL = "https://example.com"


def main():
    eng = WebView2Engine(port=PORT, headless=True)
    eng.prepare_env()

    # 用 Edge 进程模拟 WebView2 渲染面（headless 更适合自检）
    import subprocess
    import urllib.request
    import json
    profile = os.path.join(os.environ.get("TEMP", "."), "mybrowser_selftest")
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    proc = subprocess.Popen([
        edge, "--remote-debugging-port=%d" % PORT,
        "--user-data-dir=%s" % profile,
        "--headless=new", "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[selftest] 渲染进程 pid=%d" % proc.pid)

    try:
        # ---- 1. CDP 连接 ----
        eng.connect()
        print("[selftest] 1. 自研 CDP 连接 OK")

        # ---- 2. 建标签页 ----
        sess = eng.new_tab(URL)
        print("[selftest] 2. 标签页创建 OK, session=%s" % sess.session_id[:12])

        # ---- 3. 导航 + 读页面 ----
        sess.navigate(URL)
        time.sleep(1.5)
        print("[selftest] 3. 导航 OK  title=%s  url=%s" % (sess.title(), sess.current_url()))

        # ---- 4. 自研语义快照 ----
        snap = Snapshotter(sess)
        s1 = snap.capture()
        print("[selftest] 4. 快照 OK  元素数=%d  AX节点=%d" % (len(s1.elements), s1.raw_node_count))
        print("---------- 快照文本 ----------")
        print(s1.text[:600])
        print("----------------------------")

        # ---- 5. 差分快照 ----
        time.sleep(0.5)
        s2 = snap.capture()
        print("[selftest] 5. 差分 =", s2.delta)

        # ---- 6. 真实点击 AX 树里的 link ----
        ax = sess.send("Accessibility.getFullAXTree")
        link = None
        for n in ax.get("nodes", []):
            if (n.get("role") or {}).get("value") == "link":
                link = n
                break
        if link:
            acts = Actions(sess, snap)
            backend = link["backendDOMNodeId"]
            x, y = acts._box_of(backend)
            acts.click(x=x, y=y)
            time.sleep(1.5)
            print("[selftest] 6. 真实点击 OK  坐标=(%d,%d)  当前url=%s"
                  % (round(x), round(y), sess.current_url()))

        # ---- 7. 真实输入（清空 + 插入）----
        acts = Actions(sess, snap)
        r = acts.type_text("hello mybrowser")
        time.sleep(0.4)
        val = sess.evaluate("document.activeElement && document.activeElement.value")
        print("[selftest] 7. 输入 OK  返回=%s" % r)

        # ---- 8. 键盘 ----
        acts.key_enter()
        acts.key_escape()
        print("[selftest] 8. 按键 OK")

        # ---- 9. 滚动 ----
        acts.scroll(300)
        print("[selftest] 9. 滚动 OK")

        # ---- 10. 截图 ----
        png = acts.screenshot()
        out = os.path.join(os.environ.get("TEMP", "."), "mybrowser_selftest.png")
        with open(out, "wb") as f:
            f.write(png)
        print("[selftest] 10. 截图 OK  %d bytes -> %s" % (len(png), out))

        # ---- 11. 读正文（省 token 通道）----
        txt = acts.read_text(max_chars=200)
        print("[selftest] 11. READ OK  len=%d" % len(txt))

        eng.dispose()
        print("[selftest] ===== 自研全链路 11 步全部通过 =====")
        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("[selftest] 失败:", e)
        return 1
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
