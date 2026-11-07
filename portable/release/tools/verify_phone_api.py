"""
奇点OS 手机端能力回归测试（装完新 APK 后跑这个）。

覆盖四件事：
  1. /health                  服务与模型状态
  2. /chat                    普通对话（单次推理）
  3. /v1/chat/completions + tools → 标准 tool_calls（外部 agent 可驱动）
  4. /agent/run               手机端自己跑 Agent 循环并执行任务
"""
import json
import sys
import time
import urllib.error
import urllib.request
import os

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9997"
KEY = (os.environ.get("PHONE_KEY") or "").strip() or (open(pkp).read().strip() if os.path.exists(pkp := os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone.key")) else "")


def call(path, body=None, timeout=180):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + KEY)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, round(time.time() - t0, 2), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return False, round(time.time() - t0, 2), "HTTP %d: %s" % (e.code, e.read()[:200].decode("utf-8", "ignore"))
    except Exception as e:
        return False, round(time.time() - t0, 2), "%s: %s" % (type(e).__name__, str(e)[:120])


def show(title, ok, dt, msg, limit=400):
    print("\n=== %s ===" % title)
    print("结果: %s  耗时: %ss" % ("OK" if ok else "FAIL", dt))
    print(msg[:limit])
    return ok


results = []

# 1. health
ok, dt, msg = call("/health", timeout=30)
results.append(show("1. /health 服务状态", ok, dt, msg))

# 2. 普通对话
ok, dt, msg = call("/chat", {"prompt": "用一句话说：1+1等于几？"}, timeout=120)
results.append(show("2. /chat 普通对话", ok, dt, msg))

# 3. 工具调用：模型应返回 tool_calls
tools = [{
    "type": "function",
    "function": {
        "name": "phone",
        "description": "把任务交给手机上的奇点OS智能体执行（能在手机容器跑命令、改界面、启动应用）",
        "parameters": {"type": "object", "properties": {"task": {"type": "string", "description": "要执行的任务"}},
                       "required": ["task"]}
    }
}]
ok, dt, msg = call("/v1/chat/completions", {
    "model": "qwen1.5b",
    "messages": [{"role": "user", "content": "请用手机工具列一下容器里有哪些文件"}],
    "tools": tools
}, timeout=180)
has_tool_call = '"tool_calls"' in msg
results.append(show("3. tool_calls 桥接（关键）", ok and has_tool_call, dt, msg, 600))

# 4. 手机端 Agent 自执行
ok, dt, msg = call("/agent/run", {"prompt": "用 getStatus 工具告诉我容器和AI的状态", "rounds": 3}, timeout=240)
has_trace = '"trace"' in msg
results.append(show("4. /agent/run 手机端执行任务", ok and has_trace, dt, msg, 900))

print("\n================ 汇总 ================")
names = ["health", "普通对话", "tool_calls桥接", "手机端Agent"]
for n, r in zip(names, results):
    print("  %-14s %s" % (n, "通过" if r else "未通过"))
