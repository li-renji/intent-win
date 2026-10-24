# -*- coding: utf-8 -*-
"""工具注册表 + 6801 总线执行体（对齐 ai_service：所有对外能力统一出口，不绕过）。

- feature flags：只有出现在配置 tools 清单里的工具才注册 → 零未用工具 token；
- tool_max_chars 统一截断工具结果，截断带 ...(truncated)；
- 写盘类危险工具默认不注册（对齐"眼睛/笔/橡皮"授权模型）。
"""

import json
import os


def _fmt_size(n):
    """字节数 → 人类可读。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fPB" % n


# 每个工具的 OpenAI function-calling schema（模型能看懂的自描述）
# 未在 配置.qdai 的 tools= 里列出的工具不会出现在这里 → 零未用工具 token
TOOL_SCHEMAS = {
    "browser": {
        "type": "function",
        "function": {
            "name": "browser",
            "description": (
                "控制内置浏览器（WebView2）。命令为一行文本协议："
                "NAVIGATE <url> 导航；SNAPSHOT 取可交互元素编号表+正文摘要；"
                "CLICK <编号> 点击；TYPE <编号> <文本> 填表；"
                "SCROLL down|up|top|bottom 滚动；BACK/FORWARD/RELOAD 导航；"
                "READ 只取正文纯文本（最省 token）。"
                "先 SNAPSHOT 拿编号表，再 CLICK/TYPE。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "浏览器文本协议指令，如 'SNAPSHOT'"}
                },
                "required": ["cmd"],
            },
        },
    },
    "memo": {
        "type": "function",
        "function": {
            "name": "memo",
            "description": "本地记忆读写。op=write 追加写入一条记忆；op=search 按关键词检索已有记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["search", "write"],
                           "description": "操作类型：search 检索 / write 写入"},
                    "kw": {"type": "string", "description": "检索关键词（op=search 时必填）"},
                    "text": {"type": "string", "description": "要写入的记忆内容（op=write 时必填）"},
                },
                "required": ["op"],
            },
        },
    },
    "skills": {
        "type": "function",
        "function": {
            "name": "skills",
            "description": "列出已安装的技能（skills 目录）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "phone": {
        "type": "function",
        "function": {
            "name": "phone",
            "description": (
                "把任务交给手机上的奇点OS本地智能体执行。它能在手机容器里跑 Linux 命令、"
                "读写文件、修改奇点OS界面、启动安卓应用、查知识库。"
                "适合「在手机上做点什么」的任务，比如「把主题改成深色」「打开设置」「看看手机里有哪些文件」。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "交给手机执行的任务描述，要说清楚做什么"}
                },
                "required": ["task"],
            },
        },
    },
    "fs": {
        "type": "function",
        "function": {
            "name": "fs",
            "description": (
                "读取本机文件与目录。op=list 列目录（树形）；op=read 读文本文件；"
                "op=search 按文件名递归查找；op=grep 按内容递归查找；"
                "op=info 看大小/修改时间。默认只读，写盘需另行开启。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["list", "read", "search", "grep", "info"],
                           "description": "操作类型"},
                    "path": {"type": "string",
                             "description": "目标路径，留空默认桌面。如 D:\\\\代码 或 ~/Desktop"},
                    "pattern": {"type": "string", "description": "search/grep 的匹配词"},
                    "depth": {"type": "integer", "description": "list 的目录深度，默认2"},
                },
                "required": ["op"],
            },
        },
    },
    "code": {
        "type": "function",
        "function": {
            "name": "code",
            "description": (
                "在本机执行代码并返回输出。lang=python 跑 Python；lang=shell 跑 Windows 命令。"
                "适合计算、批量处理文件、调用本机能力。有超时保护。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lang": {"type": "string", "enum": ["python", "shell"],
                             "description": "语言，默认 python"},
                    "code": {"type": "string", "description": "要执行的代码/命令"},
                },
                "required": ["code"],
            },
        },
    },
    "screen": {
        "type": "function",
        "function": {
            "name": "screen",
            "description": (
                "操控本机屏幕。op=shot 全屏截图（返回图片路径与屏幕尺寸）；"
                "op=key 按键或组合键（如 'win+d'、'ctrl+c'、'enter'）；"
                "op=type 输入一段文本；op=click 点击坐标 x y；op=move 移动鼠标。"
                "坐标以屏幕左上角为原点，用 shot 拿到的尺寸换算。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["shot", "key", "type", "click", "move"],
                           "description": "操作类型"},
                    "keys": {"type": "string", "description": "op=key 的按键，如 'alt+tab'"},
                    "text": {"type": "string", "description": "op=type 要输入的文本"},
                    "x": {"type": "integer", "description": "op=click/move 的横坐标"},
                    "y": {"type": "integer", "description": "op=click/move 的纵坐标"},
                },
                "required": ["op"],
            },
        },
    },
}


class ToolBus:
    def __init__(self, cfg):
        self.cfg = cfg
        self.max_chars = cfg["tool_max_chars"]
        self.handlers = {}   # name -> callable(args: dict) -> str
        self._register_enabled()

    # ---- 注册（仅配置清单内的工具） ----
    def _register_enabled(self):
        for name in self.cfg["tools"]:
            fn = getattr(self, "_tool_" + name, None)
            if fn:
                self.handlers[name] = fn

    def enabled(self):
        return list(self.handlers)

    # ---- OpenAI function-calling schema（只导出已注册工具） ----
    def openai_schemas(self):
        out = []
        for name in self.handlers:
            sch = TOOL_SCHEMAS.get(name)
            if sch:
                out.append(sch)
        return out

    # ---- 直调单个工具（agent 循环内部用；返回纯文本结果） ----
    def invoke(self, name, args):
        """执行一个已注册工具，返回 (ok: bool, result: str)。"""
        fn = self.handlers.get(name)
        if not fn:
            return False, "工具未启用或不存在: %s" % name
        try:
            result = str(fn(args or {}))
        except Exception as e:
            return False, "工具执行失败: %s" % e
        if len(result) > self.max_chars:
            result = result[: self.max_chars] + "...(truncated)"
        return True, result

    # ---- 6801 总线入口 ----
    def dispatch(self, raw):
        """raw: JSON 行 {"tool": "...", "args": {...}} → JSON 行结果。"""
        try:
            req = json.loads(raw)
            name = req.get("tool", "")
            args = req.get("args", {}) or {}
            fn = self.handlers.get(name)
            if not fn:
                return json.dumps({"ok": False, "error": "工具未启用或不存在: %s" % name},
                                  ensure_ascii=False)
            result = fn(args)
            result = str(result)
            if len(result) > self.max_chars:
                result = result[: self.max_chars] + "...(truncated)"
            return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    # ---- 内置工具 ----
    def _tool_skills(self, args):
        """skills 列表（对齐 SKILLS LIST）：扫描 skills/ 目录。"""
        d = os.path.join(self.cfg["project_root"], "skills")
        if not os.path.isdir(d):
            return "（skills 目录为空）"
        items = sorted(f for f in os.listdir(d) if not f.startswith("."))
        return "\n".join(items) if items else "（skills 目录为空）"

    # ---- fs：本机文件与目录（默认只读） ----
    def _resolve_path(self, raw, default_desktop=True):
        """把模型给的路径解析成绝对路径。空值 → 桌面。"""
        p = str(raw or "").strip().strip('"').strip("'")
        if not p:
            if default_desktop:
                return os.path.join(os.path.expanduser("~"), "Desktop")
            return os.getcwd()
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.join(self.cfg["project_root"], p)
        return os.path.normpath(p)

    def _tool_fs(self, args):
        op = str(args.get("op", "list")).strip().lower()
        path = self._resolve_path(args.get("path"))
        if not os.path.exists(path):
            return "路径不存在: %s" % path

        if op == "list":
            depth = int(args.get("depth", 2) or 2)
            depth = max(1, min(depth, 4))
            lines = ["目录: %s" % path]
            n = [0]

            def walk(p, ind, d):
                if n[0] >= 200:
                    return
                try:
                    entries = sorted(os.listdir(p),
                                     key=lambda x: (not os.path.isdir(os.path.join(p, x)), x.lower()))
                except OSError as e:
                    lines.append(ind + "[无法读取: %s]" % e)
                    return
                for e in entries:
                    if e.startswith("."):
                        continue
                    full = os.path.join(p, e)
                    isdir = os.path.isdir(full)
                    if isdir:
                        lines.append(ind + "📁 " + e + "/")
                        n[0] += 1
                        if d < depth:
                            walk(full, ind + "  ", d + 1)
                    else:
                        try:
                            sz = os.path.getsize(full)
                        except OSError:
                            sz = -1
                        lines.append(ind + "   %s  (%s)" % (e, _fmt_size(sz)))
                        n[0] += 1

            walk(path, "", 1)
            if n[0] >= 200:
                lines.append("…（条目过多，已截断；可用 search/grep 精确查找）")
            return "\n".join(lines)

        if op == "read":
            if os.path.isdir(path):
                return "这是目录，请用 op=list"
            try:
                sz = os.path.getsize(path)
            except OSError:
                sz = 0
            limit = int(self.cfg.get("fs_read_max_chars", 6000))
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    data = f.read(limit + 1)
            except OSError as e:
                return "读取失败: %s" % e
            head = "文件: %s (%s)\n" % (path, _fmt_size(sz))
            if len(data) > limit:
                return head + "---\n" + data[:limit] + "\n…(truncated，共 %s)" % _fmt_size(sz)
            return head + "---\n" + data

        if op == "info":
            try:
                st = os.stat(path)
            except OSError as e:
                return "stat 失败: %s" % e
            kind = "目录" if os.path.isdir(path) else "文件"
            import time as _t
            return "%s: %s\n类型: %s\n大小: %s\n修改: %s" % (
                kind, path, kind, _fmt_size(st.st_size),
                _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(st.st_mtime)))

        if op == "search":
            pat = str(args.get("pattern", "")).strip().lower()
            if not pat:
                return "op=search 需要 pattern"
            hits = []
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    if pat in f.lower():
                        hits.append(os.path.join(root, f))
                        if len(hits) >= 50:
                            break
                if len(hits) >= 50:
                    break
            return "\n".join(hits) if hits else "（无匹配）"

        if op == "grep":
            pat = str(args.get("pattern", "")).strip()
            if not pat:
                return "op=grep 需要 pattern"
            pl = pat.lower()
            hits = []
            scanned = 0
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    full = os.path.join(root, f)
                    try:
                        if os.path.getsize(full) > 2 * 1024 * 1024:
                            continue
                        with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                            for i, line in enumerate(fh, 1):
                                if pl in line.lower():
                                    hits.append("%s:%d: %s" % (full, i, line.strip()[:160]))
                                    if len(hits) >= 40:
                                        break
                    except OSError:
                        continue
                    scanned += 1
                    if len(hits) >= 40:
                        break
                if len(hits) >= 40:
                    break
            if not hits:
                return "（%d 个文件中无匹配）" % scanned
            return "\n".join(hits)

        return "未知 op: %s（可用 list/read/search/grep/info）" % op

    # ---- code：本机代码执行 ----
    def _tool_code(self, args):
        lang = str(args.get("lang", "python")).strip().lower()
        code = str(args.get("code", "")).strip()
        if not code:
            return "缺少 code 参数"
        import subprocess
        import sys as _sys
        timeout = int(self.cfg.get("code_timeout", 60))
        timeout = max(5, min(timeout, 300))
        cwd = self.cfg["project_root"]
        if lang == "python":
            cmd = [_sys.executable, "-c", code]
        elif lang == "shell":
            cmd = ["cmd", "/c", code]
        else:
            return "未知 lang: %s（可用 python/shell）" % lang
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, cwd=cwd, encoding="utf-8",
                               errors="replace")
        except subprocess.TimeoutExpired:
            return "执行超时（>%ds），已终止。可拆分任务或减小数据量。" % timeout
        except Exception as e:
            return "执行失败: %s" % e
        out = []
        if p.stdout:
            out.append(p.stdout.rstrip())
        if p.stderr:
            out.append("[stderr] " + p.stderr.rstrip())
        body = "\n".join(out) if out else "（无输出）"
        body += "\n[exit=%d]" % p.returncode
        limit = int(self.cfg.get("code_max_chars", 6000))
        if len(body) > limit:
            body = body[:limit] + "\n…(truncated)"
        return body

    # ---- screen：本机屏幕操控（纯 ctypes，无第三方依赖） ----
    def _tool_screen(self, args):
        op = str(args.get("op", "")).strip().lower()
        try:
            from . import screen_ctl as sc
        except Exception as e:
            return "屏幕操控不可用（仅 Windows）: %s" % e
        if op == "shot":
            return sc.shoot(self.cfg["project_root"])
        if op == "key":
            keys = str(args.get("keys", "")).strip()
            if not keys:
                return "op=key 需要 keys，如 'win+d'"
            return sc.send_key(keys)
        if op == "type":
            text = str(args.get("text", ""))
            if not text:
                return "op=type 需要 text"
            return sc.type_text(text)
        if op in ("click", "move"):
            try:
                x = int(args.get("x", 0) or 0)
                y = int(args.get("y", 0) or 0)
            except (TypeError, ValueError):
                return "op=%s 需要整数坐标 x/y" % op
            return sc.click(x, y) if op == "click" else sc.move(x, y)
        return "未知 op: %s（可用 shot/key/type/click/move）" % op

    def _tool_memo(self, args):
        """本地记忆（对齐 MEMO 协议的 Windows 侧最小实现）：search/write。"""
        op = args.get("op", "search")
        path = os.path.join(self.cfg["project_root"], "记忆.txt")
        if op == "write":
            text = str(args.get("text", "")).strip()
            if not text:
                return "MEMO 写入需要 text"
            with open(path, "a", encoding="utf-8") as f:
                f.write(text.replace("\n", " ") + "\n")
            return "已写入记忆"
        kw = str(args.get("kw", ""))
        if not os.path.exists(path):
            return "（记忆为空）"
        hits = [l.rstrip() for l in open(path, encoding="utf-8") if kw in l]
        return "\n".join(hits[-20:]) if hits else "（无命中）"

    def _tool_browser(self, args):
        """浏览器操作：转发给引擎适配器（由 main 注入）。"""
        if self._browser_dispatch is None:
            return "浏览器未启用"
        cmd = str(args.get("cmd", "")).strip()
        return self._browser_dispatch(cmd)

    _browser_dispatch = None

    def attach_browser(self, dispatch):
        self._browser_dispatch = dispatch

    def _tool_phone(self, args):
        """把任务交给手机上的奇点OS本地智能体执行（POST /agent/run）。

        走本机网关 127.0.0.1:9997 —— 网关负责路径改写、prompt 裁剪、
        超时重试，以及手机 IP 变化时的自动发现。
        """
        import urllib.request
        import urllib.error

        task = str(args.get("task", "")).strip()
        if not task:
            return "缺少 task 参数"
        url = self.cfg.get("phone_gateway", "http://127.0.0.1:9997/agent/run")
        key = self.cfg.get("api_key", "")
        body = json.dumps({"prompt": task, "rounds": 5}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if key:
            # 与 llm._hdr_value 同因：非 latin-1 的 Key 会让 urllib 直接抛错
            try:
                key.encode("latin-1")
                hdr_key = key
            except UnicodeEncodeError:
                hdr_key = key.encode("utf-8").decode("latin-1")
            req.add_header("Authorization", "Bearer " + hdr_key)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return "手机返回 HTTP %s: %s" % (e.code, e.read()[:200].decode("utf-8", "ignore"))
        except Exception as e:
            return "连不上手机（%s）。手机可能深度睡眠了，解锁并打开起点OS mini 即可恢复。" % type(e).__name__
        try:
            data = json.loads(raw)
        except Exception:
            return raw[:1000]
        if not data.get("ok"):
            return "手机执行失败: %s" % raw[:400]
        answer = data.get("answer", "")
        trace = data.get("trace") or []
        acted = []
        for step in trace:
            if isinstance(step, dict) and step.get("tool"):
                acted.append("%s → %s" % (step.get("tool"), str(step.get("result", ""))[:120]))
        lines = ["手机执行结果：", answer]
        if acted:
            lines.append("\n实际动作：")
            lines.extend("  · " + a for a in acted)
        return "\n".join(lines)
