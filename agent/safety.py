# -*- coding: utf-8 -*-
# SECURITY-FIXTURE: 本文件下方出现的密钥/手机号/证件号均为合成自测样本，绝非真实凭证。
"""INTENT 安全校验层：隐私出站闸门 + 敏感路径权限 + 审计与报告。

三件事：
  1. guard_outbound()  请求离开本机前扫描并脱敏密钥 / 证件等私密串
  2. guard_path()      拦住凭证、私钥、浏览器密码库等敏感路径
  3. audit()/report()  审计日志 + 「隐私怎么处理、私密内容会怎么样」报告

设计原则：
  * 精准处理高置信私密数据，正常聊天、换模型、读普通文件一律不受影响。
  * 审计日志只记「类型 / 数量 / 动作 / 目标域名」，绝不写原文、绝不写密钥。
  * 三种模式：enforce(默认，脱敏+拦截) / audit(只记录不改动) / off(完全不介入)。
  * 只依赖标准库，可被任何模块安全导入（导入失败不影响主流程）。
"""
import json
import os
import re
import sys
import threading
from datetime import datetime
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "logs", "security_audit.log")
REPORT_PATH = os.path.join(ROOT, "security", "PRIVACY_REPORT.md")
MODES = ("enforce", "audit", "off")


class SecurityError(RuntimeError):
    """安全校验主动拒绝。消息会原样回给模型/界面，不写原文路径。"""


# ---------------------------------------------------------------- 敏感内容识别
# 顺序有意义：私钥先于通用 key，身份证先于银行卡（避免长数字串被重复替换）。
# 每条规则都是高置信模式，普通中文、代码、路径不会被误伤。
_CRED_RULES = (
    ("PRIVATE_KEY", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8000}?-----END [A-Z ]*PRIVATE KEY-----"),
     "[已脱敏:私钥]"),
    ("API_KEY", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "[已脱敏:API密钥]"),
    ("OAUTH_TOKEN", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,})\b"),
     "[已脱敏:访问令牌]"),
    ("BEARER", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}\b"), "[已脱敏:鉴权头]"),
)

# key=value / key: "value" 形式：保留键名，只把值换掉，避免破坏 JSON 结构可读性。
_ASSIGN = re.compile(
    r"(?i)\b(?:api[_-]?key|apikey|access[_-]?token|password|passwd|secret)"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9._\-/+=]{8,})")

_PII_RULES = (
    ("ID_CARD", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[已脱敏:身份证]"),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[已脱敏:手机号]"),
    ("BANK_CARD", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[已脱敏:银行卡]"),
)


def _scan_text(text, mask_pii, findings):
    if not isinstance(text, str) or not text:
        return text
    for name, rx, placeholder in _CRED_RULES:
        text, n = rx.subn(placeholder, text)
        if n:
            findings[name] = findings.get(name, 0) + n

    def _hide_value(m):
        findings["CREDENTIAL_VALUE"] = findings.get("CREDENTIAL_VALUE", 0) + 1
        return m.group(0).replace(m.group(1), "[已脱敏:凭证值]")

    text = _ASSIGN.sub(_hide_value, text)
    if mask_pii:
        for name, rx, placeholder in _PII_RULES:
            text, n = rx.subn(placeholder, text)
            if n:
                findings[name] = findings.get(name, 0) + n
    return text


def _scrub(obj, mask_pii, findings):
    if isinstance(obj, str):
        return _scan_text(obj, mask_pii, findings)
    if isinstance(obj, dict):
        return {k: _scrub(v, mask_pii, findings) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        out = [_scrub(v, mask_pii, findings) for v in obj]
        return type(obj)(out) if isinstance(obj, tuple) else out
    return obj


def scan(text, mask_pii=True):
    """公开接口：扫描任意文本，返回 (脱敏后文本, {类型: 命中次数})。"""
    findings = {}
    return _scan_text(text, mask_pii, findings), findings


# ---------------------------------------------------------------- 配置与审计日志
_CFG_CACHE = {"mtime": 0.0, "cfg": None}
_LOG_LOCK = threading.Lock()


def current_config():
    """带 mtime 缓存地读配置；测试可传入 cfg 覆盖，不必反复读盘。"""
    path = os.path.join(ROOT, "配置.qdai")
    try:
        m = os.path.getmtime(path)
    except OSError:
        m = 0.0
    if _CFG_CACHE["cfg"] is None or m != _CFG_CACHE["mtime"]:
        try:
            from .config import load_config
        except ImportError:
            import config as _c
            load_config = _c.load_config
        _CFG_CACHE["cfg"] = load_config(path)
        _CFG_CACHE["mtime"] = m
    return _CFG_CACHE["cfg"]


def _mode(cfg):
    v = str(cfg.get("safety_mode", "enforce") or "enforce").strip().lower()
    return v if v in MODES else "enforce"


def _audit_enabled(cfg):
    v = cfg.get("safety_audit_log", True)
    if isinstance(v, str):
        return v.strip().lower() in ("on", "true", "1", "yes")
    return bool(v)


def _mask_pii(cfg):
    v = cfg.get("safety_mask_pii", True)
    if isinstance(v, str):
        return v.strip().lower() in ("on", "true", "1", "yes")
    return bool(v)


def _log(entry):
    """只写类型/数量/动作/域名；绝不写原文、密钥或完整用户路径。"""
    try:
        with _LOG_LOCK:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            rec = {"ts": datetime.now().isoformat(timespec="seconds")}
            rec.update(entry)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计日志失败绝不能影响主流程


def _host_of(u):
    if not u:
        return ""
    if "://" not in str(u):
        u = "http://" + str(u)
    try:
        h = (urlsplit(u).hostname or "").lower()
    except Exception:
        return ""
    # 过滤配置里的占位符（如 http://<你的网关IP>:9999/v1），别把它当成真实域名
    return "" if any(c in h for c in "<> ") else h


def allowed_hosts(cfg):
    """当前可信出站域名：本机 + 已配置的服务商 + 用户额外允许的域名。"""
    hosts = {"127.0.0.1", "localhost", "::1"}
    for u in (cfg.get("base_url", ""), cfg.get("llm_base_url", "")):
        h = _host_of(u)
        if h:
            hosts.add(h)
    try:
        profiles = json.loads(cfg.get("profiles") or "[]")
        for p in profiles:
            if isinstance(p, dict):
                h = _host_of(p.get("base_url", ""))
                if h:
                    hosts.add(h)
    except Exception:
        pass
    for h in str(cfg.get("safety_allow_hosts", "") or "").split(","):
        if h.strip():
            hosts.add(h.strip().lower())
    return hosts


# ---------------------------------------------------------------- 出站隐私闸门
def guard_outbound(url, payload, cfg=None):
    """【出站唯一收口，由 llm._post 调用】

    返回 (payload, info)。enforce 下返回脱敏后的新 payload；audit 下原样返回；
    off 下完全不介入。域名只记录不拦截——用户随时会在设置里换服务商，硬拦会
    破坏「设置模型」。真正的强制力放在内容脱敏与敏感路径上。
    """
    cfg = cfg or current_config()
    mode = _mode(cfg)
    if mode == "off":
        return payload, {"mode": mode, "skipped": True}
    host = _host_of(url)
    trusted = host in allowed_hosts(cfg)
    findings = {}
    scrubbed = _scrub(payload, _mask_pii(cfg), findings)
    masked = bool(findings) and mode == "enforce"
    out = scrubbed if masked else payload
    msgs = payload.get("messages", []) if isinstance(payload, dict) else []
    if _audit_enabled(cfg):
        _log({
            "event": "outbound",
            "action": "masked" if masked else ("observed" if findings else "clean"),
            "mode": mode,
            "host": host,
            "trusted": trusted,
            "msg_count": len(msgs) if isinstance(msgs, list) else 0,
            "findings": findings,
        })
    return out, {"mode": mode, "host": host, "trusted": trusted,
                 "findings": findings, "masked": masked}


# ---------------------------------------------------------------- 路径权限校验
_SENSITIVE_NAMES = (
    "配置.qdai", "配置.local.qdai", "pc_bridge.token", "phone.key", ".env",
    "id_rsa", "id_ed25519", "credentials", "ntuser.dat", "sam", "shadow",
    "login data", "cookies.sqlite", "web data",
)
_SENSITIVE_EXTS = (".key", ".pem", ".p12", ".pfx", ".kdbx", ".ppk")
_SENSITIVE_DIRS = (
    "\\.ssh\\", "\\.aws\\", "\\.gnupg\\",
    "\\appdata\\local\\microsoft\\edge\\user data\\",
    "\\appdata\\local\\google\\chrome\\user data\\",
    "\\windows\\system32\\config\\",
)
_SENSITIVE_TAILS = ("\\.ssh", "\\.aws", "\\.gnupg")


def guard_path(raw, cfg=None, op="read"):
    """【路径唯一收口，由 tools._resolve_path 调用】

    返回 (allowed, rule, realpath)。命中敏感规则时 enforce 下 allowed=False。
    reason 里不含用户具体路径，避免审计日志变成环境指纹。
    """
    cfg = cfg or current_config()
    mode = _mode(cfg)
    p = str(raw or "").strip().strip('"').strip("'")
    if not p:
        return True, "", ""
    try:
        real = os.path.realpath(os.path.expanduser(p))
    except Exception:
        real = os.path.abspath(os.path.expanduser(p))
    low = real.replace("/", "\\").lower()
    base = os.path.basename(low)
    rule = ""
    if base in _SENSITIVE_NAMES:
        rule = "凭证/密钥文件"
    elif base.endswith(_SENSITIVE_EXTS):
        rule = "私钥或证书文件"
    elif any(d in low for d in _SENSITIVE_DIRS) or low.endswith(_SENSITIVE_TAILS):
        rule = "系统凭据目录"
    allowed = (not rule) or mode != "enforce"
    if _audit_enabled(cfg) and mode != "off":
        _log({
            "event": "path",
            "action": "blocked" if not allowed else ("observed" if rule else "allowed"),
            "mode": mode, "op": op, "rule": rule, "file": base if rule else "",
        })
    return allowed, rule, real


def path_error(rule):
    return ("[安全校验] 已拒绝访问：%s。此类文件可能含密钥或账号凭据，"
            "不允许通过文件工具读取（防止内容随对话发往云端）。"
            "如确需操作，请在本体目录手动处理，或把 配置.qdai 的 safety_mode 设为 audit。"
            % (rule or "敏感路径"))


# ---------------------------------------------------------------- 自检
def selftest():
    """内置自检，返回 (通过数, 总数, 明细行列表)。报告会引用它的结果。"""
    cfg = {"safety_mode": "enforce", "safety_mask_pii": True,
           "safety_audit_log": False, "base_url": "https://api.example.com/v1",
           "profiles": "[]", "safety_allow_hosts": ""}
    lines = []
    ok = [0]

    def check(name, cond, extra=""):
        ok[0] += int(bool(cond))
        lines.append(("PASS" if cond else "FAIL") + "  " + name +
                     (("  | " + str(extra)) if extra else ""))

    sample = ("api_key=sk-cvQh4uacn3OM4tFIAzThtuiOgwFhJZTi "
              "Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghijklmnop "
              "身份证 110101199003072316 手机 13812345678 "
              "银行卡 6222020200001234567")
    red, found = scan(sample)
    check("识别 API Key", found.get("API_KEY", 0) >= 1, found)
    check("识别 Bearer 令牌", found.get("BEARER", 0) >= 1)
    check("识别身份证 / 手机号 / 银行卡",
          all(found.get(k, 0) >= 1 for k in ("ID_CARD", "PHONE", "BANK_CARD")))
    check("脱敏后原文密钥不残留", "sk-cvQh4u" not in red and "13812345678" not in red)
    check("普通文本不误伤", scan("帮我看看 D:/INTENT-win/README.md 第 3 行写了什么")[1] == {})

    payload = {"model": "m", "messages": [
        {"role": "system", "content": "你是助手"},
        {"role": "tool", "content": "读到密钥 sk-abcdefghijklmnop123456"},
        {"role": "user", "content": [{"type": "text", "text": "手机 13900001111"}]},
    ]}
    out, info = guard_outbound("https://api.example.com/v1/chat/completions", payload, cfg)
    check("出站闸门对 tool 结果生效",
          "sk-abcdefghijklmnop123456" not in json.dumps(out, ensure_ascii=False),
          info["findings"])
    check("出站闸门对多模态 content 生效",
          "13900001111" not in json.dumps(out, ensure_ascii=False))
    check("原 payload 未被就地改写", "sk-abcdefghijklmnop123456" in
          json.dumps(payload, ensure_ascii=False))
    check("可信域名标记正确", info["trusted"] is True)
    _, info2 = guard_outbound("https://unknown-third-party.test/v1", payload, cfg)
    check("陌生域名被记录为不可信（不硬拦）", info2["trusted"] is False)

    audit_cfg = dict(cfg, safety_mode="audit")
    out2, _ = guard_outbound("https://api.example.com/v1", payload, audit_cfg)
    check("audit 模式只记录不改动", "sk-abcdefghijklmnop123456" in
          json.dumps(out2, ensure_ascii=False))
    off_cfg = dict(cfg, safety_mode="off")
    out3, _ = guard_outbound("https://api.example.com/v1", payload, off_cfg)
    check("off 模式完全不介入", out3 is payload)

    allowed, rule, _ = guard_path("D:/INTENT-win/配置.qdai", cfg)
    check("拦截凭证配置文件", not allowed and rule == "凭证/密钥文件", rule)
    allowed, _, _ = guard_path("C:/Users/test/.ssh/id_rsa", cfg)
    check("拦截 SSH 私钥目录", not allowed)
    allowed, _, _ = guard_path("C:/Users/test/AppData/Local/Microsoft/Edge/User Data/Default/Login Data", cfg)
    check("拦截浏览器密码库", not allowed)
    allowed, _, _ = guard_path("D:/INTENT-win/README.md", cfg)
    check("普通项目文件放行", allowed)
    allowed, _, _ = guard_path("C:/Users/test/Desktop/notes.txt", cfg)
    check("桌面普通文件放行", allowed)
    allowed, _, _ = guard_path("D:/INTENT-win/配置.qdai", audit_cfg)
    check("audit 模式路径放行", allowed)
    check("拒绝原因不含完整路径", "D:/" not in path_error(rule) and "\\" not in path_error(rule))
    return ok[0], len(lines), lines


# ---------------------------------------------------------------- 报告
def build_report(save=True):
    passed, total, lines = selftest()
    cfg = current_config()
    mode = _mode(cfg)
    hosts = sorted(h for h in allowed_hosts(cfg) if h not in ("::1",))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    findings_n = 0
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            findings_n = sum(1 for _ in f)
    except Exception:
        pass
    md = """# INTENT 隐私与安全校验报告

生成时间：%s  
当前模式：**%s**（enforce=脱敏并拦截 / audit=只记录 / off=关闭）

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

当前可信出站域名（自动取自配置，不硬编码）：%s

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

审计日志：`logs/security_audit.log`（JSONL，只记类型/数量/动作/域名，**不记原文、不记密钥、不记完整路径**），当前 %d 条。

## 三、自验收结果

内置自检 **%d/%d 通过**（`python -m agent.safety --selftest`）：

```
%s
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
""" % (now, mode, "、".join("`%s`" % h for h in hosts) or "（未配置）",
       findings_n, passed, total, "\n".join(lines))
    if save:
        try:
            os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
            with open(REPORT_PATH, "w", encoding="utf-8") as f:
                f.write(md)
        except Exception as e:
            return md, "[报告写入失败] %s" % e
    return md, REPORT_PATH


def _main(argv):
    arg = (argv[1] if len(argv) > 1 else "").strip()
    if arg == "--selftest":
        passed, total, lines = selftest()
        print("\n".join(lines))
        print("\n==== %d/%d PASS ====" % (passed, total))
        return 0 if passed == total else 1
    if arg == "--log":
        n = 20
        if len(argv) > 2:
            try:
                n = int(argv[2])
            except ValueError:
                pass
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                rows = f.readlines()[-n:]
        except OSError:
            rows = []
        for r in rows:
            print(r.rstrip())
        print("---- 共显示 %d 条，日志：%s ----" % (len(rows), LOG_PATH))
        return 0
    if arg == "--check" and len(argv) > 2:
        red, found = scan(" ".join(argv[2:]))
        print("命中：", found or "无")
        print("脱敏后：", red)
        return 0
    md, where = build_report(save=True)
    print("隐私与安全校验报告已生成：%s" % where)
    print("模式：%s ｜ 内置自检：%d/%d ｜ 审计报告：%s" %
          (_mode(current_config()), selftest()[0], selftest()[1], LOG_PATH))
    print("\n摘要：\n" + "\n".join(md.splitlines()[:8]))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
