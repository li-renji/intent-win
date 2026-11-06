# -*- coding: utf-8 -*-
"""
INTENT-win 推送前安全体检（公开仓库必跑）

用法:  python security/check_before_push.py
退出码: 0 = 可以推送; 1 = 存在阻断项(FAIL)

原则: 只输出 文件名/行号/命中类型, 绝不输出命中的原文或密钥值。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_MB = 90  # GitHub 单文件硬限 100MB, 留余量

# 规则刻意拆开写, 避免本脚本被自己扫中
PATTERNS = [
    ("模型 API Key", re.compile(r"\b" + "sk-" + r"[A-Za-z0-9_\-]{20,}\b")),
    ("GitHub Token", re.compile(r"\b(?:" + "ghp_|gho_|ghu_|ghs_|ghr_|github_pat_" +
                                r")[A-Za-z0-9_]{20,}\b")),
    ("云厂商 AccessKey", re.compile(r"\b" + "AKIA" + r"[0-9A-Z]{16}\b")),
    ("私钥", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?" + "PRIVATE KEY" + r"-----")),
    ("明文密钥赋值", re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("本机私有绝对路径", re.compile(r"(?i)[A-Z]:\\Users\\" + "人机")),
]
# 明确是测试/占位/文档示例的行降级为 WARN, 不阻断
FAKE_LINE = re.compile(
    r"(?i)(SECURITY-FIXTURE|test|mock|fake|example|sample|placeholder|dummy|示例|占位|"
    r"your[_-]?key|xxx+|sk-noauth|<[^>]+>|\$\{)")
# 即便文件被标记为自测样本, 这些类型也绝不降级
HARD_TYPES = {"GitHub Token", "云厂商 AccessKey", "私钥", "本机私有绝对路径"}

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".mp4", ".mp3",
    ".wav", ".zip", ".7z", ".exe", ".dll", ".pyd", ".pak", ".dat", ".bin",
    ".cab", ".msi", ".pdf", ".ttf", ".woff", ".woff2", ".pyc",
}

# 这些路径必须被 .gitignore 挡住（探针文件不存在也能测规则是否匹配）
MUST_IGNORE = [
    "配置.qdai", "配置.local.qdai", "pc_bridge.token", "phone.key",
    "runtime/python/python.exe", "runtime/webview2/msedgewebview2.exe",
    "runtime/chromium/chrome.exe",
    "logs/security_audit.log", "logs/chat_engine.log",
    "logs/user_path_backup_20261105.txt",
    "backups/probe.txt", "会话/probe.txt", "memories/probe.txt",
    "kb/probe.txt", "plugin_data/probe.txt", ".run.pid", "launch.log",
    "Q版赛车少年桌宠工程包/原型/assets/frames/jump/000.png",
    "_probe_e2e.mp3",
]

result = []


def add(level, name, detail=""):
    result.append((level, name, detail))
    mark = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[level]
    print("%s %s%s" % (mark, name, ("  -> " + detail) if detail else ""))


def git_bytes(*args):
    return subprocess.run(["git", "-C", ROOT] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def is_ignored(rel):
    r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "--", rel],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def tracked_or_addable():
    raw = git_bytes("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]


def main():
    print("== INTENT-win 推送前安全体检 ==")
    print("仓库: %s\n" % ROOT)

    files = tracked_or_addable()
    print("将纳入仓库的文件: %d 个\n" % len(files))

    # 1) 忽略规则覆盖
    print("-- A. 隐私/运行时忽略规则 --")
    for rel in MUST_IGNORE:
        if is_ignored(rel):
            add("PASS", "已忽略 %s" % rel)
        else:
            add("FAIL", "未忽略 %s" % rel, "需补进 .gitignore")

    # 2) 大文件
    print("\n-- B. 超大文件 --")
    big = []
    for rel in files:
        fp = os.path.join(ROOT, rel)
        try:
            mb = os.path.getsize(fp) / 1048576.0
        except OSError:
            continue
        if mb > MAX_MB:
            big.append((rel, mb))
    if big:
        for rel, mb in big[:10]:
            add("FAIL", "超大文件 %.1fMB %s" % (mb, rel))
    else:
        add("PASS", "无 >%dMB 文件" % MAX_MB)

    # 3) 内容扫描
    print("\n-- C. 密钥 / 隐私内容扫描 --")
    hits, warns, fixture_hits = [], [], []
    fixture_files = set()
    scanned = 0
    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        if ext in BINARY_EXT:
            continue
        fp = os.path.join(ROOT, rel)
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        scanned += 1
        # 文件头部显式声明「以下均为合成样本」才可降级；硬类型永不降级
        fixture = any("SECURITY-FIXTURE" in x for x in lines[:20])
        if fixture:
            fixture_files.add(rel)
        for no, line in enumerate(lines, 1):
            for label, rx in PATTERNS:
                if rx.search(line):
                    item = "%s:%d [%s]" % (rel, no, label)
                    if fixture and label not in HARD_TYPES:
                        fixture_hits.append(item)
                    elif FAKE_LINE.search(line):
                        warns.append(item)
                    else:
                        hits.append(item)
                    break
    print("   已扫描文本文件: %d 个" % scanned)
    if fixture_files:
        add("PASS", "合成样本文件已显式标记 SECURITY-FIXTURE (%d 个)" % len(fixture_files),
            ", ".join(sorted(fixture_files)))
    if fixture_hits:
        add("WARN", "安全自测合成样本 %d 处（硬类型密钥仍会阻断）" % len(fixture_hits),
            "如 " + "; ".join(fixture_hits[:2]))
    if hits:
        for h in hits[:25]:
            add("FAIL", "疑似真实私密数据 " + h)
        if len(hits) > 25:
            add("FAIL", "另有 %d 处同类命中" % (len(hits) - 25))
    else:
        add("PASS", "未发现真实密钥/手机号/身份证/私钥")
    if warns:
        add("WARN", "测试或文档占位值 %d 处（已确认非真实凭证）" % len(warns),
            "; ".join(warns[:4]))

    # 4) 生成产物占比（提示，不阻断）
    print("\n-- D. 待提交文件构成 --")
    from collections import Counter
    top = Counter(rel.split("/")[0] for rel in files)
    for name, n in top.most_common(8):
        print("   %-40s %d 个文件" % (name, n))
    if len(files) > 800:
        add("WARN", "待提交文件偏多(%d)，检查是否混入了生成产物" % len(files))

    fails = [r for r in result if r[0] == "FAIL"]
    warns_n = [r for r in result if r[0] == "WARN"]
    print("\n==== %d PASS / %d WARN / %d FAIL ====" %
          (len([r for r in result if r[0] == "PASS"]), len(warns_n), len(fails)))
    if fails:
        print("结论: 禁止推送，请先修复上述 FAIL。")
        return 1
    print("结论: 可以推送。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
