# -*- coding: utf-8 -*-
"""加载 .qdai 配置。键与奇点 ai_service 对齐（key=value，# 注释）。"""
import json
import os
import sys


def load_config(path=None):
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "配置.qdai")
    cfg = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()

    def flag(name, default=False):
        v = cfg.get(name, str(default))
        return str(v).lower() in ("on", "true", "1", "yes")

    def intv(name, default):
        try:
            return int(cfg.get(name, default))
        except (TypeError, ValueError):
            return default

    def _floatv(name, default):
        try:
            return float(cfg.get(name, default))
        except (TypeError, ValueError):
            return default

    return {
        # LLM
        "llm_mode": cfg.get("llm_mode", "cloud"),
        "base_url": cfg.get("base_url", ""),
        "api_key": cfg.get("api_key", ""),
        "model": cfg.get("model", ""),
        "llm_base_url": cfg.get("llm_base_url", ""),
        "llm_model": cfg.get("llm_model", "local-model"),
        # 模型库档案（JSON 字符串，save_settings 写入；_load_profiles 解析）
        "profiles": cfg.get("profiles", ""),
        # 界面外观主题（dark / light），UI 个性化面板写入
        "theme": cfg.get("theme", "dark"),
        # 上下文容量（tok）显示/自救用：0=自动按型号查表，>0=手动指定
        "llm_ctx": intv("llm_ctx", 0),
        # 端口
        "host": cfg.get("host", "127.0.0.1"),
        "port_6800": intv("port_6800", 6800),
        "port_6801": intv("port_6801", 6801),
        "http_enabled": flag("http_enabled", True),
        "http_host": cfg.get("http_host", "127.0.0.1"),
        "http_port": intv("http_port", 6802),
        # token 管线
        "tools": [t.strip() for t in cfg.get("tools", "").split(",") if t.strip()],
        "tool_max_chars": intv("tool_max_chars", 4000),
        "history_facts_n": intv("history_facts_n", 10),
        "summary_max_chars": intv("summary_max_chars", 2000),
        "fs_read_max_chars": intv("fs_read_max_chars", 6000),
        "code_timeout": intv("code_timeout", 60),
        "code_max_chars": intv("code_max_chars", 6000),
        # agent 循环（工具调用）
        "agent_enabled": flag("agent_enabled", True),
        "agent_max_rounds": intv("agent_max_rounds", 6),
        "agent_max_seconds": intv("agent_max_seconds", 180),
        "agent_text_fallback": cfg.get("agent_text_fallback", "on"),
        "agent_max_tool_result": intv("agent_max_tool_result", 1500),
        "agent_confirm_tools": cfg.get("agent_confirm_tools", ""),
        "agent_tool_mode": cfg.get("agent_tool_mode", "auto"),
        "agent_req_timeout": intv("agent_req_timeout", 90),
        # 上下文经济性（对齐 pi）：占总容量比例超过该值就主动压缩历史，
        # 而不是等上游报「上下文超限」才救（0=关闭主动压缩）
        "agent_compact_ratio": _floatv(cfg.get("agent_compact_ratio", "0.8"), 0.8),
        # 主动压缩时保留最近对话的 token 预算
        "agent_keep_tokens": intv("agent_keep_tokens", 8000),
        # 电脑文件桥（手机 qidian编辑器 无线访问/写入本机文件，默认关，UI 可一键启动）
        "pc_bridge_enabled": flag("pc_bridge_enabled", False),
        "pc_bridge_port": intv("pc_bridge_port", 8899),
        "pc_bridge_root": cfg.get("pc_bridge_root", ""),
        "pc_bridge_token_file": cfg.get("pc_bridge_token_file", "pc_bridge.token"),
        # 本地推理引擎（llama.cpp llama-server 子进程托管，配套 models/ 目录）
        "local_engine_binary": cfg.get("local_engine_binary", ""),
        "local_engine_model": cfg.get("local_engine_model", ""),
        "local_engine_host": cfg.get("local_engine_host", "127.0.0.1"),
        "local_engine_port": intv("local_engine_port", 8099),
        "local_engine_ctx": intv("local_engine_ctx", 4096),
        "local_engine_threads": intv("local_engine_threads", 0),
        "local_engine_gpu_layers": intv("local_engine_gpu_layers", 0),
        "local_engine_extra_args": cfg.get("local_engine_extra_args", ""),
        "local_engine_ready_timeout": intv("local_engine_ready_timeout", 90),
        "local_engine_autostart": flag("local_engine_autostart", False),
        # 本地引擎不可用时是否降级到云端（off=直接报错）
        "local_engine_fallback_cloud": flag("local_engine_fallback_cloud", True),
        "local_models_dir": cfg.get("local_models_dir", "models"),
        "browser_enabled": flag("browser_enabled", True),
        "browser_engine": cfg.get("browser_engine", "webview2"),
        "browser_width": intv("browser_width", 1280),
        "browser_height": intv("browser_height", 860),
        "snapshot_max_chars": intv("snapshot_max_chars", 3500),
        # 工作台入口：侧栏「工作台」按钮是否显示（0/False=剥离工作台，仅用聊天 Agent）
        "workbench_enabled": flag("workbench_enabled", True),
        # 玻璃材质：glass=整体玻璃效果开关，refract=折射层开关（折射较吃性能）
        "glass_enabled": flag("glass_enabled", True),
        "glass_refract": flag("glass_refract", True),
        # 背景可见度（0~1，越大图片越清晰）与玻璃磨砂度（px，越大越磨砂）
        "bg_visibility": _floatv("bg_visibility", 0.7),
        "glass_blur": _floatv("glass_blur", 12.0),
        # 背景画面焦点（0~100 百分比）：长图时可上移焦点，只展示图片上半部分
        "bg_pos_x": _floatv("bg_pos_x", 50.0),
        "bg_pos_y": _floatv("bg_pos_y", 50.0),
        # ===== 区域独立控制：任务栏 / 聊天区 各管各的 =====
        # 之前两者共用同一套玻璃参数，导致「调玻璃效果时任务栏跟着一起变通透」，
        # 且浅色主题下侧栏被硬编码成不透明、根本不变 —— 现在彻底拆开。
        # glass：该区域是否磨砂模糊；opacity：该区域底色浓度（0=全透，1=全不透明）
        # opacity 存 -1 表示「跟随主题默认」，用户手动拖过滑块才写入具体值。
        "side_glass": flag("side_glass", True),
        "side_opacity": _floatv("side_opacity", -1.0),
        "main_glass": flag("main_glass", True),
        "main_opacity": _floatv("main_opacity", -1.0),
        # 玻璃 / 磨砂按组件独立开关（JSON 字符串，app._load_list 解析成 list）
        "glass_components": cfg.get("glass_components", ""),
        "frost_components": cfg.get("frost_components", ""),
        "project_root": os.path.dirname(path),
    }


# 已知模型的上下文容量（tok）。云端/网关模型没有 GGUF 可读，按型号查表；
# 查不到的型号可在 配置.qdai 用 llm_ctx 手动指定（0=自动）。
MODEL_CTX_LIMITS = {
    # 小米 MiMo（OpenAI 兼容，1M 上下文）
    "mimo-v2.6-pro": 1048576,
    "mimo-v2.6-flash": 1048576,
    "mimo-v2.6-pro-ultraspeed": 1048576,
    "mimo-v2-pro": 1048576,
    "mimo-v2-flash": 1048576,
    # 深度Seek 系（官方 API 常见 64K）
    "deepseek-chat": 65536,
    "deepseek-v3": 65536,
    "deepseek-v3.1": 65536,
    "deepseek-v3.2": 131072,
    "deepseek-r1": 65536,
    # 注：厂商自造型号（deepseek-v4-flash 等）各家窗口差异极大，
    # 一律以厂商 /v1/models 返回的 context_length 为准，不在此处猜。
    # OpenAI 系
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4.1": 1047576,
    "gpt-4.1-mini": 1047576,
    "gpt-4-turbo": 128000,
    "gpt-3.5-turbo": 16385,
    "o1": 200000,
    "o3": 200000,
    # Anthropic / Google / Qwen
    "claude-3-5-sonnet": 200000,
    "claude-3-7-sonnet": 200000,
    "claude-sonnet-4": 200000,
    "gemini-1.5-pro": 2097152,
    "gemini-2.0-flash": 1048576,
    "qwen-max": 32768,
    "qwen-plus": 131072,
    "qwen-turbo": 1000000,
    "glm-4-plus": 128000,
    "glm-4-air": 128000,
    # 手机端本地 MNN 小模型（网关会裁剪 prompt，保守估计）
    "qwen2.5-1.5b-mnn": 4096,
    "minicpm-2b": 4096,
}
# 查不到型号时的兜底。故意取小值：宁可显示保守、提前压缩，
# 也不要显示 40K 却在实际 32K 的模型上被上游拒掉。
DEFAULT_CTX = 32768


def _profile_ctx(cfg, model=None):
    """当前激活模型档案里记录的上下文容量（厂商在 /v1/models 里给的 context_length）。

    档案是 list[dict] 的 JSON 字符串，按 base_url + model 匹配当前激活项。
    """
    raw = cfg.get("profiles")
    profs = []
    if isinstance(raw, list):
        profs = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                profs = v
        except Exception:
            profs = []
    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(model or cfg.get("model") or "")
    for p in profs:
        if not isinstance(p, dict):
            continue
        if str(p.get("model") or "") != model:
            continue
        pb = str(p.get("base_url") or "").rstrip("/")
        if base and pb and pb != base:
            continue
        try:
            n = int(p.get("ctx") or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            return n
    return 0


def model_ctx_limit(cfg, model=None):
    """当前模型的上下文容量（tok）。

    优先级：llm_ctx 手动指定 > 模型档案里厂商给的 context_length
            > 型号查表 > 本地引擎配置 > 保守默认值。
    """
    try:
        n = int(cfg.get("llm_ctx", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n
    model = model or cfg.get("model") or ""
    if str(cfg.get("llm_mode", "cloud")).lower() == "local":
        m = cfg.get("llm_model") or model
        if m in MODEL_CTX_LIMITS:
            return MODEL_CTX_LIMITS[m]
        n = int(cfg.get("local_engine_ctx", 0) or 0)
        return n if n > 0 else DEFAULT_CTX
    n = _profile_ctx(cfg, model)
    if n > 0:
        return n
    return MODEL_CTX_LIMITS.get(model or "", DEFAULT_CTX)


def active_llm(cfg):
    """架构分流：按 llm_mode 选择端点与模型（云端 / 本地），调用方透明。"""
    if cfg["llm_mode"] == "local":
        return cfg["llm_base_url"], cfg["llm_model"], cfg.get("api_key", "")
    return cfg["base_url"], cfg["model"], cfg.get("api_key", "")


# 设置页允许改写的键（白名单，其余配置不暴露）
EDITABLE_KEYS = ("llm_mode", "base_url", "api_key", "model",
                 "llm_base_url", "llm_model", "profiles", "theme",
                 "workbench_enabled", "glass_enabled", "glass_refract",
                 "bg_visibility", "glass_blur", "bg_pos_x", "bg_pos_y",
                 "side_glass", "side_opacity", "main_glass", "main_opacity",
                 # 玻璃 / 磨砂按组件独立开关（JSON 字符串，形如 ["top","input"]）
                 "glass_components", "frost_components")


def save_settings(cfg, updates):
    """把设置面板的修改写回 配置.qdai（保留注释与未涉及行），并热更新运行时 cfg。"""
    path = os.path.join(cfg["project_root"], "配置.qdai")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        k = s.partition("=")[0].strip() if ("=" in s and not s.startswith("#")) else None
        if k and k in updates:
            out.append("%s=%s" % (k, updates[k]))
            seen.add(k)
        else:
            out.append(line)
    for k, v in updates.items():   # 原文件没有的键追加到末尾
        if k in EDITABLE_KEYS and k not in seen and str(v).strip():
            out.append("%s=%s" % (k, v))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    # 热更新运行时配置（免重启生效）
    for k, v in updates.items():
        if k in cfg:
            cfg[k] = v
    return cfg


if __name__ == "__main__":
    c = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    for k in sorted(c):
        if k != "api_key":
            print(f"{k} = {c[k]}")
