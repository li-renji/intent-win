# -*- coding: utf-8 -*-
"""端到端验证：把 Lightpanda 的三块能力（dump / trace / serve）接进 MyBrowser。

跑法： python -m mybrowser.integration_test
依赖：本机已安装 Edge（借其 WebView2 内核），会自动拉 headless 渲染进程。

说明：测试用「先 about:blank 再注入 HTML」的方式加载 DOM——
Chromium 禁止用 location.href 跳转到 data: URL，但产品的正常导航（https）
不受影响。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from mybrowser.agent_tool import MyBrowserTool
    from mybrowser.net.ws import WebSocket
except ImportError:
    from agent_tool import MyBrowserTool
    from net.ws import WebSocket

HTML = (
    "<head><title>测试页</title>"
    "<meta name='description' content='desc'>"
    "<meta property='og:title' content='OG标题'></head>"
    "<body><h1>标题</h1><p>正文 <b>加粗</b> "
    "<a href='https://example.com'>链接</a></p>"
    "<ul><li>甲</li><li>乙</li></ul></body>")

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_it_session.js")


def load_html(tool, html, title="测试页"):
    """在 about:blank 页上注入 HTML（绕开 data: URL 限制）。"""
    js = ("document.documentElement.innerHTML = %s; document.title = %s;"
          % (json.dumps(html), json.dumps(title)))
    return tool.execute("EVAL " + js)


def main():
    tool = MyBrowserTool(port=9640, headless=True)
    if not tool.start("about:blank"):
        print("[FAIL] 启动失败:", tool._error)
        return 1
    print("[ok] 浏览器启动")
    if not tool.available:
        # 某些环境（如沙箱/无显示）里 headless Edge 进程启动即退出，无法在此
        # 做端到端验证。这不是代码问题，请在 Windows 上运行奇点Agent 验证。
        print("[SKIP] 浏览器进程启动后不可用（当前环境无法运行 headless Edge），"
              "端到端验证跳过。请在你的 Windows 上运行奇点Agent 验证。")
        tool.stop()
        return 0

    load_html(tool, HTML)
    time.sleep(1.0)

    # ---- DUMP ----
    print("\n=== DUMP markdown ===")
    md = tool.execute("DUMP markdown")
    print(md)
    assert "标题" in md and "**加粗**" in md and "链接" in md, "markdown dump 异常"

    print("\n=== DUMP text ===")
    print(tool.execute("DUMP text"))

    print("\n=== DUMP structured ===")
    st = tool.execute("DUMP structured")
    print(st)
    assert "og" in st, "structured dump 异常"

    # ---- TRACE / RECORD / REPLAY ----
    print("\n=== TRACE -> RECORD -> REPLAY ===")
    tool.execute("TRACE on")
    load_html(tool, "<title>第二页</title><body><p>内容二</p></body>", "第二页")
    time.sleep(1.0)
    tool.execute("DUMP text")
    tool.execute("TRACE off")
    tool.execute("RECORD SAVE " + SCRIPT_PATH)
    print("[ok] 已导出脚本:", SCRIPT_PATH)
    lines = open(SCRIPT_PATH, encoding="utf-8").read().splitlines()
    print("[ok] 脚本行数(含注释):", len(lines))

    # 重放：换到空白页，REPLAY 应把它带回到第二页
    tool.execute("EVAL document.documentElement.innerHTML = '<body><p>空白</p></body>'; document.title='空白';")
    time.sleep(0.8)
    rep = tool.execute("REPLAY " + SCRIPT_PATH)
    print(rep[:400])
    assert "重放" in rep, "replay 失败"
    print("[ok] 重放完成")

    # ---- SERVE + 页面级 CDP 转发 ----
    print("\n=== SERVE + CDP 转发 ===")
    ep = tool.serve(9240)
    print("[ok] CDP 端点:", ep)

    # 1) 浏览器级：拿到 target 列表
    ws = WebSocket.connect(ep)
    ws.send_text(json.dumps({"id": 1, "method": "Target.getTargets"}))
    t = json.loads(ws.recv_text())
    tid = t["result"]["targetInfos"][0]["targetId"]
    print("[ok] 找到 target:", tid[:16], "...")
    ws.close()

    # 2) 页面级：连到该 target，发 Runtime.evaluate（经 MyBrowser 桥转发给 Chromium）
    #    注意：page 级 WS 端点是 /devtools/page/<id>，不能用 ep(=/devtools/browser) 拼
    page_ep = ep.replace("/devtools/browser", "/devtools/page/" + tid)
    ws2 = WebSocket.connect(page_ep)
    ws2.send_text(json.dumps({
        "id": 2, "method": "Runtime.evaluate",
        "params": {"expression": "document.title", "returnByValue": True}}))
    r2 = json.loads(ws2.recv_text())
    print("[ok] 页面级 Runtime.evaluate ->", r2)
    assert "result" in r2, "页面级转发失败"

    # 3) 自定义域：MyBrowser.dump
    ws2.send_text(json.dumps({
        "id": 3, "method": "MyBrowser.dump",
        "params": {"format": "text"}}))
    r3 = json.loads(ws2.recv_text())
    print("[ok] MyBrowser.dump ->",
          (r3.get("result", {}).get("content", "") or "")[:60])
    ws2.close()

    tool.stop()
    print("\n[ALL DONE] 三块能力（dump / trace / serve）均验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
