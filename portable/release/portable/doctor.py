# -*- coding: utf-8 -*-
"""换机体检：插到一台新电脑上，先回答「这台机器到底能不能跑」。

原则：只报事实，不粉饰。没有随盘携带的本地模型就是没有，
不会写成「已内置」；系统缺组件就明说缺什么。
"""
import importlib.util
import json
import os
import platform
import shutil
import socket
import sys


def _fmt(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fPB" % n


def _cfg(root):
    out = {}
    path = os.path.join(root, "配置.qdai")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _gguf(root):
    found = []
    base = os.path.join(root, "models")
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            if name.lower().endswith((".gguf", ".ggmf")):
                p = os.path.join(dirpath, name)
                try:
                    found.append((p, os.path.getsize(p)))
                except OSError:
                    pass
    return found


def collect(root, env, ports=None, network=False):
    """返回 [{'level': OK/WARN/FAIL, 'item':..., 'detail':...}, ...]。

    network=True 才做云端 TCP 探测（只在 DOCTOR.cmd 用，避免拖慢每次启动）。
    """
    items = []

    def add(level, name, detail):
        items.append({"level": level, "item": name, "detail": detail})

    # --- 操作系统：内置运行时是 Windows x64 ---
    if os.name != "nt" or platform.system() != "Windows":
        add("FAIL", "操作系统", "便携运行时只支持 Windows，当前是 %s" % platform.system())
    else:
        try:
            major = sys.getwindowsversion().major
        except Exception:
            major = 10
        bits = "64" if (platform.machine().lower() in ("amd64", "x86_64", "arm64")
                        or sys.maxsize > 2 ** 32) else "32"
        if major < 10:
            add("FAIL", "操作系统", "需要 Windows 10 及以上，当前主版本 %s" % major)
        elif bits != "64":
            add("FAIL", "操作系统", "内置 Python/WebView2/Chromium 均为 x64，当前不是 64 位环境")
        else:
            add("OK", "操作系统", "Windows %s · %s 位" % (major, bits))

    # --- U 盘可写（记忆/会话/登录态都要往里写）---
    probe = os.path.join(root, "data", ".write_test")
    try:
        os.makedirs(os.path.dirname(probe), exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        with open(probe, "r", encoding="utf-8") as f:
            assert f.read() == "ok"
        os.remove(probe)
        add("OK", "U 盘写入", "可读写（会话、记忆、浏览器登录态会保存在盘内）")
    except Exception as e:
        add("FAIL", "U 盘写入", "写不进去：%s" % e)

    # --- 自带 Python ---
    py = os.path.join(root, "runtime", "python", "python.exe")
    if os.path.isfile(py):
        add("OK", "自带 Python", "runtime\\python\\python.exe（不借电脑上的 Python）")
        for mod, label in (("webview", "pywebview"), ("clr", "pythonnet"),
                           ("PyPDF2", "PyPDF2"), ("docx", "python-docx"),
                           ("openpyxl", "openpyxl"), ("pptx", "python-pptx")):
            try:
                ok = importlib.util.find_spec(mod) is not None
            except (ImportError, ValueError):
                ok = False
            if not ok:
                add("WARN", "依赖", "%s 未随盘带上，相关功能会降级" % label)
    else:
        add("FAIL", "自带 Python", "runtime\\python\\python.exe 缺失，便携版无法启动")

    # --- 自带浏览器内核 ---
    wv = os.path.join(root, "runtime", "webview2", "msedgewebview2.exe")
    ch = os.path.join(root, "runtime", "chromium", "chrome.exe")
    if os.path.isfile(wv):
        add("OK", "窗口内核", "WebView2 固定版随盘自带（主窗口不依赖系统 Edge）")
    else:
        add("WARN", "窗口内核", "WebView2 固定版缺失，桌面窗口会回退系统 Edge 或打不开")
    if os.path.isfile(ch):
        add("OK", "内置浏览器", "Chromium 随盘自带（搜索/网页操作不借系统浏览器）")
    else:
        add("WARN", "内置浏览器", "runtime\\chromium\\chrome.exe 缺失，浏览器功能会回退系统 Edge")

    # --- 环境重定向是否真的生效（防止数据漏到宿主电脑）---
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP"):
        val = env.get(key, "")
        try:
            inside = bool(val) and os.path.commonpath(
                [os.path.abspath(val), os.path.abspath(root)]) == os.path.abspath(root)
        except ValueError:
            inside = False          # 不同盘符 = 没落在 U 盘内
        if not inside:
            add("FAIL", "数据隔离", "%s 没落在 U 盘内：%s" % (key, val))
    if not any(i["level"] == "FAIL" and i["item"] == "数据隔离" for i in items):
        add("OK", "数据隔离", "用户目录/缓存/临时目录全部指向 U 盘 data\\，不写宿主电脑")

    # --- 配置与模型 ---
    cfg_path = os.path.join(root, "配置.qdai")
    if not os.path.isfile(cfg_path):
        add("FAIL", "配置", "配置.qdai 缺失")
        cfg = {}
    else:
        cfg = _cfg(root)
        add("OK", "配置", "随盘携带，模型=%s，模式=%s" % (cfg.get("model") or "未填",
                                                        cfg.get("llm_mode") or "未填"))
    if cfg and not cfg.get("api_key"):
        add("WARN", "API Key", "配置里没有 Key；云端模型不可用（可用 SAFE-START.cmd 临时输入）")
    elif cfg:
        add("OK", "API Key", "已配置（内容不打印；U 盘丢失会有泄露风险，见使用说明）")

    # --- 本地模型：如实报告，不把「空目录」说成「已内置」---
    models = _gguf(root)
    engine = os.path.join(root, "runtime", "llama", "llama-server.exe")
    configured = cfg.get("local_engine_binary", "")
    if models:
        add("OK", "本地模型", "%d 个 GGUF，共 %s（随盘走，换机不丢）"
            % (len(models), _fmt(sum(s for _p, s in models))))
        if not (os.path.isfile(engine) or (configured and os.path.isfile(
                configured if os.path.isabs(configured) else os.path.join(root, configured)))):
            add("WARN", "本地推理", "有模型但缺推理引擎 llama-server.exe，离线模式起不来")
    else:
        add("WARN", "本地模型", "models\\ 里没有 GGUF —— 目前没有随盘的本地模型；"
                              "把 .gguf 放进去即可，云端模型不受影响")
    if os.path.isfile(engine):
        add("OK", "推理引擎", "runtime\\llama\\llama-server.exe 随盘自带")

    # --- 记忆 / 会话 / 知识库 ---
    sess_dir = os.path.join(root, "会话")
    try:
        n = len([f for f in os.listdir(sess_dir) if f.endswith(".json")])
    except OSError:
        n = 0
    mem = os.path.join(root, "memories", "memory.json")
    add("OK", "记忆与上下文", "%d 个会话文件，长期记忆文件%s（都在盘内）"
        % (n, "已存在" if os.path.isfile(mem) else "尚未生成"))
    kb_dir = os.path.join(root, "kb")
    try:
        kb_files = [f for f in os.listdir(kb_dir) if f.endswith(".json")]
    except OSError:
        kb_files = []
    add("OK" if kb_files else "WARN", "知识库",
        ("%d 个索引文件随盘携带；换电脑后旧索引里的宿主路径会失效，重新扫描一次即可" % len(kb_files))
        if kb_files else "暂无索引；便携模式只索引盘内 docs\\ 与 data\\资料\\，不扫宿主磁盘")

    # --- 端口 ---
    if ports:
        busy = [str(p) for p in ports.values() if _busy(p)]
        if busy:
            add("WARN", "端口", "以下端口被占用：%s" % ",".join(busy))
        else:
            add("OK", "端口", "对话 %s / 工具 %s / API %s 均空闲（不与宿主 INTENT 冲突）"
                % (ports.get("port_6800"), ports.get("port_6801"), ports.get("http_port")))

    # --- 云端连通性：决定这台电脑能不能用云端模型（不消耗 token）---
    if network:
        from urllib.parse import urlparse
        host = urlparse(cfg.get("base_url", "")).hostname
        if not host:
            add("WARN", "云端连通", "没配 base_url，只能用本地模型")
        else:
            try:
                s = socket.create_connection((host, 443), timeout=3)
                s.close()
                add("OK", "云端连通", "%s:443 可达，云端模型可用" % host)
            except Exception:
                add("WARN", "云端连通", "%s 连不上；这台电脑只能离线跑本地模型" % host)

    # --- 空间 ---
    try:
        free = shutil.disk_usage(root).free
        if free < 200 * 1024 ** 2:
            add("FAIL", "剩余空间", "只剩 %s，无法写会话和缓存" % _fmt(free))
        else:
            add("OK", "剩余空间", "%s 可用" % _fmt(free))
    except Exception:
        pass
    return items


def _busy(port):
    import socket
    s = socket.socket()
    s.settimeout(0.12)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        s.close()


def print_report(items):
    print("=" * 58)
    print(" INTENT 便携版 · 换机体检")
    print("=" * 58)
    for r in items:
        tag = {"OK": "[可以]", "WARN": "[注意]", "FAIL": "[不行]"}[r["level"]]
        print(" %s %-10s %s" % (tag, r["item"], r["detail"]))
    fails = [r for r in items if r["level"] == "FAIL"]
    warns = [r for r in items if r["level"] == "WARN"]
    print("-" * 58)
    if fails:
        print(" 结论：这台电脑不能完整运行，缺 %d 项：" % len(fails))
        for r in fails:
            print("   - %s：%s" % (r["item"], r["detail"]))
    elif warns:
        print(" 结论：可以运行，%d 项需留意（见上方 [注意]）。" % len(warns))
    else:
        print(" 结论：这台电脑可以完整运行。")
    print("=" * 58)
    return 1 if fails else 0


def verdict(items):
    return {
        "ok": len([r for r in items if r["level"] == "OK"]),
        "warn": len([r for r in items if r["level"] == "WARN"]),
        "fail": len([r for r in items if r["level"] == "FAIL"]),
        "runnable": not any(r["level"] == "FAIL" for r in items),
        "items": items,
    }


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from boot import prepare, ROOT
    _env, _ports = prepare()
    _items = collect(ROOT, _env, _ports, network=True)
    if "--json" in sys.argv:
        print(json.dumps(verdict(_items), ensure_ascii=False, indent=1))
    else:
        print_report(_items)
