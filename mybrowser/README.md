# MyBrowser —— 自研浏览器程序

一个**真正自己写**的浏览器：界面、协议栈、语义层、动作层全部自研，
只借用系统自带的 Chromium 内核（Edge WebView2 Runtime）做渲染。

最终目标：作为 Windows Agent（`D:\INTENT-win`）的内置浏览器，
彻底替换现有的 `evaluate_js` 注入式实现。

---

## 1. 为什么这是"自研浏览器"而不是"搬别人的"

业界所有浏览器都是"借内核 + 自研程序"：
Chrome / Edge / Brave / Arc / Dia 全部使用同一个 Chromium 内核，
差别全在浏览器程序本身——窗口、标签页、地址栏、会话、控制协议。

本项目借的只有**渲染引擎**（Blink + V8），
下面这些全是自己写的：

| 层 | 文件 | 状态 |
|---|---|---|
| WebSocket 协议 | `net/ws.py` | 自研，RFC 6455，零第三方依赖 |
| CDP 协议客户端 | `net/cdp.py` | 自研，JSON-RPC over WebSocket |
| 渲染引擎控制面 | `core/engine.py` | 自研，进程生命周期 + 标签页会话 |
| 语义快照层 | `core/snapshot.py` | 自研，AX 树 → LLM 可用文本 |
| 动作执行层 | `core/actions.py` | 自研，CDP Input 域真实事件 |
| 浏览器界面 | `ui/shell.py` | 自研，WinForms 多标签外壳 |
| Agent 工具适配 | `agent_tool.py` | 自研，协议兼容现有 BROWSER 指令 |

**对比现有实现**（`agent/browser/webview2.py`，约 128 行）：
那一版把 pywebview 当黑盒，只用 `evaluate_js` 注入 JS 操作 DOM。
本项目实现了完整的浏览器控制栈。

---

## 2. 架构

```
┌─────────────────────────────────────────────────────────┐
│  Windows Agent (INTENT-win)                             │
│    tools=browser → BROWSER NAVIGATE/SNAPSHOT/CLICK/...  │
└───────────────────────┬─────────────────────────────────┘
                        │ make_dispatch()
┌───────────────────────▼─────────────────────────────────┐
│  MyBrowser                                             │
│  ┌───────────────────────────────────────────────────┐  │
│  │ agent_tool.py   协议层（兼容现有 BROWSER 指令）    │  │
│  ├───────────────────────────────────────────────────┤  │
│  │ core/snapshot.py  语义层：AX树 → 稳定ref + 差分    │  │
│  │ core/actions.py   动作层：真实鼠标/键盘事件        │  │
│  │ core/engine.py    控制面：进程 + 标签页会话        │  │
│  ├───────────────────────────────────────────────────┤  │
│  │ net/cdp.py       CDP 客户端（JSON-RPC）           │  │
│  │ net/ws.py        WebSocket（RFC 6455）            │  │
│  └───────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────┐  │
│  │ ui/shell.py      自研界面：标签栏/地址栏/书签     │  │
│  └───────────────────────────────────────────────────┘  │
└───────────────────────┬─────────────────────────────────┘
                        │ CDP over WebSocket (127.0.0.1)
┌───────────────────────▼─────────────────────────────────┐
│  Edge WebView2 Runtime（借用的 Chromium 内核）           │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 关键设计决策

### 3.1 为什么不用 pywebview

pywebview 的定位是"把网页塞进桌面窗口"，它只给一个 `js_api` 桥。
要做多标签浏览器、地址栏同步、历史、书签，全部得自己用 HTML 拼。
本项目直接用 pythonnet 挂 `Microsoft.Web.WebView2.WinForms`，
UI 是原生 WinForms 控件，标签页/地址栏/导航全部自己控制。

> pythonnet 3.x 注意：枚举必须用成员名（`FlatStyle.Flat`），
> 不能传裸整数；`System.Drawing` 需 `clr.AddReference` 显式引用。

### 3.2 快照为什么用 AX 树而不是 DOM

业界实测：同一页面，AX 树快照约 2k–5k token，纯 DOM 快照可达 50k+。
AX 树天然带角色（button / link / textbox）和可访问名，
模型判断"点哪里"更准，且不受前端重构影响。

本项目在此基础上加了两层优化：
- **稳定 ref**：用 `backendDOMNodeId` 派生 ref（`e12`），
  页面刷新后 ref 依然有效，彻底解决"DOM 下标错位"
- **增量差分**：两次快照间只报告新增/移除/变更数量

实测（百度首页）：全量快照 972 字符 → 差分模式 121 字符。

### 3.3 搜索引擎是可配置的

初版把百度写死在 4 个地方，是个设计缺陷。现已抽成 `core/search.py`：

```python
from mybrowser.core.search import SearchEngine
e = SearchEngine("bing")
e.search_url("天气")        # https://cn.bing.com/search?q=%E5%A4%A9%E6%B0%94
e.normalize("example.com")  # https://example.com
```

**内置引擎**：百度 / Bing / 搜狗 / DuckDuckGo / Google。
每个引擎带自己的关键词语法（百度 `wd=`、Bing `q=`），所以搜索 URL 不是简单拼接。

**优先级**：显式参数 > 环境变量 `MYBROWSER_SEARCH_ENGINE` > `配置.qdai` 的
`search_engine=` > 默认 `baidu`。

**地址栏智能识别**（`normalize()`）：

| 输入 | 判定 | 结果 |
|---|---|---|
| `example.com` | 像域名 | `https://example.com` |
| `localhost:8080` | 本地地址 | `http://localhost:8080` |
| `127.0.0.1:3000` | 本地 IP | `http://127.0.0.1:3000` |
| `192.168.1.5` | 局域网 IP | `http://192.168.1.5` |
| `天气怎么样` | 搜索词 | 搜索引擎 URL |
| `about:blank` | 内部协议 | 原样 |

> 选百度作默认是因为实测国内网络环境下 Google / DuckDuckGo 不可达（curl 返回 000），
> 百度 / 搜狗 / cn.bing.com 均正常。

### 3.4 为什么必须用真实事件

`el.click()` 是合成事件，`el.value = v` 直接赋值 DOM。
两者都不触发完整的键盘/输入事件链，在受控组件上必然失效。

`core/controlled.py` 精确复刻 React 的 `inputValueTracking` 机制，
对照实验结果：

| 方案 | DOM 值 | 框架 state |
|---|---|---|
| 老方案 `el.value = v` | `'legacy approach'` | `''` ❌ 提交出去是空 |
| 自研真实键盘事件 | `'real input'` | `'real input'` ✅ |

这是现有 `INTENT-win` 实现最致命的缺陷，已被实验证实。

---

## 4. 实战踩坑记录（血泪教训，必读）

实现过程中踩了 6 个坑，全部解决。**这些是 pythonnet + WebView2 的硬约束**，
不看这段代码大概率会重蹈覆辙。

### 坑 1：COM 线程模型 —— RPC_E_CHANGED_MODE (0x80010106)

WebView2 的 COM 初始化要求 **STA**，但 pythonnet 默认以 MTA 初始化 CLR，
之后再改线程模式会抛 `无法在设置线程模式后对其加以更改`。

```python
# ✗ 错误：CLR 已经以 MTA 初始化，事后无法改
import clr
clr.AddReference("System.Windows.Forms")

# ✓ 正确：必须在 import clr 之前用 ctypes 设 STA
import ctypes
ctypes.windll.ole32.CoInitializeEx(None, 2)   # COINIT_APARTMENTTHREADED
import clr
```

### 坑 2：Environment 必须同步获取

`CoreWebView2Environment.CreateAsync` 返回 Task。**只传 Task 不做同步等待，
初始化回调不会触发**，表现为 `init ok = False` 且无任何报错。

```python
task = CoreWebView2Environment.CreateAsync(None, None, opts)
env = task.GetAwaiter().GetResult()      # ← 必须同步拿
web.EnsureCoreWebView2Async(env)
```

### 坑 3：CDP 端口要用 AdditionalBrowserArguments

`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 环境变量**不可靠**（实测无效）。
唯一可靠的方式是显式构造 Environment：

```python
opts = CoreWebView2EnvironmentOptions()
opts.AdditionalBrowserArguments = "--remote-debugging-port=9555"
env = CoreWebView2Environment.CreateAsync(None, None, opts).GetAwaiter().GetResult()
```

### 坑 4：本地 CDP 请求会被 HTTP 代理拦截

环境里常设 `HTTP_PROXY=http://127.0.0.1:xxxx`，`urllib.request` 会把
`127.0.0.1` 的请求也发给代理，返回 **502 Bad Gateway**。

```python
# ✗ 错误
urllib.request.urlopen("http://127.0.0.1:9555/json/version")

# ✓ 正确：显式禁用代理
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
opener.open("http://127.0.0.1:9555/json/version")
```

### 坑 5：Page.navigate 在 WebView2 上会阻塞

`Page.navigate` 要等渲染进程确认，在 UI 线程调用会**卡死消息循环**，
后续所有 CDP 命令全部超时。

**解法**：导航用 WebView2 控件 API（UI 线程）或 `location.href` 赋值（JS 跳转），
不用 `Page.navigate`。

### 坑 6：pythonnet 3.x 的枚举与类型限制

```python
# ✗ 错误：不能传裸整数
b.FlatStyle = 0
status.TextAlign = 0

# ✓ 正确：用枚举成员
from System.Windows.Forms import FlatStyle
from System.Drawing import ContentAlignment
b.FlatStyle = FlatStyle.Flat
status.TextAlign = ContentAlignment.MiddleLeft
```

另外 `System.Drawing.Padding` 在 pythonnet 下不可用（只有 `Color`/`Font` 暴露），
需要时用 `Margin` 替代或避免使用。

### 结论：线程模型

实测确认（`threadtest`）：

| 操作 | 线程要求 |
|---|---|
| `Runtime.evaluate` / `Input.*` / `Accessibility.*` | **任意线程**都正常，秒回 |
| `Page.navigate` | 会阻塞，**禁止在 UI 线程**调用 |
| `wait_event` / 阻塞轮询 | 会阻塞，**禁止在 UI 线程**调用 |

所以最终架构是：**UI 线程只跑消息循环和画界面，所有 CDP 命令在后台线程执行。**
`BrowserUI._in_ui()` / `run_in_ui()` 保留了队列派发能力，供将来需要严格串行时使用。

---

## 5. 跑起来

```bash
cd D:\INTENT-win

# 端到端自检（11 步，无需窗口）
python -m mybrowser.selftest

# 受控组件对照实验
python -m mybrowser.controlled

# 真实站点压力测试
python -m mybrowser.stress

# Agent 工具链路验证
python -m mybrowser.agent_test

# 启动自研浏览器界面（多标签窗口）
python -m mybrowser
```

---

## 6. 协议（与现有 BROWSER 指令兼容）

**原有指令，全部可用：**

```
NAVIGATE <url>          导航
SNAPSHOT [max_chars]    语义快照（标题/URL/可交互元素ref表/正文摘要）
CLICK <i>               点击第 i 号元素
TYPE <i> <文本>         向第 i 号输入框填入文本
SCROLL <down|up|top|bottom>
BACK / FORWARD / RELOAD
READ [max_chars]        只取正文纯文本（最省 token）
```

**新增指令（向后兼容）：**

```
SEARCH <关键词>         用当前搜索引擎搜索
ENGINE [名称]           查看/切换搜索引擎（baidu/bing/sogou/duckduckgo/google）
DIFF [max_chars]        增量快照，只报相对上次的变化
CLICK_REF <ref>         按 ref 点击（比序号更稳）
TYPE_REF <ref> <文本>   按 ref 输入
PRESS <key>             按键（Enter/Tab/Escape/ArrowDown…）
HOVER <i|ref>           悬停
DRAG <ref>              拖拽
SHOT [path]             截图（AX树看不到的视觉内容）
EVAL <js>               执行 JS
```

---

## 7. 实测数据

| 指标 | 数值 |
|---|---|
| SNAPSHOT 延迟 | 16–25 ms |
| READ 延迟 | 1 ms |
| CDP 握手 | 自研实现，一次成功 |
| AX 节点数（百度首页） | 282 |
| 可交互元素识别（百度首页） | 34 个（AX 树）vs 大量噪声 div（DOM 选择器） |
| 快照体积 | 972 字符（全量）→ 121 字符（差分） |

---

## 8. 接入 Windows Agent

`agent_tool.py` 提供了 `make_dispatch()`，可直接替换现有实现：

```python
# agent/app.py 中
from mybrowser.agent_tool import MyBrowserTool, make_dispatch

browser = MyBrowserTool(port=9610, headless=False)
browser.start()
bus.attach_browser(make_dispatch(browser))
```

现有 `tools.py` 的 `browser` 工具 schema、`skills/browser-guide.qdskill`
的技能说明、6800 端口的 BROWSER 指令族——**全部无需改动**。

---

## 9. 后续路线

- [ ] **多标签 UI 完整实现**：目前 `ui/shell.py` 已有多标签框架，
      待补：标签关闭/切换、下载管理、书签面板、隐私窗口
- [ ] **会话持久化**：登录态保存与隔离（`WEBVIEW2_USER_DATA_FOLDER` 已就绪）
- [ ] **网络拦截**：`Network.setRequestInterception` + `Fetch` 域
- [ ] **下载处理**：`Browser.setDownloadBehavior` + 事件回调
- [ ] **弹窗处理**：`Page.javascriptDialogOpening` 事件
- [ ] **Linux 端**：换 QtWebEngine 适配器，复用 `net/` + `core/` 全部逻辑
- [ ] **多实例并发**：Agent 可并行开多个浏览器上下文
