# -*- coding: utf-8 -*-
"""本地推理引擎管理器（Windows 侧对等于手机端的 llama.cpp JNI）。

设计取舍：不引入 llama-cpp-python 这类需要本机编译的重依赖，改为
**托管 llama.cpp 官方 llama-server 子进程**，通过它自带的 OpenAI 兼容
HTTP 接口通信。好处：

1. 零第三方 Python 依赖（延续本项目纯标准库风格）
2. 模型只需 GGUF 文件，换模型不用改代码
3. llama-server 自带 OpenAI 兼容端点 → 复用现有 `llm.py`，Agent 循环无需改动
4. 进程隔离：引擎崩了不会拖垮 Agent 主进程，可重启

进程契约（C4/C5）
----------------
- start() 有**启动就绪等待上限**（默认 90s），就绪探测走指数退避 + 抖动
- 就绪判定只认 HTTP 健康探测成功，不看sleep 固定值
- stop() 先优雅 SIGTERM → 超时后 kill，绝不无限等待
- 所有失败判定为**可枚举错误码**，不做静默失败

状态机
------
    STOPPED --start--> STARTING --ready--> RUNNING
       ^                  |
       |                  +--timeout/crash--> FAILED(带原因)
       +------------------------ stop ----------------+
"""
import errno
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# 引擎生命周期状态
STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
FAILED = "failed"


def _now():
    return time.strftime("%H:%M:%S")


def _log(msg):
    sys.stdout.write("[local_engine %s] %s\n" % (_now(), msg))
    sys.stdout.flush()


def resolve_llm(cfg, engine):
    """决定本次请求实际用哪个端点（"`本地优先 + 可降级` 策略）。

    local 模式下的优先级：
      1. 本地引擎 RUNNING → 用引擎端点（真正的内置模型在推理引擎里跑）
      2. 引擎未运行但配置了外部本地端点（如手工起的 llama.cpp）→ 用它
      3. 都不行且 local_engine_fallback_cloud=on → 降级云端，并在 source 标注

    返回 (base, model, key, source)，source ∈ local-engine / llm-config / cloud-fallback
    """
    from .config import active_llm
    base, model, key = active_llm(cfg)
    if cfg.get("llm_mode") != "local":
        return base, model, key, "cloud"
    if engine is not None and engine.status()["state"] == RUNNING:
        return engine.endpoint(), cfg.get("local_engine_model", "") or model, key, "local-engine"
    if base:
        return base, model, key, "llm-config"
    if cfg.get("local_engine_fallback_cloud", True) and cfg.get("base_url"):
        return cfg["base_url"], cfg.get("model", ""), key, "cloud-fallback"
    return "", "", key, "unavailable"


class LocalEngine:
    """llama-server 子进程的生命周期管理。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.get("project_root", ".")
        self.binary = self._abs(cfg.get("local_engine_binary", "")) or ""
        self.host = cfg.get("local_engine_host", "127.0.0.1")
        self.port = int(cfg.get("local_engine_port", 8099))
        self.ctx_size = int(cfg.get("local_engine_ctx", 4096))
        self.threads = int(cfg.get("local_engine_threads", 0)) or (os.cpu_count() or 4)
        self.gpu_layers = int(cfg.get("local_engine_gpu_layers", 0))
        self.extra_args = [a for a in str(cfg.get("local_engine_extra_args", "")).split()
                           if a]
        self.ready_timeout = int(cfg.get("local_engine_ready_timeout", 90))
        self.model_path = cfg.get("local_engine_model", "")
        self.proc = None
        self.state = STOPPED
        self.last_error = ""
        self.started_at = 0.0
        self._lock = threading.Lock()

    def _abs(self, p):
        if not p:
            return ""
        return p if os.path.isabs(p) else os.path.join(self.root, p)

    # ---------- 端点与命令 ----------

    def endpoint(self):
        """OpenAI 兼容基址（不含 /chat/completions）。"""
        return "http://%s:%d/v1" % (self.host, self.port)

    def build_cmd(self, model_path=None):
        """拼装 llama-server 命令行。纯函数，便于单测。"""
        model = self._abs(model_path or self.model_path)
        if not model:
            return None
        cmd = [self.binary, "--model", model,
               "--host", self.host, "--port", str(self.port),
               "--ctx-size", str(self.ctx_size),
               "--threads", str(self.threads)]
        if self.gpu_layers:
            cmd += ["--n-gpu-layers", str(self.gpu_layers)]
        cmd += self.extra_args
        return cmd

    # ---------- 前置检查 ----------

    def probe(self):
        """检查二进制与模型是否就位。返回 (ok, [(项, 是否OK, 说明)])."""
        checks = []
        bin_ok = bool(self.binary) and os.path.isfile(self.binary)
        checks.append(("推理引擎二进制", bin_ok,
                       self.binary or "未配置 local_engine_binary"))
        model = self._abs(self.model_path)
        model_ok = bool(model) and os.path.isfile(model)
        size = ""
        if model_ok:
            size = "%.2f GB" % (os.path.getsize(model) / 1024 ** 3)
        checks.append(("模型文件(GGUF)", model_ok, (model or "未配置 local_engine_model")
                       + (" " + size if size else "")))
        free = self._port_free()
        checks.append(("端口 %d" % self.port, free,
                       "空闲" if free else "已被占用（换端口或先停掉占用程序）"))
        return all(ok for _, ok, _ in checks), checks

    def _port_free(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.6)
        try:
            return s.connect_ex((self.host, self.port)) != 0
        except OSError:
            return False
        finally:
            s.close()

    # ---------- 生命周期 ----------

    def start(self, model_path=None, wait_ready=True):
        """启动引擎。返回 (ok: bool, msg: str)。"""
        with self._lock:
            if self.state == RUNNING:
                return True, "已在运行"
            ok, checks = self.probe()
            if not ok:
                failed = [n for n, good, _ in checks if not good]
                self.state = FAILED
                self.last_error = "前置检查未通过: " + ", ".join(failed)
                return False, self.last_error
            cmd = self.build_cmd(model_path)
            if not cmd:
                self.state = FAILED
                self.last_error = "缺少模型路径"
                return False, self.last_error
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, creationflags=0)
            except FileNotFoundError:
                self.state = FAILED
                self.last_error = "引擎二进制不存在: %s" % self.binary
                return False, self.last_error
            except OSError as e:
                self.state = FAILED
                self.last_error = "启动失败: %s" % e
                return False, self.last_error

            self.state = STARTING
            self.started_at = time.time()
            _log("pid=%d 已启动，等待就绪…" % self.proc.pid)

        if not wait_ready:
            return True, "已启动（未等待就绪）"
        healthy, why = self._wait_ready()
        with self._lock:
            if healthy:
                self.state = RUNNING
                _log("就绪：%s" % self.endpoint())
                return True, "引擎就绪"
            self.state = FAILED
            self.last_error = why
            return False, why

    def _wait_ready(self):
        """轮询健康端点。指数退避 + 抖动，总时长受 ready_timeout 封顶（C4）。"""
        deadline = time.time() + self.ready_timeout
        attempt = 0
        delay = 0.3
        last = ""
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                return False, "引擎进程已退出（code=%s），请检查模型路径或依赖库" % self.proc.returncode
            ok, detail = self._health()
            if ok:
                return True, ""
            last = detail
            attempt += 1
            sleep = min(delay, max(0.0, deadline - time.time()))
            time.sleep(sleep * (0.75 + 0.5 * random.random()))  # 抖动，避免惊群
            delay = min(delay * 1.6, 2.0)
        return False, ("等待就绪超时(%ds，探测 %d 次)：%s"
                       % (self.ready_timeout, attempt, last))

    def _health(self):
        """探测空闲 + /v1/models 可用。返回 (ok, detail)。"""
        last_e = "未连通"
        for path in ("/health", "/v1/models"):
            url = "http://%s:%d%s" % (self.host, self.port, path)
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=3) as r:
                    body = r.read(2048) if path == "/v1/models" else b""
                    if path == "/v1/models":
                        try:
                            data = json.loads(body.decode("utf-8"))
                            names = [m.get("id") for m in data.get("data", [])]
                            return True, "模型: %s" % (", ".join(n for n in names if n) or "?")
                        except Exception:
                            return True, "OK"
                    return True, "OK"
            except urllib.error.HTTPError as e:
                # llama-server 的 /health 可能返回 503 表示仍在加载
                if e.code in (503, 404):
                    last_e = "HTTP %s" % e.code
                    continue
                last_e = "HTTP %s" % e.code
            except OSError as e:
                last_e = getattr(e, "strerror", None) or str(e) or "连接失败"
        return False, last_e

    def stop(self, timeout=8):
        """停止引擎：先优雅关闭，超时再强杀（不无限等待）。"""
        with self._lock:
            if not self.proc:
                self.state = STOPPED
                return True
            proc, self.proc = self.proc, None
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        except Exception:
            pass
        self.state = STOPPED
        self.started_at = 0.0
        _log("已停止")
        return True

    def status(self):
        alive = bool(self.proc) and self.proc.poll() is None
        return {
            "state": RUNNING if alive and self.state == RUNNING else self.state,
            "endpoint": self.endpoint(),
            "pid": self.proc.pid if alive else None,
            "uptime": int(time.time() - self.started_at) if (alive and self.started_at) else 0,
            "binary": self.binary,
            "model": self._abs(self.model_path) or "",
            "threads": self.threads,
            "gpu_layers": self.gpu_layers,
            "ctx_size": self.ctx_size,
            "last_error": self.last_error,
        }
