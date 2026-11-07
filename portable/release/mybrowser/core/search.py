# -*- coding: utf-8 -*-
"""搜索引擎配置与地址栏智能识别。

设计要点：
  · 搜索引擎可配置（配置项 / 环境变量 / 运行时 setter）
  · 每个引擎带自己的「关键词语法」：百度用 wd=，Bing 用 q=，DuckDuckGo 用 q=
  · 智能识别：网址 vs 搜索词 vs 本地地址

为什么不用硬编码：原 INTENT-win 写死 Bing（agent/app.py:723），
自研 MyBrowser 初版写死百度。硬编码会导致换引擎要改多处代码。
"""
import os
import urllib.parse

# ---------------- 引擎定义 ----------------
# name        显示名
# homepage    首页
# query       搜索 URL 模板，{q} 会被 URL 编码后的关键词替换
# 可达性      2026-10 实测（国内网络环境）
ENGINES = {
    "baidu": {
        "name": "百度",
        "homepage": "https://www.baidu.com/",
        "query": "https://www.baidu.com/s?wd={q}",
    },
    "bing": {
        "name": "Bing",
        "homepage": "https://cn.bing.com/",
        "query": "https://cn.bing.com/search?q={q}",
    },
    "sogou": {
        "name": "搜狗",
        "homepage": "https://www.sogou.com/",
        "query": "https://www.sogou.com/web?query={q}",
    },
    "duckduckgo": {
        "name": "DuckDuckGo",
        "homepage": "https://duckduckgo.com/",
        "query": "https://duckduckgo.com/?q={q}",
    },
    "google": {
        "name": "Google",
        "homepage": "https://www.google.com/",
        "query": "https://www.google.com/search?q={q}",
    },
}

DEFAULT_ENGINE = "baidu"

# 配置文件路径（与 INTENT-win 的 配置.qdai 风格一致）
_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "配置.qdai")


def _from_config_file():
    """从 配置.qdai 读 search_engine=xxx（若存在）。"""
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("search_engine") and "=" in line:
                    key, _, val = line.partition("=")
                    if key.strip() == "search_engine":
                        return val.strip()
    except Exception:
        pass
    return None


class SearchEngine:
    """当前搜索引擎。"""

    def __init__(self, name=None):
        self.name = self._resolve(name)

    @staticmethod
    def _resolve(name=None):
        """优先级：显式参数 > 环境变量 > 配置文件 > 默认。"""
        cand = (name or os.environ.get("MYBROWSER_SEARCH_ENGINE")
                or _from_config_file() or DEFAULT_ENGINE)
        cand = str(cand).strip().lower()
        return cand if cand in ENGINES else DEFAULT_ENGINE

    def set_engine(self, name):
        """运行时切换搜索引擎。"""
        key = str(name).strip().lower()
        if key not in ENGINES:
            return False, "未知搜索引擎: %s（可用: %s）" % (
                name, ", ".join(ENGINES))
        self.name = key
        return True, "已切换到 %s" % ENGINES[key]["name"]

    @property
    def info(self):
        return ENGINES[self.name]

    @property
    def homepage(self):
        return ENGINES[self.name]["homepage"]

    @property
    def display_name(self):
        return ENGINES[self.name]["name"]

    def search_url(self, keyword):
        """关键词 -> 搜索 URL。"""
        q = urllib.parse.quote(keyword)
        return ENGINES[self.name]["query"].format(q=q)

    # ---------------- 地址栏智能识别 ----------------

    def normalize(self, text):
        """把地址栏输入解析成 URL。

        判定顺序：
          1. 空 → 空
          2. about: / chrome: 等内部协议 → 原样
          3. 含 :// → 当作完整 URL
          4. localhost / 127.0.0.1 / 局域网 IP → 加 http://
          5. 像域名（含点、无空格）→ https://
          6. 其余 → 交给搜索引擎
        """
        t = (text or "").strip()
        if not t:
            return ""
        low = t.lower()
        if low.startswith(("about:", "chrome:", "edge:", "data:", "file:",
                           "javascript:", "view-source:")):
            return t
        if "://" in t:
            return t
        if low.startswith("localhost") or low.startswith("127.0.0.1") \
                or low.startswith("192.168.") or low.startswith("10."):
            return "http://" + t
        # 纯数字 IP
        parts = t.split("/")[0].split(":")[0]
        if parts.count(".") == 3 and all(p.isdigit() for p in parts):
            return "http://" + t
        # 像域名：含点、无空格、不是纯数字
        if "." in t and " " not in t:
            # 带常见端口的本地服务
            if ":" in t:
                host = t.split(":")[0]
                if host.count(".") == 3 and all(p.isdigit() for p in host):
                    return "http://" + t
            return "https://" + t
        return self.search_url(t)


# 进程级单例（供各处共享）
_default = None


def get_engine():
    global _default
    if _default is None:
        _default = SearchEngine()
    return _default
