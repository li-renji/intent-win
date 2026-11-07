# -*- coding: utf-8 -*-
"""工作台扩展层：免安装设备节点 / Token 用量中心 / 免费额度雷达。

以「请求拦截器」方式挂到 app.py 的四个动词入口，原有路由完全不动。
handle() 返回 True 表示这个请求已被本层处理。
"""
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent.parent
ROOT = str(BASE.parent)
NODES = BASE / "nodes.json"
WEB = BASE / "web"
_LOCK = threading.RLock()
_BG_STARTED = False
ONLINE_WINDOW = 35          # 秒；心跳 8s，超过这个时间视为离线

# 直接按文件加载，避开 agent 包的额外依赖
_spec = importlib.util.spec_from_file_location("_intent_usage",
                                               os.path.join(ROOT, "agent", "usage.py"))
usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usage)


# --------------------------------------------------------------------------- #
# 节点数据
# --------------------------------------------------------------------------- #
def _nodes():
    with _LOCK:
        try:
            with open(NODES, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d.setdefault("nodes", {})
        d.setdefault("messages", [])
        return d


def _save_nodes(d):
    with _LOCK:
        tmp = str(NODES) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, NODES)


def _nodes_payload():
    d = _nodes()
    now = time.time()
    rows = []
    for nid, n in d["nodes"].items():
        r = dict(n)
        r["online"] = (now - float(n.get("last_seen", 0))) <= ONLINE_WINDOW
        r["ago"] = int(now - float(n.get("last_seen", 0)))
        r["unread"] = sum(1 for m in d["messages"]
                          if m.get("to") == nid and not m.get("read"))
        rows.append(r)
    rows.sort(key=lambda x: (-x["online"], x.get("name", "")))
    inbox = [m for m in d["messages"] if m.get("to") == "workbench"][-20:]
    return {"nodes": rows, "online": sum(1 for x in rows if x["online"]),
            "total": len(rows), "inbox": inbox, "now": int(now)}


def _heartbeat(h, body):
    nid = str(body.get("id") or "").strip()[:64]
    if not nid:
        return h._err(400, "缺少节点 id")
    d = _nodes()
    n = d["nodes"].get(nid) or {"id": nid, "first_seen": int(time.time())}
    n.update({"name": str(body.get("name") or n.get("name") or ("设备-" + nid[-4:]))[:48],
              "ua": str(body.get("ua") or "")[:220],
              "screen": str(body.get("screen") or "")[:32],
              "ip": h.client_address[0],
              "kind": str(body.get("kind") or "browser")[:24],
              "last_seen": time.time()})
    d["nodes"][nid] = n
    # 消息只留最近 200 条
    d["messages"] = d["messages"][-200:]
    _save_nodes(d)
    msgs = [m for m in d["messages"] if m.get("to") == nid and not m.get("read")]
    return h._json(200, {"ok": True, "id": nid, "name": n["name"],
                         "messages": msgs, "server_time": int(time.time())})


def _pull(h, qs):
    nid = (qs.get("id", [""])[0] or "")
    d = _nodes()
    if nid and nid in d["nodes"]:
        d["nodes"][nid]["last_seen"] = time.time()
        _save_nodes(d)
    msgs = [m for m in d["messages"] if m.get("to") == nid and not m.get("read")]
    for m in msgs:
        m["read"] = True
    if msgs:
        _save_nodes(d)
    return h._json(200, {"messages": msgs, "now": int(time.time())})


def _send(h, body):
    text = str(body.get("text") or "").strip()[:500]
    to = str(body.get("to") or "").strip()
    if not text or not to or to not in _nodes()["nodes"]:
        return h._err(400, "消息或目标节点无效")
    d = _nodes()
    d["messages"].append({"to": to, "text": text, "ts": int(time.time()),
                          "from": "工作台", "read": False})
    d["messages"] = d["messages"][-200:]
    _save_nodes(d)
    return h._json(200, {"ok": True})


def _note(h, body):
    """设备 → 工作台留言。"""
    nid = str(body.get("id") or "")
    text = str(body.get("text") or "").strip()[:500]
    d = _nodes()
    if nid not in d["nodes"] or not text:
        return h._err(400, "节点或内容无效")
    d["messages"].append({"to": "workbench", "text": text, "ts": int(time.time()),
                          "from": d["nodes"][nid].get("name", nid), "read": False})
    d["messages"] = d["messages"][-200:]
    _save_nodes(d)
    return h._json(200, {"ok": True})


# --------------------------------------------------------------------------- #
# 每日巡检后台线程（工作台开着才会跑）
# --------------------------------------------------------------------------- #
def start_background():
    global _BG_STARTED
    if _BG_STARTED:
        return
    _BG_STARTED = True

    def loop():
        last = ""
        while True:
            try:
                today = time.strftime("%Y-%m-%d")
                if today != last:
                    usage.daily_scan()
                    last = today
            except Exception:
                pass
            time.sleep(120)

    threading.Thread(target=loop, name="quota-radar", daemon=True).start()


# --------------------------------------------------------------------------- #
# 请求分发
# --------------------------------------------------------------------------- #
def _serve_file(h, rel, ctype="text/html; charset=utf-8"):
    f = WEB / rel
    if not f.is_file():
        return h._err(404, "未找到页面: %s" % rel)
    data = f.read_bytes()
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    try:
        h.wfile.write(data)
    except Exception:
        pass


def handle(h, method, path, body):
    """返回 True = 已处理。"""
    try:
        parts = urlparse(path)
        p, qs = parts.path, parse_qs(parts.query)
        seg = [unquote(x) for x in p.split("/") if x]

        # ---------- 免安装节点 ----------
        if method == "GET" and p == "/node":
            return _serve_file(h, "node.html") or True
        if method == "GET" and p == "/api/nodes":
            return h._json(200, _nodes_payload()) or True
        if method == "GET" and p == "/api/nodes/messages":
            return _pull(h, qs) or True
        if method == "POST" and p == "/api/nodes/heartbeat":
            return _heartbeat(h, body) or True
        if method == "POST" and p == "/api/nodes/message":
            return _send(h, body) or True
        if method == "POST" and p == "/api/nodes/note":
            return _note(h, body) or True
        if method == "POST" and p == "/api/nodes/rename":
            d = _nodes()
            nid, name = str(body.get("id") or ""), str(body.get("name") or "")[:48]
            if nid not in d["nodes"] or not name:
                return h._err(400, "节点或名称无效")
            d["nodes"][nid]["name"] = name
            _save_nodes(d)
            return h._json(200, {"ok": True, "name": name}) or True
        if method == "DELETE" and len(seg) == 3 and seg[:2] == ["api", "nodes"]:
            d = _nodes()
            if seg[2] not in d["nodes"]:
                return h._err(404, "节点不存在")
            del d["nodes"][seg[2]]
            _save_nodes(d)
            return h._json(200, {"ok": True}) or True

        # ---------- 用量中心 ----------
        if method == "GET" and p == "/api/usage":
            force = qs.get("refresh", ["0"])[0] in ("1", "true")
            return h._json(200, usage.all_usage(force=force)) or True
        if method == "POST" and p == "/api/usage/refresh":
            return h._json(200, usage.all_usage(force=True)) or True
        if method == "GET" and p == "/api/usage/providers":
            return h._json(200, usage.public_providers()) or True
        if method == "POST" and p == "/api/usage/login":
            pid = str(body.get("id") or "")
            row = next((x for x in usage.load_providers()["providers"]
                        if x.get("id") == pid), None)
            url = (row or {}).get("page_url", "")
            if not url:
                return h._err(400, "这条供应商没有配置用量页 URL")
            ok, msg = usage.open_login(url)
            return h._json(200, {"ok": ok, "detail": msg}) or True
        if method == "POST" and p == "/api/usage/providers":
            row = body.get("provider") or {}
            if not str(row.get("id") or "").strip():
                return h._err(400, "缺少 id")
            cfg = usage.load_providers()
            old = next((x for x in cfg["providers"] if x.get("id") == row["id"]), None)
            if old:
                # 密钥留空表示不修改
                if not row.get("api_key"):
                    row.pop("api_key", None)
                    row["api_key"] = old.get("api_key", "")
                old.update(row)
            else:
                cfg["providers"].append(row)
            usage.save_providers(cfg)
            return h._json(200, {"ok": True}) or True
        if method == "DELETE" and len(seg) == 4 and seg[:3] == ["api", "usage", "providers"]:
            if seg[3] == "main":
                return h._err(400, "主力套餐不能删除")
            cfg = usage.load_providers()
            cfg["providers"] = [x for x in cfg["providers"] if x.get("id") != seg[3]]
            usage.save_providers(cfg)
            return h._json(200, {"ok": True}) or True

        # ---------- 免费额度雷达 ----------
        if method == "GET" and p == "/api/quota":
            return h._json(200, usage.quota_report()) or True
        if method == "POST" and p == "/api/quota/scan":
            return h._json(200, usage.daily_scan()) or True
        if method == "POST" and p == "/api/quota/route":
            return h._json(200, {"routing": usage.set_routing(body.get("auto"))}) or True
        if method == "POST" and p == "/api/quota/attach":
            ok, msg = usage.attach_free(str(body.get("tier_id") or ""),
                                        str(body.get("api_key") or ""),
                                        str(body.get("base_url") or ""),
                                        str(body.get("model") or ""))
            return h._json(200 if ok else 400,
                           {"ok": ok, "detail": msg}) or True
    except Exception as e:
        try:
            return h._json(200, {"ok": False, "detail": "%s: %s" %
                                 (type(e).__name__, str(e)[:160])}) or True
        except Exception:
            return True
    return False
