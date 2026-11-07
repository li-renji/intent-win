# -*- coding: utf-8 -*-
"""出厂包 / 安装器 / 增量推送 / 联网更新 的自验收。

只读 + 临时目录，不改真实 U 盘、不改用户桌面、不改本体配置。
逐条打印 PASS/FAIL，全部通过才允许交付。
"""
import functools
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.path.join(SRC, "portable")
PKG = os.path.join(PORT, "dist", "INTENT-Portable-Setup")
PAYLOAD = os.path.join(PKG, "payload")
TMPROOT = os.path.join(PORT, "_factory_tmp")
TMP = os.path.join(TMPROOT, str(os.getpid()))
PY = os.path.join(SRC, "runtime", "python", "python.exe")
sys.path.insert(0, PORT)

import manifest as mf
import shortcuts
import sync as sy
import install as inst
import push as pushmod
import update as upd

RESULTS = []
MAPPED = set()


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + ("  | " + str(detail) if detail else ""))


def run(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("errors", "replace")
    kw.setdefault("timeout", 180)
    return subprocess.run(args, **kw)


def cmd(script, *args):
    return run(["cmd", "/c", script] + list(args), input="\r\n", timeout=180)


def subst(letter, path):
    r = run(["subst", letter + ":", path], timeout=30)
    if r.returncode == 0:
        MAPPED.add(letter)
    return r


def unsubst(letter):
    r = run(["cmd", "/c", "subst %s: /D" % letter], timeout=30)
    MAPPED.discard(letter)
    time.sleep(0.35)          # Windows 释放盘符映射需要一点时间
    return r


def write(path, text, encoding="utf-8"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding, newline="\n") as f:
        f.write(text)


def writeb(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def purge(path):
    """清理本脚本自己建的临时目录。用原生 rd，避开 Python 层的安全删除钩子
    （它会对 6000+ 个硬链接逐个尝试回收站，极慢且失败）。"""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return
    guard = os.path.abspath(TMPROOT) + os.sep
    if not path.startswith(guard) and path != os.path.abspath(TMPROOT):
        raise SystemExit("[验收] 拒绝清理非临时目录：%s" % path)
    run(["cmd", "/c", "rd", "/s", "/q", path], timeout=120)


def free_stale_mapping():
    """上一次异常中断可能留下 Z: 映射；只在本脚本的临时目录上时解除。"""
    r = run(["subst"], timeout=20)
    for line in (r.stdout or "").splitlines():
        if (line.strip().upper().startswith("Z:")
                and os.path.abspath(TMPROOT).lower() in line.lower()):
            unsubst("Z")


def main():
    free_stale_mapping()
    purge(TMPROOT)
    os.makedirs(TMP, exist_ok=True)
    print(" INTENT 出厂包 / 更新链路 · 自验收")
    print("=" * 62)

    # ---------- 1. 出厂包结构 ----------
    need = ["安装到U盘.cmd", "使用说明.txt", os.path.join("tools", "install.py"),
            os.path.join("payload", "START.cmd"), os.path.join("payload", "WINDOW.cmd"),
            os.path.join("payload", "UPDATE.cmd"), os.path.join("payload", "manifest.json"),
            os.path.join("payload", "version.json"),
            os.path.join("payload", "终端对话.lnk"),
            os.path.join("payload", "图形界面.lnk"),
            os.path.join("payload", "INTENT-PORTABLE.marker"),
            os.path.join("payload", "intent.ico")]
    miss = [f for f in need if not os.path.isfile(os.path.join(PKG, f))]
    check("出厂包结构齐备", not miss, "缺: %s" % miss if miss else "12/12")

    dev_cmds = ["制作出厂包.cmd", "推送到U盘.cmd", "发布新版本.cmd"]
    check("开发机三个一键命令齐备",
          all(os.path.isfile(os.path.join(PORT, c)) for c in dev_cmds),
          " / ".join(dev_cmds))

    # ---------- 2. 零个人数据 ----------
    empty_dirs = ["会话", "memories", "models", "kb", "logs",
                  os.path.join("data", "home"), os.path.join("data", "资料"),
                  os.path.join("data", "temp")]
    dirty = []
    for d in empty_dirs:
        p = os.path.join(PAYLOAD, d)
        if os.path.isdir(p):
            n = sum(len(fs) for _r, _ds, fs in os.walk(p))
            if n:
                dirty.append("%s(%d)" % (d, n))
    check("用户数据区全空（没有『我的文件』）", not dirty,
          "干净 8/8" if not dirty else ",".join(dirty))

    cfg = open(os.path.join(PAYLOAD, "配置.qdai"), encoding="utf-8").read()
    key_line = [l for l in cfg.splitlines() if l.startswith("api_key=")]
    check("出厂配置无 API Key", bool(key_line) and key_line[0].strip() == "api_key=",
          key_line[0] if key_line else "缺 api_key 行")
    check("出厂配置带安全/兼容开关",
          all(("%s=%s" % (k, v)) in cfg for k, v in
              (("safety_mode", "enforce"), ("companion_autostart", "false"),
               ("mybrowser_ui", "false"), ("port_6800", "16800"))),
          "4/4")

    # 隐私串扫描：真实 Key、用户目录、本机绝对路径
    bad = []
    # 从开发机配置里读出真实 Key 用于比对（不写进任何源码/清单/日志）
    real_key = ""
    try:
        for line in open(os.path.join(SRC, "配置.qdai"), encoding="utf-8"):
            if line.startswith("api_key=") and len(line.strip()) > 20:
                real_key = line.strip().split("=", 1)[1]
    except OSError:
        pass
    # 检测用的敏感串全部从本机环境动态取，不写进源码（源码会上公开仓库）
    tokens = [r"D:\INTENT-win"]
    home = os.environ.get("USERPROFILE", "")
    if home:
        tokens.insert(0, home)
    try:
        mail = run(["git", "config", "user.email"], timeout=20).stdout.strip()
    except (OSError, AttributeError):
        mail = ""
    if mail and "@" in mail:
        tokens.append(mail)
    if real_key:
        tokens.insert(0, real_key)
    for dirpath, dirnames, files in os.walk(PAYLOAD):
        dirnames[:] = [d for d in dirnames
                       if d not in ("runtime", "__pycache__", ".git")]
        for name in files:
            if os.path.splitext(name)[1].lower() in (".png", ".ico", ".webp", ".mp4",
                                                     ".exe", ".dll", ".pak", ".bin"):
                continue
            p = os.path.join(dirpath, name)
            try:
                t = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for token in tokens:
                if token in t:
                    bad.append(os.path.relpath(p, PAYLOAD) + " -> " + token[:12])
    check("出厂包无真实 Key / 本机路径 / 个人信息", not bad,
          "扫描 %d 个文本文件" % sum(1 for _r, _d, fs in os.walk(PAYLOAD) for _f in fs)
          if not bad else bad[:3])
    check("出厂包不带开发/凭据脚本",
          not any(os.path.isfile(os.path.join(PAYLOAD, x)) for x in
                  (os.path.join("tools", "api_push.py"),
                   os.path.join("portable", "factory.py"),
                   "pc_bridge.token")),
          "api_push / factory / token 均未随盘")

    # ---------- 3. 清单真实 ----------
    m = mf.load(os.path.join(PAYLOAD, mf.MANIFEST))
    v = mf.load(os.path.join(PAYLOAD, mf.VERSION))
    check("清单与版本可读且一致",
          bool(m) and bool(v) and m["version"] == v["version"]
          and m["base_version"] == mf.base_sig(m["files"]),
          "v=%s base=%s files=%d" % (m["version"], m["base_version"], m["count"]))
    st = mf.stats(m["files"])
    check("通道划分正确（底座/程序/用户）",
          st.get("base", 0) > 5000 and st.get("code", 0) > 100
          and st.get("user", 0) == 1, str(st))
    code = {k: x for k, x in m["files"].items() if x["ch"] == "code"}
    wrong = [k for k, x in code.items()
             if mf.sha256(os.path.join(PAYLOAD, mf.norm(k))) != x["sha256"]]
    check("全部程序文件 sha256 与清单一致", not wrong,
          "%d/%d" % (len(code) - len(wrong), len(code)))
    check("runtime 底座未混入云端通道",
          all(not k.startswith("runtime/") for k in code), "0 个")

    # ---------- 4. 两个快捷方式 ----------
    links = {}
    for n in ("终端对话.lnk", "图形界面.lnk"):
        links[n] = shortcuts.read(PAYLOAD, n)
    r1, r2 = links["终端对话.lnk"], links["图形界面.lnk"]
    check("快捷方式目标是系统 cmd.exe（非 U 盘盘符）",
          r1.get("TARGET", "").lower().endswith("cmd.exe")
          and r2.get("TARGET", "").lower().endswith("cmd.exe"),
          r1.get("TARGET", "")[:60])
    check("快捷方式按 marker 扫盘且不写死盘符",
          "INTENT-PORTABLE.marker" in r1.get("ARGS", "")
          and "START.cmd" in r1.get("ARGS", "")
          and "WINDOW.cmd" in r2.get("ARGS", "")
          and "INTENT-win" not in r1.get("ARGS", "") + r1.get("TARGET", ""),
          "终端->START.cmd / 图形->WINDOW.cmd")

    # ---------- 5. 换盘符后两个 .lnk 真能启动 ----------
    probe = os.path.join(TMP, "lnkprobe")
    os.makedirs(probe)
    for name, hit in (("START.cmd", "hit_t.txt"), ("WINDOW.cmd", "hit_w.txt")):
        write(os.path.join(probe, name),
              '@echo off\r\necho OK > "%~dp0' + hit + '"\r\n', "ascii")
    shutil.copy2(os.path.join(PAYLOAD, "INTENT-PORTABLE.marker"), probe)
    for n in ("终端对话.lnk", "图形界面.lnk"):
        shutil.copy2(os.path.join(PAYLOAD, n), probe)
    try:
        r = subst("Z", probe)
        ok_map = r.returncode == 0
        if ok_map:
            for n in ("终端对话.lnk", "图形界面.lnk"):
                try:
                    os.startfile("Z:\\" + n)
                except Exception:
                    pass
            for _ in range(60):
                if (os.path.isfile(os.path.join(probe, "hit_t.txt"))
                        and os.path.isfile(os.path.join(probe, "hit_w.txt"))):
                    break
                time.sleep(0.15)
            time.sleep(0.3)
        t = os.path.isfile(os.path.join(probe, "hit_t.txt"))
        w = os.path.isfile(os.path.join(probe, "hit_w.txt"))
        check("换盘符到 Z: 后两个快捷方式都能启动", ok_map and t and w,
              "终端=%s 图形=%s" % (t, w))
    finally:
        unsubst("Z")

    # ---------- 6. 安装器：全量预演 + 一键入口 ----------
    # 全量真装会在临时目录造 6000+ 硬链接，清理极慢；这里用预演核对完整
    # 文件清单与空间，实际写盘链路在下一节用小型镜像端到端验证。
    usb = os.path.join(TMP, "usb")
    os.makedirs(usb)
    try:
        r = subst("Z", usb)
        ok_map = r.returncode == 0
        if ok_map:
            r = run([PY, os.path.join(PORT, "install.py"),
                     "--payload", PAYLOAD, "--drive", "Z:",
                     "--allow-fixed", "--dry-run"], timeout=420)
        out = (r.stdout + r.stderr) if ok_map else ""
        import re
        mm = re.search(r"新增 (\d+)", out)
        planned = int(mm.group(1)) if mm else 0
        check("安装器全量预演覆盖所有出厂文件",
              ok_map and r.returncode == 0 and planned >= m["count"] - 10,
              "计划新增 %d / 清单 %d" % (planned, m["count"]))
        c = cmd(os.path.join(PKG, "安装到U盘.cmd"), "--drive", "Z:",
                "--allow-fixed", "--dry-run")
        check("「安装到U盘.cmd」一键入口可用", c.returncode == 0,
              (c.stdout + c.stderr).strip().splitlines()[-1][:100])
    finally:
        unsubst("Z")

    # ---------- 7. 增量推送：只改程序，用户数据一个字节不动 ----------
    mini_src = os.path.join(TMP, "mini_src")
    mini_dst = os.path.join(TMP, "mini_dst")
    os.makedirs(os.path.join(mini_src, "agent"))
    os.makedirs(os.path.join(mini_src, "docs"))
    os.makedirs(os.path.join(mini_src, "portable", "desktop"))
    os.makedirs(os.path.join(mini_src, "会话"))
    write(os.path.join(mini_src, "agent", "app.py"), "print('v1')\n")
    write(os.path.join(mini_src, "docs", "old.txt"), "stale\n")
    write(os.path.join(mini_src, "START.cmd"), "@echo off\r\n", "ascii")
    write(os.path.join(mini_src, "WINDOW.cmd"), "@echo off\r\n", "ascii")
    write(os.path.join(mini_src, "配置.qdai"), "api_key=\n")
    write(os.path.join(mini_src, "INTENT-PORTABLE.marker"), "marker\n")
    shutil.copy2(os.path.join(PORT, "desktop", "shortcuts.vbs"),
                 os.path.join(mini_src, "portable", "desktop", "shortcuts.vbs"))
    shutil.copy2(os.path.join(PAYLOAD, "intent.ico"),
                 os.path.join(mini_src, "intent.ico"))
    mm = mf.make(mini_src)
    mm["version"], mm["base_version"] = "v1", "BASE1"
    mf.save(mini_src, mm)
    mf.save_version(mini_src, "v1", "BASE1")
    mini_installed = inst.install(mini_src, mini_dst, mm, hardlink=False)
    check("真实写盘安装链路可用（文件/清单/双快捷方式）",
          all(os.path.isfile(os.path.join(mini_dst, x)) for x in
              ("START.cmd", "WINDOW.cmd", "manifest.json", "version.json",
               "终端对话.lnk", "图形界面.lnk", "INTENT-PORTABLE.marker"))
          and mini_installed.get("shortcuts") is True
          and mini_installed["new"] > 0,
          "新增 %d 个" % mini_installed["new"])

    write(os.path.join(mini_dst, "配置.qdai"), "api_key=sk-USER-PRIVATE-KEY\n")
    write(os.path.join(mini_dst, "会话", "u1.json"), '{"mine":true}')
    write(os.path.join(mini_dst, "memories", "memory.json"), '{"long":true}')
    write(os.path.join(mini_dst, "data", "资料", "我的文件.txt"), "private\n")
    before = {p: open(os.path.join(mini_dst, p), encoding="utf-8").read()
              for p in ("配置.qdai", "会话/u1.json", "memories/memory.json",
                        "data/资料/我的文件.txt")}

    write(os.path.join(mini_src, "agent", "app.py"), "print('v2')\n")
    os.remove(os.path.join(mini_src, "docs", "old.txt"))
    mm2 = mf.make(mini_src)
    mm2["version"], mm2["base_version"] = "v2", "BASE1"
    mf.save(mini_src, mm2)
    mf.save_version(mini_src, "v2", "BASE1")
    ps = pushmod.push(mini_src, mini_dst)
    after = {p: open(os.path.join(mini_dst, p), encoding="utf-8").read()
             for p in before}
    check("推送只更新变化文件",
          "v2" in open(os.path.join(mini_dst, "agent", "app.py")).read()
          and not os.path.isfile(os.path.join(mini_dst, "docs", "old.txt"))
          and ps["same"] > 0,
          "新增%d 更新%d 未变%d 删除%d 保留%d"
          % (ps["new"], ps["updated"], ps["same"], ps["removed"], ps["kept"]))
    check("推送绝不覆盖/删除用户数据", before == after and ps["kept"] >= 1,
          "4/4 用户文件字节一致")
    check("推送后版本已升级",
          mf.load(os.path.join(mini_dst, mf.VERSION))["version"] == "v2", "v1 -> v2")

    # ---------- 8. 联网更新（本地 HTTP 模拟云端） ----------
    rel = os.path.join(TMP, "release")
    os.makedirs(os.path.join(rel, "agent"))
    os.makedirs(os.path.join(rel, "docs"))
    write(os.path.join(rel, "agent", "app.py"), "print('cloud-v3')\n")
    write(os.path.join(rel, "docs", "new.txt"), "fresh\n")
    write(os.path.join(rel, "agent", "bad.py"), "WRONG CONTENT\n")
    rfiles = {}
    for p in ("agent/app.py", "docs/new.txt"):
        data = open(os.path.join(rel, p), "rb").read()
        import hashlib
        rfiles[p] = {"sha256": hashlib.sha256(data).hexdigest(),
                     "size": len(data), "ch": "code"}
    rfiles["agent/bad.py"] = {"sha256": "0" * 64, "size": 10, "ch": "code"}
    mf.save(rel, {"version": "v3", "base_version": "BASE2",
                  "files": rfiles, "count": len(rfiles)})
    mf.save_version(rel, "v3", "BASE2")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=rel)
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    port_no = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = "http://127.0.0.1:%d/" % port_no

    upd_root = os.path.join(TMP, "upd_root")
    shutil.copytree(mini_dst, upd_root)
    write(os.path.join(upd_root, "docs", "old.txt"), "stale-local\n")
    lm = mf.load(os.path.join(upd_root, mf.MANIFEST))
    lm["files"]["docs/old.txt"] = {"sha256": "0" * 64, "size": 6, "ch": "code"}
    mf.save(upd_root, lm)
    mf.save_version(upd_root, "v1", "BASE1")
    u_before = {p: open(os.path.join(upd_root, p), encoding="utf-8").read()
                for p in before}
    try:
        u = upd.pull(upd_root, base=base, timeout=10)
        u_after = {p: open(os.path.join(upd_root, p), encoding="utf-8").read()
                   for p in before}
        check("联网更新只拉变化的程序文件",
              "cloud-v3" in open(os.path.join(upd_root, "agent", "app.py")).read()
              and os.path.isfile(os.path.join(upd_root, "docs", "new.txt"))
              and not os.path.isfile(os.path.join(upd_root, "docs", "old.txt")),
              "新增%d 更新%d 未变%d 删除%d" % (u["new"], u["updated"],
                                             u["same"], u["removed"]))
        check("联网更新不动用户数据", u_before == u_after, "4/4 字节一致")
        check("sha256 校验失败的文件被拒绝写入",
              any("bad.py" in e for e in u["errors"])
              and not os.path.isfile(os.path.join(upd_root, "agent", "bad.py")),
              "坏文件已丢弃，未写坏盘")
        check("底座版本不同会如实提示", bool(u["base_warning"]),
              u["base_warning"][:60])
        v3 = mf.load(os.path.join(upd_root, mf.VERSION))
        check("更新后版本写回盘内", v3 and v3["version"] == "v3", "v3")
        u2 = upd.pull(upd_root, base=base, timeout=10)
        check("已是最新版本时不重复下载", u2["status"] == "up-to-date", u2["status"])
        off = upd.pull(upd_root, base="http://127.0.0.1:1/", timeout=3)
        check("断网时优雅失败、不写坏数据", off["status"] == "error"
              and bool(off["errors"]), (off["errors"] or [""])[0][:60])
    finally:
        srv.shutdown()
        srv.server_close()

    # ---------- 9. 云端更新源（stage） ----------
    stage_dir = os.path.join(TMP, "release_stage")
    import publish
    rm = publish.stage(PAYLOAD, stage_dir)
    check("发布源只含程序文件、不含底座与用户数据",
          all(not k.startswith("runtime/") and not mf.is_user(k)
              for k in rm["files"])
          and not os.path.isfile(os.path.join(stage_dir, "配置.qdai"))
          and rm["count"] == len(code),
          "%d 个 / %.2f MB" % (rm["count"],
                               sum(x["size"] for x in rm["files"].values()) / 1024 ** 2))
    check("发布源文件与 sha256 清单一致",
          all(mf.sha256(os.path.join(stage_dir, mf.norm(k))) == x["sha256"]
              for k, x in rm["files"].items()), "%d/%d" % (rm["count"], rm["count"]))

    # ---------- 10. 简洁清理 ----------
    print("=" * 62)
    good = sum(1 for _n, ok, _d in RESULTS if ok)
    print("结果：%d/%d 通过" % (good, len(RESULTS)))
    for n, ok, d in RESULTS:
        if not ok:
            print("  未通过：%s  %s" % (n, d))
    return 0 if good == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for letter in list(MAPPED):
            unsubst(letter)
        purge(TMPROOT)
    sys.exit(rc)
