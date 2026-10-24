# -*- coding: utf-8 -*-
"""加载 .qdai 配置。键与奇点 ai_service 对齐（key=value，# 注释）。"""
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
        except ValueError:
            return default

    return {
        # LLM
        "llm_mode": cfg.get("llm_mode", "cloud"),
        "base_url": cfg.get("base_url", ""),
        "api_key": cfg.get("api_key", ""),
        "model": cfg.get("model", ""),
        "llm_base_url": cfg.get("llm_base_url", "http://127.0.0.1:8080/v1"),
        "llm_model": cfg.get("llm_model", "local-model"),
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
        "project_root": os.path.dirname(path),
    }


def active_llm(cfg):
    """架构分流：按 llm_mode 选择端点与模型（云端 / 本地），调用方透明。"""
    if cfg["llm_mode"] == "local":
        return cfg["llm_base_url"], cfg["llm_model"], cfg.get("api_key", "")
    return cfg["base_url"], cfg["model"], cfg.get("api_key", "")


# 设置页允许改写的键（白名单，其余配置不暴露）
EDITABLE_KEYS = ("llm_mode", "base_url", "api_key", "model",
                 "llm_base_url", "llm_model")


def save_settings(cfg, updates):
    """把设置面板的修改写回 配置.qdai（保留注释与未涉及行），并热更新运行时 cfg。"""
    path = os.path.join(cfg["project_root"], "配置.qdai")
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
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
