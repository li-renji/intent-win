# -*- coding: utf-8 -*-
"""自包含运行时验收：证明 INTENT 不借 WorkBuddy Python / 系统浏览器也能跑。

模拟手段（全部只读、不改动机器状态）：
  · 把 USERPROFILE / HOME 指向空目录、从 PATH 去掉 .workbuddy、清 PYTHONPATH
    => 复现「这台机器没有 WorkBuddy」的情形
  · 在子进程里屏蔽系统 Edge 路径的 isfile
    => 复现「这台机器没装 Edge」的情形
  · 真实用本体内核跑 CDP 自检、真实创建隐藏 WebView2 窗口
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "runtime", "python", "python.exe")
EMPTY_HOME = os.path.join(ROOT, "runtime", "_empty_home")
os.makedirs(EMPTY_HOME, exist_ok=True)

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          (("  | " + str(extra)[:200]) if extra else ""), flush=True)


def isolated_env():
    env = dict(os.environ)
    env["USERPROFILE"] = EMPTY_HOME
    env["HOME"] = EMPTY_HOME
    for k in ("PYTHONPATH", "PYTHONHOME", "INTENT_PYTHON",
              "INTENT_CHROMIUM_DIR", "INTENT_WEBVIEW2_DIR"):
        env.pop(k, None)
    env["PATH"] = ";".join(
        p for p in env.get("PATH", "").split(";") if ".workbuddy" not in p.lower())
    return env


def run_py(code, timeout=90, env=None):
    r = subprocess.run([PY, "-c", code], cwd=ROOT, env=env or isolated_env(),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def banner(t):
    print("\n" + "=" * 8 + " " + t + " " + "=" * 8, flush=True)


# ---------------------------------------------------------------- 1 Python
banner("1. 脱离 WorkBuddy 的 Python")
check("本体 Python 存在", os.path.isfile(PY), PY)
rc, out = run_py(
    "import sys;print(sys.prefix);print(sys.version.split()[0]);"
    "import webview,clr,openpyxl,docx,pptx,PyPDF2;print('MODULES_OK')")
check("隔离环境下解释器自洽且关键依赖齐全",
      rc == 0 and "MODULES_OK" in out and ROOT.lower() in out.lower(),
      out.strip().replace("\n", " / ")[:160])

rc, out = run_py(
    "import agent.app as A, webview;print('SETTING=',webview.settings['WEBVIEW2_RUNTIME_PATH'])")
check("主窗口启动时自动挂上本体 WebView2 运行时",
      rc == 0 and os.path.join("runtime", "webview2").lower() in out.lower(),
      out.strip().replace("\n", " / ")[:160])

ps = os.path.join(ROOT, "find_python.ps1")
r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", ps], env=isolated_env(), capture_output=True,
                   text=True, encoding="utf-8", errors="replace", timeout=60)
found = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
check("find_python.ps1 在没有 WorkBuddy 时仍返回本体 Python",
      found.lower() == PY.lower(), found)

# ---------------------------------------------------------------- 2 内核定位
banner("2. 脱离系统浏览器")
rc, out = run_py(
    "from mybrowser.runtime_paths import status\n"
    "s=status()\n"
    "print('PY_OWNED=',s['python_owned'])\n"
    "print('BROWSER=',s['browser'])\n"
    "print('BROWSER_OWNED=',s['browser_owned'])\n"
    "print('WEBVIEW2_OWNED=',s['webview2_owned'])")
check("三项运行时全部来自本体", rc == 0 and
      "PY_OWNED= True" in out and "BROWSER_OWNED= True" in out and
      "WEBVIEW2_OWNED= True" in out,
      out.strip().replace("\n", " / ")[:220])

rc, out = run_py(
    "import os\n"
    "from mybrowser import runtime_paths as rp\n"
    "_real = os.path.isfile\n"
    "def _no_system_edge(p):\n"
    "    s=str(p)\n"
    "    return False if ('Microsoft' in s and 'Edge' in s) else _real(p)\n"
    "rp.os.path.isfile = _no_system_edge\n"
    "print('EXE=', rp.browser_exe())")
expect = os.path.join(ROOT, "runtime", "chromium", "chrome.exe").lower()
check("屏蔽系统 Edge 后仍拿得到本体内核", rc == 0 and expect in out.lower(),
      out.strip()[:160])

# ---------------------------------------------------------------- 3 真实 CDP
banner("3. 本体 Chromium 真实跑一遍（官方 selftest）")
t0 = time.time()
try:
    r = subprocess.run([PY, "-m", "mybrowser.selftest"], cwd=ROOT,
                       env=isolated_env(), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=240)
    st = (r.stdout or "") + (r.stderr or "")
    check("selftest 全通过", r.returncode == 0 and
          "fail" not in st.lower() and "traceback" not in st.lower(),
          "rc=%s %.1fs %s" % (r.returncode, time.time() - t0,
                             st.strip().splitlines()[-1:] or ""))
except subprocess.TimeoutExpired:
    check("selftest 全通过", False, "超时 240s")

# ---------------------------------------------------------------- 4 主窗口
banner("4. 本体 WebView2 真实初始化隐藏窗口")
GUI_CODE = r"""
import webview
from mybrowser.runtime_paths import webview2_runtime_dir
p = webview2_runtime_dir()
webview.settings['WEBVIEW2_RUNTIME_PATH'] = p
w = webview.create_window('rt-check', html='<title>RT_OK</title><h1>ok</h1>',
                          hidden=True, width=240, height=160)
def boot():
    import time
    time.sleep(1.5)
    try:
        print('DOM_TITLE=', w.evaluate_js('document.title'), flush=True)
    except Exception as e:
        print('DOM_TITLE=ERR:' + str(e), flush=True)
    try:
        w.destroy()
    except Exception:
        pass
webview.start(boot, gui='edgechromium')
print('WEBVIEW2_START_OK', flush=True)
"""
try:
    rc, out = run_py(GUI_CODE, timeout=70)
    check("隐藏主窗口用本体运行时渲染成功",
          rc == 0 and "WEBVIEW2_START_OK" in out and "RT_OK" in out
          and "ERR:" not in out,
          out.strip().replace("\n", " / ")[:220])
except subprocess.TimeoutExpired:
    check("隐藏主窗口用本体运行时渲染成功", False, "超时 70s")

# ---------------------------------------------------------------- 5 全局命令
banner("5. 全局 intent 命令（隔离环境）")
try:
    r = subprocess.run(["cmd", "/c", "intent --runtime"], cwd="C:\\",
                       env=isolated_env(), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90)
    o = (r.stdout or "") + (r.stderr or "")
    check("任意目录敲 intent --runtime 报告完全自主",
          r.returncode == 0 and "完全自主运行" in o,
          o.strip().replace("\n", " / ")[:220])
except subprocess.TimeoutExpired:
    check("任意目录敲 intent --runtime 报告完全自主", False, "超时 90s")

banner("结果")
n = sum(RESULTS)
print("==== %d/%d PASS ====" % (n, len(RESULTS)), flush=True)
sys.exit(0 if n == len(RESULTS) else 1)
