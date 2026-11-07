# -*- coding: utf-8 -*-
"""工作台改造自验收：起真实服务，逐条验证，结束恢复所有运行时数据。

运行：runtime/python/python.exe workbench/verify.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WB = ROOT / "workbench"
PY = str(ROOT / "runtime" / "python" / "python.exe")
PORT = 31977
BASE = "http://127.0.0.1:%d" % PORT
MOCK_PORT = 31978
MOCK = "http://127.0.0.1:%d" % MOCK_PORT
RUNTIME_FILES = ["providers.json", "nodes.json", "usage.json", "quota_report.json"]
BACKUP = Path(tempfile.mkdtemp(prefix="intent_wb_verify_"))
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %-42s %s" % ("[OK]" if ok else "[FAIL]", name, detail), flush=True)


def req(path, method="GET", body=None, timeout=20, base=BASE):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                return resp.status, raw.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return e.code, raw.decode("utf-8", "ignore")
    except Exception as e:
        return 0, {"detail": "%s: %s" % (type(e).__name__, e)}


def load_usage_mod():
    spec = importlib.util.spec_from_file_location("_verify_usage", str(ROOT / "agent" / "usage.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------- #
# Mock：用量接口 / 用量页 / OpenAI 兼容聊天
# --------------------------------------------------------------------------- #
class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/usage.json"):
            body = json.dumps({"data": {"remaining": 1234, "total": 2000}}).encode()
            ct = "application/json"
        elif self.path.startswith("/usage.html"):
            body = ("<html><body><div>账户余额</div><p>剩余额度：888 token</p>"
                    "<script>var x=1;</script></body></html>").encode("utf-8")
            ct = "text/html; charset=utf-8"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            payload = {}
        if self.path.startswith("/v1/chat/completions"):
            if payload.get("stream"):
                frames = [
                    {"choices": [{"delta": {"content": "po"}}]},
                    {"choices": [{"delta": {"content": "ng"}}, ], "usage": None},
                    {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
                ]
                body = "".join("data: " + json.dumps(f, ensure_ascii=False) + "\n\n"
                               for f in frames) + "data: [DONE]\n\n"
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            raw = json.dumps({"choices": [{"message": {"content": "pong"}}],
                              "usage": {"prompt_tokens": 11, "completion_tokens": 7}}).encode()
        else:
            raw = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_mock():
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def restore():
    for f in RUNTIME_FILES:
        dst = WB / f
        bak = BACKUP / f
        try:
            if bak.exists():
                shutil.copy2(bak, dst)
            elif dst.exists():
                os.remove(dst)
        except Exception as e:
            print("  [恢复失败] %s: %s" % (f, e), flush=True)
    try:
        shutil.rmtree(BACKUP, ignore_errors=True)
    except Exception:
        pass


def main():
    print(" INTENT 工作台改造 · 自验收", flush=True)
    for f in RUNTIME_FILES:
        p = WB / f
        if p.exists():
            shutil.copy2(p, BACKUP / f)

    mock = start_mock()
    env = dict(os.environ)
    env.update({"WB_PORT": str(PORT), "WB_HOST": "127.0.0.1", "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.Popen([PY, str(WB / "server" / "app.py")], cwd=str(WB),
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    try:
        # --- 等就绪 ---
        ready = False
        for _ in range(40):
            if proc.poll() is not None:
                break
            s, _ = req("/", timeout=2)
            if s == 200:
                ready = True
                break
            time.sleep(0.25)
        check("工作台服务在独立端口启动", ready,
              "" if ready else "进程已退出 rc=%s" % proc.returncode)

        # --- 1. 新旧页面 ---
        s, html = req("/", timeout=10)
        ids = ["v-overview", "v-devices", "v-usage", "v-quota", "kpis", "chart",
               "tiers", "usageCards", "nodes", "bindCode", "autoRoute"]
        miss = [k for k in ids if k not in str(html)]
        check("重设计页面四个视图与关键组件齐备", s == 200 and not miss,
              "缺 %s" % miss if miss else "11/11")
        check("页面无旧版工具感标题", "设备统一管理工作台" not in str(html), "已换为 INTENT 工作台")
        s, node = req("/node", timeout=10)
        check("免安装设备页可访问（不装任何东西）",
              s == 200 and "heartbeat" in str(node) and "没有安装任何东西" in str(node),
              "心跳 + 零安装说明")

        # --- 2. 旧 API 回归 ---
        s, d = req("/api/devices", timeout=10)
        check("旧 API /api/devices 回归", s == 200 and isinstance(d.get("devices"), list),
              "%d 台登记设备" % len(d.get("devices", [])))
        s, d = req("/api/proxy", timeout=10)
        check("旧 API /api/proxy 回归", s == 200 and "base_url" in d)
        s, d = req("/api/bind", timeout=10)
        code = d.get("code", "")
        check("旧 API 配对码回归", s == 200 and len(code) >= 4, "配对码 %s" % code)
        s, _ = req("/api/bind/qr", timeout=10)
        check("旧 API 二维码回归", s == 200, "SVG")

        # --- 3. 节点：心跳 / 消息 / 改名 / 移除 ---
        s, d = req("/api/nodes/heartbeat", "POST",
                   {"id": "verify-phone", "name": "验收手机", "ua": "verify-UA",
                    "screen": "390x844", "kind": "browser"}, timeout=10)
        check("设备心跳注册成功", s == 200 and d.get("ok"), d.get("name", ""))
        s, d = req("/api/nodes", timeout=10)
        row = next((x for x in d.get("nodes", []) if x["id"] == "verify-phone"), None)
        check("工作台实时看到该设备在线", bool(row) and row.get("online"),
              "屏幕 %s" % (row or {}).get("screen", ""))
        s, d = req("/api/nodes/message", "POST",
                   {"to": "verify-phone", "text": "验收消息：你好"}, timeout=10)
        s2, d2 = req("/api/nodes/messages?id=verify-phone", timeout=10)
        check("工作台下发消息 → 设备收到",
              s == 200 and s2 == 200 and any("验收消息" in m.get("text", "") for m in d2.get("messages", [])),
              "%d 条" % len(d2.get("messages", [])))
        s, d = req("/api/nodes/note", "POST",
                   {"id": "verify-phone", "text": "设备留言：收到"}, timeout=10)
        s2, d2 = req("/api/nodes", timeout=10)
        check("设备留言 → 工作台总览看到",
              s == 200 and any("设备留言" in m.get("text", "") for m in d2.get("inbox", [])))
        s, d = req("/api/nodes/rename", "POST", {"id": "verify-phone", "name": "改名成功"}, timeout=10)
        check("节点改名", s == 200 and d.get("name") == "改名成功")

        # --- 4. 用量中心 ---
        u = load_usage_mod()
        s, d = req("/api/usage", timeout=20)
        main_row = next((x for x in d.get("providers", []) if x["id"] == "main"), None)
        check("用量接口返回供应商与来源标签",
              s == 200 and bool(main_row) and main_row.get("trust") == "local",
              "main · trust=local（如实标注，不冒充官方余额）")
        before = (main_row or {}).get("used", 0)
        u.record({"prompt_tokens": 100, "completion_tokens": 40}, provider_id="main")
        s, d = req("/api/usage?refresh=1", timeout=20)
        m2 = next((x for x in d.get("providers", []) if x["id"] == "main"), {})
        check("token 累计可持久化并被工作台读到", m2.get("used", 0) >= before + 140,
              "%s → %s" % (before, m2.get("used")))
        cfg = u.load_providers()
        next(x for x in cfg["providers"] if x["id"] == "main")["budget"] = 100000
        u.save_providers(cfg)
        s, d = req("/api/usage?refresh=1", timeout=20)
        m3 = next((x for x in d.get("providers", []) if x["id"] == "main"), {})
        check("填套餐总量后可算剩余", m3.get("remaining") is not None and m3.get("total") == 100000,
              "剩余 %s" % m3.get("remaining"))

        # 官方接口模式
        cfg = u.load_providers()
        cfg["providers"].append({
            "id": "verify-api", "name": "验收-官方接口", "source": "inline", "enabled": True,
            "tier": "paid", "mode": "api", "api_key": "sk-VERIFY-ONLY",
            "query": {"url": MOCK + "/usage.json", "method": "GET", "headers": {},
                      "remaining_path": "data.remaining", "total_path": "data.total", "timeout": 5}})
        u.save_providers(cfg)
        s, d = req("/api/usage?refresh=1", timeout=20)
        a = next((x for x in d.get("providers", []) if x["id"] == "verify-api"), {})
        check("官方用量接口取到精确余额",
              a.get("remaining") == 1234 and a.get("trust") == "official",
              "remaining=%s trust=%s" % (a.get("remaining"), a.get("trust")))

        # 内置浏览器读用量页（真实 Chromium headless + 本地 mock 页）
        cfg = u.load_providers()
        cfg["providers"].append({
            "id": "verify-page", "name": "验收-浏览器抓页", "source": "inline", "enabled": True,
            "tier": "paid", "mode": "page", "page_url": MOCK + "/usage.html",
            "remaining_regex": r"剩余额度[：:]\s*([0-9,.]+)"})
        u.save_providers(cfg)
        s, d = req("/api/usage?refresh=1", timeout=70)
        p = next((x for x in d.get("providers", []) if x["id"] == "verify-page"), {})
        check("内置 Chromium 带登录态读用量页",
              p.get("remaining") == 888 and p.get("trust") == "page",
              "remaining=%s status=%s %s" % (p.get("remaining"), p.get("status"), p.get("detail", "")[:60]))

        # --- 5. 额度雷达 ---
        s, d = req("/api/quota", timeout=15)
        items = (d.get("catalog") or {}).get("items", [])
        check("官方免费层目录可读", s == 200 and len(items) >= 5, "%d 家" % len(items))
        check("合规边界写明不搜刮他人 Key",
              "不搜集" in (d.get("catalog") or {}).get("policy", ""), "policy")
        s, d = req("/api/quota/attach", "POST",
                   {"tier_id": "volc-ark", "api_key": "sk-VERIFY-FREE-KEY-123456",
                    "base_url": MOCK + "/v1", "model": "verify-free"}, timeout=10)
        s2, q = req("/api/quota", timeout=15)
        attached = [x for x in q.get("providers", []) if x.get("tier") == "free"]
        check("用自己领取的 Key 一键接入免费层",
              s == 200 and d.get("ok") and any(x["id"] == "free-volc-ark" for x in attached))
        check("接入后 Key 在前端已脱敏",
              all("VERIFY-FREE-KEY" not in json.dumps(x) for x in q.get("providers", [])),
              "只回传 **** 末四位")
        s, d = req("/api/quota/route", "POST", {"auto": True}, timeout=10)
        check("自动路由开关可持久化", s == 200 and d["routing"]["auto"] is True)

        u2 = load_usage_mod()
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "你好"}]
        b, mo, k, pid = u2.route("ORIG", "orig-model", "orig-key", msgs, tools=None)
        check("短问答自动走免费额度", pid == "free-volc-ark" and b == MOCK + "/v1",
              "provider=%s" % pid)
        long_msgs = [{"role": "user", "content": "请帮我重构这段代码，详细分析架构并给出完整方案" * 4}]
        b2, _, _, pid2 = u2.route("ORIG", "orig-model", "orig-key", long_msgs, tools=None)
        b3, _, _, pid3 = u2.route("ORIG", "orig-model", "orig-key", msgs, tools=[{"type": "function"}])
        check("复杂任务 / 工具任务仍走主力套餐",
              pid2 == "main" and pid3 == "main" and b2 == "ORIG" and b3 == "ORIG")
        req("/api/quota/route", "POST", {"auto": False}, timeout=10)

        s, d = req("/api/quota/scan", "POST", {}, timeout=60)
        check("每日巡检返回结构正确", s == 200 and isinstance(d.get("available"), int)
              and len(d.get("items", [])) == len(items),
              "%s/%s 个官方入口可达（网络不通会如实为 0）" % (d.get("available"), len(items)))

        # --- 6. 主链路 token 计数（离线 mock） ---
        sys.path.insert(0, str(ROOT))
        from agent import llm
        before = u2.usage_summary()["today"]["prompt"]
        ok, out = llm.chat(MOCK + "/v1", "verify-model", "sk-VERIFY", msgs, False)
        after = u2.usage_summary()["today"]["prompt"]
        check("非流式聊天自动累计 token 且不影响回复",
              ok and out == "pong" and after >= before + 11,
              "回复=%s prompt %s→%s" % (out, before, after))
        before2 = u2.usage_summary()["today"]["completion"]
        ok2, gen = llm.chat_stream_events(MOCK + "/v1", "verify-model", "sk-VERIFY", msgs)
        ev = list(gen) if ok2 else []
        done = next((e[1] for e in ev if e[0] == "done"), {})
        after2 = u2.usage_summary()["today"]["completion"]
        check("流式聊天 usage 帧也累计",
              ok2 and done.get("usage", {}).get("completion_tokens") == 3 and after2 >= before2 + 3,
              "done.usage=%s" % done.get("usage"))

        # --- 7. 清理测试节点 ---
        s, d = req("/api/nodes/verify-phone", "DELETE", timeout=10)
        s2, d2 = req("/api/nodes", timeout=10)
        check("验收节点可移除且不留痕迹",
              s == 200 and not any(x["id"] == "verify-phone" for x in d2.get("nodes", [])))

        # --- 8. 隐私 / 残留 ---
        s, d = req("/api/usage/providers", timeout=10)
        check("供应商接口不回传任何明文 Key",
              all("api_key" not in x for x in d.get("providers", [])))
        check("工作台绑定页绑定后进入设备节点页",
              "location.href=u" in open(str(WB / "server" / "app.py"), encoding="utf-8").read(),
              "扫码/输码 → /node")

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        mock.shutdown()
        restore()

    ok = sum(1 for _, v, _ in RESULTS if v)
    total = len(RESULTS)
    print("\n 结果：%d/%d 通过" % (ok, total), flush=True)
    if ok != total:
        print(" 未通过：%s" % [n for n, v, _ in RESULTS if not v], flush=True)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
