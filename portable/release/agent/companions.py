# -*- coding: utf-8 -*-
"""companion 插件（人格类扩展）的发现与拉起。

通用 Agent 保持通用：它只认识两类扩展——
  · tool 插件       plugins/<id>.qdplugin（kind=tool，声明式工具）
  · companion 插件  plugins/<id>.qdplugin（kind=companion，如「阿觉」）

companion 插件自带一个常驻桌面的进程（桌宠），它是用户与该人格互动的**唯一入口**；
它完全依赖宿主 Agent 提供模型与能力（走宿主的本地 OpenAI 兼容端点），
因此宿主长时间不在时它会自行退场。

本模块只做三件事：发现、启动、停止。不含任何具体人格的逻辑。
"""
import json
import os
import subprocess
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.join(PROJECT_ROOT, "plugins")

# companion 插件在 .qdplugin 头里声明的字段
_HEAD_KEYS = ("name", "id", "version", "desc", "kind", "data_dir", "entry", "ui")


def _head(path):
    """读取 .qdplugin 头部字段（遇到未知键即停止，和既有解析器保持一致）。"""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines()[:40]:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if ":" not in s:
                    break
                k, v = s.split(":", 1)
                k = k.strip().lower()
                if k not in _HEAD_KEYS:
                    break
                out[k] = v.strip()
    except Exception:
        pass
    return out


def heartbeat_path(data_dir):
    return os.path.join(data_dir, "heartbeat.json")


def read_heartbeat(data_dir, max_age=20):
    """读桌宠心跳：返回 pid（还活着）或 None。

    桌宠是个独立进程，宿主重启后并不记得它。靠心跳文件判断「球还在不在桌面上」，
    比只记 ??Popen 更可靠（也能避免重复拉起两个阿觉）。
    """
    try:
        with open(heartbeat_path(data_dir), encoding="utf-8") as f:
            d = json.load(f)
        pid = int(d.get("pid") or 0)
        ts = float(d.get("ts") or 0)
        if pid <= 0 or ts <= 0:
            return None
        if time.time() - ts > max_age:
            return None
        if pid == os.getpid():
            return None
        # 确认进程真的还活着
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            alive = bool(h)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
            return pid if alive else None
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def kill_pid(pid):
    """尽力停掉一个 pid（跨平台，不够狠但足够用）。"""
    try:
        if os.name == "nt":
            import signal
            os.kill(pid, signal.SIGTERM)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


class CompanionManager:
    """发现并管理 companion 插件进程。"""

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self._lock = threading.Lock()
        self._procs = {}        # id -> Popen

    # ---------------------------------------------------------- 发现
    def discover(self):
        """返回已安装的 companion 插件清单。"""
        items = []
        if not os.path.isdir(PLUGINS_DIR):
            return items
        for fn in sorted(os.listdir(PLUGINS_DIR)):
            if not fn.endswith(".qdplugin"):
                continue
            pid = fn[:-len(".qdplugin")]
            h = _head(os.path.join(PLUGINS_DIR, fn))
            if (h.get("kind") or "tool").strip().lower() != "companion":
                continue
            items.append({
                "id": pid,
                "name": h.get("name") or pid,
                "desc": h.get("desc") or "",
                "version": h.get("version") or "",
                "data_dir": h.get("data_dir") or os.path.join("plugin_data", pid),
                "entry": h.get("entry") or "runner.py",
                "ui": h.get("ui") or "pet",
                "enabled": True,
                "running": self.is_running(pid),
            })
        return items

    def _paths(self, pid):
        plugin_dir = os.path.join(PLUGINS_DIR, pid)
        h = _head(os.path.join(PLUGINS_DIR, pid + ".qdplugin"))
        data_dir = os.path.join(
            PROJECT_ROOT, h.get("data_dir") or os.path.join("plugin_data", pid))
        entry = os.path.join(plugin_dir, h.get("entry") or "runner.py")
        return plugin_dir, data_dir, entry

    def endpoint(self):
        """宿主暴露给插件的本地端点（插件靠它说话）。"""
        host = self.cfg.get("http_host") or "127.0.0.1"
        try:
            port = int(self.cfg.get("http_port") or 6802)
        except (TypeError, ValueError):
            port = 6802
        return "http://%s:%d" % (host, port)

    # ---------------------------------------------------------- 启停
    def start(self, pid):
        """拉起一个 companion 插件进程（已在跑则直接返回）。"""
        with self._lock:
            proc = self._procs.get(pid)
            if proc and proc.poll() is None:
                return {"ok": True, "id": pid, "already": True}
            plugin_dir, data_dir, entry = self._paths(pid)
            if not os.path.isfile(entry):
                return {"ok": False, "id": pid, "error": "插件入口不存在：%s" % entry}
            # 心跳还活着 = 桌宠已经在桌面上了（可能是上一次宿主拉起的），别重复开一个
            alive = read_heartbeat(data_dir)
            if alive:
                return {"ok": True, "id": pid, "already": True, "pid": alive}
            try:
                os.makedirs(data_dir, exist_ok=True)
            except Exception:
                pass
            cmd = [sys.executable, entry,
                   "--data-dir", data_dir, "--endpoint", self.endpoint()]
            flags = 0
            log = None
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # 子进程 stdout 不是终端，不加这个会被缓冲，companion.log 会一直是空的
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            try:
                log = open(os.path.join(data_dir, "companion.log"), "a",
                           encoding="utf-8")
            except Exception:
                log = None
            try:
                proc = subprocess.Popen(cmd, cwd=plugin_dir, creationflags=flags,
                                        stdout=log, stderr=log,
                                        stdin=subprocess.DEVNULL, env=env)
            except Exception as e:
                if log:
                    try:
                        log.close()
                    except Exception:
                        pass
                return {"ok": False, "id": pid, "error": str(e)}
            self._procs[pid] = proc
            return {"ok": True, "id": pid, "pid": proc.pid}

    def stop(self, pid):
        """停掉一个 companion 插件进程。

        即使这个进程不是本次宿主拉起的（比如桌宠先退了、宿主重启后又想关），
        也能通过心跳文件里记的 pid 把它收回来。
        """
        with self._lock:
            plugin_dir, data_dir, entry = self._paths(pid)
            proc = self._procs.get(pid)
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self._procs.pop(pid, None)
            alive = read_heartbeat(data_dir, max_age=90)
            if alive:
                kill_pid(alive)
            # 抹掉心跳，让 UI 立刻显示「已休息」
            try:
                os.remove(heartbeat_path(data_dir))
            except Exception:
                pass
            return {"ok": True, "id": pid}

    def stop_all(self):
        for item in self.discover():
            try:
                self.stop(item["id"])
            except Exception:
                pass

    def is_running(self, pid):
        with self._lock:
            proc = self._procs.get(pid)
            if proc and proc.poll() is None:
                return True
            try:
                return read_heartbeat(self._paths(pid)[1]) is not None
            except Exception:
                return False

    def start_all(self):
        """启动所有已安装的 companion 插件（宿主启动时调用）。"""
        out = []
        for item in self.discover():
            r = self.start(item["id"])
            r["name"] = item["name"]
            out.append(r)
        return out
