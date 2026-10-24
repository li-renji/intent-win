# -*- coding: utf-8 -*-
"""qd-agent-win 入口：装配 配置.qdai → ToolBus → 浏览器 → 三端口。

用法：
  python -m agent.main            # 三端口 + 浏览器窗口
  python -m agent.main --no-gui   # 仅三端口（无浏览器窗口，浏览器工具降级）
"""
import asyncio
import sys

from .browser.webview2 import WebView2Adapter
from .config import load_config
from .history import SessionHistory
from .servers import Context, serve_6800, serve_6801, serve_6802
from .tools import ToolBus


async def main():
    no_gui = "--no-gui" in sys.argv
    cfg = load_config()

    bus = ToolBus(cfg)
    browser = WebView2Adapter(cfg)
    bus.attach_browser(browser.execute)
    history = SessionHistory(cfg["history_facts_n"], cfg["summary_max_chars"])
    ctx = Context(cfg, bus, browser, history)

    # 浏览器窗口（pywebview 自带事件循环，放后台线程，与 asyncio 并存）
    if cfg["browser_enabled"] and not no_gui:
        def _boot():
            if not browser.start():
                print("[browser] " + (browser.error or "启动失败"))
        import threading
        threading.Thread(target=_boot, daemon=True).start()
    else:
        browser.error = "浏览器未启用（browser_enabled=off 或 --no-gui）"

    servers = [await serve_6800(ctx), await serve_6801(ctx)]
    s6802 = await serve_6802(ctx)
    if s6802:
        servers.append(s6802)

    print("奇点 Windows Agent 就绪。工具清单: %s" % (", ".join(bus.enabled()) or "无"))
    try:
        await asyncio.Event().wait()  # 常驻
    finally:
        for s in servers:
            s.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
