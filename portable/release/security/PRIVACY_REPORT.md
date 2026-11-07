# INTENT 隐私与安全校验报告

生成时间：2026-11-06 22:49:40  
当前模式：**enforce**（enforce=脱敏并拦截 / audit=只记录 / off=关闭）

## 一、这个 Agent 现在怎么处理隐私

主链路只有三步，装校验前**三步之间没有任何过滤**：
界面或终端拼 `messages` → `AgentLoop` 把工具结果回灌 → `llm._post` 一次 POST 到云端。

| 本地数据 | 去哪 | 出网 | 明文落盘 |
|---|---|---|---|
| 你输入的聊天内容 | 云端大模型 | 是 | 是 `会话/*.json` |
| 长期记忆/用户画像（姓名、生日、手机号、地址） | 注入 system → 云端 | 是 | 是 `memories/memory.json` |
| 历史事实摘要、技能绝对路径 | 注入 system → 云端 | 是 | 是 |
| 文件工具读到的本地文件正文 | 作为 tool 结果回灌 → 云端 | 是 | 原文件本就在磁盘 |
| 附件图片 / 附件本地路径 | 云端 | 是 | 路径文本落会话 |
| 网页抓取、搜索词 | 搜索引擎 + 结果回灌云端 | 是 | 否 |
| 屏幕截图 | 本机 | 否 | 是 `shots/*.png` |
| 全机文件名 + 文档正文索引 | 本机 | 命中后回灌 | 是 `kb/index.json`、`kb/content.json` |
| API Key | 请求头发往服务商 | 是 | **是 `配置.qdai`（明文）** |

当前可信出站域名（自动取自配置，不硬编码）：`127.0.0.1`、`api.xiaomimimo.com`、`localhost`、`token.sensenova.cn`

## 二、装上校验后，私密内容会怎么样

| 场景 | enforce（默认） | audit | off |
|---|---|---|---|
| 对话里出现 `sk-…` / Bearer / 私钥 / `api_key=xxx` | **替换为 `[已脱敏:…]` 再发出** | 原样发出，仅记录 | 原样发出 |
| 对话里出现身份证 / 手机号 / 银行卡 | **替换为占位符再发出** | 原样发出，仅记录 | 原样发出 |
| 工具结果（如读文件）里混进密钥 | 回灌下一轮前同样被脱敏 | 仅记录 | 不处理 |
| Agent 想读 `配置.qdai`、`.ssh`、`.env`、浏览器密码库 | **拒绝**，返回中文原因 | 放行并记录 | 放行 |
| 普通聊天、读普通文件、换模型服务商 | 完全不受影响 | 同左 | 同左 |

三点边界，如实说明：
- **域名只记录不拦截**：用户随时会在设置里换服务商，硬拦会弄坏「设置模型」。陌生目标会在审计日志和本报告中列为不可信。
- **认证头里的 API Key 不会被脱敏**：那是请求必需的凭证；换掉就没法调用了。防护重点是「别让密钥出现在正文里」。
- **落盘不加密**：本层管的是「离开本机」与「敏感文件读取」，不改磁盘上的会话/记忆文件。

审计日志：`logs/security_audit.log`（JSONL，只记类型/数量/动作/域名，**不记原文、不记密钥、不记完整路径**），当前 30 条。

## 三、自验收结果

内置自检 **19/19 通过**（`python -m agent.safety --selftest`）：

```
PASS  识别 API Key  | {'API_KEY': 1, 'BEARER': 1, 'ID_CARD': 1, 'PHONE': 1, 'BANK_CARD': 1}
PASS  识别 Bearer 令牌
PASS  识别身份证 / 手机号 / 银行卡
PASS  脱敏后原文密钥不残留
PASS  普通文本不误伤
PASS  出站闸门对 tool 结果生效  | {'API_KEY': 1, 'PHONE': 1}
PASS  出站闸门对多模态 content 生效
PASS  原 payload 未被就地改写
PASS  可信域名标记正确
PASS  陌生域名被记录为不可信（不硬拦）
PASS  audit 模式只记录不改动
PASS  off 模式完全不介入
PASS  拦截凭证配置文件  | 凭证/密钥文件
PASS  拦截 SSH 私钥目录
PASS  拦截浏览器密码库
PASS  普通项目文件放行
PASS  桌面普通文件放行
PASS  audit 模式路径放行
PASS  拒绝原因不含完整路径
```

另有一套端到端验收 `python security/verify_safety.py`：用本地假服务器捕获真实出站请求体，
验证 `llm._post` 收口确实生效；再用真实 `ToolBus` 验证敏感路径拦截、6801 总线拒绝、
审计日志不泄漏原文，以及报告与命令行可用。最新一次 **29/29 通过**。

## 四、仍然存在、我没有擅自改动的风险

以下都是实测存在的，改动会牵涉你的使用方式或需要你先定策略，所以**只报告、不擅自动**：

1. `配置.qdai` 里仍有 3 把明文 API Key（主 key + profiles 里的两把）。任何能读该文件的本地进程都能拿到。改法：移到环境变量或系统凭据库，或至少收紧文件 ACL。
2. `会话/*.json`、`memories/memory.json`、`kb/content.json` 仍是明文全量落盘（聊天全文、用户画像、全机文档正文）。改法：加密存储或按需关闭持久化。
3. 6800 / 6801 本地端口无鉴权，任意本机进程可让 Agent 读文件、执行命令。改法：加本地 token。
4. 文件桥 `pc_bridge_root` 为空时等于全盘可访问，且监听 `0.0.0.0`；token 会打印进引擎日志。改法：设 root 为项目内目录、改绑 127.0.0.1。
5. `local_engine_fallback_cloud=on`：本地引擎没起来时会**静默**改发云端，用户可能以为在用本地模型。改法：设为 off 或加显式提示。
6. 桌宠 TTS 插件会借主 API Key 把待朗读文本发到第三方云。
7. `.gitignore` 只排了 `*.log`，没排 `logs/*.txt`；`logs/user_path_backup_20261105.txt` 含本机用户名与完整 PATH，尚未被忽略。

## 五、怎么查 / 怎么调

```
python -m agent.safety                 # 重新生成本报告并打印摘要
python -m agent.safety --selftest     # 跑内置自检
python -m agent.safety --log 20       # 看最近 20 条审计
python -m agent.safety --check "文本"  # 手动测一段内容会不会被识别
```

在 `配置.qdai` 里调整：

```
safety_mode=enforce        # enforce / audit / off
safety_mask_pii=on         # 是否脱敏身份证 / 手机号 / 银行卡
safety_audit_log=on        # 是否写审计日志
safety_allow_hosts=        # 额外可信出站域名，逗号分隔
```
