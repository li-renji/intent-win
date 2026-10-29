# -*- coding: utf-8 -*-
"""上下文用量 / 长论文超限的回归自检。

模拟真实场景：接入一个 32K 上下文的云端模型，把一篇长论文贴进去发送。
期望：
  1. 容量识别正确（用档案里厂商给的 context_length，而不是猜的默认值）
  2. 发送前预检裁剪生效，请求能发出去（不再被上游拒「上下文超了」）
  3. 前端 gauge 的估算口径包含本轮输入，不再显示 0%
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import econ
from agent.config import model_ctx_limit
from agent.loop import AgentLoop

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  -> " + str(extra)) if extra else ""))


PAPER = ("本节介绍深度学习模型的上下文窗口机制与长文本处理方法。"
         "论文实验部分详述了数据规模、训练轮数与评测指标的选择依据。") * 2000


def _cfg():
    return {
        "llm_mode": "cloud",
        "base_url": "https://api.example.com/v1",
        "model": "vendor-model-x",
        "api_key": "sk-test",
        "llm_ctx": 0,
        "profiles": ("[{\"name\":\"厂商模型\",\"base_url\":\"https://api.example.com/v1\","
                     "\"model\":\"vendor-model-x\",\"ctx\":32768}]"),
        "agent_compact_ratio": 0.8,
        "agent_keep_tokens": 8000,
        "agent_max_tool_result": 1500,
        "agent_tool_mode": "auto",
        "agent_max_rounds": 6,
        "agent_max_seconds": 180,
        "agent_text_fallback": "on",
        "agent_req_timeout": 90,
    }


print("=== 1. 容量识别 ===")
cfg = _cfg()
cap = model_ctx_limit(cfg, "vendor-model-x")
check("档案里的 context_length 优先于默认值", cap == 32768, cap)

cfg2 = dict(cfg)
cfg2["profiles"] = ""
check("档案缺失时回退到保守默认（不再假装 40K）",
      model_ctx_limit(cfg2, "vendor-model-x") == 32768,
      model_ctx_limit(cfg2, "vendor-model-x"))

cfg3 = dict(cfg)
cfg3["llm_ctx"] = 65536
check("手动 llm_ctx 优先级最高", model_ctx_limit(cfg3, "vendor-model-x") == 65536)


print("\n=== 2. 发送前预检裁剪 ===")


class _FakeBus:
    def enabled(self):
        return []

    def openai_schemas(self):
        return []


loop = AgentLoop(cfg, _FakeBus(), None, None)
messages = [
    {"role": "system", "content": "你是助手。" * 200},
    {"role": "user", "content": PAPER},
]
before = econ.messages_tokens2(messages)
budget = max(512, int(cap * 0.85))
check("论文原始估算确实超预算（问题可复现）", before > budget, "before=%d budget=%d" % (before, budget))

fitted, dropped = loop._fit_to_budget(messages, budget)
after = econ.messages_tokens2(fitted)
check("裁剪后装得进预算", after <= budget, "after=%d budget=%d" % (after, budget))
check("裁剪有实际发生（dropped>0）", dropped > 0, dropped)
check("system 提示未被破坏",
      fitted[0]["content"] == messages[0]["content"])
check("用户消息被裁短了", len(fitted[1]["content"]) < len(PAPER),
      "%d -> %d" % (len(PAPER), len(fitted[1]["content"])))
check("裁剪后保留了开头", PAPER[:80] in fitted[1]["content"])
check("裁剪后保留了结尾", PAPER[-80:] in fitted[1]["content"])
check("裁剪处有省略说明", "省略" in fitted[1]["content"])


print("\n=== 3. 超限重试路径：长论文也一起压 ===")
msgs = [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": "请分析"},
    {"role": "assistant", "content": "x" * 5000},
    {"role": "tool", "content": "工具结果" * 500},
    {"role": "user", "content": PAPER},
]
comp = loop._compress_for_ctx(msgs)
user_msgs = [m for m in comp if m.get("role") == "user"]
longest = max(len(str(m.get("content") or "")) for m in user_msgs)
check("压缩后最长用户消息已明显变短", longest < len(PAPER),
      "longest=%d orig=%d" % (longest, len(PAPER)))
check("系统提示仍然保留", any(m.get("role") == "system" for m in comp))


print("\n=== 4. 多模态附件场景 ===")
mm = [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": [
        {"type": "text", "text": PAPER},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]},
]
fitted2, dropped2 = loop._fit_to_budget(mm, budget)
parts = fitted2[1]["content"]
check("多模态裁剪后仍能装进预算", econ.messages_tokens2(fitted2) <= budget,
      econ.messages_tokens2(fitted2))
check("图片部分被保留", any(p.get("type") == "image_url" for p in parts))
check("文本部分被裁短", len(parts[0]["text"]) < len(PAPER))


print("\n=== 5. estimate_tokens 口径 ===")
check("中文 1 字≈1 tok", 1000 <= econ.estimate_tokens("中" * 1000) <= 1010,
      econ.estimate_tokens("中" * 1000))
check("英文 4 字符≈1 tok", 240 <= econ.estimate_tokens("a" * 1000) <= 260,
      econ.estimate_tokens("a" * 1000))
check("clamp_tokens 会返回被裁掉的字符数",
      econ.clamp_tokens(PAPER, 1000)[1] > 0)


print("\n=== 6. 错误分类：限流不得被误报成「上下文超限」 ===")
# 真实场景：厂商返回 429 "inference exceeds tpm/rpm limit"，
# 旧逻辑因为匹配到 "exceed" 而误判为上下文超限，用户被指向错误方向。
rate_msg = ('触发限流或额度不足（HTTP 429）；上游返回：'
            '{"error":{"message":"inference exceeds tpm/rpm limit",'
            '"type":"rate_limit_error","code":"insufficient_quota"}}')
ctx_msg = ('请求超出模型上下文容量（HTTP 400）；上游返回：'
           '{"error":{"message":"context length exceeded"}}')
check("TPM 限流不再判定为上下文超限",
      not AgentLoop._is_ctx_error({"kind": "http", "code": 429, "msg": rate_msg}))
check("TPM 限流被判定为限流",
      AgentLoop._is_rate_error({"kind": "http", "code": 429, "msg": rate_msg}))
check("真正的上下文超限仍判定为上下文问题",
      AgentLoop._is_ctx_error({"kind": "http", "code": 400, "msg": ctx_msg}))
check("真正的上下文超限不被判为限流",
      not AgentLoop._is_rate_error({"kind": "http", "code": 400, "msg": ctx_msg}))
check("配额不足判定为限流",
      AgentLoop._is_rate_error({"kind": "http", "code": 429,
                                "msg": "insufficient_quota"}))
check("网络超时既不是上下文也不是限流",
      not AgentLoop._is_ctx_error({"kind": "net", "msg": "连接超时"}) and
      not AgentLoop._is_rate_error({"kind": "net", "msg": "连接超时"}))
check("HTTP 413（请求体过大）判定为上下文问题",
      AgentLoop._is_ctx_error({"kind": "http", "code": 413, "msg": "请求体过大"}))

print("\n=== 7. 厂商 context_length 提取 ===")
from agent.provider import _model_ctx
check("标准字段 context_length", _model_ctx({"context_length": 1048576}) == 1048576)
check("兼容字段 max_context", _model_ctx({"max_context": 32768}) == 32768)
check("兼容字段 context", _model_ctx({"context": 65536}) == 65536)
check("字符串数字", _model_ctx({"context_length": "131072"}) == 131072)
check("认不出时返回 0", _model_ctx({"foo": "bar"}) == 0)
check("过小的值不当成容量", _model_ctx({"context_length": 100}) == 0)

print("\n=== 结果 ===")
print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("失败：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过")
