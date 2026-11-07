# -*- coding: utf-8 -*-
"""经济性工具：token 估算与输出截断（对齐 pi 的两大省钱手段）。

pi（earendil-works/pi）用两招把消耗压到极低：
1. 工具输出硬截断：2000 行 / 50KB 封顶，截断后告诉模型「用 offset 续读」，
   绝不把几十 KB 的目录列表/日志整份回灌上下文；
2. 上下文预算管理：实时估算 token，接近上限就压缩，而不是等上游报错。

本模块提供这两招的纯标准库实现：
- estimate_tokens：中英文混合估算（CJK≈1 tok/字，ASCII≈1 tok/4 字符）
- truncate_middle：保留头尾、中间打洞（错误信息通常在输出末尾）
- truncate_head：从头截断并生成「可操作」的续读提示
"""


def estimate_tokens(text):
    """粗估一段文本的 token 数（保守偏高，宁多勿少）。

    CJK 字符按 1 token/字，ASCII 按 1 token/4 字符（OpenAI 经验值）。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in str(text):
        if ord(ch) > 0x2E7F:          # CJK 及全角符号
            cjk += 1
        else:
            other += 1
    return int(cjk + other / 4) + 1


def clamp_tokens(text, max_tokens, head_ratio=0.6):
    """把一段文本压到不超过 max_tokens（粗估口径同 estimate_tokens）。

    超长时保留头尾、中间打洞，并插一行说明被省略了多少——
    论文/长文档整篇塞进上下文时，这是唯一能既保住信息又不炸容量的办法。
    返回 (result, dropped_chars)。
    """
    s = str(text or "")
    if max_tokens <= 0 or not s:
        return s, 0
    est = estimate_tokens(s)
    if est <= max_tokens:
        return s, 0
    # 按估算比例线性收敛，留 5% 余量给标点/格式化差异
    keep_ratio = max(0.05, (max_tokens / float(est)) * 0.95)
    keep_chars = max(200, int(len(s) * keep_ratio))
    res, cut = truncate_middle(s, keep_chars)
    return res, max(0, len(s) - len(res))


def messages_tokens(messages):
    """整条消息列表的 token 估算（含每条消息的固定开销）。"""
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        total += estimate_tokens(m.get("content") or "")
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            total += estimate_tokens(fn.get("name") or "")
            total += estimate_tokens(fn.get("arguments") or "")
        total += 4                     # role 等结构开销
    return total


def content_tokens(content):
    """支持多模态 content（str 或 [{type,text}] / [{type,image_url}]）。"""
    if isinstance(content, str):
        return estimate_tokens(content)
    total = 0
    for part in content or []:
        if not isinstance(part, dict):
            total += estimate_tokens(str(part))
            continue
        if part.get("type") == "text":
            total += estimate_tokens(part.get("text") or "")
        else:
            # 图片按固定开销粗估（真实按 patch 数算，这里只做量级判断）
            total += 800
    return total


def messages_tokens2(messages):
    """messages_tokens 的多模态安全版：content 可能是 list。"""
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        total += content_tokens(m.get("content"))
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            total += estimate_tokens(fn.get("name") or "")
            total += estimate_tokens(fn.get("arguments") or "")
        total += 4
    return total


def _fmt_n(n):
    """大数字缩写：12345 → 1.2万。"""
    if n >= 10000:
        return "%.1f万" % (n / 10000.0)
    return str(n)


def truncate_middle(text, max_chars):
    """头尾保留、中间打洞的截断（适合代码/日志：报错多在末尾）。

    返回 (result, truncated)。未超限时原文返回。
    """
    s = str(text or "")
    if len(s) <= max_chars:
        return s, False
    if max_chars < 200:                # 限额太小，退化为纯头截
        return s[:max_chars], True
    head = int(max_chars * 0.55)
    tail = max_chars - head - 40
    if tail < 40:
        return s[:max_chars], True
    removed = len(s) - head - tail
    marker = "\n…[中间省略 %s 字符，共 %s]…\n" % (_fmt_n(removed), _fmt_n(len(s)))
    return s[:head] + marker + s[len(s) - tail:], True


def truncate_head(text, max_lines=500, max_bytes=20000):
    """从头截断（适合文件读取/目录列表），返回 (result, info)。

    info 为 None 表示未截断；否则 dict 含 truncated_by/total_lines/output_lines/next_offset。
    """
    s = str(text or "")
    lines = s.split("\n")
    total_lines = len(lines)
    total_bytes = len(s.encode("utf-8", "ignore"))
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return s, None
    out, used, cut_by = [], 0, "lines"
    for ln in lines[:max_lines]:
        b = len(ln.encode("utf-8", "ignore")) + 1
        if used + b > max_bytes:
            cut_by = "bytes"
            break
        out.append(ln)
        used += b
    if len(out) >= max_lines and used <= max_bytes:
        cut_by = "lines"
    info = {"truncated_by": cut_by, "total_lines": total_lines,
            "output_lines": len(out), "next_offset": len(out) + 1}
    return "\n".join(out), info


if __name__ == "__main__":
    # 自检：估算与截断
    t = estimate_tokens("你好世界" * 100)
    assert 380 < t < 460, t
    assert estimate_tokens("") == 0
    s, cut = truncate_middle("a" * 100 + "ERROR_TAIL" + "b" * 100, 300)
    assert cut and "ERROR_TAIL" in s and "省略" in s
    s2, cut2 = truncate_middle("short", 100)
    assert not cut2 and s2 == "short"
    body = "\n".join("line%d" % i for i in range(1000))
    h, info = truncate_head(body, max_lines=100)
    assert info and info["output_lines"] == 100 and info["next_offset"] == 101
    print("econ 自检 OK")
