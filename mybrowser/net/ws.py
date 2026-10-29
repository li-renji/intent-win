# -*- coding: utf-8 -*-
"""自研 WebSocket 客户端（RFC 6455），零第三方依赖。

只实现浏览器控制需要的最小完备子集：
  · HTTP Upgrade 握手
  · 文本/二进制帧收发（客户端 -> 服务端必须掩码，服务端 -> 客户端不掩码）
  · 分片重组
  · ping / pong / close

刻意不引第三方库：这是浏览器程序的传输层底座，必须自己完全掌控。
"""
import base64
import os
import socket
import struct
import threading


class WebSocketError(Exception):
    """握手失败 / 连接中断 / 协议违例。"""


class WebSocket:
    """阻塞式 WebSocket 客户端。

    用法：
        ws = WebSocket.connect("ws://127.0.0.1:9222/devtools/page/XXXX")
        ws.send_text('{"id":1}')
        print(ws.recv_text())
    """

    def __init__(self, sock):
        self._sock = sock
        self._buf = b""
        self._send_lock = threading.Lock()
        self._closed = False

    # ---------------- 连接建立 ----------------

    @classmethod
    def connect(cls, url, timeout=15.0, extra_headers=None):
        if not url.startswith("ws://"):
            raise WebSocketError("只支持明文 ws://（CDP 走本地回环端口）")
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port or 80)

        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        ws = cls(sock)
        ws._handshake(hostport, path, extra_headers)
        return ws

    def _handshake(self, hostport, path, extra_headers):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            "GET %s HTTP/1.1" % path,
            "Host: %s" % hostport,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
            "User-Agent: MyBrowser/0.1",
        ]
        for k, v in (extra_headers or {}).items():
            lines.append("%s: %s" % (k, v))
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))

        # 读响应头
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手期间对端关闭连接")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest

        status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101" not in status:
            raise WebSocketError("WebSocket 握手失败: %s" % status)

        # 校验 Sec-WebSocket-Accept
        expect = base64.b64encode(
            hashlib_sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode())
        ).decode("ascii")
        for raw in head.split(b"\r\n")[1:]:
            name, _, value = raw.decode("latin-1", "replace").partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                if value.strip() != expect:
                    raise WebSocketError("Sec-WebSocket-Accept 校验失败")
                break
        else:
            raise WebSocketError("响应缺少 Sec-WebSocket-Accept")

    # ---------------- 帧层 ----------------

    def _recv_exact(self, n):
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(max(4096, n - len(self._buf)))
            except socket.timeout:
                raise WebSocketError("接收超时")
            if not chunk:
                raise WebSocketError("连接已被对端关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
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
        """客户端发送的帧必须掩码（RFC 6455 §5.3）。"""
        length = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)  # FIN=1
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < (1 << 16):
            header.append(mask_bit | 126)
            header += struct.pack(">H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        with self._send_lock:
            if self._closed:
                raise WebSocketError("连接已关闭")
            self._sock.sendall(bytes(header) + masked)

    # ---------------- 应用层 API ----------------

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def send_binary(self, data):
        self._send_frame(0x2, data)

    def recv_text(self):
        """接收一条完整消息（自动重组分片、处理 ping）。"""
        chunks = []
        msg_op = None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:  # close
                self.close()
                raise WebSocketError("对端发送 close 帧")
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                msg_op = opcode
                chunks = [payload]
            elif opcode == 0x0:  # continuation
                chunks.append(payload)
            if fin:
                data = b"".join(chunks)
                if msg_op == 0x2:
                    raise WebSocketError("收到二进制帧，本客户端只处理文本")
                return data.decode("utf-8", "replace")

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(0x8, b"\x03\xe8")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    @property
    def closed(self):
        return self._closed


def hashlib_sha1(data):
    """本地引入 hashlib 的 sha1，避免模块级 import 顺序问题。"""
    import hashlib
    return hashlib.sha1(data).digest()
