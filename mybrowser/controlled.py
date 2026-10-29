# -*- coding: utf-8 -*-
"""对照实验：老方案(el.value=) vs 自研方案(真实键盘事件)，在受控组件上的差异。

受控组件的核心机制：框架劫持 input 的 value setter，
直接赋值 el.value 不会触发框架的状态更新 —— 这正是老方案的致命缺陷。

这里用原生 JS 精确复刻 React 的做法（重定义 value 的 setter + 维护内部 tracker），
造一个和 React 行为一致的受控输入框，然后跑两套方案对比。

跑法： python -m mybrowser.controlled
"""
import os
import subprocess
import sys
import time

from .core.engine import WebView2Engine
from .core.snapshot import Snapshotter
from .core.actions import Actions

PORT = 9688

# 精确复刻 React 的受控输入：劫持 value setter + 内部 tracker
CONTROLLED_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Controlled Input Test</title></head>
<body>
<h1>受控组件测试</h1>
<input id="kw" type="text" />
<button id="go" type="button">提交</button>
<pre id="state"></pre>
<script>
// ---- 复刻 React 的受控组件实现（与 react-dom 的 inputValueTracking 一致）----
(function(){
  var input = document.getElementById('kw');
  var nativeProto = window.HTMLInputElement.prototype;
  var nativeDesc = Object.getOwnPropertyDescriptor(nativeProto, 'value');
  var nativeSetter = nativeDesc.set;
  var nativeGetter = nativeDesc.get;

  var tracker = { value: '' };        // DOM 值追踪器（React 的 inputValueTracking）
  var state = '';                    // 框架内部状态（相当于 React state）
  var lastValue = '';                 // 上一次已同步的值

  // 劫持实例上的 value：外部写入只更新 tracker（不触发 input 事件）
  Object.defineProperty(input, 'value', {
    configurable: true,
    get: function(){ return tracker.value; },
    set: function(v){
      tracker.value = String(v);
      nativeSetter.call(this, tracker.value);
      lastValue = tracker.value;
    }
  });

  // 框架监听 input：只有在 DOM 实际值与 tracker 不同时才同步状态
  input.addEventListener('input', function(e){
    var domValue = nativeGetter.call(input);   // 读原生值，绕过劫持
    if (domValue !== tracker.value) {
      tracker.value = domValue;                 // 更新 tracker
      state = domValue;                         // 框架 state 同步（关键！）
      lastValue = domValue;
      render();
    }
  });

  function render(){
    document.getElementById('state').textContent =
      '框架 state = ' + JSON.stringify(state);
  }
  render();

  // 供测试读取
  window.__frameState = function(){ return state; };
  window.__domValue   = function(){ return nativeGetter.call(input); };

  document.getElementById('go').addEventListener('click', function(){
    document.title = 'submitted:' + tracker.value;
  });
})();
</script>
</body></html>
"""


def main():
    # 写本地测试页
    page = os.path.join(os.environ.get("TEMP", "."), "mybrowser_controlled.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(CONTROLLED_HTML)
    url = "file:///" + page.replace("\\", "/")
    print("[ctrl] 测试页 =", url)

    eng = WebView2Engine(port=PORT, headless=True)
    eng.prepare_env()
    profile = os.path.join(os.environ.get("TEMP", "."), "mybrowser_ctrl_profile")
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    proc = subprocess.Popen([
        edge, "--remote-debugging-port=%d" % PORT,
        "--user-data-dir=%s" % profile,
        "--headless=new", "--no-first-run", "--no-default-browser-check",
        "--allow-file-access-from-files", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[ctrl] 渲染进程 pid=%d" % proc.pid)

    try:
        eng.connect()
        sess = eng.new_tab(url)
        time.sleep(1.5)
        snap = Snapshotter(sess)
        acts = Actions(sess, snap)

        s = snap.capture()
        print("[ctrl] 快照元素:", [(r, role, name) for r, role, name in s.elements])
        ref = None
        for r, role, name in s.elements:
            if role == "输入框":
                ref = r
                break
        if not ref:
            print("[ctrl] 未找到输入框，退出")
            return 1

        # ---------- 老方案：el.value = v ----------
        sess.evaluate("document.getElementById('kw').value = 'legacy approach'")
        time.sleep(0.5)
        legacy_frame = sess.evaluate("window.__frameState()")
        legacy_dom = sess.evaluate("window.__domValue()")
        print("[ctrl] 老方案 el.value=  → DOM值=%r  框架状态=%r"
              % (legacy_dom, legacy_frame))

        # ---------- 新方案：真实键盘事件 ----------
        # 真实点击聚焦 + Ctrl+A 全选
        acts.click(ref)
        time.sleep(0.2)
        sess.send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2})
        sess.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2})
        time.sleep(0.2)
        acts.type_text("real input")
        time.sleep(0.6)
        new_frame = sess.evaluate("window.__frameState()")
        new_dom = sess.evaluate("window.__domValue()")
        print("[ctrl] 新方案 真实键盘  → DOM值=%r  框架状态=%r" % (new_dom, new_frame))

        # ---------- 判定 ----------
        legacy_ok = legacy_frame == "legacy approach"
        new_ok = new_frame == "real input"
        print("-" * 56)
        print("[ctrl] 老方案 框架状态同步 : %s" % ("成功" if legacy_ok else "失败 ❌"))
        print("[ctrl] 新方案 框架状态同步 : %s" % ("成功 ✅" if new_ok else "失败 ❌"))
        if not legacy_ok and new_ok:
            print("[ctrl] 结论：受控组件场景下，自研真实事件方案完胜老方案")
        elif legacy_ok and new_ok:
            print("[ctrl] 结论：两者均可（该页未完全复刻 React 语义）")
        else:
            print("[ctrl] 结论：需进一步排查")

        eng.dispose()
        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
