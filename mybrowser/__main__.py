# -*- coding: utf-8 -*-
"""MyBrowser 启动入口：打开自研浏览器界面。

用法：
    python -m mybrowser              # 正常模式（带界面）
    python -m mybrowser --headless   # 无界面（仅控制面）
"""
import sys


def main():
    headless = "--headless" in sys.argv
    port = 9610
    serve_port = None
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        if a == "--serve" and i + 1 < len(sys.argv):
            serve_port = int(sys.argv[i + 1])

    if headless:
        from .agent_tool import MyBrowserTool
        tool = MyBrowserTool(port=port, headless=True)
        if not tool.start():
            print("启动失败:", tool._error)
            return 1
        print("MyBrowser(headless) 已就绪，端口", port)
        print("可用指令: NAVIGATE / SEARCH / SNAPSHOT / DUMP / CLICK / TYPE / "
              "TRACE / RECORD / REPLAY / SERVE …")
        if serve_port:
            ep = tool.serve(serve_port)
            print("CDP 服务器已启动:", ep)
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            tool.stop()
        return 0

    from .ui.shell import BrowserUI
    from .agent_tool import MyBrowserTool
    ui = BrowserUI(headless=False)
    ui.engine.port = port
    print("启动 MyBrowser …")
    if serve_port:
        # 先建一个工具并接管 UI 控制面，再起 CDP 服务器
        tool = MyBrowserTool(port=port, headless=False)
        tool.attach_to_ui(ui)
        ep = tool.serve(serve_port)
        print("CDP 服务器已启动:", ep)
    ui.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
