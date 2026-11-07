# -*- coding: utf-8 -*-
"""U 盘便携版自验收：模拟「换一台电脑 + 换一个盘符」。

不用真 U 盘，也能验出换机最容易出问题的几件事：
  subst 出一个新盘符 -> 从新盘符启动 -> 引擎能应答 ->
  写记忆/会话 -> 重启引擎 -> 记忆还在 -> 数据没漏到宿主电脑 -> 本体没被改坏。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = os.path.join(SRC, "portable", "INTENT-Portable")
CREATE_NO_WINDOW = 0x08000000
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %-26s %s" % ("[通过]" if ok else "[未过]", name, detail))
    return bool(ok)


def _run(cmd, env=None, cwd=None, timeout=60):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def _free_drive():
    for d in "ZYXWVUTSRQ":
        if not os.path.exists(d + ":\\"):
            return d
    return ""


def _status(port, timeout=0.8):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.4)
        try:
            s.settimeout(timeout)
            s.sendall(b"STATUS\n")
            return s.recv(512).decode("utf-8", "replace")
        finally:
            s.close()
    except OSError:
        return ""


def _wait_engine(port, seconds=40):
    end = time.time() + seconds
    while time.time() < end:
        r = _status(port)
        if r.startswith("STATUS|"):
            return r
        time.sleep(0.3)
    return ""


def _start_engine(root, env):
    py = os.path.join(root, "runtime", "python", "python.exe")
    log_path = os.path.join(root, "logs", "portable_verify.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "w", encoding="utf-8")
    p = subprocess.Popen([py, "-u", "-m", "agent.main", "--no-gui"],
                         cwd=root, env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT,
                         creationflags=CREATE_NO_WINDOW)
    return p, log, log_path


def _stop_engine(p, log):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=6)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=4)
    try:
        log.close()
    except Exception:
        pass


WRITE_CODE = r"""
import json, os, sys
from agent.memory import MemoryStore
root = sys.argv[1]
m = MemoryStore(root)
m.add('[便携验收] 换机后长期记忆仍在', kind='fact', tags=['portable-check'])
p = os.path.join(root, '\u4f1a\u8bdd', 'portablecheck.json')
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, 'w', encoding='utf-8') as f:
    json.dump({'title': '便携验收', 'messages': [{'role': 'user', 'content': '换机测试'}],
               'facts': ['用户:换机测试'], 'updated': 1}, f, ensure_ascii=False)
hits = [x for x in m.list() if '便携验收' in str(x.get('text', ''))]
print('WROTE', len(hits))
"""

READ_CODE = r"""
import json, os, sys
from agent.memory import MemoryStore
root = sys.argv[1]
m = MemoryStore(root)
hits = [x for x in m.list() if '便携验收' in str(x.get('text', ''))]
p = os.path.join(root, '\u4f1a\u8bdd', 'portablecheck.json')
title = ''
if os.path.isfile(p):
    with open(p, encoding='utf-8') as f:
        title = json.load(f).get('title', '')
print('READ', len(hits), title)
"""


def main():
    print("=" * 66)
    print(" INTENT 便携版 · 换机自验收")
    print("=" * 66)
    if not os.path.isdir(IMAGE):
        print("[未过] 镜像不存在，请先运行 build.py")
        return 2

    # 始终用最新的便携层代码验收（构建产物可能是上一版）
    for f in ("boot.py", "doctor.py"):
        shutil.copy2(os.path.join(SRC, "portable", f),
                     os.path.join(IMAGE, "portable", f))

    drive = _free_drive()
    if not drive:
        print("[未过] 找不到空闲盘符做 subst 映射")
        return 2
    mapped = False
    root = drive + ":\\"
    proc = None
    log = None
    mem_path = os.path.join(IMAGE, "memories", "memory.json")
    mem_before = open(mem_path, "rb").read() if os.path.isfile(mem_path) else None
    sess_test = os.path.join(IMAGE, "会话", "portablecheck.json")
    host_dir = os.path.join(os.environ.get("APPDATA", "."), "INTENT-win")
    host_before = set(os.listdir(host_dir)) if os.path.isdir(host_dir) else set()
    try:
        r = _run(["subst", drive + ":", IMAGE])
        if r.returncode != 0 or not os.path.isdir(root):
            r = _run(["cmd", "/c", "subst", drive + ":", IMAGE], timeout=20)
            print("  subst(cmd):", (r.stdout + r.stderr).strip()[:200])
        if not os.path.isdir(root):
            print("[未过] subst 映射失败，无法模拟换盘符")
            return 2
        mapped = True
        print("  模拟换机：%s -> %s\n" % (root, IMAGE))

        # --- 1. 便携层自身：只认镜像根、体检如实、路径不硬编码 ---
        sys.path.insert(0, os.path.join(root, "portable"))
        import boot
        import doctor
        check("镜像路径自动识别", os.path.normcase(boot.ROOT) == os.path.normcase(root),
              boot.ROOT)
        env, ports = boot.prepare()
        items = doctor.collect(boot.ROOT, env, ports)
        v = doctor.verdict(items)
        check("换机体检可运行", v["runnable"],
              "OK=%d 注意=%d 不行=%d" % (v["ok"], v["warn"], v["fail"]))

        # --- 2. 所有会写宿主电脑的位置都重定向到盘内 ---
        pe = _run([os.path.join(root, "runtime", "python", "python.exe"),
                   os.path.join(root, "portable", "boot.py"), "--print-env"],
                  cwd=root, env=env, timeout=30)
        try:
            raw = pe.stdout
            info = json.loads(raw[raw.find("{"):]) if "{" in raw else {}
        except Exception:
            info = {}
            print("   print-env 输出:", pe.stdout[-300:], pe.stderr[-300:])
        inside = [k for k in ("root", "home_actual", "temp_actual", "python_actual",
                              "userprofile", "appdata", "localappdata", "temp")
                  if info.get(k, "").lower().startswith(drive.lower() + ":")]
        check("用户目录/缓存/解释器全部在盘内", len(inside) == 8,
              "%d/8 项落在 %s:" % (len(inside), drive))
        check("自带 Python 生效",
              info.get("python_actual", "").lower().startswith(drive.lower() + ":"),
              info.get("python_actual", ""))

        # --- 3. 配置 project_root 跟着盘符走 ---
        pc = _run([os.path.join(root, "runtime", "python", "python.exe"), "-c",
                   "from agent.config import load_config;print(load_config()['project_root'])"],
                  cwd=root, env=env, timeout=30)
        proot = pc.stdout.strip().splitlines()[-1] if pc.stdout.strip() else ""
        check("配置根目录随盘符变化",
              os.path.normcase(proot) == os.path.normcase(root), proot)

        # --- 4. 本地模型如实报告（不把空目录写成「已内置」）---
        mdir = os.path.join(root, "models")
        gguf = [f for f in os.listdir(mdir) if f.lower().endswith(".gguf")] \
            if os.path.isdir(mdir) else []
        lm = [i for i in items if i["item"] == "本地模型"]
        honest = (lm and ((not gguf and lm[0]["level"] == "WARN")
                          or (gguf and lm[0]["level"] == "OK")))
        check("本地模型状态如实", bool(honest),
              lm[0]["detail"] if lm else "体检未报告")

        # --- 5. 引擎真能起来并应答（不依赖宿主 Python/浏览器）---
        py = os.path.join(root, "runtime", "python", "python.exe")
        proc, log, log_path = _start_engine(root, env)
        st = _wait_engine(ports["port_6800"])
        check("新盘符启动引擎并应答", st.startswith("STATUS|"), st[:70])
        if not st.startswith("STATUS|"):
            try:
                print("  引擎日志尾部:", open(log_path, encoding="utf-8",
                                          errors="replace").read()[-800:])
            except OSError:
                pass

        # --- 6. 记忆 / 会话写入，重启引擎后仍在 ---
        w = _run([py, "-c", WRITE_CODE, root], cwd=root, env=env, timeout=60)
        check("长期记忆与会话写入盘内", "WROTE 1" in w.stdout,
              (w.stdout + w.stderr).strip()[:120])
        _stop_engine(proc, log)
        proc, log = None, None
        proc, log, log_path = _start_engine(root, env)
        st2 = _wait_engine(ports["port_6800"])
        rd = _run([py, "-c", READ_CODE, root], cwd=root, env=env, timeout=60)
        check("重启后记忆/上下文不丢",
              "READ 1 便携验收" in rd.stdout and st2.startswith("STATUS|"),
              (rd.stdout + rd.stderr).strip()[:120])
        _stop_engine(proc, log)
        proc, log = None, None

        # --- 7. 一键启动全链路：START.cmd 的等价调用，由 chat.py 自己拉起/关闭引擎 ---
        one = _run([py, os.path.join(root, "portable", "boot.py"), "--status"],
                   cwd=root, env=env, timeout=150)
        ok = one.returncode == 0 and "STATUS|" in one.stdout
        status_line = next((l for l in one.stdout.splitlines() if l.startswith("STATUS|")), "")
        check("START.cmd 一键启动全链路", ok,
              status_line[:90] if ok else (one.stdout + one.stderr).strip()[-120:])

        # --- 8. 桌面窗口所需依赖确实随盘（不借系统 Edge / WorkBuddy Python）---
        g = _run([py, "-c",
                  "import json,webview,clr;from mybrowser.runtime_paths import status;"
                  "print(json.dumps(status()))"], cwd=root, env=env, timeout=60)
        try:
            gstat = json.loads(g.stdout.strip().splitlines()[-1])
        except Exception:
            gstat = {}
        gui_ok = (g.returncode == 0 and gstat.get("python_owned")
                  and gstat.get("browser_owned") and gstat.get("webview2_owned"))
        check("桌面窗口依赖随盘齐备", bool(gui_ok),
              "Python/Chromium/WebView2 均为本体内置" if gui_ok
              else (g.stdout + g.stderr).strip()[-120:])

        # --- 9. SAFE-START 的 Key 抹除 / 恢复逻辑 ---
        cfgp = os.path.join(root, "配置.qdai")
        try:
            with open(cfgp, encoding="utf-8") as f:
                orig_key = next((l.strip().split("=", 1)[1] for l in f
                                 if l.strip().startswith("api_key=")), "")
        except OSError:
            orig_key = ""
        boot.set_api_key("")
        try:
            with open(cfgp, encoding="utf-8") as f:
                cleared = all(not l.strip().split("=", 1)[1] for l in f
                              if l.strip().startswith("api_key="))
        except OSError:
            cleared = False
        boot.set_api_key(orig_key)
        try:
            with open(cfgp, encoding="utf-8") as f:
                restored = ("api_key=" + orig_key) in f.read()
        except OSError:
            restored = False
        check("SAFE 启动的 Key 抹除/恢复", cleared and restored,
              "空 Key 已抹除，退出可恢复原值" if (cleared and restored) else "配置写回异常")

        # --- 10. 体检能回答「这台电脑能不能用云端模型」---
        net_items = doctor.collect(boot.ROOT, env, ports, network=True)
        net = [i for i in net_items if i["item"] == "云端连通"]
        check("体检能判断云端可用性",
              bool(net) and doctor.verdict(net_items)["runnable"],
              net[0]["detail"] if net else "未报告")

        # --- 11. 没有把数据漏到宿主电脑 ---
        host_after = set(os.listdir(host_dir)) if os.path.isdir(host_dir) else set()
        check("宿主电脑无新增 INTENT 数据", host_after == host_before,
              "新增 %d 个文件" % len(host_after - host_before))

        # --- 8. 没有把桌面版本体改坏 ---
        try:
            with open(os.path.join(SRC, "配置.qdai"), encoding="utf-8") as f:
                base_cfg = f.read()
        except OSError:
            base_cfg = ""
        check("桌面版本体端口未被改", "port_6800=6800" in base_cfg
              and "port_6801=6801" in base_cfg, "仍为 6800/6801")

    finally:
        if proc is not None:
            _stop_engine(proc, log)
        # 清理验收痕迹（只动本次自己创建的文件）
        try:
            if os.path.isfile(sess_test):
                os.remove(sess_test)
        except OSError:
            pass
        try:
            if mem_before is None:
                if os.path.isfile(mem_path):
                    os.remove(mem_path)
            else:
                with open(mem_path, "wb") as f:
                    f.write(mem_before)
        except OSError:
            pass
        try:
            lp = os.path.join(IMAGE, "logs", "portable_verify.log")
            if os.path.isfile(lp):
                os.remove(lp)
        except OSError:
            pass
        if mapped:
            _run(["subst", drive + ":", "/D"], timeout=20)

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print("-" * 66)
    print(" 自验收：%d/%d 通过" % (passed, len(RESULTS)))
    for name, ok, detail in RESULTS:
        if not ok:
            print("   未过 -> %s (%s)" % (name, detail))
    print("=" * 66)
    return 0 if passed == len(RESULTS) and len(RESULTS) >= 10 else 1


if __name__ == "__main__":
    sys.exit(main())
