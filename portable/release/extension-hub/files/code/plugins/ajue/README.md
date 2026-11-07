# 阿觉 · companion 插件（个人生活智能体）

阿觉不是 intent-win 的一部分，而是**装上去才有**的插件。
通用 Agent 保持通用：它只认识两种扩展——

| 类型 | 声明 | 作用 |
| --- | --- | --- |
| `tool` | `plugins/<id>.qdplugin`（`kind: tool`） | 声明式工具，注册进 ToolBus 供模型调用 |
| `companion` | `plugins/<id>.qdplugin`（`kind: companion`） | 常驻桌面的独立进程（人格 / 桌宠），本目录即是 |

本插件声明在 `plugins/ajue.qdplugin`：

```
id: ajue
kind: companion
data_dir: plugin_data/ajue
entry: runner.py
ui: pet
```

## 它是什么

- **只服务你一个人的生活伙伴**：运行在本机，不是云端服务；但**可以联网**（最终走宿主 Agent 的云端端点）。
- **独立记忆**：阿觉的一切数据落在 `plugin_data/ajue/`（记忆 / 画像 / 风格 / 日记 / 授权），
  与通用 Agent 的 `记忆/` 完全隔离，互不污染。
- **读取你的上下文与各工作区会话**：扫描宿主项目根、以及你添加的其它工作区的 `会话/*.json`，
  据此认识你、越来越像你（**需授权**，随时可撤回）。
- **完全依赖通用 Agent**：阿觉不自带模型、不带密钥。说话走宿主暴露的本地
  OpenAI 兼容端点（默认 `127.0.0.1:6802`），宿主不在时阿觉会自行退场。
- **桌宠是唯一入口**：你与阿觉对话走桌宠窗口，不是在通用 Agent 的对话框里。

## 文件

| 文件 | 说明 |
| --- | --- |
| `persona.json` | 人格配置（名字 / 语气 / 音色 / 价值观 / 边界 / 口头禅），改完即生效 |
| `runner.py` | 插件进程入口：拉起桌宠窗口、对外提供阿觉的接口 |
| `ajue_core.py` | 内核：人格、被动记忆捕获、风格学习、L3 画像、日记、授权、工作区扫描、端点调用 |
| `tts.py` | 语音（edge-tts，缺失时降级系统语音） |
| `pet.html` | 桌宠界面：角色 + 对话流 + 输入框 + 授权/工作区/日记/语音控制 |

## 怎么自定义

编辑 `persona.json`：

| 字段 | 含义 |
| --- | --- |
| `name` | 它叫什么（默认「阿觉」） |
| `tone` | 说话语气 |
| `voice` | 朗读音色（`edge-tts` 的 ShortName，如 `zh-CN-XiaoxiaoNeural`） |
| `greeting` | 见面问候 |
| `values` / `boundaries` | 它认同的价值观与绝不越过的边界 |
| `catchphrases` | 口头禅 |
| `style_mimic` | 是否学习并模仿你的说话方式（**仅措辞/节奏，不学观点价值观**） |
| `permissions_requested` | 它希望申请的授权（你在桌宠「授权」里逐项批准/撤回） |

改完保存，重启插件进程即可生效。社区也可以发布自己的 companion 插件替换它。

## 授权（最小权限）

| 权限 | 含义 |
| --- | --- |
| `observe_conversations` | 读你的对话（认识你、学你的说话方式） |
| `speak_aloud` | 开口说话 |

授权状态存在 `plugin_data/ajue/permissions.json`，在桌宠「授权」面板里随时撤回，撤回即失效。

## mini MIND 集成

若宿主装了 mini MIND 插件，阿觉可以把你同意的日常对话提炼成训练数据。
**绝不自动执行**：每次都需要你明确同意才会落盘。

## 手动运行

```bash
python plugins/ajue/runner.py            # 常驻桌宠
python plugins/ajue/runner.py --learn    # 一次性学习各工作区会话
python plugins/ajue/runner.py --say "在吗"
```
