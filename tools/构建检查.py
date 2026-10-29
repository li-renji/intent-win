# -*- coding: utf-8 -*-
"""一键构建检查 / 打包（clone 下来第一件事就是跑它）。

    python 构建检查.py              # 环境体检 + 配置体检 + 跑全部测试
    python 构建检查.py --zip        # 额外打出一个可分发的 zip（不含运行时产物）

退出码：0=全部通过；1=有阻塞项（会明确指出是哪一项）。

为什么存在：clone 下来直接双击 start.bat 常常因为「依赖没装 / 配置没填」
而白屏，排查成本高。这个脚本把失败原因一次性列清楚。
"""
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED_PY = (3, 9)

# 需要跑的测试脚本（与仓库根目录同名）
TESTS = [
    "test_intent.py", "test_agent_loop.py", "test_e2e_ports.py",
    "test_new_tools.py", "test_pc_bridge.py", "test_intent_e2e.py",
    "test_local_engine.py", "test_local_api_e2e.py", "test_app_path.py",
    "test_econ.py", "test_kb.py", "test_voice.py", "test_model_autosave.py",
    "test_workbench.py",
]

# 运行时产物 / 私密文件：打包与体检都要排除
EXCLUDE_DIRS = {"__pycache__", "shots", "会话", ".git", "build", "dist", "kb"}
EXCLUDE_FILES = {"pc_bridge.token", "gateway.log", "配置.local.qdai"}
EXCLUDE_SUFFIX = (".log", ".pyc", ".tmp")

PLACEHOLDERS = ("<你的API Key>", "<手机IP>", "<备用手机IP>")


def step(title):
    print("\n" + "=" * 58)
    print("  " + title)
    print("=" * 58)


def check_python():
    step("1. Python 版本")
    v = sys.version_info
    ok = (v.major, v.minor) >= REQUIRED_PY
    print("[%s] 当前 Python %d.%d.%d （要求 ≥ %d.%d）"
          % ("PASS" if ok else "FAIL", v.major, v.minor, v.micro,
             REQUIRED_PY[0], REQUIRED_PY[1]))
    return ok


def check_deps():
    step("2. 第三方依赖")
    ok = True
    try:
        import webview  # noqa: F401
        print("[PASS] pywebview 已安装（聊天窗口 + 内置浏览器可用）")
    except ImportError:
        ok = False
        print("[FAIL] 缺 pywebview → 执行：python -m pip install -r requirements.txt")
        print("       未装也能跑：三端口引擎、工具、Agent 循环、本地推理都不依赖它，")
        print("       只是聊天窗口与内置浏览器会降级为『未启用』。")
    return ok


def check_config():
    step("3. 配置体检（配置.qdai）")
    path = os.path.join(HERE, "配置.qdai")
    if not os.path.exists(path):
        print("[FAIL] 找不到 配置.qdai")
        return False
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    todo = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        for ph in PLACEHOLDERS:
            if ph in v:
                todo.append((i, k.strip(), ph))
                break
    if not todo:
        print("[PASS] 未发现待填占位符（若是新 clone，说明你已填好）")
        return True
    print("[WARN] 以下项仍为占位符，需要你按实际情况填写：")
    for i, k, ph in todo:
        print("       第%3d 行  %-22s = %s" % (i, k, ph))
    print("       填好后重跑本脚本即可转为 PASS。")
    return True   # 占位符不算致命错误，仅提示


def check_imports():
    step("4. 核心模块可导入性")
    sys.path.insert(0, HERE)
    mods = ["config", "history", "llm", "loop", "intent", "tools",
            "screen_ctl", "servers", "prompts", "pc_bridge",
            "local_engine", "model_mgr"]
    ok = True
    for m in mods:
        try:
            __import__("agent." + m)
            print("[PASS] agent.%s" % m)
        except Exception as e:
            ok = False
            print("[FAIL] agent.%s → %s" % (m, e))
    return ok


def run_tests():
    step("5. 测试套件")
    ok_all = True
    for t in TESTS:
        p = os.path.join(HERE, "tests", t)
        if not os.path.exists(p):
            print("[SKIP] %s 不存在" % t)
            continue
        try:
            r = subprocess.run([sys.executable, "-u", p], cwd=HERE,
                               capture_output=True, text=True, timeout=300,
                               encoding="utf-8", errors="replace")
            tail = (r.stdout or "").strip().splitlines()
            last = tail[-1] if tail else "(无输出)"
            passed = r.returncode == 0
            print("[%s] %-24s %s" % ("PASS" if passed else "FAIL", t, last))
            if not passed:
                ok_all = False
                for line in (r.stdout or "").splitlines()[-12:]:
                    print("       | " + line)
        except subprocess.TimeoutExpired:
            ok_all = False
            print("[FAIL] %-24s 超时（>300s）" % t)
    return ok_all


def make_zip(out_dir):
    step("6. 打包分发 zip")
    os.makedirs(out_dir, exist_ok=True)
    zp = os.path.join(out_dir, "qd-agent-win-可运行版.zip")
    n = 0
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(HERE):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                if f in EXCLUDE_FILES or f.endswith(EXCLUDE_SUFFIX):
                    continue
                full = os.path.join(root, f)
                if os.path.abspath(full) == os.path.abspath(zp):
                    continue
                z.write(full, os.path.relpath(full, os.path.dirname(HERE)))
                n += 1
    size = os.path.getsize(zp) / 1024
    print("[OK] 已打包 %d 个文件 → %s（%.0f KB）" % (n, zp, size))
    return True


def main():
    argv = sys.argv[1:]
    r1 = check_python()
    r2 = check_deps()
    r3 = check_config()
    r4 = check_imports()
    r5 = run_tests()
    if "--zip" in argv:
        make_zip(os.path.join(HERE, "dist"))

    step("结论")
    blocking = [n for n, ok in [("Python 版本", r1), ("核心模块导入", r4),
                                ("测试套件", r5)] if not ok]
    if blocking:
        print("存在阻塞项：%s" % "、".join(blocking))
        print("修好后重跑：python 构建检查.py")
        return 1
    if not r2:
        print("可运行，但缺 pywebview → 聊天窗口不可用，建议先装：")
        print("  python -m pip install -r requirements.txt")
    else:
        print("全部通过。直接运行：start.bat（或 python -m agent.app）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
