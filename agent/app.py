# -*- coding: utf-8 -*-
"""intent 桌面应用壳（豆包式聊天软件形态）。

- 主窗口：聊天 UI（ui/index.html），JS ↔ Python 桥（pywebview js_api）
- 多会话：会话/ 目录持久化，可新建/切换/删除
- 设置：应用内可视化改 base_url / api_key / model，写回 配置.qdai 热生效
- 内置浏览器：WebView2 独立窗口（按钮唤起）
- 三端口引擎（6800/6801/6802）后台线程常驻 —— 与奇点OS对齐不变
"""
import asyncio
import base64
import json
import os
import sys
import threading
import time
import urllib.parse
import uuid

try:
    import webview
except ImportError:
    sys.stderr.write(
        "\n[启动失败] 缺少桌面窗口依赖 pywebview，无法打开主窗口。\n"
        "请在 D:\\INTENT-win 目录执行：\n"
        "    python -m pip install \"pywebview>=5.0,<7.0\"\n"
        "装好后重新双击桌面「奇点Agent」。\n"
    )
    sys.exit(2)

from .browser.webview2 import WebView2Adapter
from .config import active_llm, load_config, save_settings
from .history import SessionHistory
from .kb import KnowledgeBase
from .llm import chat as llm_chat
from .loop import AgentLoop
from .local_engine import LocalEngine, resolve_llm
from . import market
from .memory import MemoryStore, auto_capture as _auto_capture_memories
from .pc_bridge import PCBridge
from .prompts import build_system_prompt
from .servers import Context, serve_6800, serve_6801, serve_6802
from .tools import ToolBus


def _ui_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "index.html")


def _bg_path():
    """背景图持久化文件（ui/bg.json），存放当前选中的预设名或上传图片的 dataURL。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "bg.json")


class Api:
    """暴露给前端 JS 的接口（pywebview js_api）。"""

    def __init__(self, cfg, bus, browser, history):
        self.cfg = cfg
        self.bus = bus
        self.browser = browser
        self.history = history
        self.agent = AgentLoop(cfg, bus, browser, history)
        self.current_id = None
        # 窗口是否正在被拖动（拖动时整体模糊）
        self._dragging = False
        self.conv_dir = os.path.join(cfg["project_root"], "会话")
        os.makedirs(self.conv_dir, exist_ok=True)
        # 知识库（全机文件索引）：无索引/过期时后台线程扫描，不卡 UI
        self.kb = KnowledgeBase(cfg["project_root"])
        # 长期记忆（跨会话持久化）：结构化条目 + 自动捕获 + 按提问召回
        self.memory = MemoryStore(cfg["project_root"])
        # 电脑文件桥（默认不启动；UI「📱 电脑文件桥」一键开启）
        # kb 传入文件桥：手机端经 /kb/search 共用同一份知识库索引
        token_file = os.path.join(cfg["project_root"], cfg.get("pc_bridge_token_file",
                                                              "pc_bridge.token"))
        self.bridge = PCBridge(port=cfg.get("pc_bridge_port", 8899),
                               root=cfg.get("pc_bridge_root") or None,
                               token_file=token_file, kb=self.kb)
        # 本地推理引擎（llama-server 子进程，按需启动，默认不自启）
        from .model_mgr import ModelManager
        self.mgr = ModelManager(cfg)
        self.engine = LocalEngine(cfg)
        # 定时提醒：后台线程到点推送到当前对话
        from .reminders import ReminderManager
        self.reminders = ReminderManager(cfg["project_root"], push=self._push)
        # 文件改动监听：给「VS Code 风格编程面板」推实时文件内容
        from .filewatch import FileWatcher
        self._watch_roots = [cfg["project_root"]]
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        if os.path.isdir(desktop):
            self._watch_roots.append(desktop)
        self.watcher = FileWatcher(self._watch_roots, on_change=self._on_file_change)
        self.watcher.start()
        # 后台补齐档案里的真实上下文窗口（老档案没有 ctx，容量只能靠猜）
        try:
            threading.Thread(target=self._backfill_profile_ctx, daemon=True).start()
        except Exception:
            pass

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

    def rename_conversation(self, cid, title):
        """双击标题改名：写回会话文件的 title 字段并刷新 updated。"""
        d = self._load(cid)
        if d is None:
            return {"ok": False, "error": "会话不存在"}
        t = (title or "").strip() or "新对话"
        d["title"] = t[:80]
        self._persist(cid, d)
        return {"ok": True, "id": cid, "title": d["title"]}

    def _load(self, cid):
        p = self._conv_path(cid)
        if not os.path.exists(p):
            return None
        # 重试：上一进程残留 / 瞬时读锁可能导致偶发读取失败（表现为「启动看不到对话」）
        for _ in range(4):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                time.sleep(0.05)
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
            "profiles": self._load_profiles(),
            "theme": self.cfg.get("theme", "dark"),
            "workbench_enabled": str(self.cfg.get("workbench_enabled", True)).lower()
                               not in ("off", "false", "0", "no", ""),
            "glass_enabled": str(self.cfg.get("glass_enabled", True)).lower()
                             not in ("off", "false", "0", "no", ""),
            "glass_refract": str(self.cfg.get("glass_refract", True)).lower()
                             not in ("off", "false", "0", "no", ""),
            "bg_visibility": float(self.cfg.get("bg_visibility", 0.7)),
            "glass_blur": float(self.cfg.get("glass_blur", 12.0)),
            "bg_pos_x": float(self.cfg.get("bg_pos_x", 50.0)),
            "bg_pos_y": float(self.cfg.get("bg_pos_y", 50.0)),
            # 区域独立控制：任务栏 / 聊天区
            "side_glass": str(self.cfg.get("side_glass", True)).lower()
                          not in ("off", "false", "0", "no", ""),
            "side_opacity": float(self.cfg.get("side_opacity", -1.0)),
            "main_glass": str(self.cfg.get("main_glass", True)).lower()
                          not in ("off", "false", "0", "no", ""),
            "main_opacity": float(self.cfg.get("main_opacity", -1.0)),
            # 玻璃按组件独立开关 + 磨砂按组件独立（默认全部组件都开玻璃与磨砂）
            "glass_components": self._load_list("glass_components",
                ["side", "main", "top", "input", "modal", "browser"]),
            "frost_components": self._load_list("frost_components",
                ["side", "main", "top", "input", "modal", "browser"]),
        }

    def _load_profiles(self):
        raw = self.cfg.get("profiles", [])
        if isinstance(raw, list):
            return raw
        try:
            return json.loads(raw) if raw else []
        except Exception:
            return []

    def _load_list(self, key, default):
        raw = self.cfg.get(key, default)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                v = json.loads(raw)
                if isinstance(v, list):
                    return v
            except Exception:
                pass
        return list(default)

    def save_settings(self, updates):
        allowed = {k: str(v) for k, v in (updates or {}).items()
                   if k in ("llm_mode", "base_url", "api_key", "model",
                            "llm_base_url", "llm_model", "theme",
                            "workbench_enabled", "glass_enabled",
                            "glass_refract", "bg_visibility", "glass_blur",
                            "bg_pos_x", "bg_pos_y",
                            "side_glass", "side_opacity",
                            "main_glass", "main_opacity")
                   and str(v).strip()}
        # 玻璃 / 磨砂按组件独立开关（list[str] 需 JSON 序列化落盘）
        for lst_key in ("glass_components", "frost_components"):
            if isinstance(updates, dict) and lst_key in updates:
                v = updates[lst_key]
                if isinstance(v, (list, tuple)):
                    try:
                        save_settings(self.cfg, {lst_key: json.dumps(list(v), ensure_ascii=False)})
                    except Exception:
                        pass
        save_settings(self.cfg, allowed)
        # 多模型配置档案（list[dict]）单独落盘
        if isinstance(updates, dict) and "profiles" in updates:
            try:
                profs = updates["profiles"]
                if isinstance(profs, list):
                    self.cfg["profiles"] = profs
                    save_settings(self.cfg, {"profiles": json.dumps(profs, ensure_ascii=False)})
            except Exception:
                pass
        return {"ok": True, "configured": self.get_settings()["configured"]}

    # ---------- 模型切换（输入框内快速切换） ----------
    def model_switch(self, profile):
        """把某个模型档案应用到顶层配置（顶层字段即「当前激活」模型），热生效。"""
        try:
            p = profile or {}
            updates = {}
            for k in ("llm_mode", "base_url", "model", "api_key",
                     "llm_base_url", "llm_model"):
                if k in p and str(p[k]).strip():
                    updates[k] = str(p[k])
            if not updates:
                return {"ok": False, "error": "空的模型档案"}
            self.save_settings(updates)
            return {"ok": True, "settings": self.get_settings()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 云端 API 接入：验证 -> 选模型 -> 存档 ----------
    def api_probe(self, base_url, api_key):
        """第一步：验证接口与 Key，并尝试拉取该厂商提供的模型清单。

        返回 {"ok","key_ok","models","needs_manual","message","base_url"}：
            ok           —— Key 可用且拿到了模型清单
            needs_manual —— 厂商不提供清单（如自建网关 / 本地推理软件），
                           需用户手填模型名，再走 api_probe_model 验证
        """
        from .provider import check_endpoint
        try:
            return check_endpoint(base_url, api_key)
        except Exception as e:
            return {"ok": False, "key_ok": False, "models": [], "needs_manual": False,
                    "message": "验证失败：%s" % e, "base_url": (base_url or "").strip()}

    def api_probe_model(self, base_url, api_key, model):
        """第三步的兜底：用户手填模型名后，用一次极小的真实请求验证。"""
        from .provider import probe_model
        try:
            return probe_model(base_url, api_key, model)
        except Exception as e:
            return {"ok": False, "message": "验证失败：%s" % e}

    def api_add_profile(self, base_url, api_key, model, name=None, tier=None, ctx=None):
        """把验证通过的 API 存成模型档案并立即启用。

        同一 base_url + model 只存一条，重复添加是覆盖而非堆积。
        ctx：厂商声明的上下文窗口（tok），0/None=未知。
        """
        base_url = (base_url or "").strip().rstrip("/")
        api_key = (api_key or "").strip()
        model = (model or "").strip()
        if not base_url or not model:
            return {"ok": False, "error": "接口地址和模型名都不能为空"}
        profs = self._load_profiles()
        # 同址同模型 -> 覆盖更新（顺带更新 Key）
        hit = next((p for p in profs
                    if (p.get("base_url") or "").rstrip("/") == base_url
                    and (p.get("model") or "") == model), None)
        if hit is None and name and str(name).strip():
            # 同名档案 -> 覆盖更新：用于「更换模型」，保留档案名只换模型，不新建重复
            hit = next((p for p in profs
                        if (p.get("name") or "") == str(name).strip()), None)
        if hit is None:
            hit = {"llm_mode": "cloud", "base_url": base_url, "model": model}
            profs.append(hit)
        hit["llm_mode"] = "cloud"
        hit["base_url"] = base_url
        hit["model"] = model
        hit["api_key"] = api_key
        if tier:
            hit["tier"] = tier
        else:
            # 用户手填模型名（厂商没给清单）：按名字推断档位，好让菜单显示贵/便宜
            try:
                from .provider import tier_of
                hit["tier"] = tier_of(model)["tier"]
            except Exception:
                pass
        # 厂商声明的上下文窗口：记进档案，容量计算与超限预检都靠它
        try:
            n = int(ctx or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            hit["ctx"] = n
        elif hit.get("ctx"):
            pass          # 覆盖更新时保留原有的 ctx
        else:
            hit.pop("ctx", None)
        # 用户填了名字就用用户的；没填才自动生成「模型 @ 域名」
        if name and str(name).strip():
            hit["name"] = str(name).strip()
        elif not hit.get("name"):
            try:
                host = urllib.parse.urlsplit(base_url).hostname or base_url
            except Exception:
                host = base_url
            hit["name"] = "%s @ %s" % (model, host)
        self.cfg["profiles"] = profs
        save_settings(self.cfg, {"profiles": json.dumps(profs, ensure_ascii=False)})
        # 立即启用
        self.save_settings({"llm_mode": "cloud", "base_url": base_url,
                            "model": model, "api_key": api_key})
        return {"ok": True, "profile": hit, "settings": self.get_settings()}

    @staticmethod
    def _parse_manifest(path):
        d = {"name": os.path.basename(path), "desc": "", "trigger": "", "version": "",
             "id": os.path.splitext(os.path.basename(path))[0]}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("name:"):
                        d["name"] = s[5:].strip()
                    elif s.startswith("id:"):
                        d["id"] = s[3:].strip() or d["id"]
                    elif s.startswith("desc:"):
                        d["desc"] = s[5:].strip()
                    elif s.startswith("trigger:"):
                        d["trigger"] = s[8:].strip()
                    elif s.startswith("version:"):
                        d["version"] = s[8:].strip()
                    elif s.startswith("#") or not s:
                        continue
                    else:
                        # 头部块结束后遇到非注释正文，停止读取
                        if d["desc"]:
                            break
        except Exception:
            pass
        return d

    def list_skills(self):
        """列出已安装的技能（skills/*.qdskill）。"""
        d = os.path.join(self.cfg["project_root"], "skills")
        out = []
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".qdskill"):
                    out.append(self._parse_manifest(os.path.join(d, f)))
        return {"ok": True, "items": out}

    def list_plugins(self):
        """列出已安装的插件（plugins/*.qdplugin）。"""
        d = os.path.join(self.cfg["project_root"], "plugins")
        out = []
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".qdplugin"):
                    out.append(self._parse_manifest(os.path.join(d, f)))
        return {"ok": True, "items": out}

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
            "ctx": self._actual_ctx(),
            # 固定开销：system prompt + 事实摘要 + 工具 schema 的 token 估算。
            # 前端用它做「实时估算」的地基，否则只数气泡文本会严重低估。
            "ctx_base": self._ctx_base_tokens(),
        }

    def _ctx_base_tokens(self):
        """每轮固定注入的上下文开销（tok）：system prompt + 事实 + 工具 schema。"""
        try:
            from . import econ
            system = build_system_prompt(
                self.bus.enabled(),
                skills_dir=os.path.join(self.cfg["project_root"], "skills"))
            facts = self.history.inject()
            schemas = self.bus.openai_schemas()
            n = econ.estimate_tokens(system) + econ.estimate_tokens(facts or "")
            if schemas:
                n += econ.estimate_tokens(json.dumps(schemas, ensure_ascii=False))
            return int(n)
        except Exception:
            return 0

    def _backfill_profile_ctx(self, force=False):
        """给已有档案补上厂商声明的上下文窗口（context_length）。

        老档案是在「厂商不给 context_length」之前加的，档案里没有 ctx 字段，
        容量只能靠猜——这正是「显示 0% 却一发送就报超限」的根因之一。
        这里按当前激活模型去厂商拉一次清单，把 ctx 写回档案。
        force=True 时无视已有值强制重探（用户手动点「重新探测容量」）。
        """
        base = (self.cfg.get("base_url") or "").strip()
        model = (self.cfg.get("model") or "").strip()
        key = (self.cfg.get("api_key") or "").strip()
        if not base or not model or not key:
            return
        profs = self._load_profiles()
        hit = next((p for p in profs if str(p.get("model") or "") == model
                    and str(p.get("base_url") or "").rstrip("/") == base.rstrip("/")), None)
        if hit is None:
            return
        if int(hit.get("ctx") or 0) > 0 and not force:
            return                      # 已有真实值，不动
        try:
            from .provider import check_endpoint
            r = check_endpoint(base, key)
            if not r.get("ok"):
                return
            ctx = 0
            for m in r.get("models") or []:
                if str(m.get("id")) == model:
                    ctx = int(m.get("ctx") or 0)
                    break
            if ctx <= 0:
                return
            hit["ctx"] = ctx
            self.cfg["profiles"] = profs
            save_settings(self.cfg, {"profiles": json.dumps(profs, ensure_ascii=False)})
        except Exception:
            pass

    def _actual_ctx(self):
        """当前模型上下文窗口（tok）。

        本地引擎模式：读引擎实测值；
        云端模式：读档案里厂商给的 context_length / 型号查表 / 保守默认。
        （本地引擎的窗口不能拿来给云端模型用，否则容量显示会小得离谱。）
        """
        from .config import model_ctx_limit
        if str(self.cfg.get("llm_mode", "cloud")).lower() == "local":
            try:
                st = self.engine.status()
                n = int(st.get("ctx_size") or 0)
                if n > 0:
                    return n
            except Exception:
                pass
        return model_ctx_limit(self.cfg)

    # ================= 子 Agent（spawn） =================
    def _spawn_runner(self, task, tools_str):
        """主 Agent 分裂出的子 Agent：独立上下文跑自己的小循环，只把结论回给主 Agent。"""
        ep = getattr(self.bus, "_current_endpoint", None)
        if not ep or not ep[0]:
            return "未配置模型端点，子 Agent 无法运行。"
        base, model, key = ep
        from .loop import AgentLoop
        from .history import SessionHistory
        sub = AgentLoop(self.cfg, self.bus, self.browser, SessionHistory())
        sub.max_rounds = int(self.cfg.get("spawn_max_rounds", 6))
        sub.max_seconds = 0
        msgs = [
            {"role": "system", "content":
             "你是子 Agent，只完成下面这一个子任务，独立调用工具；完成后用 2-4 句话给出结论与关键结果/文件路径，不要寒暄、不要复述任务。"},
            {"role": "user", "content": task},
        ]
        out = []
        for kind, payload in sub.run(base, model, key, msgs):
            if kind == "text":
                out.append(payload)
            elif kind == "tool_start":
                self._push('window.__onToolStart(%s)', payload)
            elif kind == "tool_end":
                self._push('window.__onToolEnd(%s)', payload)
            elif kind == "done":
                out.append(payload.get("content", ""))
        result = "".join(out).strip()
        return result[-1500:] if result else "（子 Agent 未产出结论）"

    # ================= 聊天（流式） =================

    def send_message(self, text, attachments=None):
        threading.Thread(target=self._chat_stream, args=(str(text), attachments), daemon=True).start()
        return {"ok": True}

    def _push(self, js, *args):
        try:
            webview.windows[0].evaluate_js(js % tuple(json.dumps(a, ensure_ascii=False) for a in args))
        except Exception:
            pass

    def _on_file_change(self, path, content):
        """文件改动 → 推给前端的编程面板（实时显示 AI 正在改的文件）。"""
        if not path:
            return
        payload = {
            "path": path,
            "name": os.path.basename(path),
            "dir": os.path.dirname(path),
            "content": content or "",
            "ts": time.time(),
        }
        self._push('window.__onFileChange(%s)', payload)

    def ide_watch_add(self, path):
        """把某个目录加入文件监听（AI 在别处工作时的实时改动也能看到）。"""
        if not path:
            return {"ok": False, "error": "缺少路径"}
        try:
            self.watcher.add_root(path)
            return {"ok": True, "roots": self.watcher.roots_list()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _chat_stream(self, text, attachments=None):
        if not self.current_id or not os.path.exists(self._conv_path(self.current_id)):
            c = self.new_conversation()
            self._push('window.__onNewConv(%s)', c)
        cid = self.current_id
        d = self._load(cid) or {"title": "新对话", "messages": [], "facts": []}

        base, model, key, source = resolve_llm(self.cfg, self.engine)
        if not base:
            self._push('window.__onError(%s)', "未配置可用端点：本地引擎未运行，也未配置云端接口")
            return
        if source != "local-engine" and (not key or "<你的API Key>" in key):
            self._push('window.__onError(%s)', "未配置 API Key：请点左下角「设置」填写（支持任意 OpenAI 兼容接口）")
            return
        self.bus._current_endpoint = (base, model, key)  # 供 spawn 子 Agent 复用
        system = build_system_prompt(
            self.bus.enabled(),
            skills_dir=os.path.join(self.cfg["project_root"], "skills"))
        facts = self.history.inject()
        # 长期记忆：先按本轮提问召回相关记忆，再把"值得记住"的语句自动入库
        mem_block = ""
        try:
            mem_block = self.memory.inject(text)
        except Exception:
            mem_block = ""
        try:
            for body, kind in _auto_capture_memories(text):
                self.memory.add(body, kind=kind, source="auto")
        except Exception:
            pass
        parts = [system]
        if facts:
            parts.append(facts)
        if mem_block:
            parts.append(mem_block)

        # 附件：图片拼成多模态（OpenAI 兼容 image_url），文件只带元信息文本
        atts = attachments or []
        user_content = text or ""
        if atts:
            cparts = []
            if text:
                cparts.append({"type": "text", "text": text})
            for a in atts:
                if a.get("kind") == "image" and a.get("data"):
                    cparts.append({"type": "image_url", "image_url": {"url": a["data"]}})
                else:
                    meta = a.get("name", "附件")
                    if a.get("path"):
                        meta += "（路径：%s）" % a["path"]
                    cparts.append({"type": "text", "text": "[附件] %s" % meta})
            if len(cparts) == 1 and cparts[0]["type"] == "text":
                user_content = cparts[0]["text"]
            else:
                user_content = cparts

        messages = [{"role": "system", "content": "\n".join(parts)},
                    {"role": "user", "content": user_content}]

        # 发请求前先给前端一份「上下文快照」：容量 + 本次实际占用估算。
        # 否则前端只能数气泡文本，会显示 0%，然后上游才报「上下文超了」。
        try:
            from . import econ
            cap = self._actual_ctx()
            used = econ.messages_tokens2(messages)
            self._push('window.__onCtx(%s)', {"capacity": cap, "used": used,
                                              "pct": round(used * 100.0 / cap, 1) if cap else 0})
        except Exception:
            pass

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
                    elif kind == "usage":
                        # token 用量（上下文用量条）
                        self._push('window.__onUsage(%s)', payload)
                    elif kind == "model_call":
                        # 可视化：本轮调用了哪个模型（CodeBuddy 风格轨迹）
                        self._push('window.__onModelCall(%s)', payload)
                    elif kind == "thinking":
                        # 思考过程 / 深度思考：逐块透传，前端折叠展示
                        self._push('window.__onThinking(%s)', payload)
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

        # 持久化 + TurnFact 压缩（图片不落库，只记文件名，避免 dataURL 撑爆历史）
        if isinstance(user_content, list):
            note = text or ""
            for a in atts:
                note += ("\n[附件] " + a.get("name", "附件"))
            d["messages"].append({"role": "user", "content": note})
        else:
            d["messages"].append({"role": "user", "content": text})
        d["messages"].append({"role": "assistant", "content": reply})
        if d.get("title") in ("", "新对话") and d["messages"]:
            d["title"] = (text or (atts[0].get("name", "") if atts else ""))[:20]
        self.history.add_turn(text, reply, tool_names)
        d["facts"] = self.history.get_facts()
        self._persist(cid, d)
        self._push('window.__onDone(%s)', d["title"])

    # ================= 工作台快捷入口 =================

    # ================= 窗口控制：原生标题栏染色（跟随主题） =================
    def win_set_dark(self, dark):
        """切换主题时同步把原生标题栏染深/染浅。"""
        try:
            _set_titlebar_dark(bool(dark))
        except Exception:
            pass
        return {"ok": True}

    def set_glass(self, on):
        """玻璃开关：仅同步原生标题栏深浅色（整窗亚克力/窗体透明已移除，玻璃质感由前端 CSS 承担）。"""
        print("[玻璃] set_glass(%r) 主题=%s" % (bool(on), self.cfg.get("theme", "dark")), flush=True)
        try:
            hwnd = getattr(self, "_hwnd", 0) or _find_hwnd("intent")
            _apply_dwm_glass(hwnd, self.cfg.get("theme", "dark"), bool(on))
        except Exception as e:
            print("[玻璃] set_glass 失败: %s" % e, flush=True)
        return {"ok": True}

    # ================= 背景图片（可替换 + 持久化） =================

    def get_background(self):
        """返回当前背景：预设名（preset:dark/light/sunset）或上传图片的 dataURL。"""
        default = {"value": "preset:dark"}
        try:
            p = _bg_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "value" in data:
                    return {"ok": True, "value": data["value"]}
        except Exception as e:
            print("[背景] 读取失败: %s" % e, flush=True)
        return {"ok": True, "value": default["value"]}

    def set_background(self, value):
        """保存背景选择：preset:* 或 data:image/* 的 dataURL。落盘 ui/bg.json。"""
        if not isinstance(value, str) or not (value.startswith("preset:") or value.startswith("data:image")):
            return {"ok": False, "error": "invalid background value"}
        try:
            with open(_bg_path(), "w", encoding="utf-8") as f:
                json.dump({"value": value}, f)
            return {"ok": True, "value": value}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def pick_background(self):
        """系统选图框选本地图片 → 转 dataURL 持久化 → 返回供前端直接应用。"""
        try:
            import clr
            clr.AddReference('System.Windows.Forms')
            clr.AddReference('System.Drawing')
            from System.Windows.Forms import OpenFileDialog, DialogResult
            dlg = OpenFileDialog()
            dlg.Filter = "图片文件 (*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.gif)|*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.gif"
            dlg.Title = "选择背景图片"
            if dlg.ShowDialog() != DialogResult.OK:
                return {"ok": False, "cancelled": True}
            path = str(dlg.FileName)
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "bmp": "image/bmp", "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/png")
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            data_url = "data:%s;base64,%s" % (mime, b64)
            self.set_background(data_url)
            return {"ok": True, "value": data_url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_bg_visibility(self, v):
        """背景可见度（0~1）：越大图片越清晰可见。落盘配置，下次启动自动恢复。"""
        try:
            v = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            v = 1.0
        self.save_settings({"bg_visibility": v})
        return {"ok": True, "value": v}

    def set_glass_blur(self, px):
        """玻璃磨砂度（px，0~28）：越大越磨砂、背后背景越模糊。落盘配置，下次启动自动恢复。"""
        try:
            px = max(0.0, min(28.0, float(px)))
        except (TypeError, ValueError):
            px = 12.0
        self.save_settings({"glass_blur": px})
        return {"ok": True, "value": px}

    # ---------- 区域独立控制：任务栏(side) / 聊天区(main) ----------
    _REGIONS = {"side": "side", "main": "main"}

    def set_region_opacity(self, region, v):
        """区域底色浓度（0~1）：0=完全透出背景，1=完全不透明。side/main 各存各的。"""
        key = self._REGIONS.get(str(region or "").strip().lower())
        if not key:
            return {"ok": False, "error": "未知区域：%s（可用 side / main）" % region}
        try:
            val = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            val = -1.0        # 非法输入 → 回到「跟随主题默认」
        self.save_settings({key + "_opacity": val})
        return {"ok": True, "region": key, "value": val}

    def set_region_glass(self, region, on):
        """区域的玻璃（磨砂模糊）开关：side/main 各管各的，互不影响。"""
        key = self._REGIONS.get(str(region or "").strip().lower())
        if not key:
            return {"ok": False, "error": "未知区域：%s（可用 side / main）" % region}
        flag = str(on).strip().lower() not in ("off", "false", "0", "no", "")
        self.save_settings({key + "_glass": "1" if flag else "0"})
        return {"ok": True, "region": key, "value": flag}

    def set_bg_position(self, x, y):
        """背景画面焦点（0~100 百分比）：长图时把焦点上移，只展示图片上半部分。

        x=水平焦点（左0/中50/右100），y=垂直焦点（上0/中50/下100）。
        """
        try:
            x = max(0.0, min(100.0, float(x)))
        except (TypeError, ValueError):
            x = 50.0
        try:
            y = max(0.0, min(100.0, float(y)))
        except (TypeError, ValueError):
            y = 50.0
        self.save_settings({"bg_pos_x": x, "bg_pos_y": y})
        return {"ok": True, "x": x, "y": y}

    def win_is_dragging(self):
        """窗口是否正在被拖动（后端轮询 GetWindowRect 得出），前端据此开关模糊。"""
        return {"dragging": getattr(self, "_dragging", False) is True}

    # ================= 长期记忆 =================

    def mem_list(self, kind=None, limit=200):
        """列出长期记忆（界面面板用）。"""
        try:
            items = self.memory.list(kind=kind, limit=limit)
            return {"ok": True, "items": items, "stats": self.memory.stats()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mem_add(self, text, kind="fact", tags=None):
        """手动写入一条长期记忆。"""
        try:
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
            ok, msg = self.memory.add(text, kind=kind, tags=tags, source="manual")
            return {"ok": ok, "message": msg, "stats": self.memory.stats()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mem_remove(self, mid):
        """删除一条长期记忆。"""
        try:
            ok, msg = self.memory.remove(mid)
            return {"ok": ok, "message": msg, "stats": self.memory.stats()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mem_search(self, kw, limit=50):
        """按关键词检索长期记忆。"""
        try:
            items = self.memory.search(kw, limit=limit)
            return {"ok": True, "items": items}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mem_clear(self, kind=None):
        """清空长期记忆（kind 为空则全清）。"""
        try:
            n = self.memory.clear(kind=kind)
            return {"ok": True, "removed": n, "stats": self.memory.stats()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ================= 知识库 =================

    def kb_ingest(self, paths=None):
        """把文档正文入库（paths 为空则自动挑选候选文档）。"""
        try:
            if isinstance(paths, str):
                paths = [p.strip() for p in paths.replace("；", ";").split(";") if p.strip()]
            if not paths:
                paths = None
            r = self.kb.ingest(paths)
            return {"ok": True, "result": r, "content": self.kb.content_stats()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def kb_grep(self, kw, limit=8):
        """在已入库文档正文中检索。"""
        try:
            hits = self.kb.grep(kw, limit=limit)
            return {"ok": True, "hits": hits}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def kb_ingest_path(self):
        """让用户选文档（可多选），正文入库（界面「导入文档」按钮）。"""
        try:
            import clr
            clr.AddReference('System.Windows.Forms')
            from System.Windows.Forms import OpenFileDialog, DialogResult
            dlg = OpenFileDialog()
            dlg.Multiselect = True
            dlg.Filter = ("可索引文档 (*.txt;*.md;*.csv;*.json;*.xml;*.yaml;*.yml;"
                          "*.py;*.js;*.ts;*.html;*.css;*.log;*.sql)|"
                          "*.txt;*.md;*.markdown;*.csv;*.json;*.xml;*.yaml;*.yml;"
                          "*.py;*.js;*.ts;*.html;*.css;*.log;*.sql|所有文件 (*.*)|*.*")
            dlg.Title = "选择要加入知识库的文档（可多选）"
            if dlg.ShowDialog() != DialogResult.OK:
                return {"ok": True, "cancelled": True}
            paths = [str(p) for p in dlg.FileNames]
            res = self.kb.ingest(paths)
            return {"ok": True, "paths": paths, "result": res,
                    "content": self.kb.content_stats()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_workbench(self):
        """打开设备统一管理工作台（端口 3000）。

        已在运行就直接开页面；未运行则用 Agent 自己的 python 拉起
        workbench/server/app.py（纯标准库实现，不依赖 uvicorn/fastapi）。
        """
        import subprocess as _subprocess
        import time as _time
        import webbrowser

        root = os.path.join(self.cfg["project_root"], "workbench")
        script = os.path.join(root, "server", "app.py")
        if not os.path.exists(script):
            return {"ok": False, "error": "未找到 workbench/server/app.py"}
        url = "http://localhost:3000"
        started = False
        if not _port_alive("127.0.0.1", 3000):
            logf = _subprocess.DEVNULL
            try:
                logf = open(os.path.join(root, "panel.log"), "a", encoding="utf-8")
            except Exception:
                pass
            flags = getattr(_subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                _subprocess.Popen([sys.executable, script], cwd=root,
                                  creationflags=flags, stdout=logf, stderr=logf)
            except Exception as e:
                return {"ok": False, "error": "启动工作台失败: %s" % e}
            started = True
            for _ in range(40):          # 最多等 8 秒
                if _port_alive("127.0.0.1", 3000):
                    break
                _time.sleep(0.2)
        if not _port_alive("127.0.0.1", 3000):
            return {"ok": False, "error": "工作台启动超时，请查看 workbench/panel.log"}
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return {"ok": True, "msg": ("工作台已启动：" if started else "工作台已在运行：") + url}

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

    # ================= 知识库 + 记忆（设置页查看） =================

    def kb_status(self):
        """知识库状态：文件名索引 + 正文（内容）索引。"""
        st = self.kb.stats()
        try:
            st["content"] = self.kb.content_stats()
        except Exception:
            st["content"] = {"docs": 0, "chunks": 0, "chars": 0, "terms": 0}
        return st

    def kb_scan(self):
        """立即重建索引（后台线程），返回是否已启动。"""
        started = self.kb.scan_async()
        return {"ok": True, "started": started, "status": self.kb.stats()}

    def kb_search(self, kw, limit=15):
        return {"ok": True, "hits": self.kb.search(kw, limit=limit)}

    def memory_view(self, limit=30):
        """读取长期记忆（结构化条目），供界面查看。"""
        try:
            items = self.memory.list(limit=limit)
            from .memory import KIND_LABEL
            lines = ["[%s] %s" % (KIND_LABEL.get(i.get("kind"), "事实"), i.get("text", ""))
                     for i in items]
            return {"ok": True, "lines": lines, "items": items,
                    "total": len(self.memory.list(limit=1000)),
                    "path": self.memory.path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ================= 上下文自主调试（ctx） =================

    @staticmethod
    def _est_tokens(text):
        """粗略 token 估算：汉字约 1 字≈1 tok，其余约 4 字符≈1 tok。"""
        if not text:
            return 0
        han = sum(1 for ch in text if ord(ch) > 0x2E7F)
        return int(han + (len(text) - han) / 4)

    def _build_system_text(self):
        """构造当前实际注入的 system 提示（与 _chat_stream/chat_once 同源）。"""
        system = build_system_prompt(
            self.bus.enabled(),
            skills_dir=os.path.join(self.cfg["project_root"], "skills"))
        facts = self.history.inject()
        return system + ("\n" + facts if facts else "")

    def _ctx_source(self):
        """当前容量是怎么来的：manual=手动指定 / vendor=厂商声明 / table=型号查表 / default=保守默认。"""
        if int(self.cfg.get("llm_ctx", 0) or 0) > 0:
            return "manual"
        from .config import _profile_ctx, MODEL_CTX_LIMITS
        if str(self.cfg.get("llm_mode", "cloud")).lower() != "local":
            if _profile_ctx(self.cfg) > 0:
                return "vendor"
        if (self.cfg.get("model") or "") in MODEL_CTX_LIMITS:
            return "table"
        return "default"

    def _ctx_snapshot(self):
        """上下文调试快照：事实、消息条数、估算占用、容量。"""
        facts = self.history.get_facts()
        facts_chars = sum(len(f) for f in facts)
        d = self._load(self.current_id) if self.current_id else None
        msgs = (d or {}).get("messages", []) or []
        sys_text = self._build_system_text()
        sys_chars = len(sys_text)
        capacity = self._actual_ctx()
        estimated = self._est_tokens(sys_text)
        return {
            "ok": True,
            "cid": self.current_id,
            "title": (d or {}).get("title", "新对话"),
            "model": self.cfg.get("model", ""),
            "capacity": capacity,
            "ctx_source": self._ctx_source(),
            "estimated": estimated,
            "ratio": round(estimated / capacity * 100, 1) if capacity else 0,
            "sys_chars": sys_chars,
            "facts": facts,
            "facts_n": len(facts),
            "facts_chars": facts_chars,
            "facts_limit": self.cfg.get("history_facts_n", 10),
            "summary_max_chars": self.cfg.get("summary_max_chars", 2000),
            "msg_count": len(msgs),
            "llm_ctx": self.cfg.get("llm_ctx", 0),
        }

    def _persist_current_facts(self):
        """把当前 history 事实写回会话文件（跨重启保留）。"""
        if not self.current_id:
            return
        d = self._load(self.current_id)
        if d is None:
            return
        d["facts"] = self.history.get_facts()
        self._persist(self.current_id, d)

    def _write_config_key(self, key, value):
        """把单个配置键写回 配置.qdai（EDITABLE_KEYS 之外的键专用，只改一行）。"""
        path = os.path.join(self.cfg["project_root"], "配置.qdai")
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            return
        out, found = [], False
        for line in lines:
            s = line.strip()
            k = s.partition("=")[0].strip() if ("=" in s and not s.startswith("#")) else None
            if k == key:
                out.append("%s=%s" % (key, value))
                found = True
            else:
                out.append(line)
        if not found:
            out.append("%s=%s" % (key, value))
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(out) + "\n")
        except OSError:
            pass

    def _ctx_apply(self, patch):
        """执行一次上下文调试操作（UI 与 ctx 工具共用），返回最新快照。"""
        patch = patch or {}
        op = str(patch.get("op", "inspect")).lower()
        if op in ("facts_clear", "reset"):
            self.history.set_facts([])
            self._persist_current_facts()
        elif op == "facts_add":
            text = str(patch.get("text", "")).strip()
            if text:
                self.history.set_facts(self.history.get_facts() + [text[:160]])
                self._persist_current_facts()
        elif op == "facts_remove":
            try:
                i = int(patch.get("index", -1))
                facts = self.history.get_facts()
                if 0 <= i < len(facts):
                    facts.pop(i)
                    self.history.set_facts(facts)
                    self._persist_current_facts()
            except (TypeError, ValueError):
                pass
        elif op == "compact":
            self._compact_current()
        elif op == "limit":
            try:
                n = int(patch.get("limit", 0))
                n = max(0, min(n, 1048576))
            except (TypeError, ValueError):
                n = 0
            self.cfg["llm_ctx"] = n
            self._write_config_key("llm_ctx", n)
        elif op == "detect":
            # 重新向厂商探测当前模型的真实上下文窗口
            self._backfill_profile_ctx(force=True)
        return self._ctx_snapshot()

    def _compact_current(self):
        """压缩当前会话：事实只留最近 5 条；messages 折叠早期历史、保留最近 20 条。"""
        facts = self.history.get_facts()
        dropped = facts[:-5] if len(facts) > 5 else []
        if dropped:
            self.history.set_facts(facts[-5:])
        cid = self.current_id
        d = self._load(cid) if cid else None
        if d is None:
            return
        msgs = d.get("messages", []) or []
        if len(msgs) > 20:
            drop, kept = msgs[:-20], msgs[-20:]
            lines = []
            for m in drop:
                role = "用户" if m.get("role") == "user" else "助手"
                lines.append(role + ": " + str(m.get("content") or "")[:100])
            if dropped:
                lines.append("已清事实: " + "; ".join(str(x)[:60] for x in dropped[:3]))
            kept.insert(0, {"role": "user",
                            "content": "（历史已压缩）早期对话摘要：\n" + "\n".join(lines[:30])})
            d["messages"] = kept
        d["facts"] = self.history.get_facts()
        self._persist(cid, d)

    def ctx_inspect(self):
        """设置弹窗「上下文」面板：只读快照。"""
        return self._ctx_snapshot()

    def ctx_update(self, patch):
        """设置弹窗「上下文」面板：执行操作（facts 增删/清空/压缩/容量）。"""
        return self._ctx_apply(patch or {})

    def _ctx_handler(self, op, args):
        """ctx 工具回调：Agent 自主查看/调整上下文（纯文本给模型）。"""
        op = str(op or "inspect").strip() or "inspect"
        args = args or {}
        if op == "inspect":
            s = self._ctx_snapshot()
            lines = [
                "上下文状态（intent）",
                "- 模型: %s" % (s["model"] or "未配置"),
                "- 容量: %d tok（llm_ctx=%d，0=自动查表）" % (s["capacity"], s["llm_ctx"]),
                "- 估算占用: %d tok（%.1f%%）" % (s["estimated"], s["ratio"]),
                "- system prompt: %d 字符" % s["sys_chars"],
                "- 事实摘要: %d 条 / %d 字符（注入上限 %d 条 / %d 字符）" % (
                    s["facts_n"], s["facts_chars"], s["facts_limit"], s["summary_max_chars"]),
                "- 会话消息: %d 条" % s["msg_count"],
            ]
            if s["facts"]:
                lines.append("- 事实列表:")
                for i, f in enumerate(s["facts"]):
                    lines.append("  [%d] %s" % (i, f))
            else:
                lines.append("- 事实列表: （空）")
            lines.append("可用操作: ctx facts_add <文本> | ctx facts_remove <序号> | "
                         "ctx reset | ctx compact | ctx limit <tok>（0=自动）")
            return "\n".join(lines)
        if op == "facts_add":
            text = str(args.get("text", args.get("value", ""))).strip()
            if not text:
                return "ctx facts_add 需要 text 参数"
            self._ctx_apply({"op": "facts_add", "text": text})
            return "已添加事实。当前 %d 条。" % len(self.history.get_facts())
        if op == "facts_remove":
            try:
                i = int(args.get("index", args.get("i", -1)))
            except (TypeError, ValueError):
                return "ctx facts_remove 需要数字 index"
            before = len(self.history.get_facts())
            self._ctx_apply({"op": "facts_remove", "index": i})
            return "已删除第 %d 条事实。%d → %d 条。" % (i, before, len(self.history.get_facts()))
        if op == "reset":
            self._ctx_apply({"op": "reset"})
            return "已清空当前会话的事实摘要。"
        if op == "compact":
            self._ctx_apply({"op": "compact"})
            s = self._ctx_snapshot()
            return "已压缩：事实 %d 条，会话消息 %d 条，估算占用 %d tok。" % (
                s["facts_n"], s["msg_count"], s["estimated"])
        if op == "limit":
            try:
                n = int(args.get("limit", args.get("n", -1)))
            except (TypeError, ValueError):
                return "ctx limit 需要数字 tok"
            if n < 0:
                s = self._ctx_snapshot()
                return "当前容量 %d tok（llm_ctx=%d，0=自动）。如需调整: ctx limit <tok>" % (
                    s["capacity"], s["llm_ctx"])
            self._ctx_apply({"op": "limit", "limit": n})
            return "容量已设为 %d tok。" % n
        return "未知 ctx 操作: %s（可用 inspect/facts_add/facts_remove/reset/compact/limit）" % op

    # ================= 语音转文字 =================

    def voice_probe(self):
        from . import voice
        return voice.probe()

    def voice_listen(self, seconds=8):
        """聆听一次麦克风，返回 {"ok", "text", "error"}（UI 麦克风按钮用）。"""
        from . import voice
        return voice.listen(seconds)

    # ================= 浏览器 =================

    def open_browser(self):
        if self.browser.available():
            return {"ok": True, "msg": "内置浏览器已打开。"}
        ok = self.browser.start()
        return {"ok": ok, "msg": "浏览器已打开。" if ok else (self.browser.error or "浏览器启动失败")}

    def open_url(self, q):
        """标题旁搜索/网址入口：识别 URL 或拼搜索词，内置浏览器优先、系统浏览器兜底。"""
        import re as _re
        import urllib.parse as _up
        q = (q or "").strip()
        if not q:
            return {"ok": False, "msg": "请输入网址或搜索词"}
        if _re.match(r"^https?://", q, _re.I):
            url = q
        elif _re.match(r"^[\w.-]+\.[a-zA-Z]{2,}(?:[/:?#]\S*)?$", q):
            url = "https://" + q
        else:
            url = "https://www.bing.com/search?q=" + _up.quote(q)
        if self.browser.available():
            try:
                self.browser.execute("NAVIGATE " + url)
                return {"ok": True, "msg": "已在内置浏览器打开：" + url}
            except Exception:
                pass
        try:
            ok = self.browser.start()
            if ok:
                try:
                    self.browser.execute("NAVIGATE " + url)
                    return {"ok": True, "msg": "已在内置浏览器打开：" + url}
                except Exception:
                    pass
        except Exception:
            pass
        import webbrowser as _wb
        try:
            _wb.open(url)
            return {"ok": True, "msg": "已用系统浏览器打开：" + url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_cmd(self, cmd):
        """浏览器文本协议直通（NAVIGATE/SNAPSHOT/CLICK/TYPE/…）。"""
        return {"ok": True, "result": self.browser.execute(cmd)}

    # ================= 停止生成 / 导出 =================

    def stop_generation(self):
        """用户点了「停止」：通知 Agent 循环收尾（流式立即断、下一轮不发请求）。"""
        try:
            self.agent.request_stop()
        except Exception:
            pass
        return {"ok": True}

    # ---------- 市场：技能 / 插件 的统一安装、卸载、启停 ----------
    def market_list(self):
        """列出已安装与货架上的技能 / 插件。"""
        try:
            return {"ok": True, "data": market.list_all()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def market_install(self, kind, eid):
        """从内置货架安装（复制到 skills/ 或 plugins/）。"""
        return market.install(kind, eid)

    def market_install_url(self, kind, url, name):
        """从 URL 下载安装（只收 http/https，限 2MB）。"""
        return market.install_url(kind, url, name)

    def market_remove(self, kind, eid):
        """卸载并删除（含禁用态文件）。"""
        return market.remove(kind, eid)

    def market_toggle(self, kind, eid, on):
        """启用 / 禁用（只改后缀，不删文件）。"""
        return market.toggle(kind, eid, bool(on))

    def export_conversation(self, cid):
        """导出单个会话为 Markdown（含角色分段），供前端下载。"""
        p = self._conv_path(cid)
        if not os.path.exists(p):
            return {"ok": False, "error": "会话不存在"}
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        title = d.get("title", "对话")
        msgs = d.get("messages", []) or []
        lines = ["# " + title, ""]
        for m in msgs:
            role = m.get("role")
            c = m.get("content") or ""
            lines.append("## " + ("我" if role == "user" else "intent"))
            lines.append(c)
            lines.append("")
        return {"ok": True, "title": title, "content": "\n".join(lines)}


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


def _find_hwnd(title="intent"):
    """取**本进程**的主窗口 HWND。

    必须按 PID 过滤：多个实例并存时，FindWindowW 按标题会返回「别人家的」
    那个窗口，导致染色/拖动检测作用在错误的窗口上（旧实例退出后句柄失效，
    功能就静默失效了）。因此优先枚举本进程的顶层窗口，取标题匹配且可见、
    面积最大的那个；本进程找不到才退回按标题全局查找。
    """
    try:
        import ctypes
        u = ctypes.windll.user32
        pid_target = os.getpid()
        best = [0, 0]        # (hwnd, area)
        # 注意：WINFUNCTYPE 的返回值直接当构造器用，不能再继承它
        # （继承 ctypes 函数类型会抛 "class must define _flags_"）
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

        def _cb(hwnd, _):
            p = ctypes.c_ulong()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value != pid_target:
                return True
            buf = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, buf, 512)
            if title.lower() not in (buf.value or "").lower():
                return True
            if not u.IsWindowVisible(hwnd):
                return True
            rc = ctypes.create_string_buffer(16)
            u.GetWindowRect(hwnd, rc)
            rect = ctypes.cast(rc, ctypes.POINTER(ctypes.c_long * 4)).contents
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > best[1]:
                best[0] = hwnd
                best[1] = area
            return True

        u.EnumWindows(EnumProc(_cb), 0)
        if best[0]:
            return best[0]
        # 兜底：按标题全局找（极少见：窗口尚未创建但已可枚举）
        h = u.FindWindowW(None, title)
        return h or 0
    except Exception as e:
        print("[窗口] 查找主窗口句柄失败: %s: %s" % (type(e).__name__, e), flush=True)
        return 0


def _port_alive(host, port, timeout=0.4):
    """端口是否已被监听（用于判断工作台是否已启动）。"""
    try:
        import socket
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _set_titlebar_dark(dark):
    """用 DWM 沉浸深色模式把原生标题栏染深/染浅（Win10 20H1+ / Win11 生效）。"""
    try:
        import ctypes
        hwnd = _find_hwnd("intent")
        if not hwnd:
            return
        dwmapi = ctypes.windll.dwmapi
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        val = ctypes.c_int(1 if dark else 0)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                                    ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


def _left_down():
    """鼠标左键当前是否物理按下（全局状态，窗口外松手也能感知）。"""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
    except Exception:
        return False


# ---- DWM 整窗玻璃（标题栏 + 边框 + 客户区 全部玻璃化）----
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_BORDER_COLOR = 34
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_NONE = 1
_DWMSBT_TRANSIENTWINDOW = 3   # Acrylic 亚克力（Win11）

# 窗体透明化的当前状态 [on, theme]：状态没变就跳过设置（避免 UI 线程被反复打断）
_form_transparent_state = [None, None]


def _dwm():
    import ctypes
    d = ctypes.windll.dwmapi
    d.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.c_void_p, ctypes.c_uint32]
    d.DwmSetWindowAttribute.restype = ctypes.c_int
    d.DwmGetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.c_void_p, ctypes.c_uint32]
    d.DwmGetWindowAttribute.restype = ctypes.c_int
    if hasattr(d, "DwmEnableBlurBehindWindow"):
        d.DwmEnableBlurBehindWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        d.DwmEnableBlurBehindWindow.restype = ctypes.c_int
    return d


def _dwm_get_int(d, hwnd, attr):
    """读回某个 DWM 整型属性的当前值；读不到返回 None。

    用途：先比对再写入。每次 DwmSetWindowAttribute 都会促使 DWM 重新合成整窗，
    定时器里无条件重写 = 周期性触发一次全窗口重绘，是"规律性顿一下"的来源。
    """
    import ctypes
    try:
        cur = ctypes.c_int(0)
        if d.DwmGetWindowAttribute(hwnd, attr, ctypes.byref(cur),
                                   ctypes.sizeof(cur)) == 0:
            return cur.value
    except Exception:
        pass
    return None


def _set_form_transparent(hwnd, on, theme):
    """让承载 WebView2 的 WinForms 窗体透明：透明时桌面虚化（DWM 亚克力/毛玻璃）能透出；
    关闭玻璃时恢复成不透明底色。"""
    import time as _t
    # 性能：状态没变就直接返回。
    # 切换主题/开关玻璃时前端都会回调到这里，若每次都去跨线程 Invoke 一次
    # WinForms，失败重试还要 sleep(0.5)×8（最坏 4 秒），UI 会明显"卡死一下"。
    want = (bool(on), theme)
    if _form_transparent_state[0] is want[0] and _form_transparent_state[1] == want[1]:
        return
    form = None
    # 窗体引用：pywebview 把它挂在 webview.windows[i].native（即 WinForms.Form 实例）
    try:
        import webview
        for w in webview.windows:
            nat = getattr(w, "native", None)
            if nat is not None:
                form = nat
                break
    except Exception:
        form = None
    # 兜底：用句柄反查（同进程内有效）
    if form is None:
        try:
            import clr
            clr.AddReference('System.Windows.Forms')
            from System.Windows.Forms import Control
            from System import IntPtr
            form = Control.FromHandle(IntPtr(hwnd))
        except Exception:
            form = None
    if form is None:
        print("[玻璃] 找不到承载 WebView2 的窗体，无法透明化", flush=True)
        return
    try:
        import clr
        clr.AddReference('System.Drawing')
        from System.Drawing import Color
        from System import Action
    except Exception as e:
        print("[玻璃] 加载 System.Drawing 失败: %s" % e, flush=True)
        return
    for _ in range(8):
        try:
            def _apply():
                try:
                    if on:
                        # 关键：让窗体背景透明，DWM 亚克力/毛玻璃才能透出桌面虚化
                        form.BackColor = Color.Transparent
                    else:
                        bg = Color.FromArgb(13, 16, 23) if theme != "light" else Color.FromArgb(238, 241, 246)
                        form.BackColor = bg
                except Exception as e:
                    print("[玻璃] 设置 BackColor 失败: %s" % e, flush=True)

            if getattr(form, "InvokeRequired", False):
                form.Invoke(Action(_apply))
            else:
                _apply()
            _form_transparent_state[0] = bool(on)
            _form_transparent_state[1] = theme
            print("[玻璃] 窗体透明=%s" % ("透明" if on else "不透明(%s)" % theme), flush=True)
            return
        except Exception as e:
            print("[玻璃] 窗体透明设置重试: %s: %s" % (type(e).__name__, e), flush=True)
            _t.sleep(0.5)


def _apply_dwm_glass(hwnd, theme, glass_on=True):
    """仅把原生标题栏染深/染浅（DWM 沉浸模式，廉价、不参与整窗重合成）。

    注意：本应用在「窗口不透明 + 前端背景图」之上做玻璃（CSS backdrop-filter
    模糊一张静态图片），**不再**开启整窗亚克力 / 窗体透明。整窗亚克力会让 DWM
    每帧重合成整个窗口、并叠加 CSS 双重模糊，是卡顿的根因 —— 现已移除。
    """
    if not hwnd:
        return
    try:
        import ctypes
        d = _dwm()
        dark = (theme != "light")
        # 1) 原生标题栏深浅色（边框由前端 CSS 亮边呈现，不改系统边框色，避免白线）
        v = ctypes.c_int(1 if dark else 0)
        d.DwmSetWindowAttribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE,
                                ctypes.byref(v), ctypes.sizeof(v))
        # 2) 清除任何残留的整窗亚克力（避免旧会话/系统复位留下的透明背景）
        sb = ctypes.c_int(_DWMSBT_NONE)
        d.DwmSetWindowAttribute(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
                                ctypes.byref(sb), ctypes.sizeof(sb))
    except Exception as e:
        print("[玻璃] DWM 应用失败: %s: %s" % (type(e).__name__, e), flush=True)


def _refresh_dwm(hwnd, theme, glass_on=True):
    """周期性轻量重刷原生标题栏深浅色（.NET 宿主就绪时会复位该属性，每数秒补一次）。"""
    if not hwnd:
        return
    try:
        import ctypes
        d = _dwm()
        dark = (theme != "light")
        want_dark = 1 if dark else 0
        if _dwm_get_int(d, hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE) != want_dark:
            v = ctypes.c_int(want_dark)
            d.DwmSetWindowAttribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE,
                                    ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def _glass_on(cfg):
    return str(cfg.get("glass_enabled", True)).lower() not in ("off", "false", "0", "no", "")


def _drag_blur_loop(api, hwnd):
    """后台线程做两件事：
    1) 拖动窗口 → 整体模糊。**一旦开始拖动，只要左键还没松开就一直保持模糊**，
       即使鼠标停住不动也继续模糊；左键松开（或窗口停下且左键已抬起）才恢复清晰。
    2) 周期性重刷原生标题栏深浅色 —— .NET 宿主在窗口就绪时会复位该属性，
       只在启动时染一次会被冲掉，所以每 2 秒补一次。
    """
    import time, ctypes, struct
    if not hwnd:
        return
    rect = ctypes.create_string_buffer(16)
    gu = ctypes.windll.user32
    last = None
    api._dragging = False
    last_paint = 0.0
    while True:
        try:
            gu.GetWindowRect(hwnd, rect)
            x, y, _, _ = struct.unpack("llll", rect.raw)
            pos = (x, y)
        except Exception:
            pos = None
        if pos is None:
            time.sleep(0.12)
            continue
        if last is None:
            last = pos
        elif pos != last:
            # 位置在变 → 正在拖动窗口
            if not api._dragging:
                api._dragging = True
        elif api._dragging:
            # 位置停住了：左键仍按住 → 继续保持模糊；左键已松开 → 恢复清晰
            if not _left_down():
                api._dragging = False
        last = pos
        now = time.time()
        if now - last_paint > 3.0:
            last_paint = now
            try:
                # 重新解析一次句柄：窗口若被重建（或多实例并存时认错了）都能跟上
                new_h = _find_hwnd("intent")
                if new_h:
                    hwnd = new_h
                # 周期性轻量重刷 DWM 整窗材质（内含"读回比对、相同就不写"，
                # 所以绝大多数 tick 只是读一个 DWORD，不会触发整窗重合成）
                _refresh_dwm(hwnd, api.cfg.get("theme", "dark"), _glass_on(api.cfg))
            except Exception:
                pass
        # 性能：8Hz 足够感知拖动（原为 33Hz 的 GetWindowRect + GetAsyncKeyState 轮询）
        time.sleep(0.12)


def _init_window(api, win=None):
    """等原生窗口真正建好（webview.start 稍后才 create_window），再染色 + 起模糊检测线程。"""
    import time
    hwnd = 0
    for _ in range(200):          # 最多等 20 秒
        hwnd = _find_hwnd("intent")
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        print("[窗口] 未能取得主窗口句柄，整窗玻璃与拖动模糊不可用", flush=True)
        return
    try:
        api._hwnd = hwnd
        dark = (api.cfg.get("theme", "dark") != "light")
        _apply_dwm_glass(hwnd, api.cfg.get("theme", "dark"), _glass_on(api.cfg))
        print("[窗口] 已取得主窗口句柄 %s，整窗玻璃=%s 主题=%s"
              % (hwnd, "开" if _glass_on(api.cfg) else "关", "深色" if dark else "浅色"),
              flush=True)
    except Exception:
        pass
    api._dragging = False
    threading.Thread(target=_drag_blur_loop, args=(api, hwnd), daemon=True).start()


def _boot_gui(api, win=None):
    """窗口创建后回调入口。"""
    threading.Thread(target=_init_window, args=(api, win), daemon=True).start()


def main():
    cfg = load_config()
    bus = ToolBus(cfg)
    browser = WebView2Adapter(cfg)
    bus.attach_browser(browser.execute)
    history = SessionHistory(cfg["history_facts_n"], cfg["summary_max_chars"])

    api = Api(cfg, bus, browser, history)
    bus.attach_ctx(lambda op, args: api._ctx_handler(op, args))
    bus.attach_spawn(api._spawn_runner)
    bus.attach_reminders(api.reminders)
    _start_servers(cfg, bus, browser, history)

    # 知识库：首次启动（或索引超 24h）后台扫描全机常用目录，建好供 kb 工具检索
    if api.kb.ensure_index_async():
        print("[知识库] 首次启动，正在后台扫描电脑文件建立索引…")

    # 正文索引：后台增量构建（限量 300 份文档，不阻塞启动；之后可在界面「自动入库」补齐）
    def _auto_content_index():
        try:
            r = api.kb.ingest(max_docs=300)
            if r.get("added") or r.get("updated"):
                print("[知识库] 正文索引已建立：%d 份文档 / %d 块"
                      % (r.get("docs", 0), r.get("chars", 0)))
        except Exception as e:
            print("[知识库] 正文索引构建失败: %s" % e)
    try:
        import threading as _th
        _th.Thread(target=_auto_content_index, daemon=True).start()
    except Exception:
        pass

    # 配置开启则随应用自启（默认关：暴露全盘给局域网需用户知情）
    if cfg.get("pc_bridge_enabled"):
        try:
            api.bridge.start()
            print(api.bridge.banner())
        except Exception as e:
            print("[pc_bridge] 自启失败: %s" % e)

    # 配置开启则随应用自启：本地推理引擎（内置模型）
    if cfg.get("local_engine_autostart") and cfg.get("local_engine_binary"):
        res_auto = api.engine_start()
        ok_auto = res_auto.get("ok")
        msg_auto = res_auto.get("msg", "")
        print("[本地引擎] " + ("就绪 %s" % api.engine.endpoint() if ok_auto
                               else "启动失败：" + msg_auto))

    w = webview.create_window(
        "intent",
        _ui_path(),
        js_api=api,
        width=1080, height=760,
        min_size=(820, 560),
        easy_drag=False,    # 保留原生标题栏作为拖动手柄，避免客户端误拖动
    )
    # 注意：绝对不要把 pywebview 的 Window 对象挂到 api（js_api）上！
    # pywebview 暴露接口时会递归遍历 js_api 的整个对象图，一旦走进 Window →
    # .NET WebView2 对象（.Bounds.Empty.Empty… 无限递归）就会跨线程访问 COM 并
    # 抛 RecursionError，导致窗口创建失败、进程启动即退出。窗口引用只通过
    # webview.start 的 args 私下传递。
    webview.start(_boot_gui, args=(api, w), gui="edgechromium")
    api.bridge.stop()   # 窗口关闭 → 停桥（关窗即断，手机立刻失去访问）


if __name__ == "__main__":
    main()
