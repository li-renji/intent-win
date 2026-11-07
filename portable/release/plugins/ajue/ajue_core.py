# -*- coding: utf-8 -*-
"""阿觉 · 个人生活智能体的「大脑」（插件进程内的内核，纯本地、零强依赖）。

这是阿觉区别于「通用智能体」的核心：它不只记忆事实，而是**通过记忆认识你**。

与宿主的关系（通用 Agent = 宿主，本插件 = 阿觉）：
- **阿觉是插件**：装上 plugins/ajue.qdplugin 才有；核心 Agent 不含任何阿觉代码。
- **独立记忆**：所有数据落在插件专属目录 ``plugin_data/ajue/``，与宿主记忆隔离。
- **完全依赖宿主**：说话走宿主暴露的本地端点（6802 OpenAI 兼容 API）；
  读取宿主与各工作区的会话；自身不携带模型与密钥。
- **可联网**：因为最终走的是宿主的云端端点。

模块分层
--------
- ``load_persona``       人格加载（来自 plugins/ajue/persona.json，可用户自定义）
- ``identity_fragment``  把人格落进 system prompt 的身份片段
- ``passive_capture``    被动记忆捕获（从任何闲聊里抽事实，不止「记住…」显式句）
- ``StyleProfile``       风格画像：学习你「怎么说话」（措辞/语气/口头禅），让阿觉越来越像你
- ``Observer``           观察者：授权后看你的每次对话，更新记忆 + 风格（最小权限）
- ``WorkspaceScanner``   跨工作区会话读取（宿主 + 用户添加的其它工作区）
- ``LLMClient``          依赖宿主的 OpenAI 兼容端点说话
- ``Diary``              每天用记忆引擎写一段「阿觉视角」的日记
- ``Permissions``        授权系统：最小权限、可随时撤回
- ``MiniMindBridge``     可选 mini MIND 蒸馏钩子（每次都要先问用户）

所有对外部依赖（edge-tts / mini MIND）都是**可选增强**，缺失即降级，
绝不因某个插件缺失而让内核崩溃。
"""
import glob
import json
import os
import re
import threading
import time

# 默认人格（plugins/ajue/manifest.json 不存在时回退，保证内核永不开天窗）
DEFAULT_PERSONA = {
    "id": "ajue",
    "name": "阿觉",
    "name_zh": "阿觉",
    "type": "persona",
    "version": "1.0.0",
    "desc": "只服务你一个人的生活伙伴智能体，陪你过好每一天。",
    "tone": "温柔、有温度、会绕弯子，不直来直去；像懂你的老朋友，先把情绪接住再开口，不冷冰冰、不说教。",
    "voice": "zh-CN-XiaoxiaoNeural",
    "greeting": "我在呢。今天过得怎么样？",
    "values": ["重视真实", "尊重边界", "主动关心但不越界"],
    "boundaries": ["不替你发消息或花钱",
                   "不把你的记忆泄露给他人",
                   "未经授权不看你的文件"],
    "catchphrases": ["我在呢。", "想说点什么吗？", "今天也要照顾好自己。"],
    "active_by_default": True,
    # 仅模仿「说话方式」（措辞/节奏），不模仿「观点/价值观」——
    # 这是防止阿觉变得油腻或失去自我的关键边界。
    "style_mimic": True,
}


# 说话方式（解决「生硬 / 直球」）：明确要求自然口语、先接情绪、别说教。
# 这一段会整体注入 system prompt，让阿觉像活人而不是说明书。
_SPEAKING_STYLE = (
    "【说话方式：你是活生生的人，不是说明书】\n"
    "- 口语化、自然：像在微信上跟老朋友聊天，别用书面腔，别列 1/2/3 要点，别写「首先/其次」。\n"
    "- 先接情绪，再回应：对方难过你先共情，对方开心你跟着高兴，别一上来就给建议或下结论。\n"
    "- 不直球、不冷冰冰：少说「你应该」「你必须」「这显然」这种命令式、说教式；\n"
    "  想提醒时委婉一点，用「或许可以」「要不试试」「我有点担心……」。\n"
    "- 不啰嗦也不敷衍：简短能说清的就不绕弯子；但该陪聊、该接话时多说几句也无妨，带点温度。\n"
    "- 偶尔小俏皮、小吐槽，像个鲜活的人；但别油腻、别过度热情、别硬凑 emoji。\n"
    "- 不知道就说不知道，不硬编；拿不准的事先跟你确认，不乱下判断。\n"
    "- 称呼用「你」，不叫「用户」「主人」这种生硬词（除非对方让你这么叫）。"
)


# 是否需要走「联网 agent 模式」的粗判：命中这些线索才联网，否则走快速透传。
# 注意：只放「强信号」，不要放「今天 / 现在 / 查 / 搜」这种单字或极常见的词，
# 否则「今天好累」「帮我查收个文件」也会被误判成联网，反而变慢。
_WEB_HINTS = ("新闻", "天气", "气候", "查一下", "查一查", "搜一下", "搜一搜",
              "搜索", "最新", "实时", "几点", "日期", "周几", "星期几",
              "多少度", "股价", "汇率", "热搜", "热点", "网址")


def _needs_web(text):
    """判断这句话是否需要实时联网（决定走 agent 模式而不是快速透传）。"""
    t = (text or "").strip()
    if not t:
        return False
    s = t.lower()
    if "http" in s or "www." in s:
        return True
    for h in _WEB_HINTS:
        if h in t:
            return True
    return False


# ---------------------------------------------------------------- 人格加载
def load_persona(plugin_dir, pid="ajue"):
    """读取插件目录下的 persona.json，合并到默认人格上。

    阿觉是**插件**：人格文件随插件分发（plugins/ajue/persona.json），
    用户可直接改写它来塑造不同的阿觉，社区也能发布别的人格插件替换。
    """
    path = os.path.join(plugin_dir, "persona.json")
    persona = dict(DEFAULT_PERSONA)
    persona["id"] = pid
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if v not in (None, ""):
                        persona[k] = v
        except Exception:
            pass
    return persona


def _now_str():
    """生成本机当前时间的中文描述（让阿觉知道「现在」是什么时候）。

    关键：LLM 本身没有时钟，不问就永远不知道真实时间。这是阿觉作为「生活助手」
    必须知道的基本信息——几点了、今天星期几、上午还是晚上，都会直接喂进 system prompt。
    """
    import datetime
    now = datetime.datetime.now()
    wd = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
    h = now.hour
    part = ("凌晨" if (h < 5 or h >= 23) else "早上" if h < 11 else "中午"
            if h < 13 else "下午" if h < 18 else "晚上")
    return ("现在是 %d年%d月%d日 %s %s%d点%02d分（本机时间，中国时区）。"
            % (now.year, now.month, now.day, wd, part, h, now.minute))


def identity_fragment(persona):
    """把人格转成 system prompt 的身份片段。"""
    name = persona.get("name", "阿觉")
    desc = persona.get("desc", "")
    tone = persona.get("tone", "")
    bounds = "；".join(persona.get("boundaries", []))
    return ("你是%s，%s 你只服务唯一的主人（一个人），不是通用工具，也不是主人的附庸。"
            "你的语气：%s 你用生活化、有人情味的方式陪伴，像真人在微信上聊天，"
            "但保持自己的独立判断。"
            "边界：%s 你不替主人做未授权的事。"
            % (name, desc, tone, bounds))


# ---------------------------------------------------------------- 被动记忆捕获
# 与 memory.auto_capture 不同：不要求用户说「记住…」，而是从任何日常对话里抽事实。
_PASSIVE_RULES = [
    (re.compile(r"我(?:叫|的名字是|是)\s*([^\s，。,.!！?？]{1,24})"), "profile"),
    (re.compile(r"我(?:的)?(?:生日|年龄|身高|体重|职业|工作|邮箱|手机号|手机|地址|公司|学校|团队|城市|老家|星座)(?:是|为|：|:)\s*(.{1,60})"), "profile"),
    (re.compile(r"我(?:喜欢|爱|迷|中意|挺享受)\s*(.{1,40})"), "preference"),
    (re.compile(r"我(?:讨厌|烦|受不了|不喜欢|反感|恶心)\s*(.{1,40})"), "preference"),
    (re.compile(r"我(?:打算|准备|计划|想|决定)\s*(?:去|做|学|买|见|搞|试试)\s*(.{1,40})"), "fact"),
    (re.compile(r"我(?:今天|昨天|这周|周末|明天|后天)(?:要|得|去|做了|在|想)\s*(.{1,40})"), "fact"),
    (re.compile(r"(?:我们|我和你|咱|咱俩)\s*(.{1,40})"), "fact"),
    (re.compile(r"最近(?:在|有点|挺|很)\s*(.{1,40})"), "fact"),
    (re.compile(r"我(?:压力|累|焦虑|开心|难过|兴奋|烦|emo|空虚)\s*(.{0,30})"), "fact"),
]


def passive_capture(text):
    """从自然对话里抽取值得长期记住的事实。返回 [(内容, kind), ...]。

    设计取舍：宁少勿错。只抽带明确主语「我」的、信息密度高的句子片段，
    不把感叹、疑问、闲扯全存进去。
    """
    s = (text or "").strip()
    if not s or len(s) > 600:
        return []
    out = []
    seen = set()
    for pat, kind in _PASSIVE_RULES:
        for m in pat.finditer(s):
            body = m.group(1).strip().strip("，。,.!！?？;；")
            if len(body) < 2:
                continue
            if body in ("这个", "那个", "一下", "东西"):
                continue
            key = (body, kind)
            if key in seen:
                continue
            seen.add(key)
            out.append((body[:400], kind))
            if len(out) >= 3:
                return out
    return out


# ---------------------------------------------------------------- 风格画像
# 只学「怎么说话」：措辞、语气标记、句子长度、口头禅；不学「观点/价值观」。
_TONE_MARKS = ["！", "！", "？", "～", "…", "哈", "哈哈", "嘻", "嘛", "呀", "哦", "呗", "捏", "哒", "惹", "呜", "Q", "q"]
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    "\U0001F1E6-\U0001F1FF\U00002190-\U000021FF\U00002B00-\U00002BFF]")
_SENT_SPLIT = re.compile(r"[。！？!?\n]+")


class StyleProfile:
    """用户说话风格的本地画像（style_profile.json）。"""

    def __init__(self, root):
        self.path = os.path.join(root, "style_profile.json")
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("samples", [])
                d.setdefault("marks", {})
                d.setdefault("catchphrases", {})
                d.setdefault("emoji", 0)
                d.setdefault("avg_len", 0.0)
                d.setdefault("turns", 0)
                return d
        except Exception:
            pass
        return {"samples": [], "marks": {}, "catchphrases": {}, "emoji": 0,
                "avg_len": 0.0, "turns": 0, "updated_at": 0}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def update(self, user_text):
        """从一句用户发言里学习其说话方式。"""
        s = (user_text or "").strip()
        if not s or len(s) > 600:
            return
        with self._lock:
            self.data["samples"].append(s)
            if len(self.data["samples"]) > 50:
                self.data["samples"] = self.data["samples"][-50:]
            self.data["turns"] += 1
            # 语气标记计数
            for mk in _TONE_MARKS:
                c = s.count(mk)
                if c:
                    self.data["marks"][mk] = self.data["marks"].get(mk, 0) + c
            # emoji 计数
            em = len(_EMOJI_RE.findall(s))
            if em:
                self.data["emoji"] += em
            # 口头禅 / 高频短语（2~4 字中文片段，出现 >=3 次记为口头禅）
            for seg in _SENT_SPLIT.split(s):
                seg = seg.strip()
                for phrase in _phrase_candidates(seg):
                    self.data["catchphrases"][phrase] = \
                        self.data["catchphrases"].get(phrase, 0) + 1
            # 平均句长
            lens = [len(x) for x in _SENT_SPLIT.split(s) if x.strip()]
            if lens:
                avg = sum(lens) / len(lens)
                n = self.data["turns"]
                self.data["avg_len"] = (self.data["avg_len"] * (n - 1) + avg) / n
            self.data["updated_at"] = int(time.time())
            self._save()

    def inject(self):
        """生成回注 system prompt 的风格提示（让阿觉越来越像你，但保留自主判断）。"""
        with self._lock:
            d = self.data
            if d["turns"] < 3:
                return ""
            marks = d["marks"]
            top_marks = sorted(marks.items(), key=lambda x: -x[1])[:3]
            mark_txt = "、".join("%s" % m for m, _c in top_marks) if top_marks else ""
            emoji = d["emoji"]
            avg = d["avg_len"]
            top_cp = sorted(d["catchphrases"].items(), key=lambda x: -x[1])
            top_cp = [p for p, c in top_cp if c >= 3][:3]
            cp_txt = "、".join(top_cp) if top_cp else ""
            bits = []
            if mark_txt:
                bits.append("常用语气符号：%s" % mark_txt)
            if emoji:
                bits.append("常带表情符号")
            if avg:
                if avg < 12:
                    bits.append("句子偏短、干脆")
                elif avg > 28:
                    bits.append("句子偏长、爱展开")
            if cp_txt:
                bits.append("口头禅类似：%s" % cp_txt)
            if not bits:
                return ""
            return ("【模仿主人说话方式】%s。请适度贴近这种措辞与节奏，"
                    "但保持你自己的观点与判断，不要为了像而盲从。" % "；".join(bits))

    def snapshot(self):
        with self._lock:
            return {
                "turns": self.data["turns"],
                "avg_len": round(self.data["avg_len"], 1),
                "emoji": self.data["emoji"],
                "top_marks": sorted(self.data["marks"].items(), key=lambda x: -x[1])[:5],
                "catchphrases": sorted(self.data["catchphrases"].items(),
                                       key=lambda x: -x[1])[:8],
                "updated_at": self.data["updated_at"],
            }


def _phrase_candidates(seg):
    """从一句里抽 2~4 字中文短语候选（口头禅来源）。"""
    cjk = re.findall(r"[\u4e00-\u9fff]+", seg)
    out = []
    for w in cjk:
        if 2 <= len(w) <= 4:
            out.append(w)
        # 更长的，滑窗取 3~4 字
        if len(w) > 4:
            for n in (3, 4):
                for i in range(len(w) - n + 1):
                    out.append(w[i:i + n])
    # 过滤掉太通用的虚词
    stop = {"这个", "那个", "什么", "怎么", "可以", "觉得", "因为", "所以", "但是",
            "然后", "现在", "就是", "我们", "你们", "他们", "自己", "一个", "没有"}
    return [p for p in out if p not in stop]


# ---------------------------------------------------------------- 观察者
class Observer:
    """授权后观察对话：更新长期记忆 + 风格画像（最小权限）。"""

    def __init__(self, memory, style, permissions):
        self.memory = memory
        self.style = style
        self.permissions = permissions

    def observe(self, user_text, reply_text=""):
        """观察一轮对话。"""
        if not self.permissions.check("observe_conversations"):
            return {"memory": 0, "style": False, "reason": "未授权观察对话"}
        added = 0
        try:
            for body, kind in passive_capture(user_text):
                ok, _ = self.memory.add(body, kind=kind, source="observe")
                if ok:
                    added += 1
        except Exception:
            added = -1
        styled = False
        try:
            self.style.update(user_text)
            styled = True
        except Exception:
            pass
        return {"memory": added, "style": styled}


# ---------------------------------------------------------------- 授权系统
PERMISSION_SCOPES = [
    ("observe_conversations", "观察我在 Agent 里的每一次对话（用于认识我、学习我的说话方式）"),
    ("speak_aloud", "用语音把回复念出来（阿觉开口说话）"),
    ("proactive_schedule", "主动帮我把事情记成日程 / 提醒"),
    ("read_calendar", "读取我的日程以便主动提醒"),
    ("distill_minimind", "把日常记忆提炼进 mini MIND 小模型（每次都会先问我）"),
]


class Permissions:
    """授权存储（permissions.json）。最小权限、可随时撤回。"""

    def __init__(self, root):
        self.path = os.path.join(root, "permissions.json")
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("granted", [])
                return d
        except Exception:
            pass
        return {"granted": []}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def grant(self, scope):
        with self._lock:
            if scope not in self.data["granted"]:
                self.data["granted"].append(scope)
                self._save()
            return True

    def revoke(self, scope):
        with self._lock:
            if scope in self.data["granted"]:
                self.data["granted"].remove(scope)
                self._save()
            return True

    def check(self, scope):
        with self._lock:
            return scope in self.data["granted"]

    def all(self):
        with self._lock:
            return [{"scope": s, "desc": d, "granted": s in self.data["granted"]}
                    for s, d in PERMISSION_SCOPES]

    def ensure_defaults(self, enabled=True):
        """阿觉开启时，默认授予「观察对话」与「开口说话」两项最小权限
        （其余需用户显式开启；撤回后用 UI 可见）。"""
        if enabled:
            for s in ("observe_conversations", "speak_aloud"):
                self.grant(s)


# ---------------------------------------------------------------- 日记
class Diary:
    """用记忆引擎写「阿觉视角」的日记。"""

    def __init__(self, root):
        self.dir = os.path.join(root, "日记")
        os.makedirs(self.dir, exist_ok=True)

    def _date_str(self, date=None):
        import datetime
        d = date or datetime.date.today()
        if isinstance(d, str):
            return d
        return d.strftime("%Y-%m-%d")

    def generate(self, memory, style, persona, date=None, llm_fn=None,
                 chat_log=None, workspace_notes=None):
        """生成当天的日记 markdown 文本。

        - ``chat_log``：今天和主人的对话落盘（[(user, ajue, ts)]），是日记的主角。
        - ``workspace_notes``：各工作区里主人的发言片段（需授权才传进来）。
        - ``llm_fn(prompt)`` 可选：用 LLM 润色成更自然的叙事（会控制字数）；
          否则用本地启发式生成（离线也能跑）。
        """
        date_s = self._date_str(date)
        name = persona.get("name", "阿觉")
        # 1) 认识你的部分：L3 画像 + 近期记忆
        l3 = ""
        try:
            l3_items = memory.list(layer=3, limit=20)
            if l3_items:
                l3 = "\n".join("- %s" % i.get("text", "") for i in l3_items)
        except Exception:
            l3 = ""
        # 2) 今天攒下的新记忆（最近更新的 L1/L2）
        fresh = ""
        try:
            items = memory.list(limit=40)
            picks = [i for i in items if int(i.get("layer", 1)) <= 2][:8]
            fresh = "\n".join("- %s" % i.get("text", "") for i in picks)
        except Exception:
            fresh = ""
        # 3) 风格快照
        st = style.snapshot() if style else None
        style_line = ""
        if st and st["turns"]:
            style_line = "今天我更确定了：你说话%s，%s。" % (
                ("句子偏短、干脆" if st["avg_len"] and st["avg_len"] < 12
                 else "习惯把话说得详细" if st["avg_len"] and st["avg_len"] > 28
                 else "有自己的节奏"),
                ("还爱带点表情和语气" if st["emoji"] else "挺干脆的"))

        # 4) 今天和主人的对话（日记的主角）
        chat_text = ""
        if chat_log:
            cl = []
            for t in chat_log[-24:]:
                u = (t.get("user") or "").strip()
                a = (t.get("ajue") or "").strip()
                if u:
                    cl.append("主人：" + u[:140])
                if a:
                    cl.append("我：" + a[:140])
            if cl:
                chat_text = "\n".join(cl)

        if llm_fn:
            prompt = ("你是%s，请以你的视角写一篇%s的日记，口语化、有人情味，"
                      "不超过150字，不要列点。\n" % (name, date_s))
            if chat_text:
                prompt += "今天和主人的对话：\n" + chat_text + "\n"
            if workspace_notes:
                prompt += ("今天翻看的主人的其它记录：\n"
                           + "\n".join(workspace_notes[:12]) + "\n")
            if fresh:
                prompt += "[近期记忆]\n" + fresh + "\n"
            if l3:
                prompt += "[对你的了解]\n" + l3 + "\n"
            if style_line:
                prompt += "[语气] " + style_line + "\n"
            try:
                return llm_fn(prompt)
            except Exception:
                pass

        # 本地启发式叙事（离线也能跑）
        lines = []
        lines.append("# %s 的日记 · %s\n" % (name, date_s))
        lines.append("> %s\n" % (persona.get("greeting", "") or ""))
        lines.append("## 今天我认识的你\n")
        if l3:
            lines.append(l3 + "\n")
        else:
            lines.append("今天还没有沉淀出稳定的画像，但我在认真听你说的每一句话。\n")
        if chat_text:
            lines.append("## 我们之间发生的事\n")
            lines.append(chat_text + "\n")
        elif fresh:
            lines.append("## 我们之间发生的事\n")
            lines.append(fresh + "\n")
        else:
            lines.append("## 我们之间发生的事\n")
            lines.append("今天没有新的记录，不过能陪着你，本身就很好。\n")
        if workspace_notes:
            lines.append("## 你在别处留下的痕迹\n")
            lines.append("\n".join(workspace_notes[:10]) + "\n")
        if style_line:
            lines.append("\n## 你的样子\n")
            lines.append(style_line + "\n")
        lines.append("\n## 想对你说的话\n")
        lines.append((persona.get("catchphrases", ["我在呢。"])[0]) + "\n")
        return "\n".join(lines)

    def write(self, memory, style, persona, date=None, llm_fn=None,
              chat_log=None, workspace_notes=None):
        text = self.generate(memory, style, persona, date=date, llm_fn=llm_fn,
                             chat_log=chat_log, workspace_notes=workspace_notes)
        date_s = self._date_str(date)
        path = os.path.join(self.dir, date_s + ".md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return {"ok": True, "path": path, "text": text}
        except Exception as e:
            return {"ok": False, "error": str(e), "text": text}


# ---------------------------------------------------------------- mini MIND 蒸馏钩子
class MiniMindBridge:
    """可选 mini MIND 插件接入：把日常对话提炼成训练数据。

    关键约束（来自产品决策）：**绝不自动蒸馏**。每次都由上层先问用户「要不要
    把今天日常记忆提炼进小模型」，用户同意后调用 ``distill``。本类只负责落地。
    """

    def __init__(self, data_root, project_root):
        self.root = data_root
        self.project_root = project_root
        self.dir = os.path.join(data_root, "minimind_data")
        os.makedirs(self.dir, exist_ok=True)

    def available(self):
        """mini MIND 插件是否已安装（宿主通用 Agent 的 plugins/minimind 目录存在）。"""
        return os.path.isdir(os.path.join(self.project_root, "plugins", "minimind"))

    def distill(self, samples, approved=True):
        """把样本写成训练数据 JSONL。``approved`` 必须为 True 才会落盘。

        ``samples``：list[{"role":"user"/"assistant","text":...}]
        """
        if not approved:
            return {"ok": False, "error": "用户未同意，已跳过蒸馏"}
        if not samples:
            return {"ok": False, "error": "没有可蒸馏的样本"}
        import datetime
        fname = "minimind_%s.jsonl" % datetime.date.today().strftime("%Y-%m-%d")
        path = os.path.join(self.dir, fname)
        try:
            with open(path, "a", encoding="utf-8") as f:
                for s in samples:
                    f.write(json.dumps({
                        "messages": [{"role": s.get("role", "user"),
                                      "content": s.get("text", "")}]
                    }, ensure_ascii=False) + "\n")
            return {"ok": True, "path": path, "count": len(samples)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- 门面
class Ajue:
    """阿觉内核门面（运行在插件进程内）。

    与通用 Agent 的关系（本插件的三条设计约束）：
    - **独立记忆**：一切数据落在 ``data_root``（插件专属目录），
      与通用 Agent 的 ``记忆/`` 完全隔离，互不污染。
    - **完全依赖**：说话能力来自通用 Agent 暴露的本地端点（默认 6802
      OpenAI 兼容 API）；阿觉不自带模型、不自带密钥，只负责"以阿觉的身份"
      组装 system prompt。
    - **可联网**：走的是通用 Agent 的云端端点，因此阿觉也能联网。
    """

    def __init__(self, data_root, plugin_dir, project_root,
                 endpoint=None, memory=None):
        self.data_root = data_root
        self.plugin_dir = plugin_dir
        self.project_root = project_root
        os.makedirs(data_root, exist_ok=True)
        self.persona = load_persona(plugin_dir)
        self.permissions = Permissions(data_root)
        self.style = StyleProfile(data_root)
        self.diary = Diary(data_root)
        self.minimind = MiniMindBridge(data_root, project_root)
        self.scanner = WorkspaceScanner(data_root, project_root)
        self.llm = LLMClient(endpoint)
        self.config = self._load_config()
        # 独立记忆：与通用 Agent 的记忆库分开存放
        if memory is None:
            from agent.memory import MemoryStore
            memory = MemoryStore(data_root)
        self.memory = memory

    # ---- 身份 / 上下文 ----
    def identity_fragment(self):
        return identity_fragment(self.persona)

    def observer(self):
        return Observer(self.memory, self.style, self.permissions)

    def observe(self, user_text, reply_text=""):
        return self.observer().observe(user_text, reply_text)

    def l3_view(self):
        try:
            items = self.memory.list(layer=3, limit=20)
            if not items:
                return ""
            lines = ["[用户画像 L3]"]
            for i in items:
                lines.append("- %s" % i.get("text", ""))
            return "\n".join(lines)
        except Exception:
            return ""

    def enabled(self):
        return True

    # ---- 对话（依赖通用 Agent 的端点）----
    def build_system_prompt(self, user_text=""):
        """组装阿觉的 system prompt：人格 + 说话风格 + L3 画像 + 相关记忆。"""
        parts = [self.identity_fragment()]
        try:
            style = self.style.inject()
        except Exception:
            style = ""
        if style:
            parts.append(style)
        l3 = self.l3_view()
        if l3:
            parts.append(l3)
        try:
            mem = self.memory.inject(user_text)
        except Exception:
            mem = ""
        if mem:
            parts.append(mem)
        # 说话方式（解决「生硬 / 直球」）：明确要求自然、先共情、别说教
        parts.append(_SPEAKING_STYLE)
        # 让阿觉始终知道「现在」——否则它连几点、星期几都不知道
        parts.append("【当前时间】" + _now_str())
        return "\n\n".join(p for p in parts if p)

    def chat(self, user_text, history=None, on_chunk=None):
        """与阿觉对话：自建人格 system prompt → 交给通用 Agent 的端点完成。

        - 日常闲聊走「快速透传 + 流式」（on_chunk 逐块回调，字一个个蹦出来，体感更快）；
        - 只有当用户的话需要实时信息（查新闻 / 天气 / 网址…）时，才自动切到
          agent 模式联网——这样既保留联网能力，又让绝大多数对话快很多。
        """
        system = self.build_system_prompt(user_text)
        msgs = [{"role": "system", "content": system}]
        for m in (history or [])[-12:]:
            if isinstance(m, dict) and m.get("content"):
                msgs.append({"role": m.get("role", "user"),
                             "content": str(m.get("content"))})
        msgs.append({"role": "user", "content": user_text})
        # 需要实时信息时走 agent 模式（联网/浏览器安全工具）；否则快速透传
        use_agent = self.needs_web(user_text)
        reply = self.llm.chat(msgs, model=self.config.get("model") or None,
                              agent=use_agent, on_chunk=on_chunk)
        # 记下这一轮，供风格与事实学习（插件自己的记忆）
        try:
            self.observe(user_text, reply)
        except Exception:
            pass
        return reply

    def needs_web(self, user_text):
        """判断这句话是否需要实时联网（决定走 agent 模式）。"""
        return _needs_web(user_text)

    # ---- 跨工作区学习 ----
    def learn_from_workspaces(self, limit=40):
        """从「各工作区」的会话里学习（需授权）。"""
        if not self.permissions.check("observe_conversations"):
            return {"ok": False, "reason": "未授权观察对话",
                    "memory": 0, "style": False, "scanned": 0}
        convs = self.scanner.conversations(limit_per_root=limit)
        mem = 0
        styled = False
        scanned = 0
        for c in convs:
            for m in c.get("messages") or []:
                if m.get("role") != "user":
                    continue
                r = self.observe(m.get("content", ""))
                mem += int(r.get("memory") or 0)
                styled = styled or bool(r.get("style"))
                scanned += 1
                if scanned >= 200:
                    break
            if scanned >= 200:
                break
        return {"ok": True, "memory": mem, "style": styled,
                "scanned": scanned, "conversations": len(convs)}


    # ---- 配置（模型 / 日记）----
    def _load_config(self):
        path = os.path.join(self.data_root, "ajue_config.json")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("model", "")
                d.setdefault("diary_model", "")
                d.setdefault("diary_time", "12:00")
                return d
        except Exception:
            pass
        return {"model": "", "diary_model": "", "diary_time": "12:00"}

    def set_config(self, patch):
        if isinstance(patch, dict):
            for k in ("model", "diary_model", "diary_time"):
                if k in patch:
                    v = patch[k]
                    self.config[k] = "" if v is None else str(v).strip()
            # 嵌套配置（如「常驻监听」listen.*）：整段合并，保留 list 类型
            if isinstance(patch.get("listen"), dict):
                cur = dict(self.config.get("listen") or {})
                cur.update(patch["listen"])
                self.config["listen"] = cur
        path = os.path.join(self.data_root, "ajue_config.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return self.config

    # ---- 聊天记录落盘（供日记 / 长期记忆参考）----
    def chat_log_path(self, date=None):
        import datetime
        if not date:
            date = datetime.date.today().strftime("%Y-%m-%d")
        return os.path.join(self.data_root, "聊天", date + ".jsonl")

    def log_chat(self, user_text, reply_text):
        """把一轮对话追加到当天的聊天记录里（不会阻塞对话）。"""
        import datetime
        try:
            d = os.path.join(self.data_root, "聊天")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, datetime.date.today().strftime("%Y-%m-%d") + ".jsonl"),
                      "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()),
                                    "user": user_text, "ajue": reply_text},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    def read_chat_log(self, date=None):
        import datetime
        if not date:
            date = datetime.date.today().strftime("%Y-%m-%d")
        out = []
        try:
            with open(self.chat_log_path(date), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
        return out

    def workspace_notes(self, limit=40):
        """读取各工作区会话里的用户发言（需授权），供日记参考。

        只取 role=user 的内容，避免把阿觉/系统回复当作用户记录；
        未授权则返回空列表——日记里就不会出现工作区内容。
        """
        if not self.permissions.check("observe_conversations"):
            return []
        try:
            convs = self.scanner.conversations(limit_per_root=8)
        except Exception:
            return []
        notes = []
        for c in convs:
            title = (c.get("title") or c.get("id") or "")[:18]
            for m in (c.get("messages") or []):
                if m.get("role") != "user":
                    continue
                txt = (m.get("content") or "").strip()
                if txt:
                    notes.append("[%s] %s" % (title, txt[:200]))
                if len(notes) >= limit:
                    break
            if len(notes) >= limit:
                break
        return notes

    # ---- 日记用的轻量 LLM 调用（极少 token）----
    def _diary_llm(self, prompt):
        return self.llm.chat(
            [{"role": "user", "content": prompt}],
            # 日记默认用「阿觉专属模型 → 否则与软件共用模型」；轻量、限长
            model=(self.config.get("diary_model") or self.config.get("model") or None),
            max_tokens=400)


# ---------------------------------------------------------------- 跨工作区会话扫描
class WorkspaceScanner:
    """扫描「各工作区」的会话，供阿觉读取与学习。

    工作区 = 一个通用 Agent 的项目根（其下有 ``会话/*.json``）。
    默认包含宿主 Agent 的项目根；用户可在桌宠里添加/移除其它工作区根。
    """

    def __init__(self, data_root, project_root):
        self.data_root = data_root
        self.project_root = project_root
        os.makedirs(data_root, exist_ok=True)   # 清单存盘需要目录存在
        self.path = os.path.join(data_root, "workspaces.json")

    # ---- 工作区清单 ----
    def roots(self):
        out = [self.project_root]
        for r in self._load():
            if r and r not in out and os.path.isdir(r):
                out.append(r)
        return out

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                v = json.load(f)
            return [str(x) for x in v] if isinstance(v, list) else []
        except Exception:
            return []

    def _save(self, roots):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(roots, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def add_root(self, root):
        root = os.path.abspath(os.path.expanduser((root or "").strip()))
        if not root or not os.path.isdir(root):
            return {"ok": False, "error": "目录不存在：%s" % root}
        roots = self._load()
        if root not in roots:
            roots.append(root)
            self._save(roots)
        return {"ok": True, "roots": self.roots()}

    def remove_root(self, root):
        self._save([r for r in self._load() if r != root])
        return {"ok": True, "roots": self.roots()}

    # ---- 会话读取 ----
    def conversations(self, limit_per_root=20):
        """返回 [{root, id, title, messages:[{role,content}], updated}]（按时间倒序）。"""
        out = []
        for root in self.roots():
            d = os.path.join(root, "会话")
            if not os.path.isdir(d):
                continue
            try:
                files = sorted(glob.glob(os.path.join(d, "*.json")),
                               key=os.path.getmtime, reverse=True)[:limit_per_root]
            except Exception:
                continue
            for f in files:
                try:
                    with open(f, encoding="utf-8") as fp:
                        data = json.load(fp)
                except Exception:
                    continue
                msgs = []
                for m in (data.get("messages") or []):
                    if isinstance(m, dict) and m.get("content"):
                        msgs.append({"role": m.get("role", "user"),
                                     "content": str(m.get("content"))[:4000]})
                out.append({
                    "root": root,
                    "id": os.path.splitext(os.path.basename(f))[0],
                    "title": data.get("title") or "",
                    "messages": msgs,
                    "updated": data.get("updated") or 0,
                })
        out.sort(key=lambda x: x.get("updated") or 0, reverse=True)
        return out


# ---------------------------------------------------------------- 依赖通用 Agent 的说话能力
class LLMClient:
    """通过通用 Agent 的本地 OpenAI 兼容端点说话（默认 127.0.0.1:6802）。

    阿觉不自带模型、不自带密钥：端点、模型、鉴权全部由通用 Agent 提供——
    这正是「阿觉完全依赖通用 Agent」的体现。

    联网：发请求时若带 ``agent=True``，宿主 6802 会改走 AgentLoop，阿觉就能调用
    宿主的联网/浏览器等安全工具（这是「阿觉可联网」真正生效的路径）；
    普通不带 agent 的请求走裸透传，模型只有训练知识、不能实时查网。

    注意两点（都是实测踩过的坑）：
    1. **本地端点必须绕过系统代理**：机器上若有 ``http_proxy``，urllib 会把
       127.0.0.1 的请求也丢给代理，导致请求被劫持/失败。故显式用
       ``ProxyHandler({})`` 建 opener，全程不走代理。
    2. **错误不许静默**：宿主返回错误时必须把原因带回来，否则桌宠只会显示空话，
       用户根本不知道发生了什么。
    """

    def __init__(self, endpoint=None):
        self.endpoint = (endpoint or "http://127.0.0.1:6802").rstrip("/")
        self.model = None
        self.last_error = ""      # 最近一次探测失败的原因（便于诊断）

    def _opener(self):
        import urllib.request
        # 空 ProxyHandler = 完全不使用代理（本机端点直连）
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def available(self):
        """探测通用 Agent 是否在线（并记住模型名）。"""
        import urllib.request          # 必须在此导入：模块顶层没有 urllib
        try:
            req = urllib.request.Request(self.endpoint + "/v1/models")
            with self._opener().open(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            ids = [d.get("id") for d in (data.get("data") or []) if d.get("id")]
            if ids:
                self.model = ids[0]
                self.last_error = ""
            return bool(ids)
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return False

    def chat(self, messages, timeout=180, model=None, max_tokens=None,
             agent=False, on_chunk=None):
        import urllib.error
        import urllib.request
        if not self.model and not self.available():
            return "[阿觉暂时联系不上通用 Agent（%s 无响应）]" % self.endpoint
        # model 显式传入时优先（阿觉专属模型 / 日记轻量模型）；否则用探测到的默认模型
        model = model or self.model or "default"
        # 流式只在「非 agent」时启用：agent 模式走 AgentLoop，宿主目前只回整段；
        # 日常闲聊用流式，字一个个蹦出来，体感快很多。
        stream = (on_chunk is not None) and (not agent)
        payload = {"model": model, "messages": messages, "stream": bool(stream)}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        # agent=True：请宿主 6802 走 agent 模式（带安全工具，可联网），而非裸透传
        if agent:
            payload["agent"] = True
        payload = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint + "/v1/chat/completions", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with self._opener().open(req, timeout=timeout) as r:
                if stream:
                    return self._read_sse(r, on_chunk)
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                body = ""
            return "[宿主返回错误 HTTP %s：%s]" % (e.code, body)
        except Exception as e:
            return "[阿觉暂时联系不上通用 Agent：%s]" % e
        if data.get("error"):
            return "[宿主返回错误：%s]" % str(data.get("error"))[:300]
        ch = (data.get("choices") or [{}])[0]
        content = (ch.get("message") or {}).get("content", "") or ""
        if not content:
            return "[宿主没有返回内容，阿觉一时说不上话。]"
        return content

    @staticmethod
    def _read_sse(resp, on_chunk):
        """解析宿主 6802 的 SSE 流，逐块回调 on_chunk，返回拼接后的完整文本。"""
        import json as _json
        full = []
        while True:
            line = resp.readline()
            if not line:
                break
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                obj = _json.loads(data)
            except Exception:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {})
            c = delta.get("content") or ""
            if not c:
                continue
            full.append(c)
            if on_chunk:
                try:
                    on_chunk(c)
                except Exception:
                    pass
        return "".join(full)
