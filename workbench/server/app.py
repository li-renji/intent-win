#!/usr/bin/env python3
"""设备统一管理工作台 - 后端服务 (纯标准库实现, 零第三方依赖)

原先依赖 FastAPI + uvicorn + httpx + pydantic + PyYAML, 而 Agent 运行环境里
这些库全部缺失, 导致「工作台启动不了」。现改为只用 Python 标准库实现,
接口与返回结构完全保持不变, 前端 web/index.html 无需任何改动。

对外接口:
  GET    /                        工作台 Web 面板
  GET    /api/devices             设备清单与端口探活状态
  POST   /api/devices             新增设备 (持久化写入 devices.yaml)
  PUT    /api/devices/{id}        修改设备 (持久化写入 devices.yaml)
  DELETE /api/devices/{id}        删除设备
  POST   /api/devices/{id}/toggle 切换"已部署/占位"状态
  GET    /api/proxy               API 中转(LiteLLM) 连接状态
  POST   /api/proxy/key           生成 API 令牌

启动:
  python server/app.py            # 默认 0.0.0.0:3000
  set WB_PORT=8080 & python server/app.py
"""
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "devices.yaml"
WEB_DIR = BASE_DIR / "web"

_config_lock = threading.Lock()

HEADER = """# 设备统一管理工作台 - 设备清单配置
# 本文件由工作台面板自动维护: 在面板上「添加 / 编辑 / 删除设备」会写回这里。
# host: 设备 IP (局域网内设备填局域网 IP; 本机写 127.0.0.1)
# deployed: true  = 已部署, 按 probe_ports 探活
#           false = 占位登记 (尚未部署到设备上), 不探活, 状态显示"未部署"
# probe_ports: 探活端口列表, [端口, 用途标签]
"""


# --------------------------------------------------------------------------- #
# 最小 YAML 读写（只支持 devices.yaml 用到的子集，避免依赖 PyYAML）
# --------------------------------------------------------------------------- #
def _unquote(text):
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        if text[0] == "'":
            return text[1:-1].replace("''", "'")
        return text[1:-1]
    return text


def _scalar_out(value):
    """把值转成安全的 YAML 片段（必要时用单引号包起来）。"""
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ").strip()
    if text == "":
        return "''"
    low = text.lower()
    lead_special = list("#,[]{}&*!|>'\"%@`")
    need_quote = (
        low in ("true", "false", "yes", "no", "null", "~", "on", "off", "none")
        or text[0] in lead_special
        or text.startswith("- ")
        or text == "-"
        or ":" in text
        or " #" in text
    )
    if not need_quote:
        return text
    return "'" + text.replace("'", "''") + "'"


def _coerce(value):
    value = _unquote(value)
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "[]":
        return []
    return value


def _parse_pair(text):
    """解析 '[6800, 对话]' → (6800, '对话')。"""
    text = (text or "").strip()
    if text.startswith("["):
        text = text[1:]
    if text.endswith("]"):
        text = text[:-1]
    parts = text.split(",", 1)
    try:
        port = int(parts[0].strip())
    except Exception:
        port = 0
    label = _unquote(parts[1]) if len(parts) > 1 else ""
    return port, label


def parse_yaml(text):
    """解析 devices.yaml 子集 → dict。"""
    cfg = {"devices": [], "api_proxy": {"base_url": "", "admin_key": ""}}
    section = None
    cur = None
    in_ports = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if indent == 0:
            section = body.rstrip(":").strip()
            cur = None
            in_ports = False
            continue
        if section == "devices":
            # 端口条目必须先判断: '- [6800, 对话]' 也以 '- ' 开头
            if body.startswith("- ["):
                if in_ports and cur is not None:
                    port, label = _parse_pair(body[2:])
                    cur["probe_ports"].append([port, label])
                continue
            if body.startswith("- "):
                cur = {"id": "", "name": "", "host": "", "deployed": True,
                       "probe_ports": [], "note": ""}
                cfg["devices"].append(cur)
                in_ports = False
                rest = body[2:].strip()
                if rest:
                    k, sep, v = rest.partition(":")
                    if sep:
                        cur[k.strip()] = _coerce(v)
                continue
            if cur is None:
                continue
            k, sep, v = body.partition(":")
            if not sep:
                continue
            k = k.strip()
            v = v.strip()
            if k == "probe_ports":
                if v == "[]":
                    cur["probe_ports"] = []
                    in_ports = False
                else:
                    in_ports = True      # 下面的 - [端口, 标签] 才是真正的列表项
                continue
            in_ports = False
            cur[k] = _coerce(v)
        elif section == "api_proxy":
            k, sep, v = body.partition(":")
            if sep:
                cfg["api_proxy"][k.strip()] = _unquote(v)
    return cfg


def dump_yaml(cfg):
    """dict → devices.yaml 文本。"""
    lines = [HEADER, "devices:"]
    for dev in cfg.get("devices", []):
        lines.append("  - id: %s" % _scalar_out(dev.get("id", "")))
        lines.append("    name: %s" % _scalar_out(dev.get("name", "")))
        lines.append("    host: %s" % _scalar_out(dev.get("host", "")))
        lines.append("    deployed: %s" % ("true" if dev.get("deployed", True) else "false"))
        ports = dev.get("probe_ports") or []
        if ports:
            lines.append("    probe_ports:")
            for pair in ports:
                try:
                    port = int(pair[0])
                except Exception:
                    port = 0
                lines.append("      - [%d, %s]" % (port, _scalar_out(str(pair[1]))))
        else:
            lines.append("    probe_ports: []")
        lines.append("    note: %s" % _scalar_out(dev.get("note", "")))
    lines.append("")
    lines.append("# API 中转 (LiteLLM) 地址, 供前端展示连接状态")
    lines.append("api_proxy:")
    ap = cfg.get("api_proxy", {})
    lines.append("  base_url: %s" % _scalar_out(ap.get("base_url", "")))
    lines.append("  admin_key: %s" % _scalar_out(ap.get("admin_key", "")))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
def load_config():
    cp = Path(CONFIG_PATH)
    if cp.exists():
        try:
            cfg = parse_yaml(cp.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError("devices.yaml 解析失败，请检查缩进与冒号后的空格: %s" % e)
    else:
        cfg = {}
    cfg.setdefault("devices", [])
    cfg.setdefault("api_proxy", {"base_url": "http://127.0.0.1:4000", "admin_key": ""})
    for dev in cfg["devices"]:
        dev.setdefault("deployed", True)
        dev.setdefault("probe_ports", [])
        dev.setdefault("note", "")
    return cfg


def save_config(cfg):
    """写回 devices.yaml：先自检生成内容可被解析, 再原子替换, 避免写坏配置文件。"""
    cp = Path(CONFIG_PATH)
    text = dump_yaml(cfg)
    parse_yaml(text)          # 自检：生成的内容必须能被自己读回来
    tmp = cp.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(cp)


# --------------------------------------------------------------------------- #
# 设备字段处理
# --------------------------------------------------------------------------- #
def parse_ports(raw):
    """解析端口串 -> [[端口, 标签], ...]"""
    ports = []
    if not raw:
        return ports
    text = str(raw).replace("，", ",").replace("、", ",").replace(";", ",").strip()
    if not text:
        return ports
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\d{1,5})\s*[:：=/\- ]?\s*(.*)$", chunk)
        if not m:
            raise ValueError("端口格式无法识别: %s" % chunk)
        port = int(m.group(1))
        if not 1 <= port <= 65535:
            raise ValueError("端口超出范围 (1-65535): %d" % port)
        label = m.group(2).strip() or "端口"
        ports.append([port, label])
    return ports


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:24]


def unique_id(base, existing):
    base = base or "dev"
    if base not in existing:
        return base
    i = 2
    while "%s-%d" % (base, i) in existing:
        i += 1
    return "%s-%d" % (base, i)


def auto_id(name, host):
    """由名称推导 id; 中文名称推不出时退化为按地址推导 (如 192.168.3.20 -> 192-168-3-20)"""
    return slugify(name) or slugify(host) or "dev"


def build_device(body, dev_id):
    """构造设备字典；出错返回 (None, 错误信息)。"""
    body = body or {}
    name = (body.get("name") or "").strip()
    host = (body.get("host") or "").strip()
    if not name:
        return None, "设备名称不能为空"
    if not host:
        return None, "设备地址不能为空"
    try:
        ports = parse_ports(body.get("ports") or "")
    except ValueError as e:
        return None, str(e)
    return {
        "id": dev_id,
        "name": name,
        "host": host,
        "deployed": bool(body.get("deployed", True)),
        "probe_ports": ports,
        "note": (body.get("note") or "").strip(),
    }, None


# --------------------------------------------------------------------------- #
# 探活
# --------------------------------------------------------------------------- #
def probe_port(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"port": int(port), "ok": True}
    except socket.timeout:
        return {"port": int(port), "ok": False, "detail": "timeout"}
    except Exception as e:
        return {"port": int(port), "ok": False, "detail": type(e).__name__}


def _http_json(url, method="GET", headers=None, payload=None, timeout=3.0):
    """极简 HTTP JSON 请求（替代 httpx）。返回 (status, data)。"""
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        try:
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return resp.status, {}


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = "QDWorkbench/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass    # 静默，避免刷屏

    # ---------------- 响应工具 ----------------
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _err(self, code, detail):
        self._json(code, {"detail": detail})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _serve_index(self):
        f = WEB_DIR / "index.html"
        if not f.exists():
            return self._err(404, "未找到面板页面")
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    # ---------------- 业务 ----------------
    def _devices_payload(self):
        cfg = load_config()
        rows = []
        for dev in cfg["devices"]:
            deployed = dev.get("deployed", True)
            pairs = list(dev.get("probe_ports") or [])
            if deployed and pairs:
                labels = {int(p): lab for p, lab in pairs}
                results = [probe_port(dev["host"], int(p)) for p, _ in pairs]
                for r in results:
                    r["label"] = labels.get(r["port"], "")
                online = any(r["ok"] for r in results)
            else:
                results = [{"port": int(p), "label": lab, "ok": False, "detail": "undeployed"}
                           for p, lab in pairs]
                online = False
            rows.append({
                "id": dev.get("id", ""),
                "name": dev.get("name", ""),
                "host": dev.get("host", ""),
                "note": dev.get("note", ""),
                "deployed": deployed,
                "online": online,
                "ports": results,
            })
        return {"devices": rows, "config_path": str(CONFIG_PATH)}

    def _proxy_status(self):
        cfg = load_config()
        base_url = (cfg["api_proxy"].get("base_url") or "").rstrip("/")
        info = {"base_url": base_url,
                "admin_configured": bool(cfg["api_proxy"].get("admin_key"))}
        if not base_url:
            info.update({"reachable": False, "status": None, "detail": "未配置 base_url"})
            return info
        try:
            status, _ = _http_json(base_url + "/health/liveliness", timeout=2.0)
            info.update({"reachable": status == 200, "status": status})
        except Exception as e:
            info.update({"reachable": False, "status": None, "detail": type(e).__name__})
        return info

    def _proxy_key(self):
        cfg = load_config()
        admin_key = cfg["api_proxy"].get("admin_key", "")
        base_url = (cfg["api_proxy"].get("base_url") or "").rstrip("/")
        if not admin_key:
            return self._json(200, {"ok": False,
                                    "detail": "admin_key 未配置, 请在 devices.yaml 填写后重启"})
        try:
            status, data = _http_json(base_url + "/key/generate", method="POST",
                                      headers={"Authorization": "Bearer %s" % admin_key},
                                      payload={"models": [], "max_budget": 0}, timeout=5.0)
            return self._json(200, {"ok": status == 200, "status": status, "data": data})
        except Exception as e:
            return self._json(200, {"ok": False, "detail": type(e).__name__})

    # ---------------- 路由 ----------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/devices":
            return self._json(200, self._devices_payload())
        if path == "/api/proxy":
            return self._json(200, self._proxy_status())
        return self._err(404, "未找到: %s" % path)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/devices":
            with _config_lock:
                cfg = load_config()
                devices = cfg["devices"]
                ids = {d.get("id") for d in devices}
                base = (body.get("id") or "").strip()
                if base:
                    if base in ids:
                        return self._err(400, "设备 ID 已存在: %s" % base)
                    dev_id = base
                else:
                    dev_id = unique_id(auto_id(body.get("name", ""), body.get("host", "")), ids)
                dev, err = build_device(body, dev_id)
                if err:
                    return self._err(400, err)
                devices.append(dev)
                save_config(cfg)
            return self._json(200, {"ok": True, "device": dev})
        if path == "/api/proxy/key":
            return self._proxy_key()
        seg = [p for p in path.split("/") if p]
        if len(seg) == 4 and seg[0] == "api" and seg[1] == "devices" and seg[3] == "toggle":
            dev_id = unquote(seg[2])
            with _config_lock:
                cfg = load_config()
                dev = next((d for d in cfg["devices"] if d.get("id") == dev_id), None)
                if dev is None:
                    return self._err(404, "设备不存在: %s" % dev_id)
                dev["deployed"] = not dev.get("deployed", True)
                save_config(cfg)
            return self._json(200, {"ok": True, "id": dev_id, "deployed": dev["deployed"]})
        return self._err(404, "未找到: %s" % path)

    def do_PUT(self):
        path = urlparse(self.path).path
        seg = [p for p in path.split("/") if p]
        if len(seg) == 3 and seg[0] == "api" and seg[1] == "devices":
            dev_id = unquote(seg[2])
            body = self._body()
            with _config_lock:
                cfg = load_config()
                devices = cfg["devices"]
                idx = next((i for i, d in enumerate(devices) if d.get("id") == dev_id), None)
                if idx is None:
                    return self._err(404, "设备不存在: %s" % dev_id)
                new_id = (body.get("id") or "").strip() or dev_id
                if new_id != dev_id and any(d.get("id") == new_id for d in devices):
                    return self._err(400, "设备 ID 已存在: %s" % new_id)
                dev, err = build_device(body, new_id)
                if err:
                    return self._err(400, err)
                devices[idx] = dev
                save_config(cfg)
            return self._json(200, {"ok": True, "device": dev})
        return self._err(404, "未找到: %s" % path)

    def do_DELETE(self):
        path = urlparse(self.path).path
        seg = [p for p in path.split("/") if p]
        if len(seg) == 3 and seg[0] == "api" and seg[1] == "devices":
            dev_id = unquote(seg[2])
            with _config_lock:
                cfg = load_config()
                devices = cfg["devices"]
                left = [d for d in devices if d.get("id") != dev_id]
                if len(left) == len(devices):
                    return self._err(404, "设备不存在: %s" % dev_id)
                cfg["devices"] = left
                save_config(cfg)
            return self._json(200, {"ok": True, "removed": dev_id, "remain": len(left)})
        return self._err(404, "未找到: %s" % path)


def main():
    host = os.environ.get("WB_HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("WB_PORT", "3000"))
    except Exception:
        port = 3000
    srv = ThreadingHTTPServer((host, port), _Handler)
    print("[工作台] 已启动: http://localhost:%d" % port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[工作台] 已停止", flush=True)
    finally:
        try:
            srv.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
