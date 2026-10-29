# -*- coding: utf-8 -*-
"""实测手机端 1.5B 模型在 few-shot 文本协议下能否输出 [[TOOL: ...]]。

用法：python probe_text_protocol.py
"""
import json
import sys
import time
import urllib.error
import urllib.request
import os

PHONE = "http://192.168.3.4:9999"
KEY = (os.environ.get("PHONE_KEY") or "").strip() or (open(pkp).read().strip() if os.path.exists(pkp := os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone.key")) else "")
MODEL = "qwen2.5-1.5b-mnn"

TIP = """

【工具调用协议】
你可以调用以下工具：browser、memo、skills

调用格式（必须严格照此格式，单独成行）：
[[TOOL: 工具名 {"参数名": "参数值"}]]

示例对话：
用户：帮我看看百度首页上有什么
助手：[[TOOL: browser {"cmd": "NAVIGATE https://www.baidu.com"}]]
助手：[[TOOL: browser {"cmd": "SNAPSHOT"}]]
助手：百度首页上有搜索框、新闻分类和几个热搜词。

规则：
1. 需要获取网页/文件等外部信息时，必须实际调用工具，不要凭空猜测或描述你"将要"做什么。
2. 一次只调用一个工具，等系统回灌结果后再决定下一步。
3. 拿到工具结果后，用自然语言给出最终答复，答复里不要出现 [[TOOL: 字样。
4. 不需要工具的问题，直接回答即可。"""


def call(payload, timeout=45, tries=3):
    req = urllib.request.Request(PHONE + "/v1/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", "Bearer " + KEY)
    req.add_header("Content-Type", "application/json")
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return "HTTP %s" % e.code
        except Exception as e:
            print("   attempt %d: %s" % (i + 1, type(e).__name__), flush=True)
            time.sleep(2)
    return "TIMEOUT"


def content_of(raw):
    try:
        return json.loads(raw)["choices"][0]["message"]["content"]
    except Exception:
        return raw


CASES = [
    ("需要工具的问题", "帮我看看百度首页上有什么"),
    ("简单问题（不该调工具）", "你好"),
]


def main():
    print("模型: %s @ %s\n" % (MODEL, PHONE))

    # 先探活
    try:
        req = urllib.request.Request(PHONE + "/health",
                                     headers={"Authorization": "Bearer " + KEY})
        with urllib.request.urlopen(req, timeout=6) as r:
            print("health: %s %s\n" % (r.status, r.read()[:120].decode("utf-8", "ignore")))
    except Exception as e:
        print("health: 不可达 (%s) —— 手机可能处于深度睡眠\n" % type(e).__name__)

    results = []
    for label, q in CASES:
        sysmsg = "你是奇点Agent（Windows执行体）。状态如实报告，不谎称未具备的能力。" + TIP
        raw = call({"model": MODEL, "messages": [
            {"role": "system", "content": sysmsg},
            {"role": "user", "content": q}], "stream": False})
        c = content_of(raw)
        has_tool = "[[TOOL:" in c
        print("=== %s ===" % label)
        print("问题: %s" % q)
        print("输出: %s" % c[:400].replace("\n", " "))
        print("→ 含工具调用: %s" % ("是 ✅" if has_tool else "否 ❌"))
        print()
        sys.stdout.flush()
        results.append((label, has_tool))

    print("=" * 50)
    tool_ok = dict(results).get("需要工具的问题", False)
    plain_ok = not dict(results).get("简单问题（不该调工具）", True)
    if tool_ok and plain_ok:
        print("结论: ✅ 模型可用文本协议驱动工具调用")
    elif tool_ok:
        print("结论: ⚠️ 模型会调工具，但简单问题也乱调（需要更强的约束）")
    else:
        print("结论: ❌ 模型无法通过提示工程获得工具调用能力")
        print("      → 需换模型（≥7B 且经 function calling 训练）")
    print("=" * 50)


if __name__ == "__main__":
    main()
