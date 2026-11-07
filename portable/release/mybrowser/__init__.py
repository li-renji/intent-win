# -*- coding: utf-8 -*-
"""MyBrowser —— 一个真正自己写的浏览器程序。

组成：
  net/ws.py        自研 WebSocket 客户端（RFC 6455，零第三方依赖）
  net/cdp.py       自研 CDP 协议客户端（JSON-RPC over WebSocket）
  core/engine.py   渲染引擎控制面（WebView2 进程 + 标签页会话）
  core/snapshot.py 自研语义快照层（AX 树 → LLM 可用文本，稳定 ref + 差分）
  core/actions.py  自研动作执行层（CDP Input 域真实事件）
  ui/shell.py      自研浏览器界面（标签栏/地址栏/导航/书签）
  agent_tool.py    暴露给 Windows Agent 的 BROWSER 工具适配器

渲染内核借用系统自带的 Edge WebView2 Runtime（Chromium），
浏览器程序本身 —— 界面、协议栈、语义层、动作层 —— 全部自研。
"""
__version__ = "0.1.0"

from .core.engine import WebView2Engine, Session, EngineError
from .core.snapshot import Snapshotter, Snapshot
from .core.actions import Actions

__all__ = ["WebView2Engine", "Session", "EngineError",
           "Snapshotter", "Snapshot", "Actions"]
