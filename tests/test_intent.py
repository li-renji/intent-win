# -*- coding: utf-8 -*-
"""三层意图解析（agent/intent.py）专项测试。

覆盖：
  1. Tier 1 标准协议 [[TOOL: name {json}]]（含坏 JSON 宽容处理）
  2. Tier 2 变体标记：【文件列表】/ [TOOL: fs] / tool_call 标记 JSON
  3. Tier 3 自然语言意图 + seen 去重
  4. 普通对话不误触发
  5. 未知工具名 Tier1 透传（交给总线报错）、Tier2 丢弃
  6. 剥离标记后的正文干净可读
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.intent import extract

EN = ["fs", "code", "screen", "browser", "memo", "skills", "phone"]
results = []


def check(name, ok, extra=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" | " + extra) if extra else ""))
    results.append(ok)


def names(calls):
    return [c["function"]["name"] for c in calls]


def args_of(calls, i=0):
    import json
    return json.loads(calls[i]["function"]["arguments"])


# 1. Tier 1 标准协议
c, t, tier = extract('我先看看。[[TOOL: fs {"op": "list", "path": "~/Desktop"}]]', EN, set())
check("Tier1 标准协议", tier == 1 and names(c) == ["fs"] and args_of(c)["op"] == "list")
check("Tier1 剥离标记后正文干净", t == "我先看看。", repr(t))

c, _, tier = extract('[[TOOL: fs {坏 JSON}]][[TOOL: code {"lang":"python","code":"print(1)"}]]',
                    EN, set())
check("Tier1 宽容解析 + 多条调用", tier == 1 and names(c) == ["fs", "code"])

# 2. Tier 2 变体标记
c, t, tier = extract('我来看看\n【文件列表】\n桌面上有这些', EN, set())
check("Tier2 中文方括号【文件列表】", tier == 2 and names(c) == ["fs"])
check("Tier2 标记已剥离", "【" not in t and t == "我来看看\n\n桌面上有这些", repr(t))

c, _, tier = extract('[TOOL: screen]', EN, set())
check("Tier2 [TOOL: screen] 无参数", tier == 2 and names(c) == ["screen"])

c, _, tier = extract('[TOOL:fs {"op":"read","path":"a.txt"}]', EN, set())
check("Tier2 [TOOL:fs {...}] 带参数", tier == 2 and args_of(c)["path"] == "a.txt")

tc_open = "<tool" + "_call>"
tc_close = "</tool" + "_call>"
c, _, tier = extract(tc_open + '{"name":"fs","arguments":{"op":"info","path":"x"}}' + tc_close,
                     EN, set())
check("Tier2 tool_call 标记 JSON", tier == 2 and args_of(c)["op"] == "info")

c, _, tier = extract('【截图】', EN, set())
check("Tier2 中文别名【截图】→ screen", tier == 2 and names(c) == ["screen"])

# 3. Tier 3 自然语言意图 + 去重
seen = set()
c, t, tier = extract('你帮我看看桌面上有什么文件', EN, seen)
check("Tier3 自然语言→fs list 桌面", tier == 3 and names(c) == ["fs"]
      and args_of(c)["path"] == "~/Desktop")
c2, _, tier2 = extract('再列一次桌面上有什么文件', EN, seen)
check("Tier3 seen 去重不重复触发", tier2 == 0 and not c2)
seen2 = set()
c3, _, tier3 = extract('现在屏幕上是什么', EN, seen2)
check("Tier3 屏幕意图→screen shot", tier3 == 3 and names(c3) == ["screen"])

# 4. 普通对话不误触发
for txt in ["你好", "谢谢你的帮助", "1+1等于几", "今天天气怎么样", "帮我写一首诗"]:
    c, t, tier = extract(txt, EN, set())
    if c:
        break
else:
    c = None
check("普通对话不误触发工具", not c)

# 5. 未知工具名：Tier1 透传 / Tier2 丢弃
c, _, tier = extract('[[TOOL: fly {"where":"moon"}]]', EN, set())
check("Tier1 未知工具透传（总线将报错回灌）", tier == 1 and names(c) == ["fly"])
c, _, tier = extract('【飞行器】', EN, set())
check("Tier2 未知中文名丢弃", tier == 0 and not c)

# 6. 未启用的工具在 Tier2/3 不映射
c, _, tier = extract('【浏览器】', ["fs"], set())
check("Tier2 未启用工具不映射", tier == 0 and not c)
c, _, tier = extract('帮我记住我喜欢深色主题', ["fs"], set())
check("Tier3 未启用工具不映射", tier == 0 and not c)

# 7. 边界输入
c, t, tier = extract("", EN, set())
check("空输入安全", tier == 0 and not c and t == "")
c, t, tier = extract(None, EN, set())
check("None 输入安全", tier == 0 and not c)

print("\n结果: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
# 只在直接运行时才退出；被 unittest discover 导入时不能抛 SystemExit
if __name__ == "__main__":
    sys.exit(0 if all(results) else 1)
