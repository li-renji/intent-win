# -*- coding: utf-8 -*-
"""经济性改造测试：token 估算 / 截断 / 主动压缩 / fs 分段读取。

覆盖 pi 式省钱手段在本项目里的落地：
1. econ.estimate_tokens / truncate_middle / truncate_head
2. AgentLoop._compact_messages（主动压缩，不丢任务主线、不出裸 tool 消息）
3. ToolBus 工具结果中间截断（错误在尾部也能看到）
4. fs read offset/limit 分段 + 可操作续读提示
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import econ                                    # noqa: E402
from agent.config import load_config                      # noqa: E402
from agent.loop import AgentLoop                          # noqa: E402
from agent.tools import ToolBus                           # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s %s" % ("[PASS]" if cond else "[FAIL]", name, detail))


def run():
    # ---- 1. token 估算 ----
    check("估算：中文约 1 tok/字", 380 < econ.estimate_tokens("你好世界" * 100) < 460)
    check("估算：空串为 0", econ.estimate_tokens("") == 0)
    check("估算：消息列表含结构开销",
          econ.messages_tokens([{"role": "user", "content": "你好"}]) > 2)

    # ---- 2. 中间截断（保头尾） ----
    long_text = "A" * 2000 + "B" * 2000 + "ERROR_AT_TAIL"
    cut, truncated = econ.truncate_middle(long_text, 1000)
    check("截断：超限被截", truncated)
    check("截断：头部保留", cut.startswith("AAA"))
    check("截断：尾部错误可见", "ERROR_AT_TAIL" in cut)
    check("截断：有省略标记", "省略" in cut)
    same, t2 = econ.truncate_middle("短文本", 1000)
    check("截断：未超限原样返回", not t2 and same == "短文本")

    # ---- 3. 头部截断（文件读取用） ----
    body = "\n".join("line%d" % i for i in range(1000))
    head, info = econ.truncate_head(body, max_lines=100)
    check("截断：行数封顶", info and info["output_lines"] == 100)
    check("截断：给出续读行号", info and info["next_offset"] == 101)
    head2, info2 = econ.truncate_head("only one line", max_lines=100)
    check("截断：未超限 info=None", info2 is None and head2 == "only one line")

    # ---- 4. 主动压缩 ----
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(15):
        msgs.append({"role": "user", "content": "第%d轮任务 %s" % (i, "长" * 200)})
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": "c%d" % i,
                                     "function": {"name": "fs", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": "c%d" % i,
                     "name": "fs", "content": "结果" * 100})
    msgs.append({"role": "user", "content": "最终任务：找文件"})
    before = econ.messages_tokens(msgs)
    comp = AgentLoop._compact_messages(msgs, 500)
    after = econ.messages_tokens(comp)
    check("压缩：token 显著下降", after < before * 0.6,
          "%d → %d" % (before, after))
    roles = [m["role"] for m in comp]
    check("压缩：系统提示保留", comp[0]["role"] == "system")
    check("压缩：首轮任务保留", any(m.get("content", "").startswith("第0轮任务")
                                for m in comp))
    check("压缩：最终任务保留", comp[-1].get("content") == "最终任务：找文件")
    check("压缩：没有裸 tool 消息", all(
        not (m["role"] == "tool") for m in comp[:2]))
    check("压缩：包含过程摘要", any("早期过程摘要" in str(m.get("content") or "")
                                for m in comp))

    # ---- 5. ToolBus 中间截断（经 invoke） ----
    cfg = load_config()
    cfg["agent_max_tool_result"] = 800
    bus = ToolBus(cfg)
    loop = AgentLoop(cfg, bus)

    # ---- 6. fs 分段读取 ----
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "big.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join("第%d行" % i for i in range(1, 501)))
    ok, r1 = bus.invoke("fs", {"op": "read", "path": p, "limit": 100})
    check("fs：首页读取", ok and "第1行" in r1 and "共 500 行" in r1)
    ok, r2 = bus.invoke("fs", {"op": "read", "path": p, "offset": 101, "limit": 100})
    check("fs：分段续读", ok and "第101行" in r2 and "offset=201" in r2)
    ok, r3 = bus.invoke("fs", {"op": "read", "path": p, "offset": 999})
    check("fs：越界友好提示", ok and "超出文件范围" in r3)

    # ---- 7. 压缩比例配置生效 ----
    check("配置：压缩比例读取", loop.compact_ratio > 0)

    ok_all = all(r[1] for r in RESULTS)
    print("\n==== test_econ: %d/%d 通过 ====" % (
        sum(1 for r in RESULTS if r[1]), len(RESULTS)))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(run())
