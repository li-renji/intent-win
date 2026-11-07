# -*- coding: utf-8 -*-
"""把出厂镜像里的「开发机绝对路径」换成随盘可用的 %INTENT_ROOT%。

README、技能文件里原本写着 D:\\INTENT-win\\...。这些在 U 盘上既不成立，
也会把开发机目录结构带出去。boot.py 启动时会把 INTENT_ROOT 设为 U 盘根目录，
所以替换后的命令在任何盘符下都能跑。

只对文本文件做替换；runtime/（硬链接）与二进制一律跳过，避免写穿本体。
"""
import os

SKIP_DIRS = {"runtime", "__pycache__", ".git", "node_modules", "backups"}
TEXT_EXT = {".md", ".txt", ".qdskill", ".json", ".py", ".js", ".html", ".htm",
            ".css", ".cmd", ".bat", ".ps1", ".vbs", ".inf", ".qdai",
            ".yaml", ".yml", ".ini", ".cfg", ".xml", ".csv", ".svg"}
REPLACEMENTS = (
    ("D:\\\\INTENT-win", "%INTENT_ROOT%"),   # 源码里写成双反斜杠的字符串
    ("D:/INTENT-win", "%INTENT_ROOT%"),
    ("D:\\INTENT-win", "%INTENT_ROOT%"),
)


def scrub(root):
    """返回被改写的文件数。"""
    n = 0
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() not in TEXT_EXT:
                continue
            p = os.path.join(dirpath, name)
            try:
                raw = open(p, "rb").read()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out = text
            for a, b in REPLACEMENTS:
                out = out.replace(a, b)
            if out == text:
                continue
            tmp = p + ".new"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(out)
            os.replace(tmp, p)
            n += 1
    return n
