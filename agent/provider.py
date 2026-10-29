# -*- coding: utf-8 -*-
"""云端 API 接入：Key 验证 + 模型清单拉取 + 模型档位识别。

为什么单独一个模块：
    之前设置页只让用户手填 model 字符串——填错了要等到真正对话报 404 才发现，
    而且用户根本不知道自己这一轮到底在调哪个模型、哪个贵。本模块负责三件事：
      1. check_endpoint()  —— 用 GET /models 探测接口，不烧 token、不花钱
      2. probe_model()     —— 手动填模型名时，用一次极小的对话请求做真实验证
      3. tier_of()         —— 把模型名/厂商元数据归类为 旗舰 / 均衡 / 轻量

设计约束：
    - Key 绝不写进日志、异常信息或返回值（只在内存里用一次）
    - 厂商不提供 /models（自建网关、本地推理软件）时必须优雅降级，
      由上层弹窗让用户手填模型名，而不是卡死或静默失败
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15

TIER_FLAGSHIP = "flagship"
TIER_BALANCED = "balanced"
TIER_ECONOMY = "economy"

# ---- 档位关键词 ----
# 轻量词先判：否则 "o1-mini" 会被 "o1" 抢先命中成旗舰。
_LIGHT_TOKENS = ("flash", "lite", "mini", "nano", "haiku", "tiny", "light",
                 "cheap", "efficient", "instant", "turbo", "便宜", "轻量", "经济")
_FLAG_TOKENS = ("pro", "ultra", "max", "opus", "flagship", "premium", "旗舰")
_REASON_TOKENS = ("reason", "thinking", "r1", "o1", "o3", "o4")

# 例外：名字里带轻量词但其实是旗舰档的（如 gpt-4-turbo）
_TIER_OVERRIDE = {
    "gpt-4-turbo": TIER_FLAGSHIP,
    "gpt-4o-turbo": TIER_FLAGSHIP,
}

# 家族前缀：既没命中轻量词也没命中旗舰词时，用已知系列定档
_FLAGSHIP_FAMILY = ("gpt-4o", "gpt-4.1", "gpt-4", "gpt-5", "claude-opus",
                    "gemini-pro", "gemini-1.5-pro", "gemini-2.5-pro",
                    "grok-4", "deepseek-r1")
_ECONOMY_FAMILY = ("gpt-3.5", "gpt-35", "claude-3-haiku", "gemini-nano")

TIER_LABEL = {
    TIER_FLAGSHIP: "旗舰",
    TIER_BALANCED: "均衡",
    TIER_ECONOMY: "轻量",
}
TIER_HINT = {
    TIER_FLAGSHIP: "能力最强、最贵，适合复杂任务与关键代码",
    TIER_BALANCED: "能力与价格均衡，日常对话的默认选择",
    TIER_ECONOMY: "最便宜，适合简单任务、批量处理与省钱场景",
}


# ---------- 工具 ----------

def _norm_base(base_url):
    return (base_url or "").strip().rstrip("/")


def _is_loopback(url):
    """判断是否是本机地址。本机推理软件（llama-server 等）必须直连，
    否则会被系统 HTTP 代理拦截（ProxyError / 502）。"""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except Exception:
        return False
    host = host.lower().strip("[]")
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    return host.startswith("127.")


# 回环地址专用 opener：显式清空代理
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _opener_for(url):
    """本机地址绕过系统代理，其余走默认（尊重用户的代理设置）。"""
    return _LOOPBACK_OPENER if _is_loopback(url) else urllib.request.build_opener()


def _model_urls(base_url):
    """候选的模型清单地址：{base}/models，以及 base 未带 /v1 时的 {base}/v1/models。"""
    b = _norm_base(base_url)
    if not b:
        return []
    urls = [b + "/models"]
    if not b.endswith("/v1"):
        urls.append(b + "/v1/models")
    return urls


def _tokenize(model_id):
    """把模型 id 拆成小写 token，便于关键词匹配。"""
    return [t for t in re.split(r"[^a-z0-9]+", (model_id or "").lower()) if t]


def tier_of(model_id, meta=None):
    """判定模型档位。

    返回 dict:
        {"tier": "flagship|balanced|economy",
         "label": "旗舰|均衡|轻量",
         "hint": "一句话说明",
         "note": "补充标记（推理模型 / 预览版等），没有则空串"}
    """
    meta = meta or {}
    mid = (model_id or "").lower()
    toks = _tokenize(model_id)
    note = ""

    # 厂商自己标了档位就优先用（字段名各家不一，能认出来就用）
    tier = None
    for field in ("tier", "level", "grade", "model_tier", "type"):
        v = meta.get(field)
        if isinstance(v, str) and v.strip():
            vl = v.strip().lower()
            if vl in ("flagship", "premium", "pro", "tier1", "tier_1"):
                tier = TIER_FLAGSHIP
                break
            if vl in ("economy", "lite", "light", "cheap", "nano", "mini", "basic"):
                tier = TIER_ECONOMY
                break
            if vl in ("standard", "balanced", "default", "normal", "general"):
                tier = TIER_BALANCED
                break

    if tier is None:
        override = _TIER_OVERRIDE.get(mid)
        if override:
            tier = override
        elif any(t in _LIGHT_TOKENS for t in toks):
            tier = TIER_ECONOMY
        elif any(t in _FLAG_TOKENS for t in toks):
            tier = TIER_FLAGSHIP
        elif any(re.fullmatch(r"\d+(?:\.\d+)?b", t) for t in toks):
            # 本地小参数量模型（7b / 8b / 14b 之类）
            tier = TIER_ECONOMY
        elif any(mid.startswith(f) for f in _FLAGSHIP_FAMILY):
            tier = TIER_FLAGSHIP
        elif any(mid.startswith(f) for f in _ECONOMY_FAMILY):
            tier = TIER_ECONOMY
        else:
            tier = TIER_BALANCED

    if any(t.startswith("reason") or t.startswith("think") or t in _REASON_TOKENS
           for t in toks):
        note = "推理模型"
    if "preview" in mid or "latest" in mid:
        note = (note + " · 预览版").strip(" ·")
    if not note:
        ctx = meta.get("context_length") or meta.get("max_context") or meta.get("context")
        if isinstance(ctx, (int, float)) and ctx > 0:
            note = "上下文 %s" % (("%.0fK" % (ctx / 1000)) if ctx >= 1000 else str(int(ctx)))
    return {"tier": tier, "label": TIER_LABEL[tier], "hint": TIER_HINT[tier], "note": note}


def _model_ctx(rec):
    """从厂商返回的模型元数据里抠出上下文窗口（tok）。

    各家字段名不统一：context_length / max_context / context / max_input_tokens /
    max_model_len 都见过；认不出就返回 0（上层用保守默认值）。
    """
    if not isinstance(rec, dict):
        return 0
    for k in ("context_length", "max_context", "context", "max_input_tokens",
              "max_model_len", "context_window", "n_ctx"):
        v = rec.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v >= 1024:
            return int(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s.isdigit():
                n = int(s)
                if n >= 1024:
                    return n
    return 0


def _extract_price(meta):
    """尽力从厂商返回里提取计价字段，归一成「每百万 token」的值。

    厂商给的是每 token 还是每 1M token 没有统一标准：
    小于 0.01 视为每 token（×1e6），否则视为已是每 1M。识别不出就返回 None。
    """
    if not isinstance(meta, dict):
        return None, None
    cands = []
    pricing = meta.get("pricing")
    if isinstance(pricing, dict):
        cands.append(pricing)
    cands.append(meta)

    pin = pout = None
    for src in cands:
        if pin is None:
            for k in ("prompt", "input", "in", "prompt_price", "input_price",
                      "input_price_per_token", "price_in"):
                v = src.get(k)
                if isinstance(v, (int, float)):
                    pin = float(v)
                    break
        if pout is None:
            for k in ("completion", "output", "out", "completion_price",
                      "output_price", "output_price_per_token", "price_out"):
                v = src.get(k)
                if isinstance(v, (int, float)):
                    pout = float(v)
                    break
        if pin is not None or pout is not None:
            break
    if pin is None and pout is None:
        return None, None

    def per_m(v):
        if v is None:
            return None
        return v * 1000000.0 if abs(v) < 0.01 else v

    return per_m(pin), per_m(pout)


def _parse_models(payload):
    """从各种形状的响应里抠出模型清单。

    已覆盖：
        {"data": [{"id": "x"}]}            OpenAI 标准
        {"models": [{"id": "x"}]}          常见变体
        {"data": {"models": [...]}}        嵌套变体
        [{"id": "x"}]                     裸数组
        {"models": ["a", "b"]}             字符串数组
        ["a", "b"]
    """
    items = []

    def push(obj):
        if isinstance(obj, str):
            mid = obj.strip()
            if mid:
                items.append({"id": mid})
            return
        if isinstance(obj, dict):
            mid = obj.get("id") or obj.get("name") or obj.get("model") or ""
            mid = str(mid).strip()
            if mid:
                rec = {"id": mid}
                for k in ("owned_by", "tier", "level", "type", "context_length",
                          "max_context", "context", "pricing", "description",
                          "created", "object"):
                    if k in obj:
                        rec[k] = obj[k]
                items.append(rec)
            return
        if isinstance(obj, list):
            for o in obj:
                push(o)

    if isinstance(payload, dict):
        found = False
        for key in ("data", "models", "result", "results", "list", "items"):
            if key not in payload:
                continue
            found = True
            v = payload[key]
            if isinstance(v, dict):
                for k2 in ("models", "data", "list", "items"):
                    if k2 in v:
                        push(v[k2])
                        break
                else:
                    push(v)
            else:
                push(v)
            if items:
                break
        if not found:
            # 直接就是 {"gpt-4o": {...}} 这种映射
            for k, v in payload.items():
                if isinstance(v, dict):
                    v = dict(v)
                    v.setdefault("id", k)
                    push(v)
                elif isinstance(v, str):
                    push(k)
    elif isinstance(payload, list):
        push(payload)

    # 去重保序
    seen = set()
    out = []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out


def _get_json(url, api_key, timeout=None):
    """GET 一个 JSON 接口。

    返回 (status, data, err)：
        status 为 0 表示网络层失败；data 为解析后的 JSON 或 None
    """
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + (api_key or "sk-noauth"),
            "Accept": "application/json",
            "User-Agent": "qidian-agent/1.0",
        },
        method="GET",
    )
    try:
        resp = _opener_for(url).open(req, timeout=timeout or TIMEOUT)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:400]
        except Exception:
            pass
        return e.code, None, body
    except Exception as e:
        return 0, None, str(e)
    try:
        raw = resp.read().decode("utf-8", "replace")
    finally:
        try:
            resp.close()
        except Exception:
            pass
    try:
        return 200, json.loads(raw), ""
    except Exception:
        return 200, None, raw[:400]


def _post_chat(base_url, api_key, model, timeout=None):
    """发一次极小的对话请求，验证 Key + 模型名是否真的可用。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    req = urllib.request.Request(
        _norm_base(base_url) + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (api_key or "sk-noauth"),
            "User-Agent": "qidian-agent/1.0",
        },
        method="POST",
    )
    try:
        resp = _opener_for(req.full_url).open(req, timeout=timeout or 20)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        return 0, str(e)
    try:
        resp.read()
    except Exception:
        pass
    try:
        resp.close()
    except Exception:
        pass
    return 200, ""


def _explain_status(code, body):
    """把 HTTP 状态翻译成用户能行动的中文提示。"""
    b = (body or "").strip()
    low = b.lower()
    tail = ("；上游返回：" + b[:200]) if b else ""
    if code in (401, 403):
        return ("API Key 无效或没有权限（HTTP %d）：Key 复制不完整、已过期，"
                "或不属于该接口%s" % (code, tail))
    if code == 404:
        return ("接口地址不存在（HTTP 404）：请检查 Base URL 是否写对、是否需要以 /v1 结尾%s" % tail)
    if code == 429:
        return "触发限流或余额不足（HTTP 429）%s" % tail
    if code == 402:
        return "账户欠费或余额不足（HTTP 402）%s" % tail
    if code >= 500:
        return "厂商服务异常（HTTP %d）%s" % (code, tail)
    if code == 0:
        return "网络连接失败：%s（请检查地址与网络）" % (b or "未知错误")
    return "上游拒绝（HTTP %d）%s" % (code, tail)


# ---------- 对外接口 ----------

def check_endpoint(base_url, api_key):
    """验证接口可达性 + Key 可用性 + 拉取模型清单。

    返回 dict:
        ok            —— Key 可用（能拿到模型清单）
        key_ok        —— Key 本身可用（即使厂商不给模型清单）
        models        —— 模型清单 [{id, tier, label, hint, note, price_in, price_out}]
        needs_manual  —— 厂商不提供模型清单，需用户手填模型名
        message       —— 面向用户的中文说明
        base_url      —— 归一后的接口地址
    """
    b = _norm_base(base_url)
    if not b:
        return {"ok": False, "key_ok": False, "models": [], "needs_manual": False,
                "message": "请先填写接口地址（Base URL）", "base_url": ""}
    if not (api_key or "").strip():
        return {"ok": False, "key_ok": False, "models": [], "needs_manual": False,
                "message": "请先填写 API Key", "base_url": b}

    last_code, last_body = 0, ""
    saw_404 = False
    for url in _model_urls(b):
        code, data, body = _get_json(url, api_key)
        if code == 200 and data is not None:
            raw = _parse_models(data)
            if raw:
                models = []
                for it in raw:
                    info = tier_of(it["id"], it)
                    pin, pout = _extract_price(it)
                    models.append({
                        "id": it["id"],
                        "tier": info["tier"],
                        "label": info["label"],
                        "hint": info["hint"],
                        "note": info["note"],
                        "price_in": pin,
                        "price_out": pout,
                        "owned_by": it.get("owned_by", ""),
                        # 厂商声明的上下文窗口：存进档案后用于容量计算与超限预检
                        "ctx": _model_ctx(it),
                    })
                models.sort(key=lambda m: (m["tier"] != TIER_FLAGSHIP,
                                           m["tier"] != TIER_BALANCED, m["id"]))
                return {"ok": True, "key_ok": True, "models": models, "needs_manual": False,
                        "message": "Key 可用，该接口共提供 %d 个模型" % len(models),
                        "base_url": b}
            # 200 但没解析出模型：接口活着但不给清单 -> 手动模式
            saw_404 = True
            last_code, last_body = 200, body
            continue
        if code in (401, 403):
            return {"ok": False, "key_ok": False, "models": [], "needs_manual": False,
                    "message": _explain_status(code, body), "base_url": b}
        if code == 404:
            saw_404 = True
            last_code, last_body = code, body
            continue
        if code == 0:
            # 网络层失败（连不上/超时）——这是硬错误，不是「没有清单」
            return {"ok": False, "key_ok": False, "models": [], "needs_manual": False,
                    "message": _explain_status(0, body), "base_url": b}
        # 5xx 等其它状态：厂商服务异常，同样是硬错误
        return {"ok": False, "key_ok": False, "models": [], "needs_manual": False,
                "message": _explain_status(code, body), "base_url": b}

    # 走到这里：/models 不可用（404/405 或返回了非清单内容）
    return {"ok": False, "key_ok": False, "models": [], "needs_manual": True,
            "message": ("该接口不提供模型清单（%s）。可能是自建网关或本地推理软件，"
                        "请手动填写模型名，稍后用真实请求验证。"
                        % _explain_status(last_code or 404, last_body)),
            "base_url": b}


def probe_model(base_url, api_key, model):
    """手动填了模型名之后，用一次极小的对话请求验证 Key + 模型名。

    返回 {"ok": bool, "message": str}
    """
    b = _norm_base(base_url)
    m = (model or "").strip()
    if not b or not m:
        return {"ok": False, "message": "请填写接口地址和模型名"}
    code, body = _post_chat(b, api_key, m)
    if code == 200:
        return {"ok": True, "message": "验证通过：%s 可以正常调用" % m}
    return {"ok": False, "message": _explain_status(code, body)}
