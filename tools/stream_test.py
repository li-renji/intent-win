# -*- coding: utf-8 -*-
"""端到端验证「流式输出渲染」改造。

驱动真实 ui/index.html 跑一遍 __onToken → __onTextReset → __onDone，
校验：
  1. 文本节点被复用（子节点数不随 token 数增长）→ 证明是 O(1) 追加而非 O(n²) 重建
  2. 光标始终在末尾
  3. __onTextReset 后已渲染的富文本被保留
  4. __onDone 正确渲染 Markdown 并移除光标

用法：python tools/stream_test.py
"""
import json
import os
import sys
import threading
import time

import webview

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html").replace("\\", "/")

TEST_JS = r"""
(function(){
  function snap(b){
    return { text: b.textContent, nodes: b.childNodes.length,
             types: Array.prototype.map.call(b.childNodes, function(n){return n.nodeType;}).join(','),
             cursorLast: !!(b.lastChild && b.lastChild.className === 'cursor') };
  }
  var out = {};
  try{
    msgs.innerHTML = '';
    window._curBubble = addMsg('ai', '', true);
    window._full = ''; window._curText = null;

    var parts = ['你','好','，','这','是','流','式','测','试','。'];
    for (var i = 0; i < parts.length; i++) window.__onToken(parts[i]);
    out.after10 = snap(window._curBubble);
    out.text10 = window._full;

    // 再灌 200 个 token：若还是旧的 innerHTML 重建，节点会不断被销毁重建
    var nodeBefore = window._curText;
    for (var j = 0; j < 200; j++) window.__onToken('字');
    out.after210 = snap(window._curBubble);
    // 文本节点是同一个对象 → 证明是复用追加，而非重建
    out.sameTextNode = (window._curText === nodeBefore);
    out.lenFull = window._full.length;

    // 模拟工具调用后的文本重设（__onTextReset），再继续追加
    window.__onTextReset('**粗体标题**');
    window.__onToken('追加内容');
    out.afterReset = snap(window._curBubble);
    out.resetHtml = window._curBubble.innerHTML.slice(0, 70);

    // 收尾：应渲染成 Markdown（出现 <b>），且光标消失
    window.__onDone('');
    var fin = msgs.querySelector('.bubble');
    out.done = { text: fin.textContent.slice(0, 30),
                 html: fin.innerHTML.slice(0, 90),
                 hasCursor: !!fin.querySelector('.cursor') };
    out.curTextCleared = (window._curText === null);

    // ---- 新旧写法耗时对比：随回复变长，看增长曲线 ----
    out.scale = [];
    [200, 600, 1200].forEach(function (N) {
      // 新写法：复用文本节点 O(1) 追加
      msgs.innerHTML = '';
      window._curBubble = addMsg('ai', '', true);
      window._full = ''; window._curText = null;
      var t0 = performance.now();
      for (var k = 0; k < N; k++) window.__onToken('字');
      var newMs = performance.now() - t0;

      // 旧写法：每 token 都 innerHTML = esc(全文) + 光标（O(n²)）
      msgs.innerHTML = '';
      var b2 = addMsg('ai', '', true);
      var full = '';
      var t1 = performance.now();
      for (var m = 0; m < N; m++) {
        full += '字';
        b2.innerHTML = esc(full) + '<span class="cursor"></span>';
      }
      var oldMs = performance.now() - t1;

      out.scale.push({ tokens: N,
                       newMs: +newMs.toFixed(1),
                       oldMs: +oldMs.toFixed(1),
                       speedup: +(oldMs / Math.max(newMs, 0.01)).toFixed(1) });
    });
  } catch (e) { out.error = String(e); }
  return out;
})()
"""


class Api:
    def get_settings(self):
        return {"theme": "dark", "configured": True,
                "glass_enabled": True, "glass_refract": True,
                "workbench_enabled": True, "profiles": []}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {}
        return _noop


def main():
    api = Api()
    webview.create_window("streamtest", UI, js_api=api, width=900, height=620)

    def _run():
        for _ in range(100):
            if webview.windows:
                break
            time.sleep(0.1)
        time.sleep(2.5)          # 等页面 boot / pywebview.api 就绪
        out = ""
        try:
            res = webview.windows[0].evaluate_js(TEST_JS)
            out = json.dumps(res, ensure_ascii=False, indent=2)
        except Exception as e:
            out = "EVAL 失败: %r" % (e,)
        # 写文件，避免 os._exit 截断 stdout
        try:
            with open(os.path.join(ROOT, "shots", "stream_test.json"), "w",
                      encoding="utf-8") as f:
                f.write(out)
        except Exception as e:
            print("写结果文件失败: %r" % (e,), flush=True)
        os._exit(0)

    threading.Thread(target=_run, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
