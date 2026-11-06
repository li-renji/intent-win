# -*- coding: utf-8 -*-
"""阿觉插件进程入口（companion 类型插件）——**桌面浮窗球 / 桌宠**。

阿觉不是通用 Agent 的一部分，而是「装上去才有」的插件：

1. **桌宠是唯一入口**：桌面上那个小球就是阿觉本人。点一下球展开成对话面板，
   再点「收起」回到球。阿觉没有任务栏窗口、也不需要最小化——它本来就只是一个球。
2. **独立记忆**：阿觉的一切数据落在 ``plugin_data/ajue/``，与宿主记忆完全隔离。
3. **读取上下文与各工作区会话**：扫描宿主项目根、以及用户添加的其它工作区的
   ``会话/*.json``，据此认识你（需用户授权）。
4. **完全依赖宿主**：说话走宿主 Agent 暴露的本地 OpenAI 兼容端点（默认 6802），
   自己不带模型、不带密钥；因为最终走的是宿主的云端端点，所以阿觉也能联网。

本进程由宿主 Agent 在启动时拉起（intent 的「市场 → 桌宠」里也能随时唤醒），
也可以手动运行：
    python plugins/ajue/runner.py              # 常驻桌宠（默认是球）
    python plugins/ajue/runner.py --expand     # 启动后直接展开对话面板
    python plugins/ajue/runner.py --learn      # 一次性学习各工作区会话
    python plugins/ajue/runner.py --say "在吗"  # 说一句话后退出
"""
import argparse
import json
import os
import sys
import threading
import time

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))
for _p in (PLUGIN_DIR, PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ajue_core                                        # noqa: E402
import tts as _tts                                      # noqa: E402

DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "plugin_data", "ajue")

# 桌宠的两种形态
ORB_SIZE = 132          # 球：132×132，和原来尺寸完全一致
PANEL_W, PANEL_H = 380, 620   # 展开后的对话面板


def resolve_endpoint(explicit=None):
    """阿觉说话的通道：宿主 Agent 的本地 OpenAI 兼容端点。"""
    if explicit:
        return explicit.rstrip("/")
    try:
        from agent.config import load_config
        cfg = load_config()
        if not cfg.get("http_enabled", True):
            return None
        host = cfg.get("http_host") or "127.0.0.1"
        port = int(cfg.get("http_port") or 6802)
        return "http://%s:%d" % (host, port)
    except Exception:
        return "http://127.0.0.1:6802"


def _screen_size():
    """主屏幕尺寸（用于把窗口夹回可见区域）。拿不到就按 1920×1080 兜底。"""
    try:
        import webview
        for s in webview.screens():
            return int(s.width), int(s.height)
    except Exception:
        pass
    return 1920, 1080


class Geometry:
    """记住球和面板上次待的位置，下次开机回到老地方。

    ``panel.auto`` 表示用户从没手动挪过面板：这种情况下面板每次都从球当前所在的
    位置「长」出来，而不是死记一个可能已经离球很远的地方。
    """

    DEFAULTS = {
        "orb": {"x": 120, "y": 160},
        "panel": {"x": 120, "y": 90, "auto": True},
        "state": "orb",
    }

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "window_pos.json")
        self.data = json.loads(json.dumps(self.DEFAULTS))
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                for k in ("orb", "panel"):
                    if isinstance(d.get(k), dict):
                        self.data[k].update(d[k])
                if d.get("state") in ("orb", "panel"):
                    self.data["state"] = d["state"]
        except Exception:
            pass

    def get(self, state):
        p = dict(self.data.get(state, {}))
        return int(p.get("x", 120)), int(p.get("y", 120))

    def auto_panel(self):
        return bool((self.data.get("panel") or {}).get("auto", True))

    def set(self, state, x, y, manual=True):
        if state == "panel":
            self.data["panel"] = {"x": int(x), "y": int(y),
                                  "auto": not manual}
        else:
            self.data[state] = {"x": int(x), "y": int(y)}

    def set_state(self, state):
        self.data["state"] = state
        self.save()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass


class AjueApi:
    """桌宠 JS 可调用的接口。

    注意：pywebview 6.x 的 js_api **不支持 async 方法**（返回值要能 json 序列化），
    所以这里全部用同步方法；pywebview 在独立线程里调用它们，不会卡住界面。
    """

    def __init__(self, core, data_dir, start_state="orb"):
        self.core = core
        self.data_dir = data_dir
        self._win = None
        self.history = []
        self.tts_enabled = True
        # 内部状态绝不能叫 self.state：那会把同名的 js_api 方法 state() 顶掉，
        # 桌宠一开机就 TypeError: a.state is not a function。
        self._state = start_state if start_state in ("orb", "panel") else "orb"
        self.geo = Geometry(data_dir)
        self._pos_path = os.path.join(data_dir, "window_pos.json")
        self._lock = threading.Lock()
        self.shaped = True          # 是否把窗体裁成球/圆角（False = 普通矩形窗口）
        try:
            _tts.set_data_dir(data_dir)
        except Exception:
            pass
        self.tts = _tts.load_config()
        self.listener = None  # 常驻监听守护（main 里创建并绑定）

    # ---------------------------------------------------------- 窗口 / 形态
    def bind_window(self, win):
        self._win = win

    def _screen(self):
        return _screen_size()

    def _clamp(self, x, y, w, h):
        sw, sh = self._screen()
        x = max(0, min(int(x), sw - w))
        y = max(0, min(int(y), sh - h - 8))
        return x, y

    def _place(self, state, save=True):
        """把窗口切到指定形态：先改尺寸，再挪位置（resize 默认固定左上角）。"""
        if self._win is None:
            return False
        if state == "orb":
            w, h = ORB_SIZE, ORB_SIZE
            x, y = self.geo.get("orb")
        else:
            w, h = PANEL_W, PANEL_H
            px, py = self.geo.get("panel")
            if self.geo.auto_panel() or (px, py) == (120, 90):
                # 面板从球当前的位置「长」出来
                ox, oy = self.geo.get("orb")
                px, py = ox, oy
            x, y = px, py
        x, y = self._clamp(x, y, w, h)
        try:
            self._win.resize(w, h)
            self._win.move(x, y)
        except Exception:
            return False
        if getattr(self, "shaped", True):
            _shape_window(self._win, state)
        self._state = state
        if save:
            self.geo.set(state, x, y, manual=False)
            self.geo.set_state(state)
        return True

    def set_state(self, state):
        state = (state or "orb").lower()
        if state not in ("orb", "panel"):
            return {"ok": False, "error": "未知形态"}
        ok = self._place(state)
        return {"ok": ok, "state": self._state}

    def expand(self):
        return self.set_state("panel")

    def collapse(self):
        return self.set_state("orb")

    def toggle(self):
        return self.set_state("panel" if self._state == "orb" else "orb")

    def move_to(self, x, y):
        """JS 拖拽时的兜底通道（即便不用它，位置也会被记下来）。"""
        if self._win is None:
            return False
        try:
            self._win.move(int(x), int(y))
        except Exception:
            pass
        self.geo.set(self._state, int(x), int(y), manual=True)
        self.geo.save()
        return True

    def save_pos(self, x, y):
        """拖拽结束时记录当前形态的位置（省得每帧写盘）。

        面板被挪动时，球的位置跟着走同一个左上角——这样「收起成球」时
        球会出现在面板刚才所在的角上，不会突然跳到别处。
        """
        self.geo.set(self._state, int(x), int(y), manual=True)
        if self._state == "panel":
            self.geo.set("orb", int(x), int(y), manual=True)
        self.geo.save()
        return {"ok": True}

    def state(self):
        return {"state": self._state, "tts_enabled": self.tts_enabled}

    # ---------------------------------------------------------- 对话
    def chat(self, text):
        """与阿觉对话：自建人格 system prompt → 交给宿主 Agent 的端点完成。

        普通闲聊走流式：宿主把回复一段段推过来，JS 那边字一个个蹦出来，体感快很多；
        需要实时信息时自动切到联网模式（那种情况不流式，先显示「正在联网查资料」）。
        """
        text = (text or "").strip()
        if not text:
            return ""
        with self._lock:
            self.history.append({"role": "user", "content": text})
            prior = list(self.history[:-1])

        # 流式回调：把每一块文本推给前端 DOM（前端用 __ajueAppend 接收）
        def on_chunk(c):
            self._stream_append(c)

        try:
            reply = self.core.chat(text, prior, on_chunk=on_chunk)
        except Exception as e:
            reply = "[出错了：%s]" % e
        # 收尾：把最终完整文本落到 DOM（流式万一丢帧也以这个为准）
        try:
            self._stream_finish(reply)
        except Exception:
            pass
        with self._lock:
            self.history.append({"role": "assistant", "content": reply})
        # 聊天记录落盘：日记与「长期记忆」都靠它（不阻塞对话）
        try:
            self.core.log_chat(text, reply)
        except Exception:
            pass
        if self._may_speak():
            try:
                _tts.speak_async(reply, self.tts.get("voice") or None)
            except Exception:
                pass
        return reply

    def _stream_append(self, chunk):
        """把一帧流式文本推到前端（前端用全局 __ajueAppend 接收）。"""
        if self._win is None:
            return
        try:
            self._win.evaluate_js("__ajueAppend(%s)" % json.dumps(chunk))
        except Exception:
            pass

    def _stream_finish(self, text):
        """收尾：把完整文本落到前端当前的气泡（前端用全局 __ajueFinish 接收）。"""
        if self._win is None:
            return
        try:
            self._win.evaluate_js("__ajueFinish(%s)" % json.dumps(text))
        except Exception:
            pass

    def _may_speak(self):
        """开口说话需要同时满足：开关打开 + 用户授权过 speak_aloud。"""
        if not self.tts_enabled:
            return False
        try:
            return bool(self.core.permissions.check("speak_aloud"))
        except Exception:
            return False

    def clear_history(self):
        with self._lock:
            self.history = []
        return {"ok": True}

    def greet(self):
        return self.core.persona.get("greeting") or "我在呢。"

    # ---------------------------------------------------------- 状态 / 人格
    def status(self):
        try:
            stats = self.core.memory.stats()
        except Exception:
            stats = {}
        ti = _tts.info()
        return {
            "persona": self.core.persona.get("name", "阿觉"),
            "tone": self.core.persona.get("tone", ""),
            "greeting": self.core.persona.get("greeting", ""),
            "agent_online": self.core.llm.available(),
            "endpoint": self.core.llm.endpoint,
            "memory": stats,
            "l3": self.core.l3_view(),
            "tts_enabled": self.tts_enabled,
            "tts": ti,
            "permissions": self.core.permissions.all(),
            "scopes": [{"id": s, "desc": d}
                       for s, d in ajue_core.PERMISSION_SCOPES],
            "workspaces": self.core.scanner.roots(),
            "diary_dir": self.core.diary.dir,
            "minimind": self.core.minimind.available(),
            "config": self.core.config,
            "state": self._state,
            "version": "0.7",
        }

    def persona(self):
        return self.core.persona

    def style(self):
        try:
            return self.core.style.snapshot()
        except Exception:
            return {}

    def l3(self):
        return self.core.l3_view()

    # ---------------------------------------------------------- 语音
    def speak(self, text):
        try:
            ok, detail = _tts.speak(text or "", self.tts.get("voice") or None)
            return {"ok": bool(ok), "msg": detail}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def set_tts(self, on):
        self.tts_enabled = bool(on)
        return {"ok": True, "tts_enabled": self.tts_enabled}

    def tts_info(self):
        return _tts.info()

    def tts_save(self, patch):
        """保存语音配置（provider / voice / openai 通道参数）。"""
        try:
            cfg = _tts.save_config(patch or {})
            self.tts = cfg
            return {"ok": True, "tts": _tts.info()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def tts_preset(self, preset_id):
        try:
            r = _tts.apply_preset(preset_id)
            if not r.get("ok"):
                return r
            self.tts = _tts.load_config(refresh=True)
            return {"ok": True, "tts": _tts.info()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def voices(self):
        try:
            return _tts.list_voices()
        except Exception:
            return []

    # ---------------------------------------------------------- 授权
    def permissions(self):
        return self.core.permissions.all()

    def grant(self, scope):
        try:
            self.core.permissions.grant(scope)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "permissions": self.core.permissions.all()}

    def revoke(self, scope):
        try:
            self.core.permissions.revoke(scope)
        except Exception:
            pass
        return {"ok": True, "permissions": self.core.permissions.all()}

    def set_permission(self, scope, on):
        return self.grant(scope) if on else self.revoke(scope)

    # ---------------------------------------------------------- 工作区
    def workspaces(self):
        return {"roots": self.core.scanner.roots()}

    def add_workspace(self, path):
        try:
            return self.core.scanner.add_root(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_workspace(self, path):
        return self.core.scanner.remove_root(path)

    # ---------------------------------------------------------- 学习 / 日记
    def learn(self):
        try:
            return self.core.learn_from_workspaces()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def write_diary(self, date=None):
        """写一篇日记：内容来自今天的聊天记录 + （授权后）各工作区记录 + 长期记忆。

        为空则跳过，避免写一堆「今天没什么」的空日记。已写过当天则直接跳过。
        """
        try:
            import datetime
            d = date or datetime.date.today().strftime("%Y-%m-%d")
            diary_path = os.path.join(self.core.diary.dir, d + ".md")
            if os.path.exists(diary_path):
                return {"ok": True, "skipped": True, "reason": "今天已经写过了",
                        "path": diary_path}
            chat_log = self.core.read_chat_log(d)
            if not chat_log:
                return {"ok": False, "reason": "今天还没有聊天记录，先不写"}
            notes = self.core.workspace_notes()
            return self.core.diary.write(self.core.memory, self.core.style,
                                         self.core.persona, date=d,
                                         llm_fn=self.core._diary_llm,
                                         chat_log=chat_log, workspace_notes=notes)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def backfill_diaries(self, days=3):
        """补写最近几天「有聊天记录但还没写日记」的日子（阿觉没开机时错过的）。"""
        import datetime
        out = []
        for i in range(days, 0, -1):
            d = (datetime.date.today() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            diary_path = os.path.join(self.core.diary.dir, d + ".md")
            if os.path.exists(diary_path):
                continue
            if not self.core.read_chat_log(d):
                continue
            r = self.write_diary(date=d)
            out.append({"date": d, "ok": bool(r.get("ok")), "reason": r.get("reason")})
        return {"ok": True, "backfilled": out}

    # ---------------------------------------------------------- 阿觉专属配置
    def ajue_config(self):
        return self.core.config

    def set_ajue_config(self, patch):
        try:
            self.core.set_config(patch or {})
            return {"ok": True, "config": self.core.config}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def minimind_available(self):
        return {"ok": True, "available": self.core.minimind.available()}

    def distill(self, approved):
        """mini MIND 蒸馏：**必须由用户明确同意**（approved=True）才会落盘。"""
        samples = []
        with self._lock:
            for m in self.history[-40:]:
                samples.append({"role": m.get("role", "user"),
                                "text": m.get("content", "")})
        try:
            return self.core.minimind.distill(samples, bool(approved))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------------------------------------------------- 生命周期
    def quit(self):
        """让阿觉休息（关掉桌宠；宿主「市场 → 桌宠」里可以随时唤醒）。"""
        def _do():
            time.sleep(0.15)
            if self._win is not None:
                try:
                    self._win.destroy()
                except Exception:
                    pass
        threading.Thread(target=_do, daemon=True).start()
        return {"ok": True}

    # ---------------------------------------------------------- 常驻监听
    def bind_listener(self, listener):
        self.listener = listener

    def listen_status(self):
        base = self.listener.status() if self.listener else {"running": False}
        cfg = self.listener.cfg() if self.listener else None
        try:
            en = bool(self.core.config.get("listen", {}).get("enabled", False))
        except Exception:
            en = False
        base.update({"enabled": en, "config": cfg})
        return base

    def listen_set(self, patch):
        """写入监听配置（**以磁盘为准**），并按 enabled 实时启停守护。

        走磁盘而不是只改内存：intent 里的「阿觉常驻监听」工具（.qdplugin）
        也可能改这份配置，两边必须看到同一份。
        """
        try:
            cur = _read_listen_cfg(self.data_dir)
            cur.update(patch or {})
            if not _write_listen_cfg(self.data_dir, cur):
                raise RuntimeError("写入 ajue_config.json 失败")
            try:
                self.core.config["listen"] = dict(cur)
            except Exception:
                pass
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if self.listener is None:
            return {"ok": True, "note": "监听守护未初始化"}
        if cur.get("enabled"):
            self.listener.start()
        else:
            self.listener.stop()
        return {"ok": True, "running": self.listener.is_running()}

    # ---------------------------------------------------------- 开机自启
    def autostart_status(self):
        """开机自启状态。

        它是**阿觉自己的部署方式**，属于本插件内部模块，不是独立扩展。
        """
        try:
            import enable_autostart as _auto
            return _auto.status()
        except Exception as e:
            return {"ok": False, "enabled": False, "error": str(e)}

    def autostart_set(self, on):
        """打开 / 关闭开机自启（界面上的开关走这里）。"""
        try:
            import enable_autostart as _auto
            return _auto.set_enabled(bool(on))
        except Exception as e:
            return {"ok": False, "enabled": False, "error": str(e)}

    def listen_start(self):
        if self.listener is None:
            return {"ok": False, "error": "监听守护未初始化"}
        return self.listener.start()

    def listen_stop(self):
        if self.listener is None:
            return {"ok": False, "error": "监听守护未初始化"}
        return self.listener.stop()


def _call_when_ready(win, fn, what=""):
    """等窗体在 GUI 线程里创建好，然后在**普通线程**里执行 ``fn``。

    关键区别：pywebview 的 ``resize/move`` 等方法带 ``@_shown_call``，
    会一直等到「窗体已显示」这个事件为真。如果这些调用跑在 GUI 线程里
    （例如用 ``Form.Invoke`` 排进去），那个事件就永远不会来 —— 直接死锁。
    所以：**调 pywebview 的窗口操作（resize / move）一律走这里，
    不要排进 GUI 线程。**
    """

    def _worker():
        for _ in range(400):
            if getattr(win, "native", None) is not None:
                try:
                    fn()
                except Exception as e:
                    print("[阿觉] %s失败：%r" % (what or "初始化", e), flush=True)
                return
            time.sleep(0.05)
        print("[阿觉] %s：窗体始终没出现" % (what or "初始化"), flush=True)

    threading.Thread(target=_worker, daemon=True).start()


def _shape_window(win, state):
    """把窗体裁成「球」或「圆角面板」的形状（Windows）。

    为什么不直接用 pywebview 的透明窗体？试过了：WinForms 靠
    ``TransparencyKey`` 抠出来的透明窗口**整个都点不动**（鼠标穿透到下面的窗口），
    桌宠就没法点了。所以改成这条路：

    - 窗体保持不透明（底色 #0F1220，与界面同色）；
    - 用 ``SetWindowRgn`` 把可见区域裁成一个圆（球形态）/ 圆角矩形（面板形态）；
    - 球形态的 SVG 画满整个圆，看起来就是一颗浮在桌面上的球；
    - 区域裁剪是**真正的命中区域**，所以照样点得到、拖得动。

    非 Windows 平台没有这套 API，退化成普通矩形窗口（仍能正常对话）。
    """
    if os.name != "nt" or win is None:
        return False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32
        native = getattr(win, "native", None)
        if native is None:
            return False
        hwnd = int(native.Handle.ToInt32())
        if state == "orb":
            # 裁成正圆，刚好包住整个窗口，外面的黑边全部切掉
            rgn = g32.CreateEllipticRgn(0, 0, ORB_SIZE, ORB_SIZE)
        else:
            rgn = g32.CreateRoundRectRgn(0, 0, PANEL_W, PANEL_H, 28, 28)
        if not rgn:
            return False
        u32.SetWindowRgn(hwnd, rgn, True)
        return True
    except Exception as e:
        print("[阿觉] 窗体裁形失败（退回普通矩形窗口）：%r" % (e,))
        return False


def _heartbeat(data_dir):
    """给宿主留一张「我还活着」的纸条。

    宿主 Agent 重启后并不认识上一次拉起的桌宠进程，靠这个心跳文件（含 pid）
    才能知道球还在不在桌面上 —— 既不重复拉起两个阿觉，也能从 intent 里关掉它。
    """
    path = os.path.join(data_dir, "heartbeat.json")

    def _loop():
        while True:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "ts": time.time()}, f)
            except Exception:
                pass
            time.sleep(5)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return path


def _watchdog(api):
    """后台守护：定期学习 + 检测宿主是否还在线。

    阿觉**完全依赖**宿主 Agent，因此宿主长时间失联时阿觉也该退场，
    而不是留一个说不了话的桌宠在桌面上。
    """
    offline_since = None
    last_learn = 0
    while True:
        time.sleep(15)
        try:
            online = api.core.llm.available()
        except Exception:
            online = False
        if online:
            offline_since = None
            # 每 10 分钟学习一次（且需授权）
            if time.time() - last_learn > 600:
                last_learn = time.time()
                try:
                    api.learn()
                except Exception:
                    pass
        else:
            if offline_since is None:
                offline_since = time.time()
            elif time.time() - offline_since > 120:
                print("[阿觉] 通用 Agent 长时间离线，阿觉先休息了。")
                try:
                    api.quit()
                except Exception:
                    pass


def _parse_time(s):
    """把 'HH:MM' 解析成 (时, 分)，非法则回退 12:00。"""
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return 12, 0


def _diary_scheduler(api):
    """每天定时写日记的后台线程（默认 12:00，可在设置里改）。

    - 到点：先补写最近几天漏掉的日记，再写今天这一篇；
    - 今天没聊天记录就跳过，不写空日记；
    - 工作区内容只有授权了「观察对话」才会出现在日记里；
    - 用轻量模型、限长生成，token 极少。
    """
    import datetime
    while True:
        hh, mm = _parse_time((api.core.config.get("diary_time") or "12:00")
                             if hasattr(api, "core") else "12:00")
        now = datetime.datetime.now()
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += datetime.timedelta(days=1)
        wait = (nxt - now).total_seconds()
        # 别一次睡太久：封顶 25 小时，循环回来再算，避免配置改了不生效
        time.sleep(min(wait, 25 * 3600))
        try:
            api.backfill_diaries(days=3)
            api.write_diary()
        except Exception as e:
            print("[阿觉] 定时写日记失败：%r" % (e,))


def _pet_cfg(data_dir):
    """桌宠自身的偏好（目前只有「要不要把窗体裁成球的形状」）。

    个别机器上裁剪区域会出问题（表现为桌宠变成方角窗口），那时把 pet.json 里的
    shaped 改成 false，或用 --opaque 启动即可；功能不受影响。
    """
    path = os.path.join(data_dir, "pet.json")
    cfg = {"shaped": True}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("shaped"), bool):
            cfg["shaped"] = d["shaped"]
    except Exception:
        pass
    cfg["_path"] = path
    return cfg


def _listen_cfg_path(data_dir):
    return os.path.join(data_dir, "ajue_config.json")


def _read_listen_cfg(data_dir):
    """读磁盘上的 listen 配置。

    为什么必须读磁盘而不是只读内存：intent 里「阿觉常驻监听」工具（插件）
    也可能改这份配置，两边要看到同一份，桌宠的监听线程才能在 ~2 秒内
    自动跟随开关变化。
    """
    try:
        with open(_listen_cfg_path(data_dir), encoding="utf-8") as f:
            d = json.load(f) or {}
        lis = d.get("listen")
        return dict(lis) if isinstance(lis, dict) else {}
    except Exception:
        return {}


def _write_listen_cfg(data_dir, listen_cfg):
    """把 listen 配置写回 ajue_config.json（保留文件里的其它配置）。"""
    path = _listen_cfg_path(data_dir)
    try:
        d = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f) or {}
        if not isinstance(d, dict):
            d = {}
        d["listen"] = dict(listen_cfg)
        os.makedirs(data_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="阿觉 · 桌面桌宠（插件进程）")
    ap.add_argument("--data-dir", default=None, help="插件数据目录（默认 plugin_data/ajue）")
    ap.add_argument("--endpoint", default=None, help="宿主 Agent 的 OpenAI 兼容端点")
    ap.add_argument("--no-window", action="store_true", help="不开窗口（仅用于自检）")
    ap.add_argument("--orb", action="store_true", help="启动即为球形态（默认）")
    ap.add_argument("--expand", action="store_true", help="启动即展开对话面板")
    ap.add_argument("--opaque", action="store_true",
                    help="不裁剪窗体形状（老机器 / 显卡兼容，桌宠会是方角窗口）")
    ap.add_argument("--learn", action="store_true", help="一次性学习各工作区会话后退出")
    ap.add_argument("--say", default=None, help="说一句话后退出")
    ap.add_argument("--diary", action="store_true",
                    help="立刻写一篇今天的日记（用于自测）后退出")
    ap.add_argument("--listen-daemon", action="store_true",
                    help="无头常驻：不开窗口，仅运行常驻监听守护（后台隐身监听）")
    args = ap.parse_args(argv)

    data_dir = os.path.abspath(args.data_dir or DEFAULT_DATA_DIR)
    os.makedirs(data_dir, exist_ok=True)
    hb = _heartbeat(data_dir)

    endpoint = resolve_endpoint(args.endpoint)
    core = ajue_core.Ajue(data_dir, PLUGIN_DIR, PROJECT_ROOT, endpoint=endpoint)
    start_state = "panel" if args.expand else "orb"
    api = AjueApi(core, data_dir, start_state=start_state)

    # 常驻监听守护（无头也可运行）：状态变化时推给前端（若窗口在）
    from listen import Listener  # 延迟导入：仅本函数用到

    def _push_listen_state(api, s):
        if api._win is not None:
            try:
                api._win.evaluate_js("__ajueListenState(%s)" % json.dumps(s))
            except Exception:
                pass
    _listener = Listener(api, data_dir,
                         config_getter=lambda: _read_listen_cfg(data_dir),
                         on_state_change=lambda s: _push_listen_state(api, s))
    api.bind_listener(_listener)

    if args.orb:
        api._state = "orb"

    print("[阿觉] 插件启动 v0.7 · 人格=%s · 端点=%s · 数据=%s"
          % (core.persona.get("name"), endpoint, data_dir))

    if args.learn:
        print(json.dumps(core.learn_from_workspaces(), ensure_ascii=False))
        return 0
    if args.say:
        print(api.chat(args.say))
        return 0
    if args.diary:
        print(json.dumps(api.write_diary(), ensure_ascii=False))
        return 0
    if args.listen_daemon:
        print("[阿觉] 监听守护模式（无头常驻）启动；Ctrl+C 退出")
        api.listener.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        api.listener.stop()
        return 0
    if args.no_window:
        return 0

    try:
        import webview
    except ImportError:
        print("[阿觉] 缺少 pywebview，无法显示桌宠。"
              "请在宿主 Python 上执行：python -m pip install \"pywebview>=5.0,<7.0\"")
        return 2

    # 拖拽：只用 .pywebview-drag-region 标记的区域可拖（球的整个身体 + 面板顶栏）。
    # 这里是独立进程，改动不会影响宿主 Agent 的窗口。注意不要打开
    # DRAG_REGION_DIRECT_TARGET_ONLY，否则只有标记元素本身能拖、子元素拖不动。
    try:
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = False
    except Exception:
        pass

    # 一律先按小球尺寸建窗，再 resize 到目标形态：初始尺寸带 WinForms 的边框余量，
    # 直接按 380×620 建窗实际只有 364×581，而 resize() 是精确的。
    x, y = api.geo.get(start_state)
    x, y = api._clamp(x, y, ORB_SIZE, ORB_SIZE)

    shaped = bool(_pet_cfg(data_dir).get("shaped", True)) and not args.opaque
    win = webview.create_window(
        "阿觉",
        os.path.join(PLUGIN_DIR, "pet.html"),
        js_api=api,
        width=ORB_SIZE, height=ORB_SIZE,
        x=x, y=y,
        resizable=False,
        # pywebview 默认最小窗口是 200x100，不显式放开的话这颗球会被撑成 200x100
        min_size=(ORB_SIZE, ORB_SIZE),
        on_top=True,          # 常驻桌面
        frameless=True,       # 无边框：桌宠不应该有窗口的边界感
        transparent=False,
        background_color="#FFFFFF",   # 全白背景，没有黑色窗口边
    )

    api.bind_window(win)
    api.shaped = shaped
    api._state = start_state
    # 摆放 + 裁形：走普通线程（pywebview 的 resize 在 GUI 线程里会死锁）
    _call_when_ready(win, lambda: api._place(start_state), "初始摆放形态")
    if api.core.config.get("listen", {}).get("enabled"):
        api.listener.start()
    threading.Thread(target=_watchdog, args=(api,), daemon=True).start()
    threading.Thread(target=_diary_scheduler, args=(api,), daemon=True).start()
    try:
        webview.start()
    finally:
        try:
            os.remove(hb)
        except Exception:
            pass
    print("[阿觉] 桌宠已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
