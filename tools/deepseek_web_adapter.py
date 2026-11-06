# -*- coding: utf-8 -*-
r"""deepseek_web_adapter.py - 原生复刻 EAgent(yaogaochaole) 的 CDP 网页 DeepSeek 方案。

不依赖 EAgent.exe、不依赖任何 .ps1 脚本、不依赖第三方 Python 包（纯标准库）。
做的事和 EAgent 完全一样：
  1. 启动带 --remote-debugging-port=9222 的 Edge，打开 chat.deepseek.com
  2. 通过 CDP WebSocket 连上该页面
  3. 用 Runtime.evaluate 注入 JS，模拟真实用户输入（用原生 value setter 绕过 React）
  4. 模拟回车发送
  5. 每 0.3s 轮询最后一条 .ds-assistant-message-main-content，文本稳定即判定完成
  6. 把回复文本返回给调用方（intent-win 的 Agent）

接口（对应 plugins/yaogaochaole.qdplugin）：
    python deepseek_web_adapter.py <action> [message] [timeout]
    action = start | status | ask | new | last | stop
      start  : 启动调试浏览器（message 可作 URL，默认 https://chat.deepseek.com）
      status : 检查 9222 端口 / 页面 / 登录态
      ask    : message=问题文本或含问题文本的文件路径，timeout=最长等待秒数(默认150)
      new    : 打开新对话
      last   : 只读当前最后一条助手回复
      stop   : 关闭调试浏览器
输出：单行 UTF-8 JSON，status=ok|error。

说明：与 EAgent 一样，本工具通过 CDP 自动化操控网页版 DeepSeek，
该行为违反 DeepSeek 用户协议，仅供学习交流，请自行控制使用频率。
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = 9222
URL = "https://chat.deepseek.com"
# 固定在用户主目录下，保证 Bash / cmd / 服务进程看到的是同一个 profile（登录态可复用）
PROFILE = os.path.join(os.path.expanduser("~"), ".deepseek_web_profile")

# 强制不走系统代理，否则访问 127.0.0.1 可能被代理拦截
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---- 注入到页面里的 JS（选择器与 EAgent 的 2_发送并等待回复.ps1 保持一致）----
SEL_JS = r"""
var SEL = {
  assistant: ['.ds-assistant-message-main-content',
              '[class*="ds-assistant-message-main-content"]'],
  user: ['[class*="fbb737a4"]'],
  input: ['textarea'],
  sendBtn: ['div[role="button"].ds-button--primary.ds-button--circle',
            '[role="button"][aria-label*="发送"]',
            '[role="button"][aria-label*="Send"]']
};
function pick(key){
  var list = SEL[key] || [];
  if (typeof list === 'string') list = [list];
  var out = [], seen = [];
  for (var i = 0; i < list.length; i++) {
    var els = document.querySelectorAll(list[i]);
    for (var j = 0; j < els.length; j++) {
      if (seen.indexOf(els[j]) < 0) { seen.push(els[j]); out.push(els[j]); }
    }
  }
  return out;
}
"""

LAST_JS = SEL_JS + r"""
(function(){
  var a = pick('assistant');
  if (a.length === 0) return '';
  return (a[a.length - 1].textContent || '').trim();
})()
"""

PROBE_JS = SEL_JS + r"""
JSON.stringify({
  url: location.href,
  title: document.title,
  textarea: !!document.querySelector('textarea'),
  assistantCount: pick('assistant').length,
  loginHint: /登录|注册|Sign in|Log in|手机号/.test(
      (document.body.innerText || '').slice(0, 900))
})
"""

SEND_JS = SEL_JS + r"""
(function(){
  var ta = pick('input')[0];
  if (!ta) return 'NO_TA';
  ta.focus();
  ['keydown','keypress','keyup'].forEach(function(t){
    ta.dispatchEvent(new KeyboardEvent(t, {
      key:'Enter', code:'Enter', keyCode:13, which:13,
      bubbles:true, cancelable:true
    }));
  });
  return 'SENT';
})()
"""

NEW_CHAT_JS = SEL_JS + r"""
(function(){
  var els = document.querySelectorAll('a,button,[role="button"],div,span');
  for (var i = 0; i < els.length; i++) {
    var t = (els[i].innerText || '').trim();
    if (t === '开启新对话' || t === '新对话' || t === 'New chat' ||
        (/新对话/.test(t) && t.length < 12)) {
      try { els[i].click(); return 'CLICKED'; } catch (e) {}
    }
  }
  return 'NO_BTN';
})()
"""


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, default=str))


def ok(**kw):
    d = {"status": "ok"}
    d.update(kw)
    emit(d)


def err(msg, code=1, **kw):
    d = {"status": "error", "code": code, "message": msg}
    d.update(kw)
    emit(d)


def http_json(path, timeout=4):
    with _OPENER.open("http://127.0.0.1:%d%s" % (PORT, path),
                      timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def port_open():
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def find_edge():
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LOCALAPPDATA", "")
    cands = [
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(la, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(la, r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def find_target():
    pages = http_json("/json", timeout=5)
    for p in pages:
        if p.get("type") == "page" and "chat.deepseek.com" in (p.get("url") or ""):
            return p
    return None


class CDP:
    """极简 CDP WebSocket 客户端（标准库实现，零第三方依赖）。"""

    def __init__(self, ws_url, timeout=8):
        self.timeout = timeout
        rest = ws_url.split("://", 1)[-1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET /%s HTTP/1.1\r\n"
               "Host: %s\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n"
               % (path, hostport, key))
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WS handshake closed")
            buf += chunk
            if len(buf) > 65536:
                raise RuntimeError("WS handshake too large")
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("WS handshake failed: %s"
                               % head[:200].decode("latin1", "replace"))
        self._id = 0

    def _send_frame(self, data, opcode=0x1):
        payload = data.encode("utf-8") if isinstance(data, str) else data
        n = len(payload)
        h = bytearray([0x80 | opcode])
        if n < 126:
            h.append(0x80 | n)
        elif n < 65536:
            h.append(0x80 | 126)
            h += struct.pack(">H", n)
        else:
            h.append(0x80 | 127)
            h += struct.pack(">Q", n)
        mask = os.urandom(4)
        h += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(h) + masked)

    def _recv_exact(self, n):
        out = bytearray()
        while len(out) < n:
            if self.buf:
                take = min(n - len(out), len(self.buf))
                out += self.buf[:take]
                self.buf = self.buf[take:]
            else:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise RuntimeError("WS closed")
                self.buf += chunk
        return bytes(out)

    def _recv_frame(self):
        b1, b2 = self._recv_exact(2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(n)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv_json(self):
        parts = []
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x9:                    # ping -> pong
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0x8:
                raise RuntimeError("WS closed by peer")
            if opcode in (0x0, 0x1, 0x2):
                parts.append(payload)
                if fin:
                    return json.loads(b"".join(parts).decode("utf-8", "replace"))

    def call(self, method, params=None, timeout=None):
        self._id += 1
        mid = self._id
        self._send_frame(json.dumps(
            {"id": mid, "method": method, "params": params or {}},
            ensure_ascii=False))
        old = self.sock.gettimeout()
        limit = timeout or self.timeout
        self.sock.settimeout(limit)
        deadline = time.time() + limit
        try:
            while time.time() < deadline:
                try:
                    msg = self.recv_json()
                except (socket.timeout, TimeoutError):
                    return None
                if isinstance(msg, dict) and msg.get("id") == mid:
                    return msg
        finally:
            self.sock.settimeout(old)
        return None

    def evaluate(self, expr, timeout=None):
        try:
            r = self.call("Runtime.evaluate",
                          {"expression": expr, "returnByValue": True}, timeout)
        except (OSError, RuntimeError, ValueError):
            return None
        if not r:
            return None
        res = r.get("result") or {}
        if res.get("exceptionDetails"):
            return None
        return (res.get("result") or {}).get("value")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _read_message(message):
    """message 既可以是直接文本，也可以是已存在的文件路径（长文本推荐用文件）。"""
    if not message:
        return ""
    p = message.strip().strip('"')
    if len(p) < 260 and os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return message
    return message


def do_start(url):
    if port_open():
        try:
            v = http_json("/json/version", timeout=3)
            ok(action="start", running=True, browser=v.get("Browser"),
               profile=PROFILE,
               message="调试浏览器已在运行（端口 %d）。若还没登录，请在弹出的 Edge 里登录一次。" % PORT)
        except Exception as e:
            err("端口 %d 已被其他程序占用，无法确认是否为调试浏览器：%s" % (PORT, e),
                code=2)
        return
    edge = find_edge()
    if not edge:
        err("未找到 Edge/Chrome 主程序，请安装 Microsoft Edge。", code=3)
        return
    args = [
        edge,
        "--remote-debugging-port=%d" % PORT,
        "--user-data-dir=%s" % PROFILE,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--remote-allow-origins=*",
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-blink-features=AutomationControlled",
        "--test-type",
        "--app=%s" % (url or URL),
    ]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        err("启动浏览器失败：%s" % e, code=4)
        return
    for _ in range(40):
        if port_open():
            try:
                v = http_json("/json/version", timeout=2)
                ok(action="start", running=True, browser=v.get("Browser"),
                   profile=PROFILE,
                   message="已启动调试浏览器。请在弹出的 Edge 窗口里登录 DeepSeek 网页版一次，"
                           "登录态会保存在 profile 里，之后免登录。")
                return
            except Exception:
                pass
        time.sleep(0.5)
    err("浏览器已启动，但调试端口 %d 在 20 秒内未就绪。" % PORT, code=5)


def do_status():
    if not port_open():
        ok(action="status", port_ready=False, profile=PROFILE,
           message="调试浏览器未运行，请先调用 action=start。")
        return
    try:
        v = http_json("/json/version", timeout=3)
    except Exception as e:
        err("端口已开但 /json/version 不可达：%s" % e, code=6)
        return
    try:
        t = find_target()
    except Exception as e:
        err("读取页面列表失败：%s" % e, code=6)
        return
    info = {"action": "status", "port_ready": True,
            "browser": v.get("Browser"), "profile": PROFILE}
    if not t:
        info.update(page_found=False,
                    message="调试端口就绪，但未找到 DeepSeek 页面。可能窗口还在加载，稍后重试。")
        ok(**info)
        return
    info.update(page_found=True, url=t.get("url"))
    try:
        cdp = CDP(t["webSocketDebuggerUrl"], timeout=6)
        raw = cdp.evaluate(PROBE_JS, timeout=6)
        cdp.close()
        probe = json.loads(raw) if raw else {}
    except Exception as e:
        info.update(probe_error=str(e),
                    message="已连上页面，但探测失败（页面可能正在加载）。")
        ok(**info)
        return
    logged = bool(probe.get("textarea")) and not probe.get("loginHint")
    info.update(
        title=probe.get("title"),
        assistant_count=probe.get("assistantCount"),
        logged_in=logged,
        message=("已登录，可直接 action=ask 提问。" if logged else
                 "页面存在但未检测到输入框/疑似登录页，请在 Edge 窗口里手动登录 DeepSeek 网页版。"),
    )
    ok(**info)


def do_ask(message, timeout_s):
    msg = _read_message(message)
    if not msg.strip():
        err("message 为空。请传入要问的问题，或一个包含问题文本的本地文件路径。", code=7)
        return
    if not port_open():
        err("调试浏览器未运行，请先 action=start。", code=8)
        return
    try:
        t = find_target()
    except Exception as e:
        err("读取页面列表失败：%s" % e, code=9)
        return
    if not t:
        err("未找到 DeepSeek 页面（chat.deepseek.com）。请先 action=start 并确认页面已打开。",
            code=9)
        return
    try:
        cdp = CDP(t["webSocketDebuggerUrl"], timeout=10)
    except Exception as e:
        err("连接 CDP 失败：%s" % e, code=10)
        return
    try:
        cdp.call("Page.enable")
        cdp.call("Page.bringToFront")
        cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        before = str(cdp.evaluate(LAST_JS, timeout=6) or "").strip()

        inj = SEL_JS + r"""
(function(){
  var msg = %s;
  var ta = pick('input')[0];
  if (!ta) return JSON.stringify({ok:false, err:'no textarea'});
  ta.focus();
  var s = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value').set;
  s.call(ta, msg);
  ta.dispatchEvent(new Event('input', {bubbles:true}));
  ta.dispatchEvent(new Event('change', {bubbles:true}));
  return JSON.stringify({ok:true, len:ta.value.length});
})()
""" % json.dumps(msg, ensure_ascii=False)

        r = cdp.evaluate(inj, timeout=8)
        if not r or '"ok":true' not in str(r):
            err("注入消息失败（未找到输入框或页面未登录）。请先在 Edge 里登录 DeepSeek 网页版。"
                " raw=%s" % str(r)[:200], code=11)
            return
        time.sleep(0.3)
        r = cdp.evaluate(SEND_JS, timeout=8)
        if r != "SENT":
            err("发送失败（找不到输入框）。 raw=%s" % str(r)[:120], code=12)
            return

        deadline = time.time() + timeout_s
        prev = before
        stable = 0
        saw_new = False
        while time.time() < deadline:
            time.sleep(0.3)
            cur = cdp.evaluate(LAST_JS, timeout=6)
            if cur is None:
                continue
            cur = str(cur).strip()
            if not saw_new:
                if cur and cur != before:
                    saw_new = True
                    prev = cur
                continue
            if cur == prev:
                stable += 1
                if stable >= 6:                 # 约 1.8s 文本不再变化 -> 完成
                    ok(action="ask", reply=cur, chars=len(cur),
                       message="已取回 DeepSeek 回复。")
                    return
            else:
                stable = 0
                prev = cur
        if saw_new:
            ok(action="ask", reply=prev, chars=len(prev), partial=True,
               message="达到等待上限，返回已生成的内容（可能仍在生成，可用 action=last 再取）。")
        else:
            err("等待 %d 秒未检测到新回复。可能未登录、页面卡住或问题被风控拦截。"
                % timeout_s, code=13)
    finally:
        cdp.close()


def do_last():
    if not port_open():
        err("调试浏览器未运行，请先 action=start。", code=8)
        return
    t = find_target()
    if not t:
        err("未找到 DeepSeek 页面。", code=9)
        return
    try:
        cdp = CDP(t["webSocketDebuggerUrl"], timeout=8)
        text = str(cdp.evaluate(LAST_JS, timeout=6) or "").strip()
        cdp.close()
    except Exception as e:
        err("读取回复失败：%s" % e, code=10)
        return
    ok(action="last", reply=text, chars=len(text))


def do_new():
    if not port_open():
        err("调试浏览器未运行，请先 action=start。", code=8)
        return
    t = find_target()
    if not t:
        err("未找到 DeepSeek 页面。", code=9)
        return
    try:
        cdp = CDP(t["webSocketDebuggerUrl"], timeout=8)
        cdp.call("Page.enable")
        cdp.call("Page.navigate", {"url": URL})
        time.sleep(2)
        r = cdp.evaluate(NEW_CHAT_JS, timeout=6)
        cdp.close()
    except Exception as e:
        err("开新对话失败：%s" % e, code=10)
        return
    ok(action="new", clicked=(r == "CLICKED"),
       message="已导航到 DeepSeek 首页" + ("并点击了新对话按钮。" if r == "CLICKED" else
                                   "；未找到新对话按钮，请手动点一下页面上的『新对话』。"))


def do_stop():
    if not port_open():
        ok(action="stop", message="浏览器本来就没在运行。")
        return
    try:
        v = http_json("/json/version", timeout=3)
        cdp = CDP(v["webSocketDebuggerUrl"], timeout=5)
        cdp._send_frame(json.dumps({"id": 1, "method": "Browser.close"}))
        cdp.close()
    except Exception as e:
        err("关闭失败：%s" % e, code=14)
        return
    ok(action="stop", message="已发送关闭指令（Browser.close）。")


def main():
    action = (sys.argv[1].strip().lower() if len(sys.argv) > 1 else "status")
    message = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        timeout_s = int(sys.argv[3]) if len(sys.argv) > 3 else 150
    except (TypeError, ValueError):
        timeout_s = 150
    timeout_s = max(10, min(timeout_s, 300))
    try:
        if action == "start":
            do_start(message or URL)
        elif action == "status":
            do_status()
        elif action == "ask":
            do_ask(message, timeout_s)
        elif action == "new":
            do_new()
        elif action == "last":
            do_last()
        elif action == "stop":
            do_stop()
        else:
            err("未知 action=%s。可用：start | status | ask | new | last | stop" % action,
                code=15)
    except Exception as e:
        err("%s: %s" % (type(e).__name__, e), code=99, action=action)


if __name__ == "__main__":
    main()
