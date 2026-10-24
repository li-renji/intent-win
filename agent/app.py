# -*- coding: utf-8 -*-
"""奇点 Agent 桌面应用壳（豆包式聊天软件形态）。

- 主窗口：聊天 UI（ui/index.html），JS ↔ Python 桥（pywebview js_api）
- 多会话：会话/ 目录持久化，可新建/切换/删除
- 设置：应用内可视化改 base_url / api_key / model，写回 配置.qdai 热生效
- 内置浏览器：WebView2 独立窗口（按钮唤起）
- 三端口引擎（6800/6801/6802）后台线程常驻 —— 与奇点OS对齐不变
"""
import asyncio
import json
import os
import sys
import threading
import time
import uuid

import webview

from .browser.webview2 import WebView2Adapter
from .config import active_llm, load_config, save_settings
from .history import SessionHistory
from .llm import chat as llm_chat
from .loop import AgentLoop
from .local_engine import LocalEngine
from .pc_bridge import PCBridge
from .prompts import build_system_prompt
from .servers import Context, serve_6800, serve_6801, serve_6802
from .tools import ToolBus


def _ui_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "index.html")


class Api:
    """暴露给前端 JS 的接口（pywebview js_api）。"""

    def __init__(self, cfg, bus, browser, history):
        self.cfg = cfg
        self.bus = bus
        self.browser = browser
        self.history = history
        self.agent = AgentLoop(cfg, bus, browser, history)
        self.current_id = None
        self.conv_dir = os.path.join(cfg["project_root"], "会话")
        os.makedirs(self.conv_dir, exist_ok=True)
        # 电脑文件桥（默认不启动；UI「📱 电脑文件桥」一键开启）
        token_file = os.path.join(cfg["project_root"], cfg.get("pc_bridge_token_file",
                                                              "pc_bridge.token"))
        self.bridge = PCBridge(port=cfg.get("pc_bridge_port", 8899),
                               root=cfg.get("pc_bridge_root") or None,
                               token_file=token_file)
        # 本地推理引擎（llama-server 子进程，按需启动，默认不自启）
        from .model_mgr import ModelManager
        self.mgr = ModelManager(cfg)
        self.engine = LocalEngine(cfg)

    # ================= 会话管理 =================

    def _conv_path(self, cid):
        return os.path.join(self.conv_dir, cid + ".json")

    def new_conversation(self):
        cid = uuid.uuid4().hex[:12]
        self._persist(cid, {"title": "新对话", "messages": [], "facts": []})
        self.current_id = cid
        self.history.set_facts([])
        return {"id": cid, "title": "新对话", "messages": []}

    def list_conversations(self):
        items = []
        for f in os.listdir(self.conv_dir):
            if not f.endswith(".json"):
                continue
            try:
                d = json.load(open(self._conv_path(f[:-5]), encoding="utf-8"))
                items.append({"id": f[:-5], "title": d.get("title", "新对话"),
                              "count": len(d.get("messages", [])),
                              "updated": d.get("updated", 0)})
            except Exception:
                continue
        items.sort(key=lambda x: -x["updated"])
        return items

    def open_conversation(self, cid):
        d = self._load(cid)
        if d is None:
            return {"ok": False, "error": "会话不存在"}
        self.current_id = cid
        self.history.set_facts(d.get("facts", []))
        return {"ok": True, "id": cid, "title": d.get("title", "新对话"),
                "messages": d.get("messages", [])}

    def delete_conversation(self, cid):
        try:
            os.remove(self._conv_path(cid))
        except OSError:
            pass
        if self.current_id == cid:
            self.current_id = None
            self.history.set_facts([])
        return {"ok": True, "list": self.list_conversations()}

    def _load(self, cid):
        p = self._conv_path(cid)
        if not os.path.exists(p):
            return None
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None

    def _persist(self, cid, d):
        d["updated"] = time.time()
        try:
            with open(self._conv_path(cid), "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ================= 设置 =================

    def get_settings(self):
        base, model, key = active_llm(self.cfg)
        return {
            "llm_mode": self.cfg["llm_mode"],
            "base_url": self.cfg["base_url"],
            "model": self.cfg["model"],
            "api_key": self.cfg.get("api_key", ""),
            "llm_base_url": self.cfg["llm_base_url"],
            "llm_model": self.cfg["llm_model"],
            "configured": bool(base) and "<你的API Key>" not in self.cfg.get("api_key", ""),
        }

    def save_settings(self, updates):
        allowed = {k: str(v) for k, v in (updates or {}).items()
                   if k in ("llm_mode", "base_url", "api_key", "model",
                            "llm_base_url", "llm_model") and str(v).strip()}
        save_settings(self.cfg, allowed)
        return {"ok": True, "configured": self.get_settings()["configured"]}

    # ================= 状态 =================

    def get_status(self):
        base, _, _ = active_llm(self.cfg)
        if self.browser.available():
            bt = "就绪"
        elif self.browser.error:
            bt = "未启用"
        else:
            bt = "未打开"
        return {
            "engine": self.cfg["llm_mode"],
            "llm": bool(base) and "<你的API Key>" not in self.cfg.get("api_key", ""),
            "browser": self.browser.available(),
            "browser_text": bt,
            "tools": ", ".join(self.bus.enabled()) or "无",
        }

    # ================= 聊天（流式） =================

    def send_message(self, text):
        threading.Thread(target=self._chat_stream, args=(str(text),), daemon=True).start()
        return {"ok": True}

    def _push(self, js, *args):
        try:
            webview.windows[0].evaluate_js(js % tuple(json.dumps(a, ensure_ascii=False) for a in args))
        except Exception:
            pass

    def _chat_stream(self, text):
        if not self.current_id or not os.path.exists(self._conv_path(self.current_id)):
            c = self.new_conversation()
            self._push('window.__onNewConv(%s)', c)
        cid = self.current_id
        d = self._load(cid) or {"title": "新对话", "messages": [], "facts": []}

        base, model, key = active_llm(self.cfg)
        if not base or "<你的API Key>" in key:
            self._push('window.__onError(%s)', "未配置 API Key：请点左下角「设置」填写（支持任意 OpenAI 兼容接口）")
            return
        system = build_system_prompt(self.bus.enabled())
        facts = self.history.inject()
        messages = [{"role": "system", "content": (system + ("\n" + facts if facts else ""))},
                    {"role": "user", "content": text}]

        agent_on = bool(self.cfg.get("agent_enabled", True))
        full = []
        tool_names = []

        if agent_on:
            # ---- agent 模式：多轮工具调用 ----
            def _run():
                nonlocal full, tool_names
                for kind, payload in self.agent.run(base, model, key, messages):
                    if kind == "text":
                        full.append(payload)
                        self._push('window.__onToken(%s)', payload)
                    elif kind == "text_reset":
                        # 文本协议降级：正文里的 [[TOOL: ...]] 已剥离，重置气泡
                        full = [payload]
                        self._push('window.__onTextReset(%s)', payload)
                    elif kind == "tool_start":
                        self._push('window.__onToolStart(%s)', payload)
                    elif kind == "tool_end":
                        tool_names.append(payload.get("name"))
                        self._push('window.__onToolEnd(%s)', payload)
                    elif kind == "notice":
                        # 非致命提示（如协议降级）：显示但不影响后续输出
                        self._push('window.__onNotice(%s)', str(payload))
                    elif kind == "done":
                        pass
                    elif kind == "error":
                        # 致命错误：结束本轮（payload 可能是 dict 或字符串）
                        msg = payload.get("msg") if isinstance(payload, dict) else str(payload)
                        self._push('window.__onError(%s)', str(msg))
                        return
            try:
                _run()
            except Exception as e:
                self._push('window.__onError(%s)', "Agent 循环异常: %s" % e)
                return
            reply = "".join(full)
        else:
            # ---- 原有一问一答 ----
            ok, out = llm_chat(base, model, key, messages, stream=True)
            if not ok:
                self._push('window.__onError(%s)', str(out))
                return
            try:
                for chunk in out:
                    full.append(chunk)
                    self._push('window.__onToken(%s)', chunk)
            except Exception as e:
                self._push('window.__onError(%s)', "流中断: %s" % e)
                return
            reply = "".join(full)

        # 持久化 + TurnFact 压缩
        d["messages"].append({"role": "user", "content": text})
        d["messages"].append({"role": "assistant", "content": reply})
        if d.get("title") in ("", "新对话") and d["messages"]:
            d["title"] = text[:20]
        self.history.add_turn(text, reply, tool_names)
        d["facts"] = self.history.get_facts()
        self._persist(cid, d)
        self._push('window.__onDone(%s)', d["title"])

    # ================= 电脑文件桥（手机 ⇄ 电脑） =================

    def bridge_status(self):
        return self.bridge.status()

    def bridge_start(self):
        try:
            self.bridge.start()
            return {"ok": True, "status": self.bridge.status()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def bridge_stop(self):
        self.bridge.stop()
        return {"ok": True, "status": self.bridge.status()}

    # ================= 本地推理引擎（内置模型） =================

    def engine_status(self):
        s = self.engine.status()
        ok, checks = self.engine.probe()
        s["checks"] = [{"item": n, "ok": g, "detail": d} for n, g, d in checks]
        return s

    def engine_start(self, model_path=None):
        ok, msg = self.engine.start(model_path)
        return {"ok": ok, "msg": msg, "status": self.engine.status()}

    def engine_stop(self):
        self.engine.stop()
        return {"ok": True, "status": self.engine.status()}

    def model_list(self):
        return self.mgr.summary()

    def model_activate(self, model_id):
        ok, payload = self.mgr.set_active(model_id)
        return {"ok": ok, "detail": payload if ok else payload}

    def model_recommend(self, ram_gb=0.0):
        m, why = self.mgr.recommend(float(ram_gb or 0))
        return {"model": m, "reason": why}

    # ================= 浏览器 =================

    def open_browser(self):
        if self.browser.available():
            return {"ok": True, "msg": "内置浏览器已打开。"}
        ok = self.browser.start()
        return {"ok": ok, "msg": "浏览器已打开。" if ok else (self.browser.error or "浏览器启动失败")}

    def browser_cmd(self, cmd):
        """浏览器文本协议直通（NAVIGATE/SNAPSHOT/CLICK/TYPE/…）。"""
        return {"ok": True, "result": self.browser.execute(cmd)}


def _start_servers(cfg, bus, browser, history):
    """三端口引擎后台常驻（独立事件循环线程）。

    Windows 注意：asyncio 默认的 IocpProactor 不支持跨线程唤醒，
    事件循环跑在后台线程时客户端连接会挂住（readline 永不返回）。
    这里显式改用 SelectorEventLoop，Windows/Linux 行为一致。
    """
    ctx = Context(cfg, bus, browser, history)

    def _run():
        loop = _new_loop()
        asyncio.set_event_loop(loop)

        async def _boot():
            # serve_6802 在 http_enabled=off 时返回 None，需过滤
            await asyncio.gather(serve_6800(ctx), serve_6801(ctx),
                                 serve_6802(ctx), return_exceptions=True)

        try:
            loop.run_until_complete(_boot())
        except Exception as e:
            print("[servers] 启动失败: %s" % e)
        loop.run_forever()

    threading.Thread(target=_run, daemon=True).start()


def _new_loop():
    """跨平台创建事件循环：Windows 用 Selector 策略（支持跨线程唤醒）。"""
    try:
        if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    return asyncio.new_event_loop()


def main():
    cfg = load_config()
    bus = ToolBus(cfg)
    browser = WebView2Adapter(cfg)
    bus.attach_browser(browser.execute)
    history = SessionHistory(cfg["history_facts_n"], cfg["summary_max_chars"])

    _start_servers(cfg, bus, browser, history)
    api = Api(cfg, bus, browser, history)

    # 配置开启则随应用自启（默认关：暴露全盘给局域网需用户知情）
    if cfg.get("pc_bridge_enabled"):
        try:
            api.bridge.start()
            print(api.bridge.banner())
        except Exception as e:
            print("[pc_bridge] 自启失败: %s" % e)

    # 配置开启则随应用自启：本地推理引擎（内置模型）
    if cfg.get("local_engine_autostart") and cfg.get("local_engine_binary"):
        ok_auto, msg_auto = api.engine_start()
        print("[本地引擎] " + ("就绪 %s" % api.engine.endpoint() if ok_auto
                               else "启动失败：" + msg_auto))

    webview.create_window(
        "奇点 Agent",
        _ui_path(),
        js_api=api,
        width=1080, height=760,
        min_size=(820, 560),
    )
    webview.start(gui="edgechromium")
    api.bridge.stop()   # 窗口关闭 → 停桥（关窗即断，手机立刻失去访问）


if __name__ == "__main__":
    main()
