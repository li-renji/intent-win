# -*- coding: utf-8 -*-
"""浏览器文本协议（引擎无关）。

AI 与浏览器之间只说这套协议 —— Windows 端用 WebView2 适配器执行，
将来 Linux 端用 QtWebEngine 的 runJavaScript 实现同一协议，ai_service 零改动。

  NAVIGATE <url> | SNAPSHOT [max_chars] | CLICK <i> | TYPE <i> <文本>
  SCROLL <down|up|top|bottom> | BACK | FORWARD | RELOAD | READ [max_chars]
"""

_ACTIONS = {"NAVIGATE", "SNAPSHOT", "CLICK", "TYPE", "SCROLL",
            "BACK", "FORWARD", "RELOAD", "READ"}


def parse(cmd_text):
    """'TYPE 3 你好' → ('TYPE', ['3', '你好'])；非法返回 (None, None)。"""
    parts = str(cmd_text).strip().split(" ", 1)
    if not parts or not parts[0]:
        return None, None
    action = parts[0].upper()
    if action not in _ACTIONS:
        return None, None
    rest = parts[1] if len(parts) > 1 else ""
    return action, rest.split(" ", 1) if action in ("TYPE",) and rest else ([rest] if rest else [])


# ---- 注入页面的 JS（快照：可交互元素编号表 + 正文摘要；纯文本，无 HTML）----
_SNAPSHOT_JS = r"""
(() => {
  const MAXE = 60, T = s => (s||'').replace(/\s+/g,' ').trim();
  const els = document.querySelectorAll(
    'a[href],button,input,textarea,select,[role=button],[onclick]');
  const items = [];
  els.forEach(el => {
    if (items.length >= MAXE) return;
    let txt = el.tagName === 'INPUT'
      ? (el.type==='submit'||el.type==='button' ? el.value : (el.placeholder||el.name||el.type))
      : T(el.innerText || el.getAttribute('aria-label') || el.value);
    if (!txt) return;
    if (txt.length > 60) txt = txt.slice(0,60)+'…';
    items.push(items.length + '.' + el.tagName.toLowerCase() + ':' + txt);
  });
  const body = T(document.body ? document.body.innerText : '').slice(0, 1800);
  return JSON.stringify({title: document.title, url: location.href,
                         elems: items.join('\n'), text: body});
})()
"""

_TEXT_JS = r"""
(() => (document.body ? document.body.innerText : '')
      .replace(/\n{3,}/g,'\n\n').trim().slice(0, 4000))()
"""

_CLICK_JS = """
(i => { const el = document.querySelectorAll(
  'a[href],button,input,textarea,select,[role=button],[onclick]')[i];
  if (!el) return 'NO_ELEMENT'; el.scrollIntoView({block:'center'}); el.click();
  return 'CLICKED'; })(%d)
"""

_TYPE_JS = """
((i, v) => { const sel = 'a[href],button,input,textarea,select,[role=button],[onclick]';
  const el = document.querySelectorAll(sel)[i];
  if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) return 'NO_ELEMENT';
  el.focus(); el.value = v;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return 'TYPED'; })(%d, %s)
"""

_SCROLL_JS = "(() => { window.scrollBy({top: %s, behavior: 'smooth'}); return 'OK'; })()"


def format_snapshot(data, max_chars):
    """把适配器取回的快照 dict 压成省 token 的文本。"""
    lines = ["[页面] %s | %s" % (data.get("title", ""), data.get("url", ""))]
    elems = data.get("elems", "")
    if elems:
        lines.append("[可交互元素（编号供CLICK/TYPE使用）]")
        lines.append(elems)
    lines.append("[正文摘要]")
    lines.append(data.get("text", ""))
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "...(truncated)"
    return out
