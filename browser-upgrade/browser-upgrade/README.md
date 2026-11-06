# INTENT-win 内置浏览器升级包

把 A 机（本机）的新版**自研 MyBrowser 浏览器**移植到 B 机（旧版软件），
**只换浏览器，不动 B 机其他任何功能**。

---

## 一、先说结论

新版浏览器不是散落在主程序里的几行代码，而是：

| 组成 | 位置 | 性质 |
|---|---|---|
| 自研浏览器子项目（CDP/WebSocket/AX 快照/动作层/WinForms 界面/AI 能力） | `mybrowser/`（25 个文件） | **纯新增** |
| 引擎适配层（与旧 `WebView2Adapter` 同接口） | `agent/browser/mybrowser_adapter.py` | **纯新增** |
| 主程序接线 | `agent/app.py`、`agent/main.py` 各 2 行 | **替换 import + 实例化** |

`mybrowser_adapter.py` 与旧 `WebView2Adapter` 暴露完全相同的 `execute / available / start / show` 接口，
当初的 `protocol.py` 就是按"引擎无关文本协议"设计的 —— 所以**换引擎不需要改业务代码**。

> ⚠️ **一个关键事实**：这批浏览器改动目前**只在本机工作区，还没提交到 GitHub**
> （`git status` 里是一堆 `M` / `??`）。所以 B 机现在 `git pull` 是拉不到的。
> 要么用本升级包，要么先把改动推上去（见第四节方案 B）。

---

## 二、文件清单

```
browser-upgrade/
├── install.py                 一键安装（在 B 机运行）
├── rollback.py                一键回滚
├── README.md                  本说明
├── payload/                   要复制到 B 机的文件
│   ├── mybrowser/             自研浏览器全量源码（不含 __pycache__）
│   ├── agent/browser/mybrowser_adapter.py
│   └── skills/browser-guide.qdskill
└── reference/                 A 机完整原版，仅 --full-app 用
    └── agent/{app.py,main.py}
```

**不会删除** B 机的 `agent/browser/webview2.py`、`protocol.py` —— 留着当回退。

---

## 三、操作步骤（推荐方案）

### A 机（这台）
1. 把整个 `browser-upgrade` 文件夹打包成 zip（或直接复制文件夹）。
2. 传到 B 机：U 盘 / 微信 / 网盘 / 局域网都行。

### B 机（另一台）
1. **先完全退出**正在运行的 INTENT（托盘也要退），避免文件被占用。
2. 解压，把 `browser-upgrade` 文件夹放到 INTENT 根目录**里面或旁边**都行。
3. 打开 CMD，`cd` 到 `browser-upgrade` 目录，先预演：

   ```
   chcp 65001
   python install.py D:\INTENT-win --dry-run
   ```

   路径换成 B 机实际的 INTENT 目录；若包就放在该目录里，可省略路径参数。

4. 确认预演没问题，正式安装：

   ```
   python install.py D:\INTENT-win
   ```

5. 装依赖（**最容易漏的一步**）：

   ```
   pip install pythonnet pywebview
   ```

   A 机实测 `pythonnet 3.2.1` + `pywebview`（自带 `Microsoft.Web.WebView2.*.dll`）。
   `requirements.txt` 里只写了 pywebview，**没写 pythonnet**，所以全新环境必装。
   Edge WebView2 Runtime 由 Windows 自带。

6. 重启 INTENT（`start.bat`），打开软件点「浏览器」验证。

---

## 四、两种安装模式怎么选

| 模式 | 命令 | 改什么 | 适用 |
|---|---|---|---|
| **默认（推荐）** | `python install.py D:\INTENT-win` | 只替换 app.py/main.py 各 2 行接线 | B 机 app.py 有自己定制，或只想要浏览器引擎一致 |
| **完全对齐** | `python install.py D:\INTENT-win --full-app` | 用 A 机 `app.py`/`main.py` **整份覆盖** | 确认 B 机 app.py 无独有改动，想要 100% 一样 |

`--full-app` 会额外带来 A 机这三项体验改动（默认模式没有）：
- 标题栏输网址/搜索词**一步直达**结果页（旧版是「先开主页再跳」两步）
- 应用启动**预热**浏览器（首次点击零冷启动）
- `open_browser()` 调 `show()` 把内嵌浏览器切到前台

> 旧 `WebView2Adapter` **没有 `show()` 方法**。如果 B 机 app.py 已经是新版（会调 `show()`）
> 却还配着旧适配器，点「打开浏览器」其实会报 `AttributeError` —— 换适配器正好一并修掉。

还有三处**可选同步**（默认模式会尽力而为，匹配不到就跳过，不影响功能）：
`agent/tools.py` 的浏览器工具描述、`agent/prompts.py` 的模型提示、
`配置.qdai` 追加 `mybrowser_*` 配置段。不想动这些就加 `--core-only`。

---

## 五、验证清单

安装完成后：

```
python install.py D:\INTENT-win          # 脚本自带导入+接口验证
python -m mybrowser.selftest             # 完整自检（会起无头 Edge，需联网）
python -m mybrowser                      # 单独启动自研浏览器窗口看界面
```

在软件里让 AI 执行 `BROWSER NAVIGATE https://example.com` 再 `BROWSER SNAPSHOT`，
应能看到带稳定 ref 的 AX 语义快照（新版特征），而不是旧版 DOM 注入结果。

新版比旧版多出的指令：`SEARCH / DIFF / CLICK_REF / TYPE_REF / PRESS / HOVER / DRAG /
SHOT / EVAL / DUMP / TRACE / RECORD / REPLAY / SERVE`。

---

## 六、回滚

```
python rollback.py D:\INTENT-win
python rollback.py D:\INTENT-win --list   # 查看所有备份
```

每次安装都会把被改动/覆盖的文件备份到 `backups/browser-upgrade-<时间戳>/`
（含 `manifest.json`）。回滚会还原原文件、删除新增文件，回到旧版 WebView2 浏览器。

---

## 七、其他方案对比

| 方案 | 做法 | 评价 |
|---|---|---|
| **A. 升级包（本包）** | 复制文件 + 跑脚本 | ✅ 最小侵入、可回滚、不依赖 git |
| B. Git 同步 | A 机 `git add/commit/push` 浏览器改动 → B 机 `git pull` | 适合 B 机是同一 git 仓库且版本接近；但 A 机的 `app.py` 改动会整块过去，若 B 机有定制会冲突。且本机 git HTTPS 曾被网络拦截，之前是靠 `tools/api_push.py` 走 REST 推送的 |
| C. 整包覆盖 | 把 A 机整个目录复制过去 | ❌ 会把 B 机的配置、模型档案、会话、记忆一起冲掉，不推荐 |

---

## 八、给另一台机器上的 AI 的提示词（可直接复制）

> 我在另一台电脑上有 INTENT-win（Windows Agent），主程序功能是最新的，但内置浏览器
> 还是旧版 pywebview `WebView2Adapter`。我已把 `browser-upgrade` 升级包放到本机，
> 里面有 `install.py`、`rollback.py`、`payload/`、`reference/` 和 `README.md`。
> 请帮我完成浏览器升级：
> 1. 先读 `README.md`，确认 INTENT 根目录的实际路径。
> 2. 确认已完全退出正在运行的 INTENT 进程。
> 3. 先跑 `python install.py <INTENT根目录> --dry-run` 预演，把结果告诉我。
> 4. 预演无异常后跑 `python install.py <INTENT根目录>` 正式安装；
>    如果我的 `agent/app.py` 确认没有任何独有改动，可改用 `--full-app` 做到 100% 对齐。
> 5. 检查并补齐依赖 `pythonnet`、`pywebview`（`pip install pythonnet pywebview`）。
> 6. 按 README 的验证清单验证；失败就跑 `python rollback.py <INTENT根目录>` 回滚并报告原因。
> 7. **不要覆盖或删除** `agent/app.py` 之外的业务代码、不要动 `配置.qdai` 的已有项、
>    不要动 `会话/`、`kb/`、`models/`、`backups/` 里的任何数据。
> 全程先备份、可回滚，任何一步不确定就停下来问我。

---

## 九、已知差异（诚实说明）

- 默认模式只保证**浏览器引擎**一致；`app.py` 的直达导航/预热/玻璃效果等 UI 层体验
  仍是 B 机原样。要完全一致用 `--full-app`。
- `skills/browser-guide.qdskill` 在 A 机仍是 5 条基础说明（未随新版指令更新），
  升级包按"完全复制 A 机"原则原样带上，所以两边一致，但都没写 DUMP/TRACE 等新指令。
- 浏览器用户数据目录默认是 `%APPDATA%/INTENT-win/mybrowser_profile`，
  B 机首次使用需重新登录网站（cookie 不跨机器）。
- `mybrowser_cdp_port` 默认 9700、`mybrowser_serve_port` 默认 9223，
  若 B 机有程序占用这些端口，在 `配置.qdai` 里改掉即可。
