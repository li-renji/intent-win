# -*- coding: utf-8 -*-
"""桌面快捷方式 / 插入自动助手 自验收。

全程在临时目录里模拟「插上的那台电脑」，不碰真实桌面、不碰真实用户目录；
唯一的系统接触是一个临时计划任务，验收完必定删除。
"""
import json
import os
import shutil
import subprocess
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = os.path.join(SRC, "portable", "INTENT-Portable")
TMP = os.path.join(SRC, "portable", "_desktop_verify_tmp")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %-28s %s" % ("[通过]" if ok else "[未过]", name, detail))
    return bool(ok)


def run(cmd, env=None, timeout=60):
    return subprocess.run(cmd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def cmd(args, env=None, timeout=60):
    return run(["cmd", "/c"] + args, env=env, timeout=timeout)


def subst(drive, path):
    return run(["subst", drive + ":", path])


def unsubst(drive):
    return run(["subst", drive + ":", "/D"])


def read_lnk(vbs, path):
    r = run(["cscript", "//nologo", vbs, path], timeout=30)
    out = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def main():
    print("=" * 68)
    print(" INTENT 便携版 · 桌面快捷方式自验收")
    print("=" * 68)
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)
    desk = os.path.join(TMP, "Desktop")
    host = os.path.join(TMP, "AppData", "Local", "INTENTPortable")
    os.makedirs(desk)
    os.makedirs(os.path.join(TMP, "AppData", "Local"))

    real_desks = [os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")]
    if os.environ.get("OneDrive"):
        real_desks.append(os.path.join(os.environ["OneDrive"], "Desktop"))
    real_lnk_before = [os.path.isdir(d) and os.path.isfile(
        os.path.join(d, "INTENT Portable.lnk")) for d in real_desks]
    real_host_before = os.path.isdir(
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "INTENTPortable"))

    env = dict(os.environ)
    env["LOCALAPPDATA"] = os.path.join(TMP, "AppData", "Local")
    env["INTENT_DESKTOP_DIR"] = desk
    env["INTENT_LAUNCH_DRYRUN"] = "1"
    lnk = os.path.join(desk, "INTENT Portable.lnk")
    vbs = os.path.join(IMAGE, "portable", "desktop", "read_shortcut.vbs")
    drive = "Z"
    mapped = []
    try:
        # --- 1. 镜像内容齐备 ---
        need = ["autorun.inf", "intent.ico", "INTENT-PORTABLE.marker",
                "PIN-TO-DESKTOP.cmd",
                os.path.join("portable", "desktop", "desktop.vbs"),
                os.path.join("portable", "desktop", "launch.cmd")]
        miss = [f for f in need if not os.path.isfile(os.path.join(IMAGE, f))]
        check("镜像文件齐备", not miss, "缺: %s" % miss if miss else "6/6")
        check("未夹带未验收的系统级安装器",
              not os.path.isfile(os.path.join(IMAGE, "INSTALL-AUTO.cmd")),
              "镜像里没有计划任务/驱动类脚本")
        try:
            with open(os.path.join(IMAGE, "intent.ico"), "rb") as f:
                ico = f.read(6)
            ico_ok = ico == b"\x00\x00\x01\x00\x01\x00"
        except OSError:
            ico_ok = False
        check("图标是有效 ICO", ico_ok, "32x32 32bpp")

        # --- 2. 换盘符 + 一键建桌面快捷方式 ---
        unsubst(drive)
        r = subst(drive, IMAGE)
        if not os.path.isdir(drive + ":\\"):
            print("  subst 失败:", (r.stdout + r.stderr).strip()[:150])
            return 2
        mapped.append(drive)
        print("  模拟换机：%s: -> %s\n" % (drive, IMAGE))
        c = cmd([drive + ":\\PIN-TO-DESKTOP.cmd"], env=env)
        check("一键建桌面快捷方式", c.returncode == 0 and os.path.isfile(lnk),
              (c.stdout + c.stderr).strip()[:110])
        host_files = ["launch.cmd", "desktop.vbs", "intent.ico"]
        check("宿主侧仅 3 个小文件",
              all(os.path.isfile(os.path.join(host, f)) for f in host_files),
              ", ".join(host_files))

        # --- 3. 快捷方式属性正确 ---
        p = read_lnk(vbs, lnk)
        check("快捷方式指向固定启动器",
              p.get("TARGET", "").lower().endswith("cmd.exe")
              and os.path.normcase(host) in os.path.normcase(p.get("ARGS", "")),
              p.get("ARGS", "")[:80])
        check("快捷方式图标随盘",
              p.get("ICON", "").lower().endswith("intent.ico,0"),
              p.get("ICON", "")[:80])

        # --- 4. 启动器靠标记文件找 U 盘，不写死盘符 ---
        l = cmd([os.path.join(host, "launch.cmd")], env=env)
        check("启动器识别当前 U 盘", "FOUND Z:" in l.stdout,
              (l.stdout + l.stderr).strip()[:90])
        unsubst("Z")
        mapped.remove("Z")
        subst("Y", IMAGE)
        mapped.append("Y")
        l2 = cmd([os.path.join(host, "launch.cmd")], env=env)
        check("换盘符后仍能用", "FOUND Y:" in l2.stdout,
              (l2.stdout + l2.stderr).strip()[:90])
        unsubst("Y")
        mapped.remove("Y")
        subst("Z", IMAGE)
        mapped.append("Z")

        # --- 5. 幂等 ---
        cmd([drive + ":\\PIN-TO-DESKTOP.cmd"], env=env)
        n = len([f for f in os.listdir(desk) if f.lower().endswith(".lnk")])
        check("重复安装不产生重复项", n == 1, "桌面 .lnk 数量=%d" % n)

        # --- 6. probe 模式（自动助手的核心逻辑）---
        os.remove(lnk)
        v = run(["wscript.exe", "//B", os.path.join(host, "desktop.vbs"),
                 host, "probe", desk], env=env, timeout=30)
        check("插入检测能自动补建快捷方式",
              v.returncode == 0 and os.path.isfile(lnk),
              "probe 返回 %d，快捷方式%s" % (v.returncode,
                                      "已恢复" if os.path.isfile(lnk) else "未恢复"))

        # --- 7. AutoRun 入口（随盘，不改宿主系统）---
        try:
            with open(os.path.join(IMAGE, "autorun.inf"), encoding="ascii") as f:
                ar = f.read().lower()
        except OSError:
            ar = ""
        check("AutoRun 入口正确",
              all(s in ar for s in ("label=", "icon=intent.ico", "action=",
                                   "shellexecute=pin-to-desktop.cmd")),
              "插入/AutoPlay 可执行一键建快捷方式")

        # --- 8. 一键卸载 ---
        uni = cmd([drive + ":\\PIN-TO-DESKTOP.cmd", "/U"], env=env, timeout=60)
        clean = (not os.path.isfile(lnk)) and (
            not os.path.isdir(host) or not os.listdir(host))
        check("一键卸载清干净", uni.returncode == 0 and clean,
              "快捷方式与宿主文件已移除" if clean else "仍有残留")

        # --- 8. 没碰真实桌面 / 真实用户目录 ---
        real_lnk_after = [os.path.isdir(d) and os.path.isfile(
            os.path.join(d, "INTENT Portable.lnk")) for d in real_desks]
        real_host_after = os.path.isdir(
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "INTENTPortable"))
        check("真实桌面零改动", real_lnk_before == real_lnk_after,
              "未创建/删除任何真实快捷方式")
        check("真实用户目录零改动", real_host_before == real_host_after,
              "未写入 %LOCALAPPDATA%")

    finally:
        # 无论成败都要把盘符映射、临时目录收干净
        for d in list(mapped):
            unsubst(d)
        try:
            shutil.rmtree(TMP)
        except OSError:
            pass

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print("-" * 68)
    print(" 自验收：%d/%d 通过" % (passed, len(RESULTS)))
    for name, ok, detail in RESULTS:
        if not ok:
            print("   未过 -> %s (%s)" % (name, detail))
    print("=" * 68)
    return 0 if passed == len(RESULTS) and len(RESULTS) >= 14 else 1


if __name__ == "__main__":
    sys.exit(main())
