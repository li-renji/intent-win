# -*- coding: utf-8 -*-
"""自研 CDP（Chrome DevTools Protocol）客户端。

CDP 是浏览器引擎对外暴露的控制协议：JSON-RPC over WebSocket。
本模块提供：
  · 连接 / 断开
  · 同步命令调用（带 id 配对，自动重定向到正确的 session）
  · 事件订阅（按 method 匹配，可带通配符）
  · 扁平会话（flatten）支持：Target.attachToTarget 后所有命令自动带上 sessionId

所有命令都走同一个 WebSocket，用 id 配对，事件走同一个流，
所以必须有一个后台读取线程做分发。这是自研实现里最容易出并发 bug 的地方，
务必保留那把锁。
"""
import itertools
import json
import threading

from .ws import WebSocket, WebSocketError


class CDPError(Exception):
    """CDP 返回了 error 字段。"""


class CDPClient:
    def __init__(self, ws_url, timeout=30.0):
        self._ws = WebSocket.connect(ws_url, timeout=timeout)
        self._id = itertools.count(1)
        self._pending = {}          # id -> Event
        self._results = {}           # id -> (ok, payload)
        self._handlers = {}          # method -> [fn]
        self._any_handlers = []      # [fn(event_dict)]
        self._closed = False
        self._err = None
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ---------------- 连接生命周期 ----------------

    @classmethod
    def from_http_endpoint(cls, http_base, timeout=30.0):
        """从 http://127.0.0.1:port 拉 /json/version 拿到浏览器级 ws 地址。

        必须绕过 HTTP 代理：环境常设 HTTP_PROXY 指向本地代理，
        否则 127.0.0.1 的请求会被代理拦截返回 502。
        """
        import json
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(http_base.rstrip("/") + "/json/version", timeout=10) as r:
            info = json.loads(r.read().decode("utf-8"))
        return cls(info["webSocketDebuggerUrl"], timeout=timeout)

    @property
    def closed(self):
        return self._closed

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass
        for ev in list(self._pending.values()):
            ev.set()

    # ---------------- 读取分发线程 ----------------

    def _read_loop(self):
        try:
            while not self._closed:
                raw = self._ws.recv_text()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                mid = msg.get("id")
                if mid is not None:
                    ev = self._pending.pop(mid, None)
                    if ev is not None:
                        self._results[mid] = (True, msg)
                        ev.set()
                    continue

                method = msg.get("method")
                if not method:
                    continue
                with self._lock:
                    fns = list(self._handlers.get(method, ()))
                    anys = list(self._any_handlers)
                params = msg.get("params") or {}
                session_id = msg.get("sessionId")
                for fn in fns:
                    self._safe_call(fn, method, params, session_id)
                for fn in anys:
                    self._safe_call(fn, method, params, session_id)
        except WebSocketError as e:
            self._err = e
        except Exception:
            pass
        finally:
            self._closed = True
            for ev in list(self._pending.values()):
                ev.set()

    @staticmethod
    def _safe_call(fn, method, params, session_id):
        try:
            fn(method, params, session_id)
        except Exception:
            pass

    # ---------------- 命令 ----------------

    def send(self, method, params=None, session_id=None, timeout=30.0, retries=1):
        """发送命令并等待结果。返回 result dict。

        session_id 非空时命令会被路由到该 target（扁平会话模式）。
        """
        last_err = None
        for attempt in range(retries + 1):
            if self._closed:
                raise CDPError("CDP 连接已断开: %s" % (self._err or ""))
            mid = next(self._id)
            payload = {"id": mid, "method": method, "params": params or {}}
            if session_id:
                payload["sessionId"] = session_id

            ev = threading.Event()
            with self._lock:
                self._pending[mid] = ev
            try:
                self._ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                with self._lock:
                    self._pending.pop(mid, None)
                raise CDPError("发送失败: %s" % e)

            if not ev.wait(timeout):
                with self._lock:
                    self._pending.pop(mid, None)
                raise CDPError("命令超时: %s (%.1fs)" % (method, timeout))

            ok, res = self._results.pop(mid, (False, {}))
            if not ok:
                last_err = CDPError("命令失败: %s" % method)
                continue
            if "error" in res:
                err = res["error"]
                # 目标已关闭属于可重试的瞬时错误
                msg = err.get("message", "")
                if attempt < retries and ("Session" in msg or "Target" in msg or "closed" in msg):
                    last_err = CDPError("%s: %s" % (err.get("code"), msg))
                    continue
                raise CDPError("%s -> %s (%s)" % (method, msg, err.get("data", "")))
            return res.get("result", {})
        raise last_err or CDPError("命令失败: %s" % method)

    # ---------------- 事件订阅 ----------------

    def on(self, method, handler):
        """订阅指定 method 的事件。handler(method, params, session_id)。"""
        with self._lock:
            self._handlers.setdefault(method, []).append(handler)
        return handler

    def on_any(self, handler):
        """订阅所有事件。handler(method, params, session_id)。"""
        with self._lock:
            self._any_handlers.append(handler)
        return handler

    def off(self, method, handler):
        with self._lock:
            fns = self._handlers.get(method)
            if fns and handler in fns:
                fns.remove(handler)

    def wait_event(self, method, timeout=10.0, predicate=None):
        """阻塞等待某个事件到达，返回 params。用于导航完成之类的同步点。"""
        box = {}
        done = threading.Event()

        def _hit(m, params, sid):
            if predicate and not predicate(params):
                return
            box["p"] = params
            done.set()

        self.on(method, _hit)
        try:
            if not done.wait(timeout):
                raise CDPError("等待事件超时: %s" % method)
            return box.get("p")
        finally:
            self.off(method, _hit)

    # ---------------- 便捷封装 ----------------

    def evaluate(self, expression, session_id=None, await_promise=False, timeout=30.0):
        """在页面执行 JS，返回 JS 侧的值（自动解包 RemoteObject）。"""
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }, session_id=session_id, timeout=timeout)
        if res.get("exceptionDetails"):
            exc = res["exceptionDetails"]
            desc = (exc.get("exception") or {}).get("description") or exc.get("text")
            raise CDPError("页面 JS 异常: %s" % desc)
        return (res.get("result") or {}).get("value")
