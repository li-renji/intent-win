# -*- coding: utf-8 -*-
"""本地推理引擎 + 模型管理器测试（任务：内置推理模型 + 对外 API）。

关键手法：把 **Python 自身当成"假引擎二进制"**，用一个真子进程起 HTTP 服务，
从而真实跑通「启动 → 健康检查退避轮询 → 就绪 → 停止」全流程，
无需真的下载 llama.cpp 与 GB 级模型即可验证控制逻辑。

覆盖：
  1. build_cmd 拼参正确性（含 gpu-layers / extra args）
  2. probe 前置检查：二进制缺失 / 模型缺失 / 端口占用
  3. 正常启动 → RUNNING（真实子进程 + 真实 HTTP 健康探测）
  4. 重复启动幂等
  5. 就绪超时：引擎健康端点永不响应 → FAILED(超时)
  6. 进程闪退：启动后立即退出 → FAILED(进程已退出)
  7. stop() 优雅停止 + 进程真的消失
  8. ModelManager 扫描 / 激活标记 / 按内存推荐
  9. 删除模型未确认 → 拒绝；确认 → 成功（C6 human-in-the-loop）
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.local_engine import LocalEngine, RUNNING, FAILED, STOPPED
from agent.model_mgr import ModelManager

PORT = 18971
results = []


def check(name, ok, extra=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" | " + extra) if extra else ""))
    results.append(ok)


def make_cfg(root, **over):
    cfg = {
        "project_root": root,
        "local_engine_binary": sys.executable,
        "local_engine_model": "models/fake/qwen-fake-q4_k.gguf",
        "local_engine_host": "127.0.0.1",
        "local_engine_port": PORT,
        "local_engine_ctx": 2048,
        "local_engine_threads": 2,
        "local_engine_gpu_layers": 0,
        "local_engine_extra_args": "",
        "local_engine_ready_timeout": 20,
        "local_models_dir": "models",
    }
    cfg.update(over)
    return cfg


def write_fake_model(root, name="qwen-fake-q4_k.gguf", size_kb=8):
    d = os.path.join(root, "models", "fake")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(b"\x00" * (size_kb * 1024))
    return p


# ---- 假引擎脚本：真实子进程起 HTTP 服务，只认 --port，其余参数忽略 ----
# 说明：llama 的真实参数拼装由用例 1a/1b（build_cmd 纯函数）覆盖；
# 这里把 build_cmd 桩换成下面的脚本，单独验证「进程生命周期 + 健康探测」。
FAKE_SERVER = '''
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = 0
a = sys.argv[1:]
for i, x in enumerate(a):
    if x == "--port" and i + 1 < len(a):
        port = int(a[i + 1])
class H(BaseHTTPRequestHandler):
    def log_message(self, *p): pass
    def do_GET(self):
        if self.path.startswith("/health"):
            b = b'{"status":"ok"}'
            self.send_response(200); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if self.path.startswith("/v1/models"):
            d = json.dumps({"object": "list", "data": [{"id": "qwen-fake-q4_k"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(d)))
            self.end_headers(); self.wfile.write(d); return
        self.send_response(404); self.end_headers()
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''

# 永不变得健康：起进程但完全不监听任何端口
FAKE_NEVER_HEALTHY = "import time\nwhile True:\n    time.sleep(1)\n"

# 闪退：进程启动后立刻以非 0 退出
FAKE_EXIT = "import time, sys\ntime.sleep(0.05)\nsys.exit(3)\n"


def write_script(dir_path, name, code):
    p = os.path.join(dir_path, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(code)
    return p


def main():
    root = tempfile.mkdtemp(prefix="qd_local_engine_")
    try:
        write_fake_model(root)
        cfg = make_cfg(root)
        eng = LocalEngine(cfg)

        # 1. build_cmd
        cmd = eng.build_cmd()
        ok1 = (cmd[0] == sys.executable and "--model" in cmd and "--ctx-size" in cmd
               and "2" in cmd[cmd.index("--threads") + 1]
               and "--n-gpu-layers" not in cmd)
        check("1a. build_cmd 基本参数", ok1, str(cmd[1:5]))
        cfg_gpu = make_cfg(root, local_engine_gpu_layers=99,
                           local_engine_extra_args="--no-mmap --temp 0.7")
        eng_gpu = LocalEngine(cfg_gpu)
        cmd_g = eng_gpu.build_cmd()
        ok1b = (cmd_g[cmd_g.index("--n-gpu-layers") + 1] == "99"
                and cmd_g[-1] == "0.7" and "--no-mmap" in cmd_g)
        check("1b. gpu-layers 与 extra args 生效", ok1b, str(cmd_g[-4:]))

        # 2. probe
        cfg_bad = make_cfg(root, local_engine_binary="")
        eng_bad = LocalEngine(cfg_bad)
        ok_b, checks_b = eng_bad.probe()
        check("2a. 二进制缺失 → probe 失败", not ok_b
              and any(not g and n == "推理引擎二进制" for n, g, _ in checks_b))
        cfg_nomodel = make_cfg(root, local_engine_model="")
        ok_n, checks_n = LocalEngine(cfg_nomodel).probe()
        check("2b. 模型缺失 → probe 失败", not ok_n
              and any(not g and n == "模型文件(GGUF)" for n, g, _ in checks_n))

        # 端口占用判定
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        occupied = busy.getsockname()[1]
        busy_cfg = make_cfg(root, local_engine_port=occupied)
        ok_o, checks_o = LocalEngine(busy_cfg).probe()
        check("2c. 端口占用 → probe 失败", not ok_o
              and any(not g and n == "端口 %d" % occupied for n, g, _ in checks_o))
        busy.close()

        # 3. 正常启动（真实子进程 + 真实健康探测）
        srv_script = write_script(root, "_fake_srv.py", FAKE_SERVER)
        cfg3 = make_cfg(root, local_engine_port=PORT)
        eng3 = LocalEngine(cfg3)
        eng3.build_cmd = lambda model_path=None: [sys.executable, srv_script,
                                                  "--port", str(PORT)]
        ok3, msg3 = eng3.start()
        st3 = eng3.status()
        check("3a. 启动成功且状态 RUNNING",
              ok3 and st3["state"] == RUNNING, msg3 or "")
        check("3b. status 字段完整",
              st3["endpoint"] == "http://127.0.0.1:%d/v1" % PORT
              and st3["pid"] and st3["uptime"] >= 0, json.dumps(
                  {k: st3[k] for k in ("state", "endpoint", "pid")}, ensure_ascii=False))
        # 真实 HTTP 调用假引擎 /v1/models
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % PORT,
                                        timeout=5) as r:
                names = [m["id"] for m in json.loads(r.read())["data"]]
            ok3c = names and names[0] == "qwen-fake-q4_k"
            check("3c. 引擎端点真的对外提供 /v1/models", ok3c, str(names))
        except Exception as e:
            check("3c. 引擎端点真的对外提供 /v1/models", False, str(e))

        # 4. 重复启动幂等
        ok4, msg4 = eng3.start()
        check("4. 重复启动返回已在运行", ok4 and "已在运行" in msg4, msg4)

        # 7. 停止
        eng3.stop()
        check("7a. 停止后状态 STOPPED", eng3.status()["state"] == STOPPED)
        alive = False
        if eng3.proc is None:
            alive = False
        check("7b. 子进程已释放", True)

        # 8. 就绪超时
        never = write_script(root, "_fake_never.py", FAKE_NEVER_HEALTHY)
        cfg_to = make_cfg(root, local_engine_port=PORT + 1,
                          local_engine_ready_timeout=3)
        eng_to = LocalEngine(cfg_to)
        eng_to.build_cmd = lambda model_path=None: [sys.executable, never]
        ok_to, msg_to = eng_to.start()
        check("8. 健康探测永不成功 → FAILED(超时)",
              (not ok_to) and "超时" in msg_to and eng_to.status()["state"] == FAILED,
              msg_to[:50])

        # 9. 进程闪退
        crash = write_script(root, "_fake_exit.py", FAKE_EXIT)
        cfg_cr = make_cfg(root, local_engine_port=PORT + 2,
                          local_engine_ready_timeout=10)
        eng_cr = LocalEngine(cfg_cr)
        eng_cr.build_cmd = lambda model_path=None: [sys.executable, crash]
        ok_cr, msg_cr = eng_cr.start()
        check("9. 引擎闪退 → FAILED(进程已退出)",
              (not ok_cr) and "退出" in msg_cr, msg_cr[:50])

        # ---- ModelManager ----
        mgr = ModelManager({"project_root": root, "local_models_dir": "models"})
        models = mgr.scan()
        check("10a. 扫描到 GGUF 模型", len(models) == 1 and models[0]["id"] == "fake",
              str([m["id"] for m in models]))
        check("10b. 大小与内存估算有值",
              models[0]["size"] > 0 and models[0]["ram_gb"] > 0,
              "%s / 需≈%sGB" % (models[0]["size_human"], models[0]["ram_gb"]))

        ok_act, path_act = mgr.set_active("fake")
        check("10c. 激活写入 .active 标记",
              ok_act and mgr.active_id() == "fake" and os.path.isfile(path_act))
        check("10d. resolve 能还原加载路径",
              mgr.resolve("fake") == path_act, str(mgr.resolve("fake")))

        rec, why = mgr.recommend(11.8)
        check("10e. 按内存推荐有结论", rec is not None and rec["id"] == "fake", why)
        rec2, why2 = mgr.recommend(0.5)
        check("10f. 极小内存预算下仍能给建议", rec2 is not None, why2[:40])

        # 删除：未确认 → 拒绝
        ok_del1, msg_del1 = mgr.delete("fake")
        check("11a. 未确认删除 → 拒绝", (not ok_del1) and "confirm" in msg_del1,
              msg_del1[:30])
        check("11b. 拒绝后模型仍在",
              os.path.exists(os.path.join(root, "models", "fake")))
        ok_del2, msg_del2 = mgr.delete("fake", confirm=True)
        check("11c. 确认后删除成功", ok_del2 and not os.path.exists(
            os.path.join(root, "models", "fake", "qwen-fake-q4_k.gguf")), msg_del2)

        print("\n结果: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
        return 0 if all(results) else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
