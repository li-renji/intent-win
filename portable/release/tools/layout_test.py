# -*- coding: utf-8 -*-
"""实测侧栏各元素的真实几何：文字左缘是否对齐、有没有被截断。"""
import json
import os
import threading
import time

import webview

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html").replace("\\", "/")

JS = r"""
(function(){
  const out = {slots:[]};
  // 造一条会话项，否则 .conv 不存在量不到
  const cl = document.getElementById('convList');
  if(cl && !cl.querySelector('.conv')){
    const c = document.createElement('div');
    c.className = 'conv'; c.textContent = '测试会话标题';
    cl.appendChild(c);
  }
  const side = document.querySelector('.side');
  const sr = side.getBoundingClientRect();
  out.sideWidth = Math.round(sr.width);
  out.savedW = localStorage.getItem('qd-side-w');
  ['236','268','176'].forEach(function(w){ });
  // 逐个量：文字左缘（左padding + 图标 + gap）与是否被截断
  function probe(sel, label){
    const e = document.querySelector(sel);
    if(!e) { out.slots.push({label:label, missing:true}); return; }
    const cs = getComputedStyle(e);
    const r = e.getBoundingClientRect();
    out.slots.push({
      label: label,
      left: Math.round(r.left - sr.left),
      width: Math.round(r.width),
      padL: cs.paddingLeft,
      scrollOverflow: e.scrollWidth - e.clientWidth,   // >0 说明内容被挤出
      text: (e.textContent||'').trim().slice(0,8)
    });
  }
  probe('.logo', '标题');
  probe('.side .btn.primary', '导航按钮');
  probe('.conv', '会话项');
  probe('.side-foot .btn', '页脚按钮');
  // 按钮内文字的实际左缘 = 按钮左 + padding + 图标 + gap
  const b = document.querySelector('.side-foot .btn');
  if(b){
    const cs = getComputedStyle(b);
    const svg = b.querySelector('svg');
    const r0 = b.getBoundingClientRect();
    let textLeft = r0.left + parseFloat(cs.paddingLeft);
    if(svg) textLeft += svg.getBoundingClientRect().width + parseFloat(cs.gap || 10);
    out.footerTextLeft = Math.round(textLeft - sr.left);
  }
  const lg = document.querySelector('.logo');
  if(lg){
    const lgSpan = lg.querySelector('span');
    if(lgSpan) out.logoTextLeft = Math.round(lgSpan.getBoundingClientRect().left - sr.left);
  }
  const cv = document.querySelector('.conv');
  if(cv) out.convTextLeft = Math.round(cv.getBoundingClientRect().left - sr.left
                                       + parseFloat(getComputedStyle(cv).paddingLeft));
  // ---------- 设计令牌是否真的生效 ----------
  // 同心圆角：容器 23 = 内层 11 + padding 12
  const ib = document.querySelector('.input-box');
  const sd = document.querySelector('.send');
  if(ib && sd){
    out.concentric = {
      outer: getComputedStyle(ib).borderRadius,
      inner: getComputedStyle(sd).borderRadius,
      pad: getComputedStyle(ib).paddingTop
    };
  }
  // 等宽数字
  const cn = document.getElementById('ctxNum');
  if(cn) out.tabular = getComputedStyle(cn).fontVariantNumeric;
  // 平衡换行
  const tt = document.getElementById('curTitle');
  if(tt) out.wrap = getComputedStyle(tt).textWrap;
  // 顶栏按钮：用多层阴影，不用硬边框
  const ta = document.querySelector('.tb-act');
  if(ta){
    const cs = getComputedStyle(ta);
    out.tbact = {borderWidth: cs.borderTopWidth,
                 shadow: cs.boxShadow.slice(0, 60),
                 shadowLayers: cs.boxShadow.split('rgba').length - 1};
  }
  // 圆点胶囊
  const pill = document.getElementById('quickPill');
  if(pill){
    const core = pill.querySelector('.dp-core');
    const cr = core.getBoundingClientRect();
    out.pill = {w: Math.round(cr.width), h: Math.round(cr.height),
                round: Math.round(cr.width) === Math.round(cr.height)};
  }
  return out;
})()
"""


class Api:
    def get_settings(self):
        return {"theme": "light", "configured": True,
                "glass_enabled": True, "glass_refract": True,
                "workbench_enabled": True, "profiles": []}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {}
        return _noop


def main():
    webview.create_window("layouttest", UI, js_api=Api(), width=1000, height=700)

    def _run():
        for _ in range(100):
            if webview.windows:
                break
            time.sleep(0.1)
        time.sleep(2.5)
        try:
            # 先把保存的宽度清掉，确保按默认 236 量
            webview.windows[0].evaluate_js(
                "localStorage.removeItem('qd-side-w');"
                "document.documentElement.style.setProperty('--side-w','236px');")
            time.sleep(0.4)
            res = webview.windows[0].evaluate_js(JS)
            txt = json.dumps(res, ensure_ascii=False, indent=2)
        except Exception as e:
            txt = "EVAL 失败: %r" % (e,)
        try:
            with open(os.path.join(ROOT, "shots", "layout_test.json"), "w",
                      encoding="utf-8") as f:
                f.write(txt)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_run, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
