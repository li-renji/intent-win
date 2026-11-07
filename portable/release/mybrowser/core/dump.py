# -*- coding: utf-8 -*-
"""页面提取层：对齐 Lightpanda 的 `fetch --dump`。

Lightpanda 的 dump 支持多种目标格式：
  --dump html           原始 HTML
  --dump markdown       转成 markdown
  --dump text           纯文本正文
  --dump structured_data JSON-LD / OpenGraph / Twitter Card / meta / link

MyBrowser 借的是真 Chromium 内核，提取保真度天然高于 Lightpanda 的无头引擎。
本模块把"取页面"这件事标准化成 5 种 DUMP 格式，供 Agent 按需取用。
"""
import json
import re

try:
    from html.parser import HTMLParser
except Exception:  # pragma: no cover
    HTMLParser = None


# ---------------------------------------------------------------------------
# 纯函数：HTML -> Markdown（不依赖浏览器，可单测）
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLOCK_SKIP = ("script", "style", "noscript", "template", "head")


class _Markdownizer(HTMLParser):
    """把 HTML 解析成轻量 markdown。

    只做结构化转换（标题/列表/链接/强调/代码/图片/引用），
    不追求像素级排版——Agent 要的是可读语义，不是完美排版。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out = []            # 输出缓冲（顶层）
        self._skip = 0            # 嵌套的 <script>/<style> 深度
        self._list_stack = []     # [type, index]
        self._in_pre = 0
        self._anchor_href = None

    # ----- 缓冲抽象 -----
    def _nl(self):
        """确保缓冲以换行结尾（块级分隔）。"""
        if self._out and not self._out[-1].endswith("\n"):
            self._out.append("\n")

    def _space(self):
        """只在英文/数字词边界补空格；CJK 不强行插空格。"""
        if not self._out:
            return
        last = self._out[-1]
        if last[-1:].isspace():
            return
        if last and last[-1:].isascii() and last[-1:].isalnum():
            self._out.append(" ")

    def _emit(self, text):
        self._out.append(text)

    def _list_prefix(self):
        prefix = ""
        for lt, idx in self._list_stack:
            prefix += "  " if lt == "ul" else "   "
        if self._list_stack[-1][0] == "ul":
            return prefix + "- "
        prefix += "%d. " % self._list_stack[-1][1]
        self._list_stack[-1][1] += 1
        return prefix

    # ----- 标签处理 -----
    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._nl()
            self._emit("\n%s " % ("#" * int(tag[1:])))
        elif tag == "p":
            self._nl()
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._nl()
            self._emit("\n---\n")
        elif tag == "a" and a.get("href"):
            self._space()
            self._emit("[")
            self._anchor_href = a["href"]
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self._in_pre:
            self._emit("`")
        elif tag == "pre":
            self._in_pre += 1
            self._nl()
            self._emit("\n```\n")
        elif tag == "blockquote":
            self._nl()
            self._emit("\n> ")
        elif tag == "ul":
            self._nl()
            self._list_stack.append(["ul", 1])
        elif tag == "ol":
            self._nl()
            self._list_stack.append(["ol", 1])
        elif tag == "li":
            self._nl()
            self._emit(self._list_prefix())
        elif tag == "img" and a.get("src"):
            self._space()
            alt = a.get("alt") or ""
            self._emit("![%s](%s)" % (alt, a["src"]))
        elif tag in ("div", "section", "article", "header", "footer",
                     "main", "nav", "table", "tr"):
            self._nl()

    def handle_endtag(self, tag):
        if tag in _BLOCK_SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p"):
            self._nl()
        elif tag == "a":
            if self._anchor_href:
                self._emit("](%s)" % self._anchor_href)
                self._anchor_href = None
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self._in_pre:
            self._emit("`")
        elif tag == "pre":
            self._in_pre = max(0, self._in_pre - 1)
            self._emit("\n```\n")
        elif tag == "ul" and self._list_stack and self._list_stack[-1][0] == "ul":
            self._list_stack.pop()
            self._nl()
        elif tag == "ol" and self._list_stack and self._list_stack[-1][0] == "ol":
            self._list_stack.pop()
            self._nl()
        elif tag == "blockquote":
            self._nl()
            self._emit("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_pre:
            self._emit(data)
            return
        text = _WHITESPACE.sub(" ", data).strip()
        if not text:
            return
        if self._out and self._out[-1][-1:].isspace():
            self._emit(text)
        else:
            self._space()
            self._emit(text)

    def getvalue(self):
        raw = "".join(self._out)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_markdown(html):
    """把一段 HTML 字符串转换为可读 markdown。纯函数，无需浏览器。"""
    if not html:
        return ""
    if HTMLParser is None:
        return html
    m = _Markdownizer()
    try:
        m.feed(html)
    except Exception:
        return html
    return m.getvalue()


# 常用去噪：CDP 拿到的 outerHTML 常含海量 <script>；先剥离再转
_SCRIPT_RE = re.compile(r"<script[\s\S]*?</script>", re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[\s\S]*?</style>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(html):
    html = _SCRIPT_RE.sub("", html)
    html = _STYLE_RE.sub("", html)
    return html


# ---------------------------------------------------------------------------
# 浏览器侧：把页面 dump 成各种格式
# ---------------------------------------------------------------------------

_STRUCTURED_JS = r"""
(function(){
  function meta(name){
    var m = document.querySelector('meta[name="'+name+'"], meta[property="'+name+'"]');
    return m ? (m.getAttribute('content')||'') : null;
  }
  function metaProp(prop){
    var m = document.querySelector('meta[property="'+prop+'"]');
    return m ? (m.getAttribute('content')||'') : null;
  }
  var data = {
    url: location.href,
    title: document.title || null,
    description: meta('description') || metaProp('og:description'),
    canonical: (function(){var l=document.querySelector('link[rel="canonical"]');return l?l.getAttribute('href'):null;})(),
    og: {},
    twitter: {},
    jsonld: []
  };
  // OpenGraph / Twitter
  var metas = document.querySelectorAll('meta[property^="og:"], meta[name^="twitter:"]');
  for (var i=0;i<metas.length;i++){
    var m = metas[i];
    var k = m.getAttribute('property') || m.getAttribute('name');
    var v = m.getAttribute('content');
    if (k && v!==null){
      if (k.indexOf('og:')===0) data.og[k.slice(3)] = v;
      else data.twitter[k] = v;
    }
  }
  // JSON-LD
  var ld = document.querySelectorAll('script[type="application/ld+json"]');
  for (var j=0;j<ld.length;j++){
    try { data.jsonld.push(JSON.parse(ld[j].textContent)); } catch(e){}
  }
  return JSON.stringify(data);
})()
"""

_LINKS_JS = r"""
(function(){
  var out = [];
  var seen = {};
  var as = document.querySelectorAll('a[href]');
  for (var i=0;i<as.length;i++){
    var a = as[i];
    var href = a.href;
    var text = (a.textContent||'').trim().replace(/\s+/g,' ');
    if (!href || href.indexOf('javascript:')===0) continue;
    if (seen[href+'|'+text]) continue;
    seen[href+'|'+text] = 1;
    if (out.length < 500) out.push({text:text, href:href});
  }
  return JSON.stringify(out);
})()
"""


class Dump:
    """对一个 Session 做各种格式的 dump。"""

    def __init__(self, session):
        self.session = session

    def text(self, max_chars=4000):
        """纯文本正文（最省 token）。"""
        try:
            t = self.session.evaluate(
                "(document.body?document.body.innerText:'').replace(/\\n{3,}/g,'\\n\\n').trim()")
            t = t or ""
        except Exception:
            t = ""
        if len(t) > max_chars:
            t = t[:max_chars] + "...(truncated)"
        return t

    def html(self, max_chars=200000):
        """整页 outerHTML。"""
        try:
            h = self.session.evaluate("document.documentElement.outerHTML")
        except Exception:
            return ""
        if not h:
            return ""
        if len(h) > max_chars:
            h = h[:max_chars] + "\n<!-- truncated -->"
        return h

    def markdown(self, max_chars=200000):
        """把页面清洗后转成 markdown（Lightpanda 的 --dump markdown）。"""
        h = self.html(max_chars=max_chars)
        if not h:
            return ""
        return html_to_markdown(strip_html(h))

    def structured(self):
        """JSON-LD / OpenGraph / Twitter Card / meta / canonical。

        对应 Lightpanda 的 --dump structured_data。
        """
        try:
            raw = self.session.evaluate(_STRUCTURED_JS)
            if isinstance(raw, str):
                return json.loads(raw)
            return raw or {}
        except Exception as e:
            return {"error": str(e)}

    def links(self, max_chars=40000):
        """页面所有链接（文本 + href），去重。"""
        try:
            raw = self.session.evaluate(_LINKS_JS)
            if isinstance(raw, str):
                arr = json.loads(raw)
            else:
                arr = raw or []
        except Exception:
            return []
        return arr

    def dump(self, fmt="markdown", max_chars=4000):
        """统一入口：根据格式分派。"""
        fmt = (fmt or "markdown").lower()
        if fmt == "text":
            return self.text(max_chars=max_chars)
        if fmt == "html":
            return self.html(max_chars=max_chars)
        if fmt == "links":
            links = self.links()
            if max_chars:
                links = links[:max_chars]
            return "\n".join("[%s](%s)" % (l.get("text", ""), l.get("href", ""))
                             for l in links)
        if fmt == "structured":
            import json as _j
            return _j.dumps(self.structured(), ensure_ascii=False, indent=2)
        # 默认 markdown
        md = self.markdown(max_chars=max_chars)
        return md
