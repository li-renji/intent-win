# -*- coding: utf-8 -*-
"""实机验收：真实执行 agent.app.main() 启动流程，确认桌宠不被自动拉起。

做法：在独立子进程里 import 真实 main()，只把 webview.start（弹窗阻塞）
替换成短等待，其余代码路径全部真实执行（含配置加载、端口服务、插件发现）。
用 spy 记录 CompanionManager.start_all 是否被调用。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("  %s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                        ("  -> " + str(detail)) if detail else ""))


from agent.config import load_config  # noqa: E402
cfg = load_config()
check("配置 companion_autostart=False", cfg.get("companion_autostart") is False)

src = open(os.path.join(ROOT, "agent", "app.py"), encoding="utf-8").read()
check("start_all() 仅在开关为 True 的分支内",
      "if cfg.get(\"companion_autostart\", False):" in src
      and src.index("if cfg.get(\"companion_autostart\", False):")
      < src.index("start_all()"))

ui = open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8").read()
check("界面『唤醒 / 让它休息』手动入口完好",
      "companion_start" in ui and "companion_stop" in ui
      and "唤醒" in ui and "让它休息" in ui)
check("手动启动接口仍在", "def companion_start" in src and "def companion_stop" in src)

# ---- 真实执行 main()，spy start_all ----
PROBE = r'''
import json, os, sys, time
sys.path.insert(0, r"%s")
os.chdir(r"%s")
import webview
import agent.app as A
from agent.companions import CompanionManager
calls = []
CompanionManager.start_all = lambda self: calls.append(1) or []
CompanionManager.stop_all = lambda self: None   # 不误杀用户已在跑的桌宠
webview.start = lambda *a, **kw: time.sleep(4) # 给自动拉起线程留触发时间
err = None
try:
    A.main()
except Exception as e:
    err = repr(e)
print("PROBE_RESULT " + json.dumps({"start_all_calls": len(calls), "error": err}))
''' % (ROOT, ROOT)

r = subprocess.run([sys.executable, "-u", "-c", PROBE], cwd=ROOT,
                   capture_output=True, text=True, timeout=90)
line = [l for l in (r.stdout or "").splitlines() if l.startswith("PROBE_RESULT")]
if not line:
    check("main() 真实执行完成", False,
          (r.stdout or "")[-300:] + " / " + (r.stderr or "")[-300:])
    data = {"start_all_calls": -1, "error": "no result"}
else:
    data = json.loads(line[0].split(" ", 1)[1])
    check("main() 真实执行无异常", data["error"] is None, data["error"])
    check("桌面端启动时未自动拉起桌宠（start_all 调用 0 次）",
          data["start_all_calls"] == 0, data)

# ---- 反证：开关打开时确实会拉起（证明不是把功能删了）----
PROBE_ON = PROBE.replace(
    "from agent.companions import CompanionManager",
    "from agent.companions import CompanionManager\n"
    "import agent.config as _c\n"
    "_orig = _c.load_config\n"
    "def _patched(*a, **k):\n"
    "    d = _orig(*a, **k); d['companion_autostart'] = True; return d\n"
    "_c.load_config = _patched\n"
    "A.load_config = _patched")
r2 = subprocess.run([sys.executable, "-u", "-c", PROBE_ON], cwd=ROOT,
                    capture_output=True, text=True, timeout=90)
line2 = [l for l in (r2.stdout or "").splitlines() if l.startswith("PROBE_RESULT")]
if line2:
    d2 = json.loads(line2[0].split(" ", 1)[1])
    check("开关设为 true 时确实会拉起桌宠（功能未丢失）",
          d2["start_all_calls"] == 1, d2)
else:
    check("开关设为 true 时确实会拉起桌宠（功能未丢失）", False,
          (r2.stdout or "")[-300:] + (r2.stderr or "")[-300:])

# ---- 无残留 ----
import socket  # noqa: E402
s = socket.socket()
s.settimeout(0.5)
check("测试后 6800 端口已释放", s.connect_ex(("127.0.0.1", 6800)) != 0)
s.close()
ps = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
     "Where-Object { $_.CommandLine -like '*runner.py*' "
     "-and $_.CommandLine -like '*ajue*' }).Count"],
    capture_output=True, text=True, timeout=40)
n = (ps.stdout or "0").strip().splitlines()[-1].strip() if ps.stdout.strip() else "0"
check("无桌宠进程被误拉起", n == "0", "count=%s" % n)

print("\n===== %d/%d PASS =====" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
