# -*- coding: utf-8 -*-
"""「一行命令安装器」端到端自验收。

模拟一台新机：用本地源码包 + 复用已有 runtime 的目录链接，
完整跑一遍 install.ps1，验证源码落位、中文文件名、配置生成与保护、
intent --runtime 自检、下载源可达性、推送体检，并且绝不破坏本体。

跑法:  cd D:/INTENT-win && runtime/python/python.exe tests/verify_installer.py
"""
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "runtime", "python", "python.exe")
E2E = os.path.join(ROOT, "tests", "_e2e_install")
TARGET = os.path.join(E2E, "INTENT-win")
SRCZIP = os.path.join(E2E, "source.zip")
JUNCTIONS = []

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(("[PASS] " if cond else "[FAIL] ") + name +
          (("  -> " + str(extra)) if extra else ""), flush=True)


def run(cmd, env=None, timeout=300, encoding="utf-8"):
    e = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        e.pop(k, None)
    if env:
        e.update(env)
    e["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(cmd, cwd=ROOT, env=e, capture_output=True,
                           timeout=timeout, text=True, encoding=encoding,
                           errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as ex:
        return -1, "TIMEOUT: %s" % ex


def powershell(script, env=None, timeout=300):
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", script], env=env, timeout=timeout, encoding="utf-8")


def mklink(dst, src):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    r = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                       capture_output=True, text=True, encoding="gbk",
                       errors="replace")
    if r.returncode == 0:
        JUNCTIONS.append(dst)
    return r.returncode == 0


def unlink_junctions():
    for j in JUNCTIONS:
        try:
            subprocess.run(["cmd", "/c", "rmdir", j], capture_output=True,
                           timeout=20)  # removes the link only, never the target
        except Exception:
            pass
    JUNCTIONS.clear()


def banner(t):
    print("\n==== %s ====" % t, flush=True)


def main():
    banner("0. 静态检查")
    ps1 = os.path.join(ROOT, "install.ps1")
    raw = open(ps1, "rb").read()
    check("install.ps1 存在", os.path.isfile(ps1))
    check("纯 ASCII（irm|iex 不乱码）", all(b < 128 for b in raw),
          "size=%d" % len(raw))
    check("无独立 exit 语句（不会关掉用户终端）",
          not re.search(rb"(?m)^\s*exit\b", raw))
    check("含源码/runtime/配置/PATH/自检五段",
          all(x in raw for x in (b"source.zip", b"runtime\\python",
                                 b"example.qdai", b"SetEnvironmentVariable",
                                 b"'--runtime'")))
    code, out = run(["powershell", "-NoProfile", "-Command",
                     "$t=$null;$e=$null;"
                     "[System.Management.Automation.Language.Parser]::ParseFile("
                     "'%s',[ref]$t,[ref]$e) | Out-Null; $e.Count" % ps1.replace("\\", "/")],
                    timeout=90, encoding="gbk")
    check("PowerShell 语法解析 0 错误", code == 0 and out.strip().endswith("0"),
          out.strip()[-80:])

    banner("1. 构造源码包（模拟 GitHub archive）")
    os.makedirs(E2E, exist_ok=True)
    raw_files = subprocess.run(
        ["git", "-C", ROOT, "ls-files", "--cached", "--others",
         "--exclude-standard", "-z"], capture_output=True).stdout
    rels = [p.decode("utf-8", "replace") for p in raw_files.split(b"\0") if p]
    check("将入库文件清单非空", len(rels) > 100, "%d 个" % len(rels))
    with zipfile.ZipFile(SRCZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in rels:
            fp = os.path.join(ROOT, rel)
            if os.path.isfile(fp):
                z.write(fp, "intent-win-main/" + rel)
    with zipfile.ZipFile(SRCZIP) as z:
        names = z.namelist()
    check("源码包含安装器与配置模板",
          any(n.endswith("/install.ps1") for n in names) and
          any(n.endswith("配置.example.qdai") for n in names))
    check("源码包未混入 runtime / 密钥配置",
          not any("/runtime/" in n for n in names) and
          not any(n.endswith("/配置.qdai") for n in names))

    banner("2. 准备模拟新机目录")
    unlink_junctions()
    if os.path.isdir(TARGET):
        shutil.rmtree(TARGET, ignore_errors=True)
    for sub in ("python", "webview2", "chromium"):
        src = os.path.join(ROOT, "runtime", sub)
        dst = os.path.join(TARGET, "runtime", sub)
        ok = mklink(dst, src)
        check("目录链接 %s 建立成功" % sub, ok and os.path.isdir(dst))
    check("复用自有 Python（无需下载 2GB）",
          os.path.isfile(os.path.join(TARGET, "runtime", "python", "python.exe")))

    env = {
        "INTENT_INSTALL_DIR": TARGET,
        "INTENT_SOURCE_ZIP": SRCZIP,
        "INTENT_SKIP_RUNTIME": "1",
        "INTENT_NO_PATH": "1",
    }

    banner("3. 第一次安装（模拟新机从零安装）")
    code, out = powershell(ps1, env=env, timeout=400)
    check("安装器退出码 0", code == 0, out.strip().replace("\n", " / ")[-260:])
    check("输出含安装完成标记", "install complete" in out)
    check("输出含本地源码包提示", "using local source archive" in out)
    key_files = ["chat.py", "intent.cmd", "start.bat", "install.ps1",
                 "requirements.txt", "agent/app.py", "agent/safety.py",
                 "mybrowser/runtime_paths.py", "security/check_before_push.py",
                 "README.md", "配置.example.qdai"]
    missing = [f for f in key_files
               if not os.path.isfile(os.path.join(TARGET, f))]
    check("关键源码文件全部落位", not missing, "缺失=%s" % missing)
    cfg = os.path.join(TARGET, "配置.qdai")
    tmpl = os.path.join(TARGET, "配置.example.qdai")
    check("中文文件名未乱码并生成了本地配置", os.path.isfile(cfg))
    check("配置内容来自脱敏模板",
          os.path.isfile(cfg) and os.path.isfile(tmpl) and
          open(cfg, "r", encoding="utf-8").read() ==
          open(tmpl, "r", encoding="utf-8").read())
    check("未改动用户 PATH（INTENT_NO_PATH=1）", "PATH untouched" in out)
    check("install.cmd 仍为纯 ASCII",
          os.path.isfile(os.path.join(TARGET, "intent.cmd")) and
          all(b < 128 for b in open(os.path.join(TARGET, "intent.cmd"), "rb").read()))

    banner("4. 装后自检 intent --runtime")
    code, out = run([os.path.join(TARGET, "runtime", "python", "python.exe"),
                     os.path.join(TARGET, "chat.py"), "--runtime"], timeout=120)
    check("自检命令执行成功", code == 0, out.strip().replace("\n", " / ")[-180:])
    check("报告为完全自主运行（不借外部）",
          "完全自主" in out or "OK" in out,
          out.strip().replace("\n", " / ")[-180:])

    banner("5. 第二次安装（幂等 + 不覆盖用户配置）")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("SENTINEL=keep-my-local-config\n")
    code, out = powershell(ps1, env=env, timeout=400)
    check("重复安装退出码 0", code == 0, out.strip().replace("\n", " / ")[-200:])
    check("已有本地配置被保留，未被模板覆盖",
          open(cfg, "r", encoding="utf-8").read().strip() ==
          "SENTINEL=keep-my-local-config")
    check("输出提示保留现有配置", "existing local config preserved" in out)
    check("重复安装后源码仍完整",
          os.path.isfile(os.path.join(TARGET, "agent", "app.py")))

    banner("6. 下载源可达性（真实联网探测）")
    code, out = powershell(ps1, env={"INTENT_CHECK_SOURCES": "1"}, timeout=240)
    m = re.search(r"sources:\s*(\d+)/(\d+)", out)
    check("全部下载源可达", bool(m) and m.group(1) == m.group(2),
          (m.group(0) if m else out.strip()[-160:]))
    check("无 FAIL 源", "FAIL" not in out,
          [l for l in out.splitlines() if "FAIL" in l][:3])

    banner("7. 推送前安全体检回归")
    code, out = run([PY, os.path.join(ROOT, "security", "check_before_push.py")],
                    timeout=180)
    check("体检结论为「可以推送」", code == 0 and "0 FAIL" in out,
          out.strip().splitlines()[-1] if out.strip() else "")

    banner("8. 清理并确认本体完好")
    unlink_junctions()
    shutil.rmtree(E2E, ignore_errors=True)
    check("临时目录已清理", not os.path.exists(E2E))
    check("本体 Python 运行时未被误删", os.path.isfile(PY))
    n = sum(len(fs) for _, _, fs in os.walk(os.path.join(ROOT, "runtime", "python")))
    check("本体运行时文件数完好", n > 7000, "%d 个文件" % n)

    passed = sum(RESULTS)
    print("\n==== %d/%d PASS ====" % (passed, len(RESULTS)), flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        unlink_junctions()
