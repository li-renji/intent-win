# -*- coding: utf-8 -*-
"""实测「留白与成组」是否成立：

  1. 可点击胶囊格是否左右都留空（不能只有上下）
  2. 组内间距 < 组间距
  3. 每行上下留空
"""
import json
import os
import threading
import time

import webview

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html").replace("\\", "/")

JS = r"""
(function(){
  const out = {};
  const cs = e => getComputedStyle(e);

  // 造一条会话项（否则量不到）
  const cl = document.getElementById('convList');
  if(cl && !cl.querySelector('.conv')){
    const c = document.createElement('div');
    c.className = 'conv';
    c.innerHTML = '<div class="t">测试会话标题</div><button class="del" title="删除">✕</button>';
    cl.appendChild(c);
  }

  // ---------- 1. 左右留空 ----------
  // 行本身可能把左右留白交由外层胶囊承担（.sw-row 在 .sw-group 里），
  // 所以要把祖先链上的横向 padding 累加起来，才是内容到胶囊边的真实留白。
  function effX(e){
    let l = 0, r = 0, t = 0, b = 0, n = e, hops = 0;
    while(n && n !== document.body && hops < 3){
      const s = cs(n);
      // 累加到"胶囊"为止：遇到有阴影/圆角的容器就不再往上
      l += parseFloat(s.paddingLeft) || 0;
      r += parseFloat(s.paddingRight) || 0;
      t += parseFloat(s.paddingTop) || 0;
      b += parseFloat(s.paddingBottom) || 0;
      if(s.boxShadow && s.boxShadow !== 'none' && s.borderRadius !== '0px') break;
      n = n.parentElement; hops++;
    }
    return {t:Math.round(t), r:Math.round(r), b:Math.round(b), l:Math.round(l)};
  }
  function pad(sel, label){
    const e = document.querySelector(sel);
    if(!e){ out[label] = {missing:true}; return; }
    out[label] = effX(e);
  }
  pad('.conv', 'conv');
  pad('.sw-row', 'swRow');
  pad('.sw-group', 'swGroup');
  pad('.side .btn', 'sideBtn');
  pad('.tb-act', 'tbAct');
  pad('.msg-acts', 'msgActs');

  // ---------- 2. 组内 vs 组间 ----------
  // 组内：两条 sw-row 的文字基线间距
  const rows = document.querySelectorAll('.sw-group .sw-row');
  if(rows.length >= 2){
    const a = rows[0].getBoundingClientRect();
    const b = rows[1].getBoundingClientRect();
    out.inGroup = Math.round(b.top - a.bottom);
  }
  // 组间：会话列表 ↔ 页脚 两条组之间的间距
  const list = document.getElementById('convList');
  const foot = document.querySelector('.side-foot');
  if(list && foot){
    const s = cs(foot);
    const csList = cs(list);
    out.betweenGroups = Math.round(parseFloat(s.paddingTop) + parseFloat(csList.marginBottom||0));
    out.footPadTop = s.paddingTop;
  }
  // 设置里：玻璃材质组与上一组的间距
  const gf = document.querySelector('.sw-group');
  if(gf){
    const field = gf.closest('.field');
    if(field) out.groupFieldMarginTop = cs(field).marginTop;
  }

  // ---------- 3. 会话项右侧是否还有留白（删除按钮不贴边）----------
  const cv = document.querySelector('.conv');
  if(cv){
    const cr = cv.getBoundingClientRect();
    const del = cv.querySelector('.del');
    if(del){
      const dr = del.getBoundingClientRect();
      out.convRightGap = Math.round(cr.right - dr.right);
    }
    const t = cv.querySelector('.t');
    if(t) out.convTextLeft = Math.round(t.getBoundingClientRect().left - cr.left);
  }

  // 开关胶囊里：标签左留白 / 开关右留白
  const grp = document.querySelector('.sw-group');
  if(grp){
    const gr = grp.getBoundingClientRect();
    const lbl = grp.querySelector('label');
    const swi = grp.querySelector('.sw');
    if(lbl) out.groupLabelLeft = Math.round(lbl.getBoundingClientRect().left - gr.left);
    if(swi) out.groupSwitchRight = Math.round(gr.right - swi.getBoundingClientRect().right);
  }
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
    webview.create_window("spacingtest", UI, js_api=Api(), width=1000, height=760)

    def _run():
        for _ in range(100):
            if webview.windows:
                break
            time.sleep(0.1)
        time.sleep(2.5)
        # 打开设置面板，否则 .sw-group 还在隐藏的 pane 里量不到
        try:
            webview.windows[0].evaluate_js(
                "const m=document.getElementById('mask'); if(m) m.classList.add('show');"
                "const g=document.getElementById('s_glass'); if(g){ "
                "  let p=g.closest('.set-pane'); if(p) p.style.display='block'; }")
            time.sleep(0.5)
            res = webview.windows[0].evaluate_js(JS)
            txt = json.dumps(res, ensure_ascii=False, indent=2)
        except Exception as e:
            txt = "EVAL 失败: %r" % (e,)
        try:
            with open(os.path.join(ROOT, "shots", "spacing_test.json"), "w",
                      encoding="utf-8") as f:
                f.write(txt)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_run, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
