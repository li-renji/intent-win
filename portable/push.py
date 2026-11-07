# -*- coding: utf-8 -*-
"""开发机 · 插盘增量推送（「推送到U盘.cmd」调用）。

把最新出厂包的程序文件推到已装好的 U 盘，只传 sha256 变了的文件，
用户的会话 / 记忆 / 模型 / 配置 / 浏览器登录态一个字节都不动。
runtime/ 底座也能更新（这是云端 UPDATE.cmd 做不到的部分）。

用法：
    python portable\\push.py --drive E:        # 推送到 E:
    python portable\\push.py --drive E: --dry-run
    python portable\\push.py --target Z:\\      # 指定路径（验收/自托管）
    python portable\\push.py --no-build        # 用现有 payload，不重新打包
"""
import argparse
import json
import os
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.path.join(SRC, "portable")
sys.path.insert(0, PORT)

import install as inst
import manifest as mf
import shortcuts
import sync as sy

PAYLOAD = os.path.join(PORT, "dist", "INTENT-Portable-Setup", "payload")


def target_root(drive=None, target=None, allow_fixed=False):
    if target:
        return os.path.abspath(target)
    m = mf.load(os.path.join(PAYLOAD, mf.MANIFEST)) or {"files": {}}
    d, _need = inst.pick(m, drive, allow_fixed)
    return d["root"]


def push(payload=PAYLOAD, target=None, dry=False, collect=None):
    m = mf.load(os.path.join(payload, mf.MANIFEST))
    if not m:
        raise SystemExit("[推送] payload 缺 manifest.json，请先运行 factory.py")
    dm = mf.load(os.path.join(target, mf.MANIFEST))
    t0 = time.time()
    st = sy.sync(payload, target, m, dm, dry=dry, keep_user=True, progress=collect)
    if not dry:
        # 用户区文件不在同步清单里时，也要保证清单完整：合并旧清单的 user 项
        merged = dict(m["files"])
        if dm:
            for rel, meta in dm["files"].items():
                if meta.get("ch") in ("user", "local") and rel not in merged:
                    merged[rel] = meta
        out = {"version": m["version"], "base_version": m["base_version"],
               "files": merged, "count": len(merged)}
        mf.save(target, out)
        mf.save_version(target, m["version"], m["base_version"], "local push")
        ok, msg = shortcuts.make(target)
        st["shortcuts"] = ok
        st["shortcuts_msg"] = msg
    st["seconds"] = round(time.time() - t0, 2)
    st["version"] = m["version"]
    return st


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", default=PAYLOAD)
    ap.add_argument("--drive", default=None)
    ap.add_argument("--target", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--allow-fixed", action="store_true")
    a = ap.parse_args(argv)

    if not a.no_build:
        import factory
        factory.build()
    payload = os.path.abspath(a.payload)
    target = target_root(a.drive, a.target, a.allow_fixed)
    if not os.path.isfile(os.path.join(target, "INTENT-PORTABLE.marker")):
        raise SystemExit("[推送] %s 上没有 INTENT-PORTABLE.marker；"
                         "新盘请先运行「安装到U盘.cmd」" % target)
    print("[推送] 源 %s" % payload)
    print("[推送] 目标 %s（保留用户数据）" % target)
    st = push(payload, target, dry=a.dry_run)
    print("[推送] %s：新增 %d / 更新 %d / 未变 %d / 保留用户数据 %d / 删除 %d / %.1fs"
          % ("预演" if a.dry_run else "完成", st["new"], st["updated"],
             st["same"], st["kept"], st["removed"], st["seconds"]))
    print("[推送] 版本：%s" % st["version"])
    if st.get("shortcuts") is False:
        print("[推送] 快捷方式生成失败：%s" % st.get("shortcuts_msg"))
    for e in st["errors"][:6]:
        print("  ! " + e)
    return 0 if not st["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
