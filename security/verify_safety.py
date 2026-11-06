# -*- coding: utf-8 -*-
# SECURITY-FIXTURE: 本文件下方出现的密钥/手机号均为合成自测样本，绝非真实凭证。
"""安全校验端到端自验收：验证真实接线，而不是只测模块函数。

跑法：cd D:/INTENT-win && python security/verify_safety.py
"""
import http.server
import json
import os
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import llm, safety  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.tools import ToolBus  # noqa: E402

RESULTS = []
SECRET = "sk-abcdefghijklmnop123456"
CAPTURED = []
HEADERS = []


def check(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra else ""))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        HEADERS.append(dict(self.headers))
        CAPTURED.append(raw)
        try:
            is_stream = bool(json.loads(raw.decode("utf-8")).get("stream"))
        except Exception:
            is_stream = False
        if is_stream:
            body = (b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                    b'data: [DONE]\n\n')
        else:
            body = json.dumps({"choices": [{"message": {"content": "ok"},
                                            "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


print("\n==== 0. 配置生效 ====")
cfg = load_config()
check("safety_* 已进配置白名单",
      cfg.get("safety_mode") == "enforce" and cfg.get("safety_mask_pii") is True
      and cfg.get("safety_audit_log") is True,
      {k: cfg.get(k) for k in ("safety_mode", "safety_mask_pii", "safety_audit_log")})
check("当前运行模式为 enforce", safety._mode(cfg) == "enforce")

LOG = safety.LOG_PATH
try:
    LOG_BEFORE = os.path.getsize(LOG)
except OSError:
    LOG_BEFORE = 0

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
endpoint = "http://127.0.0.1:%d/v1" % port

messages = [
    {"role": "system", "content": "你是助手"},
    {"role": "tool", "content": "读到配置：api_key=" + SECRET},
    {"role": "user", "content": "我的手机号 13812345678，请记住"},
]

print("\n==== 1. 真实 llm._post 出站收口（本地假服务器捕获请求体）====")
ok, content = llm.chat(endpoint, "mock", "sk-noauth", messages, stream=False)
body = CAPTURED[-1].decode("utf-8") if CAPTURED else ""
check("chat() 正常返回", ok is True and content == "ok", (ok, content))
check("真实请求体不含原文 API Key", SECRET not in body, body[:120])
check("真实请求体不含手机号", "13812345678" not in body)
check("脱敏占位符确实进了请求体", "已脱敏" in body)
check("正常聊天内容未被破坏", "你是助手" in body and "我的手机号" in body)
check("认证头仍然完整（不影响调用）",
      bool(HEADERS) and HEADERS[-1].get("Authorization") == "Bearer sk-noauth",
      HEADERS[-1].get("Authorization") if HEADERS else "")

ok2, gen = llm.chat_stream_events(endpoint, "mock", "sk-noauth", messages)
events = list(gen) if ok2 else []
body2 = CAPTURED[-1].decode("utf-8") if CAPTURED else ""
check("chat_stream_events 正常产出事件", ok2 and any(e[0] == "text" for e in events), events[:2])
check("流式路径同样过闸门", SECRET not in body2 and "13812345678" not in body2)

print("\n==== 2. 真实 ToolBus 路径拦截 ====")
bus = ToolBus(cfg)
try:
    bus._resolve_path("配置.qdai")
    blocked, reason = False, ""
except Exception as e:
    blocked, reason = "[安全校验]" in str(e), str(e)
check("_resolve_path 拦住凭证文件", blocked, reason[:70])
check("拒绝原因含可读说明", "凭证" in reason or "密钥" in reason)
check("普通路径放行且解析结果不变",
      bus._resolve_path("README.md") == os.path.join(cfg["project_root"], "README.md"))

bus.handlers["fs"] = bus._tool_fs  # 确保被测工具已注册，不受配置清单影响
ok3, res3 = bus.invoke("fs", {"op": "read", "path": "配置.qdai"})
check("invoke 返回明确拒绝而非崩溃", ok3 is False and "[安全校验]" in res3, res3[:70])
r = json.loads(bus.dispatch(json.dumps(
    {"tool": "fs", "args": {"op": "read", "path": "配置.qdai"}}, ensure_ascii=False)))
check("6801 总线同样返回拒绝", r.get("ok") is False and "[安全校验]" in r.get("error", ""),
      r.get("error", "")[:70])
ok4, res4 = bus.invoke("fs", {"op": "read", "path": "README.md"})
check("普通文件工具调用不受影响", ok4 is True, res4[:60])

print("\n==== 3. 审计日志（只记类型，不记原文）====")
try:
    with open(LOG, "r", encoding="utf-8") as f:
        f.seek(LOG_BEFORE)
        tail = f.read()
except OSError:
    tail = ""
check("审计日志已落盘且含两类事件", "outbound" in tail and "path" in tail)
check("日志不含原文 API Key", SECRET not in tail)
check("日志不含真实手机号", "13812345678" not in tail)
check("日志不含完整本机路径", "D:\\INTENT-win\\配置.qdai" not in tail
      and "C:\\Users\\" not in tail)
check("日志含敏感类型统计", "API_KEY" in tail or "PHONE" in tail)
check("日志含 blocked 动作", "blocked" in tail)

print("\n==== 4. 报告与命令行 ====")
md, where = safety.build_report(save=True)
check("报告写在任务目录 security/", where == safety.REPORT_PATH and os.path.exists(where), where)
check("报告回答「隐私怎么处理」", "出网" in md and "明文落盘" in md)
check("报告回答「私密内容会怎么样」", "装上校验后" in md and "替换为" in md)
check("报告如实列出未改动风险", "明文 API Key" in md and "我没有擅自改动" in md)
check("报告自带自验收结果", "PASS" in md and "内置自检" in md)
r1 = subprocess.run([sys.executable, "-m", "agent.safety", "--selftest"],
                    cwd=ROOT, capture_output=True, text=True, timeout=60)
check("CLI --selftest 可用", r1.returncode == 0 and "PASS" in r1.stdout,
      r1.stdout.strip().splitlines()[-1] if r1.stdout.strip() else r1.stderr[:80])
r2 = subprocess.run([sys.executable, "-m", "agent.safety", "--check",
                     "api_key=" + SECRET], cwd=ROOT, capture_output=True, text=True, timeout=60)
check("CLI --check 能识别样本", r2.returncode == 0 and "API_KEY" in r2.stdout,
      r2.stdout.strip().splitlines()[0] if r2.stdout.strip() else "")

srv.shutdown()
srv.server_close()
print("\n==== %d/%d PASS ====" % (sum(RESULTS), len(RESULTS)))
sys.exit(0 if all(RESULTS) else 1)
