# -*- coding: utf-8 -*-
"""INTENT 便携版 · 增量清单（出厂包 / U 盘 / 云端更新三处共用）。

设计要点：
  * 每个文件记 sha256 + size + 通道，更新时只传「内容真的变了」的文件。
  * 通道 ch：
      code  程序本体（本机推送 + 云端拉取都能更新）
      base  runtime/ 底座（约 1.9GB，只能本机插盘推送，不上云）
      user  用户数据（出厂包不含；更新时绝不覆盖、绝不删除）
      local 本地生成的 .lnk / 标记（不传输，安装或推送时重新生成）
  * 删除只针对「上一版清单里登记为 code/base 的文件」，
    任何未知文件、任何 user 区文件一律不碰。
"""
import hashlib
import json
import os

MANIFEST = "manifest.json"
VERSION = "version.json"

USER_PREFIX = (
    "会话/", "memories/", "models/", "kb/", "logs/",
    "data/home/", "data/资料/", "data/temp/",
)
USER_FILES = {"配置.qdai", "配置.local.qdai", "pc_bridge.token"}
# 注意：INTENT-PORTABLE.marker 必须随盘传输！两个快捷方式靠它扫盘定位 U 盘，
# 若在这里排除，安装器就不会把它拷到 U 盘，快捷方式全部失效。
LOCAL_FILES = {"manifest.json", "version.json"}
LOCAL_EXT = {".lnk"}

SKIP_DIRS = {"__pycache__", ".git", "node_modules", "backups", "browser-upgrade",
             "shots", "tests", "_publish", "_lnk_probe", "_factory_tmp",
             "_desktop_verify_tmp", "INTENT-Portable", "dist"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".tmp", ".bak", ".new"}
SKIP_FILES = {"manifest.json", "version.json", "launch.log", "debug.log"}

CHUNK = 1 << 20


def norm(rel):
    return rel.replace("\\", "/").lstrip("./")


def is_user(rel):
    rel = norm(rel)
    return rel in USER_FILES or any(rel.startswith(p) for p in USER_PREFIX)


def channel(rel):
    rel = norm(rel)
    if is_user(rel):
        return "user"
    if rel in LOCAL_FILES or os.path.splitext(rel)[1].lower() in LOCAL_EXT:
        return "local"
    if rel.startswith("runtime/"):
        return "base"
    return "code"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def scan(root, progress=None):
    """扫描目录，返回 {相对路径: {sha256,size,ch}}。"""
    out = {}
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            rel = norm(os.path.relpath(os.path.join(dirpath, name), root))
            if (name in SKIP_FILES or name in LOCAL_FILES
                    or os.path.splitext(name)[1].lower() in SKIP_EXT
                    or os.path.splitext(name)[1].lower() in LOCAL_EXT
                    or name in {"manifest.json", "version.json"}):
                continue
            full = os.path.join(dirpath, name)
            try:
                out[rel] = {"sha256": sha256(full), "size": os.path.getsize(full),
                            "ch": channel(rel)}
            except OSError:
                continue
            n += 1
            if progress and n % 500 == 0:
                progress(n)
    return out


def version_stamp():
    import datetime
    return datetime.datetime.now().strftime("%Y.%m.%d-%H%M")


def make(root, version=None, base_version=None):
    files = scan(root)
    ver = version or version_stamp()
    return {"version": ver, "base_version": base_version or ver,
            "files": files, "count": len(files)}


def save(root, data, path=None):
    p = path or os.path.join(root, MANIFEST)
    tmp = p + ".new"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)
    return p


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_version(root, version, base_version, note=""):
    p = os.path.join(root, VERSION)
    tmp = p + ".new"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": version, "base_version": base_version,
                   "note": note}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def stats(files):
    c = {}
    for meta in files.values():
        c[meta.get("ch", "code")] = c.get(meta.get("ch", "code"), 0) + 1
    return c


def base_sig(files):
    """底座（runtime/）指纹：只有 runtime 内容变了它才变。"""
    sub = {k: v["sha256"] for k, v in files.items() if v.get("ch") == "base"}
    return hashlib.sha256(json.dumps(sub, sort_keys=True).encode("utf-8")).hexdigest()[:12]
