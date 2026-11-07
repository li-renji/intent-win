# -*- coding: utf-8 -*-
"""U 盘侧 · 联网增量更新（UPDATE.cmd 调用）。

只拉「程序文件」，而且只拉 sha256 真的变了的：
  * 会话 / 记忆 / 模型 / 配置.qdai / 浏览器登录态 —— 一个字节都不动
  * runtime/ 底座（约 1.9GB）不联网传输；底座版本不同会明确提示回开发机推送
  * 每个下载文件先验 sha256 再原子替换，断网/坏文件不会写坏 U 盘

用法：
    python portable\\update.py            检查并更新
    python portable\\update.py --check   只看有没有新版本
    python portable\\update.py --dry-run 预演
    python portable\\update.py --base http://127.0.0.1:8765/   自托管/验收用
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import manifest as mf
from sync import atomic_copy

DEFAULT_BASE = ("https://raw.githubusercontent.com/li-renji/intent-win/"
                "main/portable/release/")


def fetch(base, rel, timeout=30):
    url = base.rstrip("/") + "/" + urllib.parse.quote(rel, safe="/")
    req = urllib.request.Request(url, headers={"User-Agent": "intent-portable-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _remove(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def pull(root=ROOT, base=DEFAULT_BASE, dry=False, check_only=False, timeout=30):
    out = {"status": "error", "new": 0, "updated": 0, "same": 0,
           "removed": 0, "kept": 0, "errors": [], "remote": "", "local": "",
           "base_warning": ""}
    local_m = mf.load(os.path.join(root, mf.MANIFEST)) or {"files": {}}
    local_v = mf.load(os.path.join(root, mf.VERSION)) or {}
    out["local"] = local_v.get("version", "(未记录)")

    try:
        ver = json.loads(fetch(base, mf.VERSION, timeout).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["errors"].append("取不到版本信息：%s（可离线使用，稍后重试）" % e)
        return out
    out["remote"] = ver.get("version", "")
    if ver.get("version") == out["local"]:
        out["status"] = "up-to-date"
        out["kept"] = sum(1 for m in local_m["files"].values() if m.get("ch") == "user")
        return out
    if check_only:
        out["status"] = "available"
        return out

    try:
        remote_m = json.loads(fetch(base, mf.MANIFEST, timeout).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["errors"].append("取不到更新清单：%s" % e)
        return out
    rfiles = {mf.norm(k): v for k, v in remote_m.get("files", {}).items()
              if v.get("ch") == "code" and not mf.is_user(k)}

    if ver.get("base_version") and local_v.get("base_version") \
            and ver["base_version"] != local_v["base_version"]:
        out["base_warning"] = ("底座 runtime 有新版本（%s -> %s），联网更新不含底座；"
                               "请回开发机插盘运行「推送到U盘.cmd」或重新装盘。"
                               % (local_v["base_version"], ver["base_version"]))

    tmp_dir = os.path.join(root, "data", "temp")
    os.makedirs(tmp_dir, exist_ok=True)
    import hashlib

    for rel, meta in rfiles.items():
        d = os.path.join(root, rel)
        existed = os.path.isfile(d)
        if existed:
            try:
                if mf.sha256(d) == meta["sha256"]:
                    out["same"] += 1
                    continue
            except OSError:
                pass
        if dry:
            out["updated" if existed else "new"] += 1
            continue
        tmp = os.path.join(tmp_dir, "upd_" + str(abs(hash(rel))) + ".part")
        try:
            data = fetch(base, rel, timeout)
            if hashlib.sha256(data).hexdigest() != meta["sha256"]:
                out["errors"].append("校验失败已丢弃: " + rel)
                continue
            with open(tmp, "wb") as f:
                f.write(data)
            atomic_copy(tmp, d)
            os.remove(tmp)
            out["updated" if existed else "new"] += 1
        except (urllib.error.URLError, OSError, ValueError) as e:
            out["errors"].append("%s: %s" % (rel, e))
            if os.path.isfile(tmp):
                _remove(tmp)
            continue

    # 只删「本地清单登记为 code」且云端已移除的程序文件
    for rel, meta in local_m.get("files", {}).items():
        rel = mf.norm(rel)
        if rel in rfiles or mf.is_user(rel) or meta.get("ch") != "code":
            continue
        d = os.path.join(root, rel)
        if os.path.isfile(d):
            if dry or _remove(d):
                out["removed"] += 1

    if not dry:
        merged = {k: v for k, v in local_m.get("files", {}).items()
                  if v.get("ch") in ("base", "user", "local")}
        merged.update(rfiles)
        mf.save(root, {"version": ver.get("version", ""),
                       "base_version": local_v.get("base_version",
                                                   ver.get("base_version", "")),
                       "files": merged, "count": len(merged)})
        mf.save_version(root, ver.get("version", ""),
                        local_v.get("base_version", ver.get("base_version", "")),
                        "online incremental update")
    out["kept"] = sum(1 for m in local_m["files"].values() if m.get("ch") == "user")
    out["status"] = "updated" if not out["errors"] else "partial"
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--root", default=ROOT)
    a = ap.parse_args(argv)
    r = pull(a.root, a.base, dry=a.dry_run, check_only=a.check, timeout=a.timeout)
    print("本地版本: %s   云端版本: %s   状态: %s"
          % (r["local"], r["remote"] or "(取不到)", r["status"]))
    if r["status"] not in ("up-to-date", "available"):
        print("新增 %d / 更新 %d / 未变 %d / 删除 %d / 用户数据 %d 项原样保留"
              % (r["new"], r["updated"], r["same"], r["removed"], r["kept"]))
    if r["base_warning"]:
        print("[提示] " + r["base_warning"])
    for e in r["errors"][:6]:
        print("  ! " + e)
    return 0 if r["status"] != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
