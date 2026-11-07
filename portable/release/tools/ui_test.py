# -*- coding: utf-8 -*-
"""端到端验证本轮新增的界面交互：

  1. 水墨晕染打字：span 个数是否被限制（而非字符被限制）、文字有没有漏字、
     动画结束后是否摘掉 filter
  2. 侧栏拖拽：改 --side-w 是否即时生效、松手是否吸附到档位
  3. 勾选胶囊开关：aria-checked 是否正确翻转
  4. 圆点 → 胶囊 → 信息卡：三段状态切换
  5. 点赞 Q 弹：pop / on 状态

用法：python tools/ui_test.py
"""
import json
import os
import threading
import time

import webview

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html").replace("\\", "/")

TEST_JS = r"""
/* 注意：evaluate_js 不会自动 await Promise，
   所以这里把异步测试的结果挂到 window.__t，由 Python 侧轮询取回。 */
(function(){
  window.__t = null;
  (async function(){
  const out = {};
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  try{
    /* ---------- 1. 水墨晕染 ---------- */
    msgs.innerHTML = '';
    window._curBubble = addMsg('ai', '', true);
    window._full = ''; _inkBuf = ''; _inkOn = false;

    // 模拟 240 个 token 在一瞬间全部到达（最极端的突发）
    for (let i = 0; i < 240; i++) window.__onToken('字');
    await sleep(60);
    const spansEarly = window._curBubble.querySelectorAll('span.ink').length;
    await sleep(700);
    const spans700 = window._curBubble.querySelectorAll('span.ink').length;
    await sleep(1200);
    const spansEnd = window._curBubble.querySelectorAll('span.ink').length;

    out.ink = {
      fed: 240,
      spansAt60ms: spansEarly,      // 应远小于 240 → 说明做了批处理
      spansAt760ms: spans700,
      spansAt1960ms: spansEnd,      // 上限约 = 帧数，不应是 240
      renderedChars: window._curBubble.textContent.length,
      fullLen: window._full.length,
      cappedSpans: spansEnd < 240,                    // span 个数被限制
      noCharLoss: window._curBubble.textContent.length === 240  // 字符一个不少
    };
    // 动画结束后应摘掉 ink 类（不再持有 filter）
    await sleep(1200);
    out.ink.settledSpans =
      window._curBubble.querySelectorAll('span:not(.ink)').length;

    // 直接调 _inkFlush 造一个 span，立刻查动画是否真的挂上
    // （测试窗口不可见时 rAF 会被浏览器节流，只能绕开时序直接验 CSS 是否生效）
    _inkBuf = '晕';
    _inkFlush();
    const one = window._curBubble.querySelector('span.ink');
    if (one) {
      const cs = getComputedStyle(one);
      out.ink.animName = cs.animationName;
      out.ink.animDur = cs.animationDuration;
      out.ink.filterNow = cs.filter;          // 动画进行中应带 blur()
      out.ink.opacityNow = cs.opacity;
    }
    // 逐帧合并验证：同一帧内到达的多个 token 必须合成一个 span
    const beforeB = window._curBubble.querySelectorAll('span.ink').length;
    for (let i = 0; i < 30; i++) window.__onToken('合');
    _inkFlush();                               // 模拟"一帧内全部到达"
    out.ink.spansPerFrame = window._curBubble.querySelectorAll('span.ink').length - beforeB;

    /* ---------- 2. 侧栏拖拽 ---------- */
    const root = document.documentElement;
    const grip = document.getElementById('sideGrip');
    out.side = { hasGrip: !!grip };
    if (grip) {
      const before = getComputedStyle(root).getPropertyValue('--side-w').trim();
      const rect = grip.getBoundingClientRect();
      const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
      const pd = new PointerEvent('pointerdown', {clientX: cx, clientY: cy, bubbles: true});
      grip.dispatchEvent(pd);
      // 拖拽中应立即进入 side-resizing（关掉所有过渡 = 零延迟）
      out.side.resizingClassOn = root.classList.contains('side-resizing');
      // 向右拖 46px
      for (let d = 6; d <= 46; d += 8) {
        grip.dispatchEvent(new PointerEvent('pointermove',
          {clientX: cx + d, clientY: cy, bubbles: true}));
      }
      await sleep(60);
      const during = getComputedStyle(root).getPropertyValue('--side-w').trim();
      grip.dispatchEvent(new PointerEvent('pointerup',
        {clientX: cx + 46, clientY: cy, bubbles: true}));
      await sleep(80);
      const after = getComputedStyle(root).getPropertyValue('--side-w').trim();
      out.side.before = before;
      out.side.during = during;         // 拖拽中应立刻跟随
      out.side.after = after;           // 松手后应吸附到整数档位
      out.side.changedOnDrag = (during !== before);
      out.side.snapped = ['176px','200px','236px','268px','300px','340px'].indexOf(after) >= 0;
      out.side.snapClassUsed = root.classList.contains('side-snap');
      out.side.persisted = localStorage.getItem('qd-side-w');
    }

    /* ---------- 3. 勾选胶囊开关 ---------- */
    const sw = document.getElementById('s_wb');
    const v0 = sw.getAttribute('aria-checked');
    sw.click();
    await sleep(30);
    const v1 = sw.getAttribute('aria-checked');
    sw.click();
    await sleep(30);
    const v2 = sw.getAttribute('aria-checked');
    out.switch = { before: v0, after1: v1, after2: v2,
                   toggles: (v0 !== v1) && (v1 !== v2) && (v0 === v2) };

    /* ---------- 4. 圆点 → 胶囊 → 信息卡 ---------- */
    const pill = document.getElementById('quickPill');
    const core = pill.querySelector('.dp-core');
    const wDot = core.getBoundingClientRect().width;
    toggleQuickPill();                       // 圆点 → 胶囊
    await sleep(520);
    const wPill = core.getBoundingClientRect().width;
    const icOpacity = getComputedStyle(pill.querySelector('.dp-ic')).opacity;
    const txOpacity = getComputedStyle(pill.querySelector('.dp-t')).opacity;
    toggleQuickPill();                       // 胶囊 → 信息卡
    await sleep(520);
    const card = pill.querySelector('.dp-card');
    out.pill = {
      dotWidth: Math.round(wDot),
      capsuleWidth: Math.round(wPill),
      expanded: wPill > wDot + 60,
      iconVisible: +icOpacity > .9,
      textVisible: +txOpacity > .9,
      cardOpacity: +getComputedStyle(card).opacity,
      cardOpen: pill.classList.contains('card-open')
    };

    /* ---------- 5. 点赞 Q 弹 ---------- */
    msgs.innerHTML = '';
    const b2 = addMsg('ai', '测试点赞', false);
    _addMsgActs(b2);
    const host = b2.parentElement;
    const like = host.querySelector('.act-like');
    out.like = { exists: !!like, onAiOnly: !!like };
    if (like) {
      likeAct(like);
      out.like.popClass = like.classList.contains('pop');
      out.like.onClass = like.classList.contains('on');
      likeAct(like);
      out.like.offAfterSecond = !like.classList.contains('on');
    }
    // 用户消息不应有点赞
    const b3 = addMsg('me', '我发的', false);
    _addMsgActs(b3);
    out.like.hiddenOnMine = !b3.parentElement.querySelector('.act-like');
  } catch (e) { out.error = String(e) + ' @ ' + (e.stack||'').split('\n')[1]; }
  window.__t = out;
  })();
  return 1;
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
    webview.create_window("uitest", UI, js_api=Api(), width=1000, height=700)

    def _run():
        for _ in range(100):
            if webview.windows:
                break
            time.sleep(0.1)
        time.sleep(2.5)
        txt = "{}"
        try:
            win = webview.windows[0]
            win.evaluate_js(TEST_JS)          # 启动异步测试
            # 轮询取回结果（测试内有多段 sleep，总耗时约 5 秒）
            for _ in range(60):
                r = win.evaluate_js("window.__t ? JSON.stringify(window.__t) : null")
                if r:
                    txt = r
                    break
                time.sleep(0.2)
            else:
                txt = "超时：未取到结果（window.__t 始终为空）"
        except Exception as e:
            txt = "EVAL 失败: %r" % (e,)
        try:
            with open(os.path.join(ROOT, "shots", "ui_test.json"), "w",
                      encoding="utf-8") as f:
                f.write(txt)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_run, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
