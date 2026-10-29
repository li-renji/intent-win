# -*- coding: utf-8 -*-
"""市场：技能（skills）与插件（plugins）的统一管理。

两类扩展都是「文件驱动」，与既有 .qdskill/.qdplugin 约定完全一致：
    技能  skills/<id>.qdskill      →  <id>.qdskill.disabled   表示已禁用
    插件  plugins/<id>.qdplugin    →  <id>.qdplugin.disabled  表示已禁用
安装 = 复制进目录；删除 = 移出目录；禁用 = 改后缀（不删文件，可随时恢复）。
技能摘要与插件注册都在启动时完成，因此装/卸/启停后需重启软件生效。
"""
import os
import re
import shutil
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")
PLUGINS_DIR = os.path.join(PROJECT_ROOT, "plugins")
STORE_DIR = os.path.join(PROJECT_ROOT, "market", "store")

KINDS = {
    "skill": {"dir": SKILLS_DIR, "ext": ".qdskill"},
    "plugin": {"dir": PLUGINS_DIR, "ext": ".qdplugin"},
}

_HEAD_KEYS = ("name", "version", "desc", "trigger", "action", "run", "params", "id")
_MAX_BYTES = 2 * 1024 * 1024
_TIMEOUT = 20


def _safe_id(s):
    """把任意名字规整成安全的文件名 id（防路径穿越）。"""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "").strip())
    return s.strip("-").lower()[:48]


def _head_fields(path):
    """读取扩展文件头部字段（name/desc/trigger/...），遇到正文即停止。"""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return out
    for line in lines[:30]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            break
        k, v = s.split(":", 1)
        k = k.strip().lower()
        if k not in _HEAD_KEYS:
            break
        out[k] = v.strip()
    return out


def _scan_dir(directory, ext):
    """扫描一个扩展目录，返回 [{id,name,desc,trigger,enabled}]。"""
    items = []
    if not os.path.isdir(directory):
        return items
    for fn in sorted(os.listdir(directory)):
        if fn.endswith(ext + ".disabled"):
            eid, enabled = fn[:-(len(ext) + 9)], False
        elif fn.endswith(ext):
            eid, enabled = fn[:-len(ext)], True
        else:
            continue
        h = _head_fields(os.path.join(directory, fn))
        items.append({
            "id": eid,
            "name": h.get("name") or eid,
            "desc": h.get("desc") or "",
            "trigger": h.get("trigger") or "",
            "enabled": enabled,
        })
    return items


def list_all():
    """返回 {"skills": {"installed": [...], "store": [...]}, "plugins": {...}}。"""
    out = {}
    for kind, meta in KINDS.items():
        installed = _scan_dir(meta["dir"], meta["ext"])
        have = {i["id"] for i in installed}
        store = []
        if os.path.isdir(STORE_DIR):
            for fn in sorted(os.listdir(STORE_DIR)):
                if not fn.endswith(meta["ext"]):
                    continue
                eid = fn[:-len(meta["ext"])]
                h = _head_fields(os.path.join(STORE_DIR, fn))
                store.append({
                    "id": eid,
                    "name": h.get("name") or eid,
                    "desc": h.get("desc") or "",
                    "kind": kind,
                    "installed": eid in have,
                "trigger": h.get("trigger") or "",
                })
        out[kind + "s"] = {"installed": installed, "store": store}
    return out


def install(kind, eid):
    """从内置货架安装（复制文件到对应目录）。"""
    meta = KINDS.get(kind)
    if not meta:
        return {"ok": False, "error": "未知类型: %s" % kind}
    eid = _safe_id(eid)
    src = os.path.join(STORE_DIR, eid + meta["ext"])
    if not os.path.isfile(src):
        return {"ok": False, "error": "货架上没有这个扩展: %s" % eid}
    os.makedirs(meta["dir"], exist_ok=True)
    dst = os.path.join(meta["dir"], eid + meta["ext"])
    dis = dst + ".disabled"
    if os.path.exists(dis):
        try:
            os.remove(dis)
        except Exception:
            pass
    shutil.copyfile(src, dst)
    return {"ok": True, "id": eid, "kind": kind}


def install_url(kind, url, name):
    """从 URL 下载安装：只接受 http/https，限 2MB，内容需含 name/id 头。"""
    meta = KINDS.get(kind)
    if not meta:
        return {"ok": False, "error": "未知类型: %s" % kind}
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        return {"ok": False, "error": "只支持 http/https 开头的地址"}
    eid = _safe_id(name)
    if not eid:
        return {"ok": False, "error": "请先给扩展起个 id（字母/数字）"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "intent-market"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = r.read(_MAX_BYTES + 1)
    except Exception as e:
        return {"ok": False, "error": "下载失败: %s" % e}
    if len(data) > _MAX_BYTES:
        return {"ok": False, "error": "文件超过 2MB，已取消安装"}
    try:
        text = data.decode("utf-8")
    except Exception:
        return {"ok": False, "error": "下载内容不是 UTF-8 文本"}
    if not text.strip():
        return {"ok": False, "error": "下载内容为空"}
    if "name:" not in text and "id:" not in text:
        return {"ok": False, "error": "内容不像一个 .qdskill/.qdplugin（缺少 name/id 头）"}
    os.makedirs(meta["dir"], exist_ok=True)
    dst = os.path.join(meta["dir"], eid + meta["ext"])
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text)
    return {"ok": True, "id": eid, "kind": kind}


def remove(kind, eid):
    """卸载并删除（同时清理禁用态文件）。"""
    meta = KINDS.get(kind)
    if not meta:
        return {"ok": False, "error": "未知类型: %s" % kind}
    eid = _safe_id(eid)
    base = os.path.join(meta["dir"], eid + meta["ext"])
    dis = base + ".disabled"
    gone = False
    for p in (base, dis):
        if os.path.exists(p):
            try:
                os.remove(p)
                gone = True
            except Exception as e:
                return {"ok": False, "error": "删除失败: %s" % e}
    if not gone:
        return {"ok": False, "error": "没有找到这个扩展: %s" % eid}
    return {"ok": True, "id": eid, "kind": kind}


def toggle(kind, eid, on):
    """启用 / 禁用（只改后缀，不删文件）。"""
    meta = KINDS.get(kind)
    if not meta:
        return {"ok": False, "error": "未知类型: %s" % kind}
    eid = _safe_id(eid)
    base = os.path.join(meta["dir"], eid + meta["ext"])
    dis = base + ".disabled"
    if on:
        if os.path.exists(dis):
            os.rename(dis, base)
        elif not os.path.exists(base):
            return {"ok": False, "error": "没有找到这个扩展: %s" % eid}
        return {"ok": True, "id": eid, "kind": kind, "enabled": True}
    if os.path.exists(base):
        os.rename(base, dis)
    elif not os.path.exists(dis):
        return {"ok": False, "error": "没有找到这个扩展: %s" % eid}
    return {"ok": True, "id": eid, "kind": kind, "enabled": False}
