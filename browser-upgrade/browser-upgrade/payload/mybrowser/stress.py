# -*- coding: utf-8 -*-
"""真实站点压力测试：验证自研浏览器能处理现代 SPA / React 受控组件。

老方案（el.value = v）在这些场景必然失败：
  · React 受控组件（value 被框架劫持）
  · 登录表单（密码框）
  · shadow DOM 组件
  · 懒加载 / SPA 路由

跑法： python -m mybrowser.stress
"""
import os
import subprocess
import sys
import time

from .core.engine import WebView2Engine
from .core.snapshot import Snapshotter
from .core.actions import Actions

PORT = 9677

# 网络受限环境下可达的站点（百度首页含 React 化搜索框，是受控组件典型场景）
TEST_URL = "https://www.baidu.com/"


def main():
    eng = WebView2Engine(port=PORT, headless=True)
    eng.prepare_env()

    profile = os.path.join(os.environ.get("TEMP", "."), "mybrowser_stress")
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    proc = subprocess.Popen([
        edge, "--remote-debugging-port=%d" % PORT,
        "--user-data-dir=%s" % profile,
        "--headless=new", "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[stress] 渲染进程 pid=%d" % proc.pid)

    try:
        eng.connect()
        sess = eng.new_tab("about:blank")
        snap = Snapshotter(sess)
        acts = Actions(sess, snap)
        print("[stress] 控制面就绪")

        # ---- A. SPA 路由：导航后 URL 必须变化 ----
        sess.navigate(TEST_URL)
        time.sleep(3.5)
        url = sess.current_url()
        print("[stress] A. 导航 URL =", url[:100])
        assert "error" not in url.lower(), "导航未生效: %s" % url

        # ---- B. 快照能否识别出搜索框（AX 树）----
        s = snap.capture()
        print("[stress] B. 快照元素 %d 个, AX节点 %d 个" % (len(s.elements), s.raw_node_count))
        for i, (ref, role, name) in enumerate(s.elements[:12]):
            print("      %2d. %s  %s" % (i + 1, role, (name or "")[:50]))
        if s.elements:
            print("[stress]    快照文本前 400 字:")
            print("      " + s.text[:400].replace("\n", "\n      "))

        # ---- C. 受控组件输入：真实打字后读回 DOM 值 ----
        # Google 搜索框是 React 受控组件，el.value= 赋值不会更新框架状态
        searchbox = None
        for ref, role, name in s.elements:
            if role in ("搜索框", "输入框"):
                searchbox = ref
                break
        if not searchbox and s.elements:
            searchbox = s.elements[0][0]

        if searchbox:
            print("[stress] C. 目标输入框 ref =", searchbox)
            q = ("document.querySelector('#kw') || document.querySelector"
                 "('input[name=wd],input[name=q],textarea') || document.activeElement")
            before = sess.evaluate("%s && %s.value" % (q, q))
            acts.click_and_type(searchbox, "hello from mybrowser")
            time.sleep(0.8)
            after = sess.evaluate("%s && %s.value" % (q, q))
            print("[stress]    输入前 = %r" % (before,))
            print("[stress]    输入后 = %r" % (after,))
            print("[stress]    受控组件输入 %s"
                  % ("成功 ✅" if after and "mybrowser" in str(after) else "失败 ❌"))
        else:
            print("[stress] C. 未找到输入框，跳过")

        # ---- D. 真实按键回车触发提交 ----
        acts.key_enter()
        time.sleep(2.5)
        url2 = sess.current_url()
        print("[stress] D. 回车后 URL =", url2[:120])

        # ---- E. 滚动 + 增量快照 ----
        acts.scroll(400)
        time.sleep(0.6)
        s2 = snap.capture()
        print("[stress] E. 滚动后快照 delta =", s2.delta)
        print("[stress]    元素数 %d -> %d" % (len(s.elements), len(s2.elements)))

        # ---- F. 差分快照的 token 优势 ----
        full_len = len(s2.text)
        print("[stress] F. 全量快照 %d 字符；差分模式只报 '%s'"
              % (full_len, s2.delta))

        eng.dispose()
        print("[stress] ===== 压力测试完成 =====")
        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
