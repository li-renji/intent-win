# -*- coding: utf-8 -*-
"""自研动作执行层：用 CDP 发真实输入事件。

核心区别（对比 INTENT-win 原有的 evaluate_js 方案）：

| 操作 | 原方案 | 本模块 |
|---|---|---|
| 点击 | `el.click()` 合成事件 | `Input.dispatchMouseEvent` 真实鼠标事件，坐标定位 |
| 输入 | `el.value = v` | 逐字符 `Input.insertText` + 真实 keydown/keyup |
| 滚动 | `window.scrollBy` | `Input.dispatchMouseEvent` 滚轮事件 |
| 焦点 | `el.focus()` | 真实点击聚焦，走完整事件链 |

真实事件能触发 hover 菜单、拖拽、React/Vue 受控组件、键盘快捷键 ——
这些都是合成事件做不到的。
"""
import json
import time

from ..net.cdp import CDPError


class Actions:
    """对某个 Session 执行动作。"""

    def __init__(self, session, snapshotter=None):
        self.session = session
        self.snapshotter = snapshotter

    # ---------------- 定位 ----------------

    def _box_of(self, backend_id):
        """取元素的可视中心坐标（会先滚动进视口）。"""
        self.session.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id})
        box = self.session.send("DOM.getBoxModel", {"backendNodeId": backend_id})
        quad = box["model"]["content"]
        x = (quad[0] + quad[2]) / 2.0
        y = (quad[1] + quad[3]) / 2.0
        return x, y

    def _backend_of(self, ref):
        if not self.snapshotter:
            raise CDPError("未绑定快照器，无法按 ref 定位")
        backend = self.snapshotter.resolve_ref(ref)
        if backend is None:
            raise CDPError("ref 无效或已过期: %s（请重新 SNAPSHOT）" % ref)
        return backend

    # ---------------- 鼠标 ----------------

    def click(self, ref=None, x=None, y=None, button="left", click_count=1):
        """真实点击。给 ref 则按快照定位；给 x/y 则直接点坐标。"""
        if ref is not None:
            x, y = self._box_of(self._backend_of(ref))
        if x is None or y is None:
            raise CDPError("click 需要 ref 或 x/y")

        for ev_type in ("mousePressed", "mouseReleased"):
            self.session.send("Input.dispatchMouseEvent", {
                "type": ev_type,
                "x": round(x), "y": round(y),
                "button": button,
                "clickCount": click_count,
            })
        return "CLICKED %s" % (("at %d,%d" % (round(x), round(y))) if ref is None else ref)

    def hover(self, ref=None, x=None, y=None):
        if ref is not None:
            x, y = self._box_of(self._backend_of(ref))
        self.session.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": round(x), "y": round(y),
        })
        return "HOVERED"

    def scroll(self, delta_y=600, x=None, y=None):
        """滚轮滚动。delta_y > 0 向下。"""
        if x is None:
            x, y = 400, 300
        self.session.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": round(x), "y": round(y),
            "deltaX": 0, "deltaY": delta_y,
        })
        return "SCROLLED %d" % delta_y

    def drag(self, ref=None, x1=None, y1=None, x2=None, y2=None):
        """按下 -> 移动 -> 释放。用于拖拽控件。"""
        if ref is not None:
            x1, y1 = self._box_of(self._backend_of(ref))
        if x2 is None:
            x2, y2 = x1, y1 + 200
        self.session.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": round(x1), "y": round(y1),
            "button": "left", "clickCount": 1})
        for i in range(1, 11):
            self.session.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": round(x1 + (x2 - x1) * i / 10.0),
                "y": round(y1 + (y2 - y1) * i / 10.0),
                "button": "left", "buttons": 1})
        self.session.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": round(x2), "y": round(y2),
            "button": "left", "clickCount": 1})
        return "DRAGGED"

    # ---------------- 键盘 ----------------

    def type_text(self, text, ref=None):
        """输入文本。

        用 Input.insertText 而非逐键 —— 对中文 IME 场景更可靠，
        且比逐字符 dispatchKeyEvent 快得多。
        若给了 ref，先真实点击聚焦目标。
        """
        if ref is not None:
            self.click(ref)
            time.sleep(0.15)
        self.session.send("Input.insertText", {"text": text})
        return "TYPED %d chars" % len(text)

    def press_key(self, key, code=None, key_code=None, modifiers=0):
        """按键。key 形如 'Enter' / 'Tab' / 'Escape' / 'ArrowDown'。

        注意：CDP 的 dispatchKeyEvent 若带 text 字段则必须是字符串，
        None 也会被拒（非可打印字符必须完全省略 text）。
        """
        base = {
            "key": key,
            "code": code or key,
            "windowsVirtualKeyCode": key_code or 0,
            "nativeVirtualKeyCode": key_code or 0,
            "modifiers": modifiers,
        }
        down = dict(base, type="keyDown")
        if len(key) == 1:
            down["text"] = key
        self.session.send("Input.dispatchKeyEvent", down)
        self.session.send("Input.dispatchKeyEvent", dict(base, type="keyUp"))
        return "PRESSED %s" % key

    def key_enter(self):
        return self.press_key("Enter", code="Enter", key_code=13)

    def key_escape(self):
        return self.press_key("Escape", code="Escape", key_code=27)

    def key_tab(self):
        return self.press_key("Tab", code="Tab", key_code=9)

    def key_backspace(self):
        return self.press_key("Backspace", code="Backspace", key_code=8)

    # ---------------- 组合 ----------------

    def click_and_type(self, ref, text, submit=False):
        """点进输入框并填入文本，可选回车提交。"""
        self.click(ref)
        time.sleep(0.15)
        # 先清空
        self.session.send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2})
        self.session.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2})
        time.sleep(0.05)
        self.session.send("Input.insertText", {"text": text})
        if submit:
            time.sleep(0.1)
            self.key_enter()
        return "FILLED %s" % ref

    # ---------------- 页面级 ----------------

    def read_text(self, max_chars=4000):
        """只取正文纯文本（最省 token）。"""
        try:
            text = self.session.evaluate(
                "(document.body ? document.body.innerText : '')"
                ".replace(/\\n{3,}/g,'\\n\\n').trim()")
            text = text or ""
        except CDPError:
            return ""
        if len(text) > max_chars:
            text = text[:max_chars] + "...(truncated)"
        return text

    def screenshot(self, path=None):
        """截图。AX 树看不到的视觉内容（canvas/验证码/图表）靠它。"""
        data = self.session.screenshot()
        if path:
            with open(path, "wb") as f:
                f.write(data)
            return path
        return data

    def eval_js(self, expr):
        return self.session.evaluate(expr)
