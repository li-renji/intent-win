# -*- coding: utf-8 -*-
"""工具注册表 + 6801 总线执行体（对齐 ai_service：所有对外能力统一出口，不绕过）。

- feature flags：只有出现在配置 tools 清单里的工具才注册 → 零未用工具 token；
- tool_max_chars 统一截断工具结果，截断带 ...(truncated)；
- 写盘类危险工具默认不注册（对齐"眼睛/笔/橡皮"授权模型）。
"""

import datetime
import json
import os
import re
import shutil
import subprocess

from . import econ


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
    "spawn": {
        "type": "function",
        "function": {
            "name": "spawn",
            "description": (
                "分裂一个子 Agent 去独立执行一个子任务。遇到大任务时，你（主 Agent）先把它拆成几个"
                "可独立完成的子任务，逐个 spawn 给子 Agent；子 Agent 用自己的上下文跑工具、互不干扰，"
                "只把结论汇报给你。你收回所有子任务结论后再统一汇总交付。适合「分头查资料/分头处理」类任务。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string",
                             "description": "交给子 Agent 的子任务，要说清楚目标、范围和要回报什么"},
                    "tools": {"type": "string",
                              "description": "允许子 Agent 用的工具，逗号分隔，默认 fs,code"},
                },
                "required": ["task"],
            },
        },
    },
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
            "description": ("长期记忆（跨会话持久化，用户画像/偏好/事实/待办）。"
                            "op=write 写入一条记忆（自动去重合并）；op=search 按关键词检索；"
                            "op=list 列出记忆；op=delete 按 id 删除。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["search", "write", "list", "delete"],
                           "description": "操作类型：search 检索 / write 写入 / list 列出 / delete 删除"},
                    "kw": {"type": "string", "description": "检索关键词（op=search 时必填）"},
                    "text": {"type": "string", "description": "要写入的记忆内容（op=write 时必填）"},
                    "kind": {"type": "string", "enum": ["profile", "preference", "fact", "task"],
                             "description": "记忆类型：档案/偏好/事实/待办（默认 fact）"},
                    "tags": {"type": "string", "description": "标签，逗号分隔（可选）"},
                    "id": {"type": "string", "description": "记忆 id（op=delete 时必填）"},
                    "limit": {"type": "integer", "description": "返回条数上限"},
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
                "读取本机文件与目录。op=list 列目录（树形）；op=read 读文本文件（大文件用 offset/limit 分段）；"
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
                    "offset": {"type": "integer",
                               "description": "read 的起始行号（从1开始），续读时用上一轮提示的 next_offset"},
                    "limit": {"type": "integer", "description": "read 的最大行数，默认 300"},
                },
                "required": ["op"],
            },
        },
    },
    "kb": {
        "type": "function",
        "function": {
            "name": "kb",
            "description": (
                "知识库检索。op=search 按文件名关键词检索（电脑里有哪些文件，秒查路径）；"
                "op=grep 检索已入库文档的正文内容（回答'文件里写了什么'）；"
                "op=ingest 把指定文件（或自动挑选候选文档）正文入库以便检索；"
                "op=stats 看索引状态。找不到时再用 fs/search 深挖。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["search", "grep", "ingest", "stats"],
                           "description": "操作类型"},
                    "kw": {"type": "string", "description": "检索关键词（op=search / op=grep 必填，可多个词空格分隔）"},
                    "paths": {"type": "string", "description": "op=ingest 时的文件路径，多个用分号分隔；留空则自动挑选候选文档"},
                    "limit": {"type": "integer", "description": "返回条数，默认 15，最大 30"},
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
    "ctx": {
        "type": "function",
        "function": {
            "name": "ctx",
            "description": (
                "查看与调试自身上下文。op=inspect 查看模型容量/估算占用/系统提示/事实摘要/会话消息；"
                "op=facts_add 追加一条事实（text）；op=facts_remove 按序号删除事实（index）；"
                "op=reset 清空事实摘要；op=compact 折叠早期历史；op=limit 查看或设置容量（tok，0=自动）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string",
                           "enum": ["inspect", "facts_add", "facts_remove", "reset", "compact", "limit"],
                           "description": "操作类型"},
                    "text": {"type": "string", "description": "facts_add 要追加的事实文本"},
                    "index": {"type": "integer",
                              "description": "facts_remove 要删除的事实序号（从0开始）"},
                    "limit": {"type": "integer",
                              "description": "limit 设置的容量 tok（0=自动）"},
                },
                "required": ["op"],
            },
        },
    },
    "web": {
        "type": "function",
        "function": {
            "name": "web",
            "description": (
                "联网能力：op=search 实时搜索关键词（返回标题+链接，无需 API Key）；"
                "op=fetch 抓取某个网页的正文纯文本（去广告/脚本，最省 token）。"
                "适合查最新资料、读网页内容。网络不可用时会明确提示。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["search", "fetch"],
                           "description": "操作类型"},
                    "q": {"type": "string", "description": "search 的搜索词"},
                    "url": {"type": "string", "description": "fetch 的目标网址"},
                    "limit": {"type": "integer", "description": "search 返回条数，默认 5"},
                    "max_chars": {"type": "integer", "description": "fetch 正文最大字符，默认 4000"},
                },
                "required": ["op"],
            },
        },
    },
    "organize": {
        "type": "function",
        "function": {
            "name": "organize",
            "description": (
                "文件与文档处理：op=plan 预览某个目录按扩展名整理的方案（不改动）；"
                "op=run 执行整理，把文件按扩展名移入对应子文件夹；"
                "op=doc 抽取单个文档的正文文本（支持 pdf/docx/xlsx/txt；缺库会提示安装）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["plan", "run", "doc"],
                           "description": "操作类型"},
                    "path": {"type": "string", "description": "目标目录或文档路径，留空默认桌面"},
                },
                "required": ["op"],
            },
        },
    },
    "remind": {
        "type": "function",
        "function": {
            "name": "remind",
            "description": (
                "定时提醒：op=add 设一个未来时刻的提醒（text+when）；op=list 查看待办；op=clear 清空。"
                "when 支持 in 10m / in 2h / HH:MM（今天或明天）/ 2026-10-26 15:30。"
                "到点后会在对话里弹出提醒。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["add", "list", "clear"],
                           "description": "操作类型"},
                    "text": {"type": "string", "description": "提醒内容（op=add 必填）"},
                    "when": {"type": "string", "description": "提醒时间（op=add 必填）"},
                },
                "required": ["op"],
            },
        },
    },
    "docgen": {
        "type": "function",
        "function": {
            "name": "docgen",
            "description": (
                "生成 Office 文档（纯本地，无需联网/密钥）：kind=docx 生成 Word（标题+多章节+要点），"
                "kind=xlsx 生成 Excel（表头+数据行），kind=pptx 生成 PowerPoint（每章一页）。"
                "适合「帮我写一份周报/出个表格/做个PPT」类任务。返回输出路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["docx", "xlsx", "pptx"],
                             "description": "文档类型：docx=Word, xlsx=Excel, pptx=PPT"},
                    "title": {"type": "string", "description": "文档标题（也用作文件名前缀）"},
                    "outline": {"type": "array",
                                "description": "章节列表（docx/pptx 用）：每项 {heading: 章节标题, bullets: [要点...]}",
                                "items": {"type": "object",
                                          "properties": {
                                              "heading": {"type": "string"},
                                              "bullets": {"type": "array", "items": {"type": "string"}},
                                          }}},
                    "table": {"type": "object",
                              "description": "表格（xlsx 用，也可嵌入 docx）：{headers:[...], rows:[[...],...]}",
                              "properties": {
                                  "headers": {"type": "array", "items": {"type": "string"}},
                                  "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                              }},
                    "path": {"type": "string", "description": "输出路径（可选，默认桌面 docgen_时间戳.扩展名）"},
                },
                "required": ["kind", "title"],
            },
        },
    },
}


# ---------- 插件：声明式 .qdplugin（丢进 plugins/ 即成为真工具，重启生效） ----------
# 与 .qdskill 同构：只描述「执行什么命令」，不加载/执行任意 Python（安全）。
_PROJECT_ROOT_TPL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGINS_DIR = os.path.join(_PROJECT_ROOT_TPL, "plugins")


def _parse_qdplugin(path):
    """解析 .qdplugin 头部，返回 (tool_name, schema, run_cmd)；不合格返回 None。

    头格式（仿 .qdskill，保持工程一致）：
        name: 系统信息
        id:   sysinfo
        desc: 获取本机系统与内存信息
        run:  powershell -NoProfile -Command "..."
        params: （可选）逗号分隔的参数名，均按 string 处理
    run 里的 @ROOT@ 替换为项目根，@参数名@ 替换为调用时传入的值。
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return None
    head = {}
    for line in lines[:40]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            break
        k, v = s.split(":", 1)
        k = k.strip().lower()
        if k in ("name", "id", "desc", "run", "params", "version"):
            head[k] = v.strip()
        else:
            break
    tool = (head.get("id") or "").strip()
    run = (head.get("run") or "").strip()
    if not tool or not run:
        return None
    desc = head.get("desc") or head.get("name") or tool
    props, required = {}, []
    for p in [x.strip() for x in (head.get("params") or "").split(",") if x.strip()]:
        props[p] = {"type": "string", "description": p}
        required.append(p)
    schema = {
        "type": "function",
        "function": {
            "name": tool,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }
    return tool, schema, run


class ToolBus:
    def __init__(self, cfg):
        self.cfg = cfg
        self.max_chars = cfg["tool_max_chars"]
        self.handlers = {}   # name -> callable(args: dict) -> str
        self._spawn_runner = None   # 外部注入：分裂子 Agent 的执行体
        self._spawn_depth = 0       # 递归深度，防止子 Agent 再分裂
        self._current_endpoint = None  # 主任务当前模型端点，供子 Agent 复用
        self.extra_schemas = {}   # 插件工具自带的 schema（不在 TOOL_SCHEMAS 里）
        self._register_enabled()
        self._register_plugins()

    # ---- 注册（仅配置清单内的工具） ----
    def _register_enabled(self):
        for name in self.cfg["tools"]:
            fn = getattr(self, "_tool_" + name, None)
            if fn:
                self.handlers[name] = fn

    # ---- 插件：扫描 plugins/*.qdplugin 注册为真工具（声明式，不执行任意 Python） ----
    def _register_plugins(self):
        if not os.path.isdir(_PLUGINS_DIR):
            return
        for fn in sorted(os.listdir(_PLUGINS_DIR)):
            if not fn.endswith(".qdplugin"):
                continue
            parsed = _parse_qdplugin(os.path.join(_PLUGINS_DIR, fn))
            if not parsed:
                continue
            tool, schema, run = parsed
            if tool in self.handlers:
                continue  # 不覆盖内置同名工具
            self.extra_schemas[tool] = schema
            self.handlers[tool] = lambda a, _r=run: self._run_plugin(_r, a)

    def _run_plugin(self, run_cmd, args):
        """执行插件声明的命令：@ROOT@ 换项目根，@参数名@ 换入参。"""
        cmd = run_cmd.replace("@ROOT@", _PROJECT_ROOT_TPL)
        for k, v in (args or {}).items():
            cmd = cmd.replace("@%s@" % str(k).upper(), str(v))
            cmd = cmd.replace("@%s@" % str(k), str(v))
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=60, encoding="utf-8", errors="replace")
            out = p.stdout or ""
            if p.stderr:
                out += "\n[stderr]\n" + p.stderr
            return out.strip() or "(命令无输出)"
        except subprocess.TimeoutExpired:
            return "插件命令超时（60 秒）"
        except Exception as e:
            return "插件执行失败: %s" % e

    def enabled(self):
        return list(self.handlers)

    # ---- OpenAI function-calling schema（只导出已注册工具） ----
    def openai_schemas(self):
        out = []
        for name in self.handlers:
            sch = TOOL_SCHEMAS.get(name) or self.extra_schemas.get(name)
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
        result, _cut = econ.truncate_middle(result, self.max_chars)
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
            result, _cut = econ.truncate_middle(result, self.max_chars)
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
            # 行式分段读取（对齐 pi 的 read 工具）：默认 300 行封顶，
            # 截断时给出可操作的续读行号，绝不把大文件整份灌进上下文
            max_lines = int(args.get("limit", 300) or 300)
            max_lines = max(10, min(max_lines, 2000))
            try:
                offset = int(args.get("offset", 1) or 1)
            except (TypeError, ValueError):
                offset = 1
            offset = max(1, offset)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().split("\n")
            except OSError as e:
                return "读取失败: %s" % e
            total = len(lines)
            if offset > total:
                return "起始行 %d 超出文件范围（共 %d 行）" % (offset, total)
            chunk = lines[offset - 1: offset - 1 + max_lines]
            body = "\n".join(chunk)
            body, info = econ.truncate_head(body, max_lines=max_lines,
                                            max_bytes=int(self.cfg.get("fs_read_max_chars", 6000)))
            head = "文件: %s (%s) 第 %d-%d 行 / 共 %d 行\n" % (
                path, _fmt_size(sz), offset, min(offset - 1 + len(chunk), total), total)
            if info:
                head += ("（已截断：%s；续读用 offset=%d）\n"
                         % (info["truncated_by"], offset + len(chunk)))
            elif offset - 1 + len(chunk) < total:
                head += ("（还有 %d 行；续读用 offset=%d）\n"
                         % (total - (offset - 1 + len(chunk)), offset + len(chunk)))
            return head + "---\n" + body

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
                        if len(hits) >= 30:
                            break
                if len(hits) >= 30:
                    break
            if not hits:
                return "（无匹配）"
            note = "" if len(hits) < 30 else "\n…（命中过多，仅显示前 30 条，请换更精确的关键词）"
            return "\n".join(hits) + note

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

    # ---- kb：知识库检索（全机文件索引，首次启动后台扫描建立） ----
    def _tool_kb(self, args):
        from . import kb as kb_mod
        op = str(args.get("op", "search")).strip().lower()
        store = kb_mod.KnowledgeBase(self.cfg["project_root"])
        if op == "stats":
            st = store.stats()
            cs = store.content_stats()
            if not st.get("indexed") and not cs.get("docs"):
                return "知识库尚未建立（首次启动会自动扫描，也可在设置里点「立即扫描」）"
            import time as _t
            when = _t.strftime("%m-%d %H:%M", _t.localtime(st.get("scanned_at", 0)))
            return ("知识库：已索引 %d 个文件（最近扫描 %s）；正文索引 %d 份文档 / %d 块"
                    % (st.get("count", 0), when, cs.get("docs", 0), cs.get("chunks", 0)))
        if op == "search":
            kw = str(args.get("kw", "")).strip()
            if not kw:
                return "op=search 需要 kw 关键词"
            hits = store.search(kw, limit=args.get("limit", 15))
            if not hits:
                if store.is_stale():
                    return "（无匹配。索引可能过期，可在设置里点「立即扫描」后重试）"
                return "（无匹配，可换关键词或用 fs/search 深挖）"
            return "找到 %d 个文件：\n%s" % (len(hits), "\n".join(hits))
        if op == "grep":
            """按关键词检索已入库文档的正文内容（回答"文件里写了什么"）。"""
            kw = str(args.get("kw", "")).strip()
            if not kw:
                return "op=grep 需要 kw 关键词"
            hits = store.grep(kw, limit=args.get("limit", 6))
            if not hits:
                return ("（正文索引中没有命中。若目标文档尚未入库，"
                        "用 op=ingest 指定文件路径先入库，或在界面里手动导入。）")
            out = []
            for h in hits:
                out.append("%s（%s 第%d段）：%s" % (h["name"], h["path"], h["chunk"] + 1,
                                                  h["snippet"]))
            return "正文命中 %d 处：\n%s" % (len(out), "\n".join(out))
        if op == "ingest":
            paths = args.get("paths")
            if isinstance(paths, str):
                paths = [p.strip() for p in paths.replace("；", ";").split(";") if p.strip()]
            if not paths:
                paths = None      # None → 按文件名索引自动挑候选文档
            r = store.ingest(paths)
            return ("正文入库完成：新增 %d，更新 %d，跳过 %d，当前共 %d 份文档 / %d 字"
                    % (r["added"], r["updated"], r["skipped"], r["docs"], r["chars"]))
        return "未知 op: %s（可用 search/grep/ingest/stats）" % op

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
        # 头尾保留中间截断（报错通常在末尾），退出码永远保留在最后
        body, _cut = econ.truncate_middle(body, int(self.cfg.get("code_max_chars", 6000)))
        return body + "\n[exit=%d]" % p.returncode

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
        """长期记忆（结构化、跨会话持久化）。

        op=search  按关键词检索；op=write 写入一条记忆（自动去重合并）；
        op=list   列出记忆；op=delete 按 id 删除。
        """
        from . import memory as mem_mod
        op = str(args.get("op", "search")).strip().lower()
        store = mem_mod.MemoryStore(self.cfg["project_root"])
        if op == "write":
            text = str(args.get("text", "")).strip()
            if not text:
                return "MEMO 写入需要 text"
            kind = str(args.get("kind", "fact")).strip().lower()
            tags = args.get("tags")
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
            ok, msg = store.add(text, kind=kind, tags=tags, source="tool")
            return ("已写入长期记忆（%s）" % msg) if ok else ("写入失败：" + msg)
        if op == "list":
            items = store.list(kind=str(args.get("kind", "")).strip() or None,
                               limit=args.get("limit", 50))
            if not items:
                return "（长期记忆为空）"
            lines = []
            for it in items:
                label = mem_mod.KIND_LABEL.get(it.get("kind"), "事实")
                lines.append("[%s] %s（id=%s）" % (label, it.get("text", ""), it.get("id")))
            return "长期记忆 %d 条：\n%s" % (len(items), "\n".join(lines))
        if op == "delete":
            mid = str(args.get("id", "")).strip()
            if not mid:
                return "MEMO 删除需要 id"
            ok, msg = store.remove(mid)
            return msg if ok else ("删除失败：" + msg)
        kw = str(args.get("kw", "")).strip()
        if not kw:
            return "MEMO 检索需要 kw（op=search）"
        hits = store.search(kw, limit=args.get("limit", 10))
        if not hits:
            return "（无命中）"
        return "\n".join("[%s] %s" % (mem_mod.KIND_LABEL.get(h.get("kind"), "事实"),
                                     h.get("text", "")) for h in hits)

    def _tool_browser(self, args):
        """浏览器操作：转发给引擎适配器（由 main 注入）。"""
        if self._browser_dispatch is None:
            return "浏览器未启用"
        cmd = str(args.get("cmd", "")).strip()
        return self._browser_dispatch(cmd)

    _browser_dispatch = None

    def attach_browser(self, dispatch):
        self._browser_dispatch = dispatch

    # ---- ctx：上下文自主调试（handler 由 app.py 注入） ----
    _ctx_handler = None

    def attach_ctx(self, handler):
        """注入上下文调试回调：handler(op: str, args: dict) -> str。"""
        self._ctx_handler = handler

    def _tool_ctx(self, args):
        if self._ctx_handler is None:
            return "上下文调试未启用"
        op = str(args.get("op", "inspect")).strip() or "inspect"
        return self._ctx_handler(op, args)

    # ---- spawn：分裂子 Agent ----
    def attach_spawn(self, runner):
        """注入子 Agent 执行体：runner(task:str, tools:str) -> str(结论)。"""
        self._spawn_runner = runner

    def _tool_spawn(self, args):
        if self._spawn_runner is None:
            return "子 Agent 未启用"
        if self._spawn_depth >= 1:
            return "子 Agent 不能再分裂（已到一层上限）。"
        task = str(args.get("task", "")).strip()
        if not task:
            return "缺少 task 参数"
        tools = str(args.get("tools", "fs,code")).strip()
        self._spawn_depth += 1
        try:
            result = self._spawn_runner(task, tools)
        except Exception as e:
            result = "子 Agent 执行失败: %s" % e
        finally:
            self._spawn_depth -= 1
        return str(result)

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

    # ---- 联网：搜索 + 抓取（无 API Key，走公开 HTML 端点） ----
    def _tool_web(self, args):
        import urllib.request
        import urllib.parse
        import urllib.error
        import html as _html

        op = str(args.get("op", "search")).strip().lower()
        if op == "search":
            q = str(args.get("q", "")).strip()
            if not q:
                return "op=search 需要 q"
            return self._web_search(q, int(args.get("limit", 5) or 5))
        if op == "fetch":
            url = str(args.get("url", "")).strip()
            if not url:
                return "op=fetch 需要 url"
            return self._web_fetch(url, int(args.get("max_chars", 4000) or 4000))
        return "未知 op（可用 search/fetch）"

    def _web_search(self, q, limit):
        try:
            data = urllib.parse.urlencode({"q": q}).encode("utf-8")
            req = urllib.request.Request(
                "https://html.duckduckgo.com/html/",
                data=data,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                page = r.read().decode("utf-8", "ignore")
            items = []
            for m in re.finditer(
                    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    page, re.S):
                href = _html.unescape(m.group(1))
                title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
                items.append((title, href))
            if not items:
                return "（搜索无结果，或当前网络不可用）"
            out = ["搜索「%s」得到 %d 条：" % (q, len(items[:limit]))]
            for i, (t, u) in enumerate(items[:limit]):
                out.append("%d. %s\n   %s" % (i + 1, t, u))
            return "\n".join(out)
        except Exception as e:
            return "联网搜索失败（%s）。请检查本机网络，或换关键词重试。" % type(e).__name__

    def _web_fetch(self, url, max_chars):
        if not url.startswith("http"):
            url = "https://" + url
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read(2_000_000)
            text = raw.decode("utf-8", "ignore")
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text,
                         flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = _html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > max_chars:
                text = text[:max_chars] + "…（已截断）"
            return "网页 %s 正文：\n%s" % (url, text)
        except Exception as e:
            return "抓取失败（%s）：%s" % (type(e).__name__, url)

    # ---- 文件整理 + 文档读取 ----
    def _tool_organize(self, args):
        op = str(args.get("op", "plan")).strip().lower()
        path = self._resolve_path(args.get("path"))
        if op == "plan":
            if not os.path.isdir(path):
                return "路径不存在或不是目录: %s" % path
            cats = {}
            for f in os.listdir(path):
                full = os.path.join(path, f)
                if os.path.isdir(full):
                    continue
                ext = (os.path.splitext(f)[1].lower() or "(无扩展名)")
                cats[ext] = cats.get(ext, 0) + 1
            if not cats:
                return "（目录里没有文件）"
            lines = ["整理计划（%s）：" % path]
            for ext, n in sorted(cats.items(), key=lambda x: -x[1]):
                lines.append("  %s → %d 个文件" % (ext, n))
            lines.append("执行 op=run 将按扩展名把文件移入对应子文件夹。")
            return "\n".join(lines)
        if op == "run":
            if not os.path.isdir(path):
                return "路径不存在或不是目录: %s" % path
            moved = 0
            for f in os.listdir(path):
                full = os.path.join(path, f)
                if os.path.isdir(full):
                    continue
                ext = os.path.splitext(f)[1].lower().lstrip(".") or "其他"
                dest = os.path.join(path, ext)
                os.makedirs(dest, exist_ok=True)
                try:
                    shutil.move(full, os.path.join(dest, f))
                    moved += 1
                except Exception:
                    pass
            return "已按扩展名整理 %d 个文件到子文件夹。" % moved
        if op == "doc":
            f = self._resolve_path(args.get("path"))
            if not os.path.isfile(f):
                return "文件不存在: %s" % f
            return self._doc_text(f, int(args.get("max_chars", 6000) or 6000))
        return "未知 op（可用 plan/run/doc）"

    def _doc_text(self, f, max_chars):
        ext = os.path.splitext(f)[1].lower()
        try:
            if ext == ".txt":
                t = open(f, encoding="utf-8", errors="replace").read()
            elif ext == ".pdf":
                try:
                    import PyPDF2
                    t = "\n".join((p.extract_text() or "")
                                 for p in PyPDF2.PdfReader(f).pages)
                except ImportError:
                    return "读取 PDF 需安装 PyPDF2：pip install PyPDF2"
            elif ext == ".docx":
                try:
                    import docx
                    t = "\n".join(p.text for p in docx.Document(f).paragraphs)
                except ImportError:
                    return "读取 Word 需安装 python-docx：pip install python-docx"
            elif ext in (".xlsx", ".xls"):
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
                    parts = []
                    for ws in wb.worksheets:
                        for row in ws.iter_rows(values_only=True):
                            parts.append(" ".join("" if c is None else str(c)
                                                 for c in row))
                    t = "\n".join(parts)
                except ImportError:
                    return "读取 Excel 需安装 openpyxl：pip install openpyxl"
            else:
                return "暂不支持的文档类型：%s（可用 code 工具按需处理）" % ext
            if len(t) > max_chars:
                t = t[:max_chars] + "\n…（已截断）"
            return "文档 %s 正文：\n%s" % (f, t)
        except Exception as e:
            return "读取失败: %s" % e

    # ---- 定时提醒（管理器由 app.py 注入） ----
    _reminder_mgr = None

    def attach_reminders(self, mgr):
        self._reminder_mgr = mgr

    def _tool_remind(self, args):
        if self._reminder_mgr is None:
            return "提醒未启用"
        op = str(args.get("op", "list")).strip().lower()
        if op == "add":
            text = str(args.get("text", "")).strip()
            when = str(args.get("when", "")).strip()
            if not text or not when:
                return "op=add 需要 text 与 when"
            return self._reminder_mgr.add(text, when)
        if op == "list":
            return self._reminder_mgr.list()
        if op == "clear":
            return self._reminder_mgr.clear()
        return "未知 op（可用 add/list/clear）"

    # ---- docgen：生成 Office 文档（纯本地，补齐「能读不能造」的缺口） ----
    def _ensure_doc_lib(self, mod, pkg):
        try:
            return __import__(mod)
        except ImportError:
            import subprocess, sys
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", pkg],
                               check=True, timeout=180)
            except Exception:
                return None
            try:
                return __import__(mod)
            except ImportError:
                return None

    def _tool_docgen(self, args):
        kind = str(args.get("kind", "")).strip().lower()
        title = str(args.get("title", "")).strip()
        if not title:
            return "docgen 需要 title"
        outline = args.get("outline") or []
        table = args.get("table") or {}
        path = str(args.get("path", "")).strip()
        if not path:
            ext = {"docx": "docx", "xlsx": "xlsx", "pptx": "pptx"}.get(kind, "docx")
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(os.path.expanduser("~"), "Desktop",
                                "docgen_%s_%s.%s" % (title[:20] or "doc", stamp, ext))
        if kind == "docx":
            docx = self._ensure_doc_lib("docx", "python-docx")
            if docx is None:
                return "未安装 python-docx，且自动安装失败（离线环境请手动 pip install python-docx）"
            d = docx.Document()
            d.add_heading(title, level=0)
            for sec in outline:
                h = str(sec.get("heading", "")) if isinstance(sec, dict) else str(sec)
                b = sec.get("bullets", []) if isinstance(sec, dict) else []
                if h:
                    d.add_heading(h, level=1)
                for it in b:
                    d.add_paragraph(str(it), style="List Bullet")
            if table:
                hdr = table.get("headers", [])
                rows = table.get("rows", [])
                if hdr or rows:
                    t = d.add_table(rows=0, cols=max(len(hdr), 1))
                    t.style = "Light Grid Accent 1"
                    if hdr:
                        hr = t.add_row().cells
                        for i, c in enumerate(hdr):
                            hr[i].text = str(c)
                    for row in rows:
                        rc = t.add_row().cells
                        for i, c in enumerate(row):
                            rc[i].text = str(c)
            d.save(path)
            return "已生成 Word 文档：%s" % path
        if kind == "xlsx":
            oxl = self._ensure_doc_lib("openpyxl", "openpyxl")
            if oxl is None:
                return "未安装 openpyxl，且自动安装失败（离线环境请手动 pip install openpyxl）"
            wb = oxl.Workbook()
            ws = wb.active
            ws.title = title[:31] or "Sheet1"
            hdr = table.get("headers", []) if table else []
            rows = table.get("rows", []) if table else []
            if (not hdr and not rows) and outline:
                hdr = ["章节", "要点"]
                rows = [[str(s.get("heading", "")) if isinstance(s, dict) else str(s),
                         "；".join(str(x) for x in (s.get("bullets", []) if isinstance(s, dict) else []))]
                        for s in outline]
            r = 1
            if hdr:
                for i, c in enumerate(hdr):
                    ws.cell(row=r, column=i + 1, value=str(c))
                r += 1
            for row in rows:
                for i, c in enumerate(row):
                    ws.cell(row=r, column=i + 1, value=str(c))
                r += 1
            wb.save(path)
            return "已生成 Excel 表格：%s" % path
        if kind == "pptx":
            ppt = self._ensure_doc_lib("pptx", "python-pptx")
            if ppt is None:
                return "未安装 python-pptx，且自动安装失败（离线环境请手动 pip install python-pptx）"
            prs = ppt.Presentation()
            slide = prs.slides.add_slide(prs.slide_layouts[0])
            slide.shapes.title.text = title
            for sec in outline:
                h = str(sec.get("heading", "")) if isinstance(sec, dict) else str(sec)
                b = sec.get("bullets", []) if isinstance(sec, dict) else []
                s = prs.slides.add_slide(prs.slide_layouts[1])
                s.shapes.title.text = h or title
                body = s.placeholders[1].text_frame
                for it in b:
                    body.add_paragraph().text = str(it)
            prs.save(path)
            return "已生成 PPT：%s" % path
        return "未知 kind（可用 docx/xlsx/pptx）"
