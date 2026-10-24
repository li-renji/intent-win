# -*- coding: utf-8 -*-
"""电脑文件桥（PC File Bridge）—— 移植自手机端 qidian编辑器 的 Node 版。

让手机（qidian编辑器）在同一局域网内无线浏览 / 读取 / 修改 / 上传 / 下载
电脑文件。零第三方依赖（标准库 http.server）；API 与 Node 版逐条一致，
手机端无需任何改动，把原来的「双击 启动文件桥.bat」换成本模块即可。

安全模型（与 Node 版相同）：
  - token 持久化在 pc_bridge.token（首次运行自动生成，删除后重启换新码）
  - 所有请求必须带 token（Authorization: Bearer <token> 或 ?token=）
  - 可选 root 限制：只允许访问某个目录树（默认全盘）
  - 只监听局域网，CORS 放开给手机 WebView fetch

API 一览（均需 token，除 /api/ping 外）：
  GET  /api/ping                 → 连通性测试 + 电脑信息
  GET  /api/list?path=<目录>     → 列目录（path 为空 = 列出所有盘符）
  GET  /api/read?path=<文件>     → 读文本文件（<=8MB）
  POST /api/write  {path,content}→ 写文本文件
  GET  /api/download?path=<文件> → 下载二进制文件
  POST /api/upload?path=<目标>   ← 上传二进制文件（body 为原始字节）
  POST /api/mkdir  {path}        → 新建目录
  POST /api/delete {path}        → 删除文件/空目录

用法：
  python -m agent.pc_bridge                    # 默认 8899 端口
  python -m agent.pc_bridge --port 9000 --root D:\\代码
"""
import base64
import json
import os
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TEXT_EXTS = (
    ".txt", ".md", ".markdown", ".json", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".jsx", ".html", ".htm", ".css", ".scss", ".less", ".xml", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".py", ".java", ".kt", ".kts", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".cmd",
    ".ps1", ".gradle", ".properties", ".sql", ".vue", ".svelte", ".dart",
    ".swift", ".r", ".m", ".pl", ".lua", ".vim", ".gitignore", ".editorconfig",
    ".env", ".log", ".csv", ".qdai",
)
MAX_TEXT = 8 * 1024 * 1024        # read 文本上限（与 Node 版一致）
MAX_B64 = 1 * 1024 * 1024         # read 二进制 base64 上限
MAX_WRITE = 12 * 1024 * 1024      # write 文本上限
MAX_UPLOAD = 512 * 1024 * 1024    # upload 二进制上限


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fTB" % n


def lan_ips():
    """本机局域网 IPv4 列表（手机要连的地址）。"""
    out = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except Exception:
        pass
    # 兜底：UDP 套接字探测（不实际发包）
    if not out:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            out.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return out or ["127.0.0.1"]


def list_drives():
    """Windows 枚举 A-Z 盘符；其他系统返回 /。"""
    if sys.platform != "win32":
        return [{"name": "/", "type": "dir", "path": "/"}]
    drives = []
    for i in range(65, 91):
        letter = chr(i) + ":\\"
        if os.path.isdir(letter):
            drives.append({"name": letter, "type": "dir", "path": letter})
    return drives


def is_text_file(name):
    return os.path.splitext(name)[1].lower() in TEXT_EXTS or not os.path.splitext(name)[1]


class _BridgeHandler(BaseHTTPRequestHandler):
    """HTTP 处理器：token 校验 + 八个接口。"""

    server_version = "PCFileBridge/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础工具 ----
    def log_message(self, fmt, *args):
        import time
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self, qs):
        auth = self.headers.get("authorization", "")
        provided = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        provided = provided or (qs.get("token") or [""])[0]
        return secrets.compare_digest(provided, self.server.bridge.token)

    def _safe(self, raw):
        """路径解析 + root 限制（与 Node 版语义一致）。"""
        if not raw:
            return self.server.bridge.root or None
        abs_p = os.path.abspath(os.path.expanduser(raw))
        root = self.server.bridge.root
        if root and not (abs_p == root or abs_p.startswith(root + os.sep)):
            raise ValueError("超出允许访问的根目录: " + root)
        return abs_p

    def _read_body(self, limit):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > limit:
            raise ValueError("body 太大（>%s）" % _fmt_size(limit))
        return self.rfile.read(n) if n else b""

    # ---- 路由 ----
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path != "/api/ping" and not self._token_ok(qs):
            return self._json(401, {"ok": False,
                                    "error": "token 不匹配，请在手机端重新输入电脑上显示的连接码"})
        try:
            if u.path == "/api/ping":
                return self._json(200, {"ok": True, "host": socket.gethostname(),
                                        "platform": sys.platform,
                                        "user": os.environ.get("USERNAME", ""),
                                        "root": self.server.bridge.root,
                                        "hasToken": bool(self.server.bridge.token)})
            if u.path == "/api/list":
                return self._list(qs)
            if u.path == "/api/read":
                return self._read(qs)
            if u.path == "/api/download":
                return self._download(qs)
            return self._json(404, {"ok": False, "error": "未知接口: " + u.path})
        except Exception as e:
            return self._json(500, {"ok": False, "error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._token_ok(qs):
            return self._json(401, {"ok": False,
                                    "error": "token 不匹配，请在手机端重新输入电脑上显示的连接码"})
        try:
            if u.path == "/api/write":
                return self._write()
            if u.path == "/api/mkdir":
                return self._mkdir()
            if u.path == "/api/delete":
                return self._delete()
            if u.path == "/api/upload":
                return self._upload(qs)
            return self._json(404, {"ok": False, "error": "未知接口: " + u.path})
        except Exception as e:
            return self._json(500, {"ok": False, "error": str(e)})

    # ---- 接口实现 ----
    def _list(self, qs):
        p = self._safe((qs.get("path") or [""])[0])
        if not p:
            return self._json(200, {"ok": True, "path": "", "items": list_drives()})
        items = []
        for name in os.listdir(p):
            if name.startswith("$"):
                continue
            full = os.path.join(p, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            items.append({"name": name,
                          "type": "dir" if os.path.isdir(full) else "file",
                          "size": st.st_size, "mtime": int(st.st_mtime * 1000)})
        items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        self.log_message("LIST %s (%d 项)", p, len(items))
        return self._json(200, {"ok": True, "path": p, "items": items})

    def _read(self, qs):
        p = self._safe((qs.get("path") or [""])[0])
        if not p or not os.path.exists(p):
            return self._json(400, {"ok": False, "error": "文件不存在"})
        if os.path.isdir(p):
            return self._json(400, {"ok": False, "error": "这是目录不是文件"})
        size = os.path.getsize(p)
        if is_text_file(p):
            if size > MAX_TEXT:
                return self._json(400, {"ok": False, "error": "文件超过 8MB，请用下载"})
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            self.log_message("READ %s (%s)", p, _fmt_size(size))
            return self._json(200, {"ok": True, "path": p, "content": content,
                                    "size": size, "text": True})
        if size > MAX_B64:
            return self._json(400, {"ok": False, "error": "二进制文件请用 /api/download",
                                    "size": size})
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        self.log_message("READ(bin) %s (%s)", p, _fmt_size(size))
        return self._json(200, {"ok": True, "path": p, "content": b64,
                                "size": size, "text": False})

    def _write(self):
        body = json.loads(self._read_body(MAX_WRITE).decode("utf-8") or "{}")
        p = self._safe(body.get("path"))
        if not p:
            return self._json(400, {"ok": False, "error": "缺少 path"})
        content = body.get("content", "")
        if not isinstance(content, str):
            return self._json(400, {"ok": False, "error": "content 必须是字符串"})
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        size = os.path.getsize(p)
        self.log_message("WRITE %s (%s)", p, _fmt_size(size))
        return self._json(200, {"ok": True, "path": p, "size": size})

    def _mkdir(self):
        body = json.loads(self._read_body(64 * 1024).decode("utf-8") or "{}")
        p = self._safe(body.get("path"))
        if not p:
            return self._json(400, {"ok": False, "error": "缺少 path"})
        os.makedirs(p, exist_ok=True)
        self.log_message("MKDIR %s", p)
        return self._json(200, {"ok": True, "path": p})

    def _delete(self):
        body = json.loads(self._read_body(64 * 1024).decode("utf-8") or "{}")
        p = self._safe(body.get("path"))
        if not p or not os.path.exists(p):
            return self._json(400, {"ok": False, "error": "路径不存在"})
        if os.path.isdir(p):
            os.rmdir(p)
        else:
            os.unlink(p)
        self.log_message("DELETE %s", p)
        return self._json(200, {"ok": True})

    def _download(self, qs):
        p = self._safe((qs.get("path") or [""])[0])
        if not p or not os.path.isfile(p):
            return self._json(400, {"ok": False, "error": "文件不存在"})
        size = os.path.getsize(p)
        name = os.path.basename(p)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''" + _quote(name))
        self._cors()
        self.end_headers()
        self.log_message("DOWNLOAD %s (%s)", p, _fmt_size(size))
        with open(p, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _upload(self, qs):
        p = self._safe((qs.get("path") or [""])[0])
        if not p:
            return self._json(400, {"ok": False, "error": "缺少 path"})
        buf = self._read_body(MAX_UPLOAD)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "wb") as f:
            f.write(buf)
        self.log_message("UPLOAD %s (%s)", p, _fmt_size(len(buf)))
        return self._json(200, {"ok": True, "path": p, "size": len(buf)})


def _quote(name):
    from urllib.parse import quote
    return quote(name)


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, bridge):
        self.bridge = bridge
        super().__init__(addr, _BridgeHandler)


class PCBridge:
    """文件桥运行时：token 管理 + 后台线程服务 + 状态查询。"""

    def __init__(self, port=8899, root=None, token_file=None):
        self.port = int(port)
        self.root = os.path.abspath(root) if root else None
        self.token_file = token_file
        self.token = self._load_or_create_token()
        self._server = None
        self._thread = None

    # ---- token ----
    def _load_or_create_token(self):
        path = self.token_file
        if path and os.path.exists(path):
            try:
                t = open(path, encoding="utf-8").read().strip()
                if t:
                    return t
            except OSError:
                pass
        t = secrets.token_hex(16)
        if path:
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(t)
            except OSError:
                pass
        return t

    # ---- 生命周期 ----
    def start(self):
        if self._server:
            return True
        self._server = _BridgeServer(("0.0.0.0", self.port), self)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.5}, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None

    @property
    def running(self):
        return self._server is not None

    def status(self):
        return {
            "running": self.running,
            "port": self.port,
            "root": self.root or "整个电脑（所有盘符）",
            "token": self.token,
            "urls": ["http://%s:%d" % (ip, self.port) for ip in lan_ips()],
        }

    def banner(self):
        s = self.status()
        lines = [
            "==============================================",
            "  qidian编辑器 · 电脑文件桥 已启动（qd-agent-win 内置）",
            "==============================================",
            "  端口      : %d" % s["port"],
            "  本机 IP   : %s" % (", ".join(s["urls"]) or "-"),
            "  连接码    : %s" % s["token"],
            "  允许范围  : %s" % s["root"],
            "----------------------------------------------",
            "  手机端：qidian编辑器 → 设置 → 电脑连接 → 填入上面的 IP 和连接码",
            "==============================================",
        ]
        return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port, root = 8899, None
    i = 0
    while i < len(argv):
        if argv[i] == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
        elif argv[i] == "--root" and i + 1 < len(argv):
            root = argv[i + 1]
            i += 2
        else:
            i += 1
    here = os.path.dirname(os.path.abspath(__file__))
    token_file = os.path.join(os.path.dirname(here), "pc_bridge.token")
    bridge = PCBridge(port=port, root=root, token_file=token_file)
    bridge.start()
    print(bridge.banner())
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        bridge.stop()
        print("\n文件桥已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
