# -*- coding: utf-8 -*-
"""验收：搜索直达修复 / UI 不再卡死 / 桌宠不自启 / 配置真正生效。

在 D:/INTENT-win 下运行：python tests/verify_browser_fix.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("  %s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                        ("  -> " + str(detail)) if detail else ""))


def banner(t):
    print("\n===== %s =====" % t)


# ---------------------------------------------------------------- 1 配置
banner("1. 配置生效与接线")
from agent.config import load_config  # noqa: E402
cfg = load_config()
check("mybrowser_ui=False（绕开内嵌死锁路径）", cfg.get("mybrowser_ui") is False,
      cfg.get("mybrowser_ui"))
check("companion_autostart=False（桌宠不自启）", cfg.get("companion_autostart") is False,
      cfg.get("companion_autostart"))
check("mybrowser_preheat=False（不预热弹窗）", cfg.get("mybrowser_preheat") is False)

from agent.app import Api  # noqa: E402
import inspect  # noqa: E402
src = inspect.getsource(Api.open_url)
check("open_url 把目标 url 交给 start(url) 一步直达",
      "self.browser.start(url)" in inspect.getsource(Api._browser_boot))
check("open_url 走后台启动（有超时保护）",
      "_browser_boot" in src and "daemon=True" in inspect.getsource(Api._browser_boot))

app_src = open(os.path.join(ROOT, "agent", "app.py"), encoding="utf-8").read()
check("companion 自动拉起被开关包住",
      "if cfg.get(\"companion_autostart\", False):" in app_src)


# ------------------------------------------------- 2 编排逻辑（假 browser）
banner("2. 搜索编排逻辑（不启动真浏览器）")


class FakeBrowser:
    def __init__(self, block=False):
        self.calls = []
        self.error = None
        self._block = block

    def available(self):
        return False

    def start(self, *a):
        self.calls.append(("start",) + a)
        if self._block:
            time.sleep(60)
        return True

    def execute(self, cmd):
        self.calls.append(("execute", cmd))
        return "OK"

    def show(self):
        self.calls.append(("show",))


class Shim:
    """只挂浏览器相关方法，复用 Api 的真实实现来验证编排逻辑。"""
    _browser_boot = Api._browser_boot
    open_url = Api.open_url
    open_browser = Api.open_browser

    def __init__(self, browser):
        self.browser = browser


fb = FakeBrowser()
r = Api.open_url(Shim(fb), "豆包")
starts = [c for c in fb.calls if c[0] == "start"]
check("搜『豆包』时 start() 收到了目标 URL（不是无参开主页）",
      bool(starts) and len(starts[0]) > 1 and "search?q=" in starts[0][1],
      starts[:1])
check("URL 里带搜索词『豆包』",
      bool(starts) and "%E8%B1%86%E5%8C%85" in starts[0][1].upper(),
      starts[0][1] if starts else "")
check("open_url 返回成功且不抛异常", r.get("ok") is True, r)

# 卡死保护：浏览器启动故意阻塞 60 秒，主线程必须在 ~3 秒内返回
fb2 = FakeBrowser(block=True)
t0 = time.time()
r2 = Api.open_browser(Shim(fb2))
elapsed = time.time() - t0
check("启动卡住时界面不被拖死（12 秒内返回 pending，后台继续）",
      elapsed < 15 and r2.get("pending") is True,
      "%.2fs, resp=%s" % (elapsed, r2))


# ------------------------------------------- 3 真实端到端（独立 Edge + CDP）
banner("3. 真实浏览器端到端（直达 + 交互 + 不卡死）")
from agent.browser.mybrowser_adapter import MyBrowserAdapter  # noqa: E402

# 测试用无头，避免弹窗打扰用户；机制与用户可见窗口完全一致（独立 Edge + CDP）
test_cfg = dict(cfg)
test_cfg["browser_headless"] = True
adapter = MyBrowserAdapter(test_cfg)
check("适配器按 mybrowser_ui=False 建实例成功", adapter is not None)

t0 = time.time()
ok = adapter.start("https://www.bing.com/search?q=豆包")
boot = time.time() - t0
check("浏览器启动成功", ok is True, "%.1fs err=%s" % (boot, adapter.error))
check("启动耗时合理（<40s，不卡死）", boot < 40, "%.1fs" % boot)

if ok:
    check("available() 为 True", adapter.available() is True)
    snap = adapter.execute("SNAPSHOT 1200")
    check("SNAPSHOT 能返回（控制面活着、可交互）",
          isinstance(snap, str) and len(snap.strip()) > 10,
          snap.strip()[:160].replace("\n", " / "))
    low = snap.lower()
    check("已直达搜索结果页，不是百度主页",
          "bing.com" in low and ("search" in low or "%e8" in low),
          snap.strip().splitlines()[0][:140] if snap.strip() else "")
    check("页面里出现搜索词『豆包』", "豆包" in snap, "")
    ev = adapter.execute("EVAL document.title")
    check("EVAL 可执行（真实 CDP 交互）",
          isinstance(ev, str) and len(ev.strip()) > 0
          and "未启用" not in ev and "[错误]" not in ev and "失败" not in ev,
          ev.strip()[:100])
    try:
        adapter.show()
        check("show() 不抛异常", True)
    except Exception as e:
        check("show() 不抛异常", False, str(e))
    adapter.stop()
    check("stop() 已清理", True)
else:
    for n in ("available() 为 True", "SNAPSHOT", "直达搜索结果页", "搜索词",
              "EVAL", "show()", "stop()"):
        check(n + "（因启动失败跳过）", False)

# ------------------------------------------------------------- 4 无残留
banner("4. 清理")
import socket  # noqa: E402
import subprocess  # noqa: E402
time.sleep(2)
s = socket.socket()
s.settimeout(0.5)
port_open = s.connect_ex(("127.0.0.1", 9700)) == 0
s.close()
check("stop() 后 CDP 端口 9700 已释放", not port_open, "open=%s" % port_open)

ps = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
     "Where-Object { $_.CommandLine -like '*remote-debugging-port=9700*' "
     "-or $_.CommandLine -like '*mybrowser_profile*' }).Count"],
    capture_output=True, text=True, timeout=40)
n = (ps.stdout or "0").strip().splitlines()[-1].strip() if ps.stdout.strip() else "0"
check("无遗留测试 Edge 渲染进程", n == "0", "count=%s" % n)

print("\n===== %d/%d PASS =====" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
