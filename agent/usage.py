# -*- coding: utf-8 -*-
"""Token 用量中心 + 免费额度雷达（纯标准库，零外部依赖）。

Agent 与工作台通过本体内 JSON 解耦，不需要进程间通信：
  workbench/providers.example.json  供应商模板（随盘，不含密钥）
  workbench/providers.json          实际配置（含用户自己填的 Key，已 gitignore）
  workbench/free_tiers.json        官方免费层目录（随盘，不含密钥）
  workbench/usage.json             按天累计用量（运行时生成）
  workbench/quota_report.json      每日巡检报告（运行时生成）

用量取数三档，取到哪档就如实标哪档，绝不把本地计数冒充官方余额：
  api   —— 厂商官方用量接口（精确）
  page  —— 内置 Chromium 带登录态读控制台页面（半精确，页面改版会失效）
  local —— 本机累计每次调用 token（精确的是"已用"，不是"剩余"）
"""
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WB = os.path.join(ROOT, "workbench")
PROVIDERS = os.path.join(WB, "providers.json")
PROVIDERS_TPL = os.path.join(WB, "providers.example.json")
FREE_TIERS = os.path.join(WB, "free_tiers.json")
USAGE = os.path.join(WB, "usage.json")
REPORT = os.path.join(WB, "quota_report.json")
QDAI = os.path.join(ROOT, "配置.qdai")

_LOCK = threading.RLock()
_PROBE_CACHE = {}          # id -> (ts, result)
PROBE_TTL = 60.0


# --------------------------------------------------------------------------- #
# 基础 IO
# --------------------------------------------------------------------------- #
def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def qdai():
    """读 配置.qdai 的 key=value（忽略注释/空行）。"""
    out = {}
    try:
        with open(QDAI, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# 供应商配置
# --------------------------------------------------------------------------- #
def _defaults():
    return {
        "routing": {
            "auto": False,
            "simple_max_chars": 160,
            "complex_keywords": ["代码", "重构", "分析", "长文", "写一篇", "翻译全文", "debug"],
            "note": "自动路由只影响无工具调用的短问答；Agent 多轮/工具任务始终走主力套餐。"
        },
        "providers": [
            {
                "id": "main", "name": "当前主力套餐", "vendor": "自定义",
                "source": "qdai", "enabled": True, "tier": "paid",
                "budget": None, "mode": "local",
                "query": {}, "note": "端点/Key/模型直接读 配置.qdai，不在这里重复存密钥。"
            }
        ]
    }


def load_providers():
    with _LOCK:
        p = _read(PROVIDERS, None)
        if not isinstance(p, dict) or not p.get("providers"):
            p = _read(PROVIDERS_TPL, None) or _defaults()
            try:
                _write(PROVIDERS, p)
            except Exception:
                pass
        p.setdefault("routing", _defaults()["routing"])
        return p


def save_providers(p):
    with _LOCK:
        _write(PROVIDERS, p)
        _PROBE_CACHE.clear()
        return True


def public_providers():
    """给前端的版本：Key 只留末四位。"""
    p = load_providers()
    rows = []
    for x in p.get("providers", []):
        d = dict(x)
        if d.get("source") == "qdai":
            d["masked_key"] = _mask(qdai().get("api_key", ""))
            d["endpoint"] = qdai().get("base_url", "")
            d["model"] = qdai().get("model", "")
        else:
            d["masked_key"] = _mask(d.get("api_key", ""))
            d.pop("api_key", None)
            d["endpoint"] = d.get("base_url", "")
        rows.append(d)
    return {"providers": rows, "routing": p.get("routing", {})}


def _mask(key):
    k = str(key or "")
    return ("****" + k[-4:]) if len(k) > 4 else ("已配置" if k else "未配置")


def _credentials(provider):
    """返回 (base_url, model, api_key)。"""
    src = provider.get("source", "inline")
    if src == "qdai":
        c = qdai()
        return c.get("base_url", ""), c.get("model", ""), c.get("api_key", "")
    if src == "env":
        return (provider.get("base_url", ""), provider.get("model", ""),
                os.environ.get(provider.get("key_env", ""), ""))
    return (provider.get("base_url", ""), provider.get("model", ""),
            provider.get("api_key", ""))


# --------------------------------------------------------------------------- #
# 用量累计
# --------------------------------------------------------------------------- #
def _day():
    return time.strftime("%Y-%m-%d")


def record(usage, provider_id="main", model="", estimated=False):
    """累计一次调用的 token。usage 是上游的 {prompt_tokens, completion_tokens}。"""
    if not isinstance(usage, dict):
        return False
    p = int(usage.get("prompt_tokens") or 0)
    if "completion_tokens" in usage:
        c = int(usage.get("completion_tokens") or 0)
    elif "total_tokens" in usage:
        c = max(0, int(usage.get("total_tokens") or 0) - p)
    else:
        c = 0
    if p <= 0 and c <= 0:
        return False
    with _LOCK:
        db = _read(USAGE, {"days": {}})
        d = db.setdefault("days", {}).setdefault(_day(), {})
        pr = d.setdefault("providers", {}).setdefault(provider_id,
                                                      {"prompt": 0, "completion": 0,
                                                       "requests": 0, "estimated": 0})
        pr["prompt"] += p
        pr["completion"] += c
        pr["requests"] += 1
        if estimated:
            pr["estimated"] += 1
        d["model"] = model or d.get("model", "")
        db["updated"] = int(time.time())
        try:
            _write(USAGE, db)
        except Exception:
            return False
    return True


def estimate(text):
    """粗略 token 估算：中文约 1 字 1 token，其余 4 字符 1 token。"""
    s = str(text or "")
    zh = len(re.findall(r"[\u4e00-\u9fff]", s))
    return int(zh + (len(s) - zh) / 4.0)


def usage_summary(days=7):
    db = _read(USAGE, {"days": {}})
    all_days = db.get("days", {})
    items = []
    for key in sorted(all_days.keys())[-days:]:
        d = all_days[key]
        tot = {"prompt": 0, "completion": 0, "requests": 0}
        by = {}
        for pid, v in (d.get("providers") or {}).items():
            by[pid] = dict(v)
            for k in tot:
                tot[k] += int(v.get(k, 0))
        items.append({"date": key, "total": tot, "providers": by})
    today = all_days.get(_day(), {})
    today_total = {"prompt": 0, "completion": 0, "requests": 0}
    for v in (today.get("providers") or {}).values():
        for k in today_total:
            today_total[k] += int(v.get(k, 0))
    return {"trend": items, "today": today_total,
            "all_time": sum(x["total"]["prompt"] + x["total"]["completion"]
                            for x in items),
            "updated": db.get("updated", 0)}


# --------------------------------------------------------------------------- #
# 余额探测
# --------------------------------------------------------------------------- #
def _json_path(data, path):
    cur = data
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _page_text(url, timeout=30):
    """用本体 Chromium 的调试协议带登录态读页面文本。

    不用 --dump-dom：Windows 上 Chrome 158 是 GUI 子系统程序，headless 下
    stdout 会被吞掉（实测文件句柄也拿不到）。改走 CDP：
    启一个临时 headless 实例 → Page.navigate → Runtime.evaluate 取 innerText。
    profile 固定在盘内 data/usage-profile，登录一次即可持续复用。
    """
    exe = os.path.join(ROOT, "runtime", "chromium", "chrome.exe")
    if not os.path.isfile(exe):
        try:
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            from mybrowser.runtime_paths import chromium_exe
            exe = chromium_exe() or exe
        except Exception:
            pass
    if not os.path.isfile(exe):
        return None, "未找到内置 Chromium"
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from mybrowser.net.cdp import CDPClient
    except Exception as e:
        return None, "内置浏览器控制层不可用: %s" % type(e).__name__

    profile = os.path.join(WB, "data", "usage-profile")
    os.makedirs(profile, exist_ok=True)
    port = _free_port()
    endpoint = "http://127.0.0.1:%d" % port
    proc = subprocess.Popen(
        [exe, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--no-sandbox", "--disable-dev-shm-usage",
         "--remote-debugging-port=%d" % port, "--user-data-dir=" + profile, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        # 绕过系统代理，否则 127.0.0.1 可能走到代理返回 502
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        tabs = None
        deadline = time.time() + min(timeout, 15)
        while time.time() < deadline:
            if proc.poll() is not None:
                return None, "浏览器启动即退出（登录窗口可能还开着，请关掉后重试）"
            try:
                with opener.open(endpoint + "/json/list", timeout=1.5) as r:
                    tabs = json.loads(r.read().decode("utf-8"))
                if any(t.get("type") == "page" and t.get("webSocketDebuggerUrl") for t in tabs):
                    break
            except Exception:
                pass
            time.sleep(0.35)
        if not tabs:
            return None, "浏览器调试端口未就绪（登录窗口可能还开着）"
        target = next((t for t in tabs if t.get("type") == "page"
                       and t.get("webSocketDebuggerUrl")), None)
        if not target:
            return None, "没有可用的页面标签"
        cdp = CDPClient(target["webSocketDebuggerUrl"], timeout=12)
        cdp.send("Page.enable", timeout=8)
        cdp.send("Page.navigate", {"url": url}, timeout=8)

        href, text = "", ""
        end = time.time() + max(6, timeout - 8)
        stable = 0
        while time.time() < end:
            time.sleep(0.4)
            try:
                h = cdp.evaluate("location.href", timeout=6) or ""
                st = cdp.evaluate("document.readyState", timeout=6) or ""
                t = cdp.evaluate("document.body ? document.body.innerText : ''", timeout=6) or ""
            except Exception:
                continue
            if h and h != "about:blank" and st == "complete" and t == text and t.strip():
                stable += 1
                if stable >= 3:
                    href, text = h, t
                    break
            else:
                stable = 0
            href, text = h, t
        if not text:
            return None, "页面没有取到文本（可能需要先登录，或页面加载超时）"
        low = (href or "").lower()
        if any(k in low for k in ("login", "signin", "passport", "/auth")) \
                and not any(k in url.lower() for k in ("login", "signin", "passport", "/auth")):
            return None, "跳到了登录页：请点「登录用量页」用自己的账号登录一次"
        plain = html.unescape(re.sub(r"<[^>]+>", " ", text))
        return re.sub(r"\s+", " ", plain).strip(), None
    except Exception as e:
        return None, "读取用量页失败: %s" % type(e).__name__
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def open_login(url):
    """打开有头浏览器让用户登录一次用量页；登录态留在盘内 profile。"""
    exe = os.path.join(ROOT, "runtime", "chromium", "chrome.exe")
    if not os.path.isfile(exe):
        return False, "未找到内置 Chromium"
    profile = os.path.join(WB, "data", "usage-profile")
    os.makedirs(profile, exist_ok=True)
    try:
        subprocess.Popen([exe, "--no-first-run", "--no-default-browser-check",
                          "--user-data-dir=" + profile, url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, "已打开登录窗口，登录完成后回工作台点「刷新余额」"
    except Exception as e:
        return False, str(e)


def probe(provider, force=False):
    """返回统一结构：{remaining, used, total, unit, source,可信度, status, detail}。"""
    pid = provider.get("id", "?")
    if not force:
        hit = _PROBE_CACHE.get(pid)
        if hit and time.time() - hit[0] < PROBE_TTL:
            return hit[1]

    mode = provider.get("mode", "local")
    out = {"id": pid, "remaining": None, "used": None, "total": None,
           "unit": provider.get("unit", "token"), "source": mode,
           "trust": "local", "status": "unknown", "detail": ""}

    # 本机累计（所有模式都叠加，用来显示"已用"）
    db = _read(USAGE, {"days": {}})
    used_tokens = 0
    for d in db.get("days", {}).values():
        v = (d.get("providers") or {}).get(pid) or {}
        used_tokens += int(v.get("prompt", 0)) + int(v.get("completion", 0))
    out["used"] = used_tokens

    if mode == "local":
        budget = provider.get("budget")
        out["status"] = "ok" if provider.get("source") == "qdai" or used_tokens else "idle"
        out["detail"] = "本机累计；未在厂商侧核对"
        if budget:
            try:
                total = float(budget)
                consumed = used_tokens / 1000.0 if provider.get("unit") == "Ktoken" else used_tokens
                out["total"] = total
                out["remaining"] = max(0.0, total - consumed)
                out["status"] = "ok"
                out["detail"] = "按你填写的套餐总量减去本机已用（估算剩余）"
            except Exception:
                pass
        else:
            out["detail"] = "未填套餐总量，只显示本机已用；点编辑填总量即可算剩余"
    elif mode == "api":
        q = provider.get("query") or {}
        url = q.get("url", "")
        if not url:
            out.update(status="unconfigured", detail="未配置官方用量接口 URL")
        else:
            try:
                _, _, key = _credentials(provider)
                headers = {k: str(v).replace("${KEY}", key or "")
                           for k, v in (q.get("headers") or {}).items()}
                req = urllib.request.Request(url, headers=headers, method=q.get("method", "GET"))
                with urllib.request.urlopen(req, timeout=float(q.get("timeout", 8))) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                rem = _json_path(data, q.get("remaining_path", ""))
                tot = _json_path(data, q.get("total_path", ""))
                if rem is None:
                    out.update(status="parse_error", detail="接口返回里没找到剩余额度字段")
                else:
                    out.update(remaining=float(rem), total=float(tot) if tot is not None else None,
                               trust="official", status="ok", detail="厂商官方接口")
            except Exception as e:
                out.update(status="error", detail="查询失败: %s" % type(e).__name__)
    elif mode == "page":
        url = provider.get("page_url", "")
        regex = provider.get("remaining_regex", "")
        if not url or not regex:
            out.update(status="unconfigured", detail="未配置用量页 URL 或提取规则")
        else:
            text, err = _page_text(url)
            if err:
                out.update(status="error", detail=err)
            else:
                try:
                    m = re.search(regex, text or "")
                    if not m:
                        out.update(status="needs_login",
                                   detail="页面里没匹配到额度数字：可能未登录或页面改版，点「登录用量页」")
                    else:
                        val = float((m.group(1) if m.groups() else m.group(0)).replace(",", ""))
                        out.update(remaining=val, trust="page", status="ok",
                                   detail="内置浏览器读用量页（需保持登录态）")
                except Exception as e:
                    out.update(status="parse_error", detail="提取规则有误: %s" % e)
    else:
        out.update(status="unconfigured", detail="未知取数方式: %s" % mode)

    _PROBE_CACHE[pid] = (time.time(), out)
    return out


def all_usage(force=False):
    p = load_providers()
    rows = []
    for x in p.get("providers", []):
        if not x.get("enabled", True):
            continue
        r = probe(x, force=force)
        r["name"] = x.get("name", x.get("id"))
        r["vendor"] = x.get("vendor", "")
        r["tier"] = x.get("tier", "")
        r["budget"] = x.get("budget")
        r["note"] = x.get("note", "")
        rows.append(r)
    s = usage_summary()
    return {"providers": rows, "today": s["today"], "trend": s["trend"],
            "all_time": s["all_time"], "updated": int(time.time()),
            "routing": p.get("routing", {})}


# --------------------------------------------------------------------------- #
# 免费额度雷达（只做官方免费层，不搜刮他人 Key）
# --------------------------------------------------------------------------- #
def free_tiers():
    return _read(FREE_TIERS, {"items": [], "policy": ""})


def _check_tier(it):
    url = it.get("check_url") or it.get("signup_url") or ""
    row = dict(it)
    row["reachable"] = False
    row["checked_at"] = int(time.time())
    if not url:
        return row
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 INTENT-QuotaRadar"})
        with urllib.request.urlopen(req, timeout=5) as r:
            row["reachable"] = r.status < 400
            row["http"] = r.status
    except urllib.error.HTTPError as e:
        row["reachable"] = e.code < 500
        row["http"] = e.code
    except Exception as e:
        row["detail"] = type(e).__name__
    return row


def daily_scan():
    """巡检官方免费层入口连通性 + 生成今日可用清单。并发跑，不抓取/存储任何 Key。"""
    from concurrent.futures import ThreadPoolExecutor
    data = free_tiers()
    src = list(data.get("items", []))
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            items = list(ex.map(_check_tier, src))
    except Exception:
        items = [_check_tier(x) for x in src]
    report = {"date": _day(), "scanned_at": int(time.time()),
              "items": items,
              "available": sum(1 for x in items if x.get("reachable")),
              "note": "只检查官方入口是否可达；额度以各家官网为准，请用自己的账号领取。"}
    try:
        _write(REPORT, report)
    except Exception:
        pass
    return report


def quota_report():
    r = _read(REPORT, None)
    if not r:
        r = {"date": "", "scanned_at": 0, "items": [], "available": 0,
             "note": "尚未巡检，点「立即巡检」运行一次。"}
    r["catalog"] = free_tiers()
    r["routing"] = load_providers().get("routing", {})
    r["providers"] = public_providers()["providers"]
    return r


def set_routing(auto):
    p = load_providers()
    p.setdefault("routing", {})["auto"] = bool(auto)
    save_providers(p)
    return p["routing"]


def attach_free(tier_id, api_key, base_url="", model=""):
    """把用户自己领到的官方免费额度写入供应商表。不接收来路不明的 Key。"""
    if not api_key or len(api_key) < 8:
        return False, "Key 看起来不完整（至少 8 位）"
    data = free_tiers()
    t = next((x for x in data.get("items", []) if x.get("id") == tier_id), None)
    if not t:
        return False, "目录里没有这个免费层"
    p = load_providers()
    pid = "free-" + tier_id
    row = next((x for x in p["providers"] if x.get("id") == pid), None)
    if row is None:
        row = {"id": pid, "name": t.get("name", tier_id), "vendor": t.get("vendor", ""),
               "source": "inline", "enabled": True, "tier": "free", "mode": "local",
               "budget": None, "note": "官方免费层 · 由额度雷达接入"}
        p["providers"].append(row)
    row["api_key"] = api_key
    row["base_url"] = base_url or t.get("base_url", "")
    row["model"] = model or t.get("model", "")
    row["attached_at"] = int(time.time())
    save_providers(p)
    return True, row["name"]


# --------------------------------------------------------------------------- #
# 自动路由：只有"短、无工具"的任务才走免费额度
# --------------------------------------------------------------------------- #
def route(endpoint, model, key, messages, tools=None, strict=False):
    """返回 (endpoint, model, key, provider_id)。任何异常都原样返回，绝不影响聊天。"""
    try:
        p = load_providers()
        r = p.get("routing") or {}
        if not r.get("auto"):
            return endpoint, model, key, "main"
        if tools or strict:
            return endpoint, model, key, "main"
        text = ""
        for m in reversed(messages or []):
            if m.get("role") == "user":
                text = str(m.get("content") or "")
                break
        if not text or len(text) > int(r.get("simple_max_chars", 160)):
            return endpoint, model, key, "main"
        if any(k in text for k in r.get("complex_keywords", [])):
            return endpoint, model, key, "main"
        cands = [x for x in p.get("providers", [])
                 if x.get("enabled") and x.get("tier") == "free"]
        for x in cands:
            b, mo, k = _credentials(x)
            if b and k:
                return b, (mo or model), k, x["id"]
    except Exception:
        pass
    return endpoint, model, key, "main"
