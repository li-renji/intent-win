# -*- coding: utf-8 -*-
"""CDP-over-WebSocket 服务器：对齐 Lightpanda 的 `serve`。

Lightpanda 的 `serve` 把浏览器本身暴露成一个标准 CDP 端点，
于是 Puppeteer / Playwright / 任意 CDP 客户端都能直接驱动它：

    puppeteer.connect({ browserWSEndpoint: "ws://127.0.0.1:9222" })

MyBrowser 借的是真 Chromium 内核，本模块在其之上架一层「CDP 服务器」：
  · 实现 Chromium 标准的 HTTP 发现端点（/json/version、/json、/json/new…）
  · 实现 /devtools/browser 与 /devtools/page/<id> 两个 WebSocket 端点
  · 把进来的 CDP 命令转发给底层 Chromium（经 mybrowser 自研 CDP 客户端）
  · 额外提供 MyBrowser.* 自定义域：snapshot / dump / trace / ping
    —— 这些是 mybrowser 自研语义层（AX 快照、markdown dump、会话录制），
       Lightpanda 没有，是我们的差异化能力。

转发模型：本服务器是 **桥（bridge）**——外面说 CDP，里面也是 CDP，
所以不需要逐条实现 CDP 方法，转发即可。
"""
import base64
import hashlib
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.parse

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------------------
# 最小 WebSocket 服务端帧编解码（自研，零依赖）
# ---------------------------------------------------------------------------

class _WSLink:
    """服务端侧的一条 WS 连接：负责握手后的帧收发。"""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.lock = threading.Lock()
        self.closed = False

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("对端关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_frame(self):
        b0, b1 = self._recv_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode, payload):
        # 服务端 -> 客户端：禁止掩码（RFC 6455 §5.3）
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(length)
        elif length < (1 << 16):
            header.append(126)
            header += struct.pack(">H", length)
        else:
            header.append(127)
            header += struct.pack(">Q", length)
        with self.lock:
            if self.closed:
                raise ConnectionError("已关闭")
            self.sock.sendall(bytes(header) + payload)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self):
        """读一条文本消息；遇到 close/ping/pong 自动处理。"""
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:           # close
                self.close()
                raise ConnectionError("对端 close")
            if opcode == 0x9:           # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:           # pong
                continue
            if opcode in (0x1, 0x2):
                return payload.decode("utf-8", "replace")
            # 0x0 continuation：简单起见直接读下一帧
            return payload.decode("utf-8", "replace")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CDP 服务器
# ---------------------------------------------------------------------------

def _http_response(conn, status, body, content_type="application/json"):
    if isinstance(body, str):
        body = body.encode("utf-8")
    head = (
        "HTTP/1.1 %d OK\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %d\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Connection: close\r\n\r\n"
    ) % (status, content_type, len(body))
    try:
        conn.sendall(head.encode("utf-8") + body)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


class CDPServer:
    """把 MyBrowser 暴露成标准 CDP 服务器。"""

    def __init__(self, tool, host="127.0.0.1", port=9223):
        """
        tool: MyBrowserTool 实例（提供 engine / session / snapshotter /
              recorder / actions）。
        """
        self.tool = tool
        self.host = host
        self.port = port
        self._sock = None
        self._running = False
        self._thread = None

    # ---------------- 生命周期 ----------------

    def start(self):
        if self._running:
            return True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(64)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass

    @property
    def ws_endpoint(self):
        return "ws://%s:%d/devtools/browser" % (self.host, self.port)

    # ---------------- 接受连接 ----------------

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except Exception:
                if self._running:
                    continue
                break
            conn.settimeout(30)
            threading.Thread(target=self._handle_conn,
                            args=(conn,), daemon=True).start()

    def _handle_conn(self, conn):
        try:
            req = self._read_http_request(conn)
            if not req:
                conn.close()
                return
            method, path, headers = req
            is_ws = headers.get("upgrade", "").lower() == "websocket"
            if is_ws:
                self._handshake(conn, headers)
                self._ws_loop(conn, path)
            else:
                self._http(conn, method, path)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _read_http_request(conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buf += chunk
        head, _ = buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, _, v = ln.partition(":")
                headers[k.strip().lower()] = v.strip()
        return method, path, headers

    @staticmethod
    def _handshake(conn, headers):
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
        )
        conn.sendall(resp.encode("utf-8"))

    # ---------------- HTTP 发现端点 ----------------

    def _http(self, conn, method, path):
        if method != "GET":
            _http_response(conn, 405, json.dumps({"error": "method"}))
            return
        base = "http://%s:%d" % (self.host, self.port)
        if path in ("/json/version", "/json/version/"):
            info = {
                "Browser": "MyBrowser/0.2 (Chromium/WebView2)",
                "Protocol-Version": "1.3",
                "User-Agent": "MyBrowser/0.2",
                "webSocketDebuggerUrl": self.ws_endpoint,
            }
            _http_response(conn, 200, json.dumps(info))
            return
        if path in ("/json", "/json/", "/json/list", "/json/list/"):
            _http_response(conn, 200, json.dumps(self._list_targets(base)))
            return
        if path.startswith("/json/new"):
            q = urllib.parse.urlparse(path).query
            url = urllib.parse.parse_qs(q).get("url", ["about:blank"])[0]
            tid = self._create_target(url)
            _http_response(conn, 200, json.dumps(
                self._target_info(tid, base) if tid else {"error": "create failed"}))
            return
        if path.startswith("/json/close/"):
            tid = path.rsplit("/", 1)[-1]
            self._close_target(tid)
            _http_response(conn, 200, json.dumps({"success": True}))
            return
        _http_response(conn, 404, json.dumps({"error": "not found"}))

    # ---------------- 目标管理 ----------------

    def _backend(self):
        """底层 Chromium 的浏览器级 CDP 客户端。"""
        return self.tool.engine.client if self.tool.engine else None

    def _list_targets(self, base):
        out = []
        client = self._backend()
        if client:
            try:
                res = client.send("Target.getTargets")
                for t in res.get("targetInfos", []):
                    if t.get("type") == "page":
                        out.append(self._target_info(t["targetId"], base))
            except Exception:
                pass
        return out

    def _target_info(self, target_id, base):
        return {
            "id": target_id,
            "type": "page",
            "url": "about:blank",
            "title": "MyBrowser",
            "webSocketDebuggerUrl":
                "ws://%s:%d/devtools/page/%s" % (self.host, self.port, target_id),
        }

    def _create_target(self, url):
        client = self._backend()
        if not client:
            return None
        try:
            res = client.send("Target.createTarget", {"url": url})
            return res.get("targetId")
        except Exception:
            return None

    def _close_target(self, target_id):
        client = self._backend()
        if not client:
            return
        try:
            client.send("Target.closeTarget", {"targetId": target_id})
        except Exception:
            pass

    def _resolve_session(self, target_id):
        """按 targetId 找 Session；找不到用当前活动会话兜底。"""
        tabs = getattr(self.tool.engine, "_tabs", {}) or {}
        return tabs.get(target_id) or self.tool.session

    def _page_session_id(self, target_id):
        """页面级 WS 要用的 sessionId。

        优先复用 engine 已有的 Session（其 Page/Runtime/DOM… 域早已 enable），
        否则新建一次 attach 并把必要域 enable 上。

        关键坑：对同一个 target 做第二次 Target.attachToTarget 在 WebView2 上
        会失败，且即便成功，新会话若没 enable 域，转发 Runtime.evaluate 会报
        "X wasn't found"。所以必须先复用、再新建 + enable。
        """
        engine = getattr(self.tool, "engine", None)
        tabs = getattr(engine, "_tabs", {}) or {}
        sess = tabs.get(target_id)
        if sess is not None and getattr(sess, "session_id", None):
            return sess.session_id
        client = self._backend()
        if not client:
            return None
        try:
            att = client.send("Target.attachToTarget",
                              {"targetId": target_id, "flatten": True})
            sid = att.get("sessionId")
            if not sid:
                return None
            for domain in ("Page", "Runtime", "DOM", "Network", "Accessibility"):
                try:
                    client.send("%s.enable" % domain, {}, session_id=sid)
                except Exception:
                    pass
            try:
                from ..core.engine import Session
                engine._tabs[target_id] = Session(engine, target_id, sid, -1)
            except Exception:
                pass
            return sid
        except Exception:
            return None

    # ---------------- WebSocket 控制面 ----------------

    def _ws_loop(self, conn, path):
        link = _WSLink(conn)
        target_id = None
        m = re.match(r"/devtools/page/([\w\-]+)", path)
        if m:
            target_id = m.group(1)
        session_id = None
        attached = set()        # 浏览器级 WS 由客户端 attach 出来的会话

        # 页面级连接：复用（或新建）该 target 的会话
        if target_id:
            session_id = self._page_session_id(target_id)
            if session_id is None:
                link.send_text(json.dumps(
                    {"id": 0, "error": {"message": "no session for target"}}))

        # 事件转发：只把属于本连接的事件发回客户端
        def on_event(method, params, sid):
            try:
                if target_id is None:
                    # 浏览器级：浏览器本身事件 + 本连接 attach 出来的会话事件
                    if sid is None or sid in attached:
                        link.send_text(json.dumps(
                            {"method": method, "params": params or {}}))
                else:
                    if sid == session_id:
                        link.send_text(json.dumps(
                            {"method": method, "params": params or {}}))
            except Exception:
                pass

        client = self._backend()
        handler = client.on_any(on_event) if client else None

        try:
            while True:
                msg = link.recv_text()
                if not msg:
                    break
                try:
                    obj = json.loads(msg)
                except Exception:
                    continue
                mid = obj.get("id")
                method = obj.get("method")
                params = obj.get("params") or {}
                incoming_sid = obj.get("sessionId")
                if mid is None or not method:
                    continue
                self._dispatch_command(link, mid, method, params,
                                       target_id, session_id,
                                       incoming_sid=incoming_sid,
                                       attached=attached)
        except ConnectionError:
            pass
        finally:
            if handler:
                try:
                    client.off_any(handler)
                except Exception:
                    pass
            link.close()

    def _dispatch_command(self, link, mid, method, params, target_id,
                          session_id, incoming_sid=None, attached=None):
        # ---- 自定义域 MyBrowser.* ----
        if method.startswith("MyBrowser."):
            result = self._mybrowser_command(method, params, target_id, session_id)
            link.send_text(json.dumps({"id": mid, "result": result or {}}))
            return
        # ---- 转发给底层 Chromium ----
        client = self._backend()
        if not client:
            link.send_text(json.dumps(
                {"id": mid, "error": {"message": "no backend connected"}}))
            return
        try:
            if target_id:
                sid = session_id           # 页面级 WS：固定路由到该 target
            else:
                sid = incoming_sid or None  # 浏览器级 WS：尊重客户端自带的 sessionId
            res = client.send(method, params, session_id=sid)
            # 浏览器级 WS：登记客户端自己 attach 出来的会话，便于回传其事件
            if (target_id is None and method == "Target.attachToTarget"
                    and attached is not None):
                rs = (res or {}).get("result") or {}
                s2 = rs.get("sessionId")
                if s2:
                    attached.add(s2)
            link.send_text(json.dumps({"id": mid, "result": res or {}}))
        except Exception as e:
            link.send_text(json.dumps(
                {"id": mid, "error": {"message": str(e)}}))

    # ---------------- MyBrowser.* 自定义命令 ----------------

    def _mybrowser_command(self, method, params, target_id, session_id):
        name = method.split(".", 1)[1]
        if name == "ping":
            return {"pong": True, "version": "MyBrowser/0.2",
                    "engine": "WebView2/Chromium"}
        if name == "version":
            return {"browser": "MyBrowser/0.2",
                    "protocol": "1.3", "engine": "WebView2/Chromium"}
        sess = self._resolve_session(target_id)
        if name == "snapshot":
            if not sess:
                return {"error": "no active session"}
            from ..core.snapshot import Snapshotter
            try:
                snap = Snapshotter(sess).capture(
                    max_chars=int(params.get("maxChars", 3500)))
                return {"url": snap.url, "title": snap.title,
                        "text": snap.text,
                        "elements": [list(e) for e in snap.elements]}
            except Exception as e:
                return {"error": str(e)}
        if name == "dump":
            if not sess:
                return {"error": "no active session"}
            from ..core.dump import Dump
            try:
                fmt = (params.get("format") or "markdown").lower()
                return {"format": fmt,
                        "content": Dump(sess).dump(
                            fmt, max_chars=int(params.get("maxChars", 8000)))}
            except Exception as e:
                return {"error": str(e)}
        if name == "trace":
            try:
                sub = (params.get("action") or "status").lower()
                rec = self.tool.recorder
                if sub == "on":
                    rec.start()
                elif sub == "off":
                    rec.stop()
                elif sub == "save":
                    rec.save_script(params.get("path") or
                                   os.path.join(os.environ.get("TEMP", "."),
                                                "mybrowser_session.js"))
                return {"status": rec.status()}
            except Exception as e:
                return {"error": str(e)}
        return {"error": "unknown MyBrowser command: %s" % name}
