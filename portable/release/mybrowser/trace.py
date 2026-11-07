# -*- coding: utf-8 -*-
"""会话录制 / 回放：对齐 Lightpanda 的 `agent --trace` + `/save` PandaScript。

Lightpanda 的设计哲学（非常值得借鉴）：
  · `--trace` 把 Agent 每步操作写成「一行 JSON」，便于复盘与调试；
  · `/save` 把整段会话导出成 **PandaScript**——原生 JS + 一组浏览器原语，
    确定性、零 token，上线时无需再跑模型。

本模块在 MyBrowser 里复刻这一思路：
  · Recorder 记录每一条浏览器指令（一行 JSON trace）；
  · to_script() 导出成 MyBrowser 版 PandaScript（navigate/click/type/press…）；
  · replay_script() 把脚本重新喂给浏览器，确定性复现整段操作。
"""
import json
import os
import re
import time


# ---------------------------------------------------------------------------
# 指令 -> PandaScript 原语（录制时把命令翻译成语义原语）
# ---------------------------------------------------------------------------

def _q(s):
    """包成 JS 字符串字面量。"""
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % s


def _type_prim(rest):
    parts = rest.strip().split(" ", 1)
    ref = parts[0] if parts else "e0"
    text = parts[1] if len(parts) > 1 else ""
    return "type(%s, %s)" % (ref, _q(text))


SCRIPT_MAP = {
    "NAVIGATE": lambda rest: "navigate(%s)" % _q(rest.strip()),
    "SEARCH": lambda rest: "search(%s)" % _q(rest.strip()),
    "SNAPSHOT": lambda rest: "snapshot()",
    "DIFF": lambda rest: "snapshot()",
    "READ": lambda rest: "read()",
    "CLICK": lambda rest: "click(%s)" % rest.strip(),
    "CLICK_REF": lambda rest: "click(%s)" % rest.strip(),
    "TYPE": lambda rest: _type_prim(rest),
    "TYPE_REF": lambda rest: _type_prim(rest),
    "PRESS": lambda rest: "press(%s)" % _q(rest.strip()),
    "SCROLL": lambda rest: "scroll(%s)" % _q(rest.strip() or "down"),
    "BACK": lambda rest: "back()",
    "FORWARD": lambda rest: "forward()",
    "RELOAD": lambda rest: "reload()",
    "DUMP": lambda rest: "dump(%s)" % _q((rest.split()[0] if rest.split() else "markdown")),
    "SHOT": lambda rest: "shot(%s)" % _q(rest.strip() or "shot.png"),
}


def _looks_like_ref(s):
    return s.startswith("e") and s[1:].isdigit()


# ---------------------------------------------------------------------------
# 回放：PandaScript 原语 -> 浏览器指令
# ---------------------------------------------------------------------------

REPLAY_MAP = {
    "navigate": lambda a: "NAVIGATE " + (a[0] if a else ""),
    "search": lambda a: "SEARCH " + (a[0] if a else ""),
    "snapshot": lambda a: "SNAPSHOT",
    "read": lambda a: "READ",
    "click": lambda a: ("CLICK_REF " + a[0]) if (a and _looks_like_ref(a[0])) else ("CLICK " + (a[0] if a else "")),
    "type": lambda a: (("TYPE_REF " + a[0] + " " + (a[1] if len(a) > 1 else ""))
                       if (a and _looks_like_ref(a[0]))
                       else ("TYPE " + (a[0] if a else "") + " " + (a[1] if len(a) > 1 else ""))),
    "press": lambda a: "PRESS " + (a[0] if a else ""),
    "scroll": lambda a: "SCROLL " + (a[0] if a else "down"),
    "back": lambda a: "BACK",
    "forward": lambda a: "FORWARD",
    "reload": lambda a: "RELOAD",
    "dump": lambda a: "DUMP " + (a[0] if a else "markdown"),
    "shot": lambda a: "SHOT " + (a[0] if a else "shot.png"),
    "cmd": lambda a: (a[0] if a else ""),
}


def _unq(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        try:
            # 用 JSON 解码，正确处理 \" 转义与 \uXXXX
            v = json.loads(s)
            return v if isinstance(v, str) else str(v)
        except Exception:
            return s[1:-1]
    return s


def _parse_call(line):
    """解析 `name(arg1, arg2)` 形式的一行 PandaScript。"""
    line = line.strip().rstrip(";").strip()
    if not line:
        return None
    m = re.match(r"^([A-Za-z_][\w]*)\s*\((.*)\)\s*$", line, re.DOTALL)
    if not m:
        return None
    name = m.group(1)
    raw = m.group(2)
    args = []
    buf = ""
    q = None
    for ch in raw:
        if q:
            buf += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            buf += ch
        elif ch == ",":
            args.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        args.append(buf.strip())
    return name.lower(), [_unq(a) for a in args]


def replay_script(text, execute_fn, on_step=None):
    """把一段 PandaScript 回放进浏览器。

    execute_fn: 接受一个完整浏览器指令字符串（如 "NAVIGATE https://x"），
                返回结果文本。
    返回每步的结果列表。
    """
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        parsed = _parse_call(line)
        if not parsed:
            results.append("跳过无法解析的行: %s" % line)
            continue
        name, args = parsed
        builder = REPLAY_MAP.get(name)
        if not builder:
            results.append("未知原语: %s" % name)
            continue
        cmd = builder(args)
        try:
            r = execute_fn(cmd)
        except Exception as e:
            r = "ERR: %s" % e
        results.append("%s -> %s" % (cmd, str(r)[:200]))
        if on_step:
            on_step(cmd, r)
    return results


# ---------------------------------------------------------------------------
# Recorder：录制 Agent 的每一步操作
# ---------------------------------------------------------------------------

class Recorder:
    """记录浏览器指令序列，导出 trace(JSONL) 与 PandaScript。"""

    def __init__(self):
        self.steps = []
        self.active = False

    def start(self):
        self.active = True
        return "录制已开始（每步操作都会写入 trace）"

    def stop(self):
        self.active = False
        return "录制已停止，共 %d 步" % len(self.steps)

    def toggle(self):
        return self.start() if not self.active else self.stop()

    def status(self):
        return "录制: %s | 步数: %d" % ("开" if self.active else "关", len(self.steps))

    def record(self, action, rest="", result=None):
        """记录一步（仅当 active）。action 是原指令名，rest 是参数。"""
        if not self.active:
            return
        res = (result or "")
        if isinstance(res, str) and len(res) > 160:
            res = res[:157] + "..."
        self.steps.append({
            "ts": time.time(),
            "action": action,
            "args": rest.strip(),
            "result": res,
        })

    def clear(self):
        self.steps = []

    # ---------------- 导出 ----------------

    def to_trace_jsonl(self):
        return "\n".join(json.dumps(s, ensure_ascii=False) for s in self.steps)

    def save_trace(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_trace_jsonl())
            if self.steps:
                f.write("\n")
        return path

    def to_script(self):
        """导出成 MyBrowser 版 PandaScript（确定性、可重放）。"""
        header = [
            "// MyBrowser PandaScript",
            "// 由会话录制自动生成，确定性、零 token。",
            "// 重放：在 BROWSER 指令里执行 REPLAY <本文件>",
            "",
        ]
        lines = list(header)
        for s in self.steps:
            fn = SCRIPT_MAP.get(s["action"])
            if fn:
                lines.append(fn(s["args"]))
            else:
                # 兜底：原样记录为 cmd("RAW ...")
                lines.append("cmd(%s)" % _q("%s %s" % (s["action"], s["args"])))
        return "\n".join(lines) + "\n"

    def save_script(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_script())
        return path
