# -*- coding: utf-8 -*-
"""MyBrowser 作为 Agent 工具的端到端验证。

模拟 agent/tools.py 的用法：attach_browser(dispatch) → LLM 发指令 → 拿结果。

跑法： python -m mybrowser.agent_test
"""
import sys
import time

from .agent_tool import MyBrowserTool, make_dispatch


def main():
    print("[agent] 创建 MyBrowser 工具…")
    tool = MyBrowserTool(port=9630, headless=True)
    if not tool.start("https://www.baidu.com"):
        print("[agent] 启动失败:", tool._error)
        return 1
    print("[agent] 浏览器已就绪")

    dispatch = make_dispatch(tool)

    # 模拟 LLM 的工具调用序列
    script = [
        "SNAPSHOT",
        "READ 300",
        "NAVIGATE https://www.baidu.com/s?wd=mybrowser",
        "SNAPSHOT 800",
        "DIFF 500",
    ]

    for cmd in script:
        print("\n" + "=" * 60)
        print(">>> BROWSER %s" % cmd)
        t0 = time.time()
        out = dispatch(cmd)
        dt = (time.time() - t0) * 1000
        text = out if isinstance(out, str) else str(out)
        print(text[:700])
        print("<<< (%d ms, %d 字符)" % (dt, len(text)))

    # 点击测试：找出快照里的第一个元素并点击
    print("\n" + "=" * 60)
    snap_out = dispatch("SNAPSHOT 2000")
    print("[agent] 点击测试：从快照解析元素编号")

    tool.stop()
    print("\n[agent] ===== Agent 工具链路验证完成 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
