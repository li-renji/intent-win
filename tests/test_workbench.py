#!/usr/bin/env python3
"""工作台后端回归测试（纯标准库实现，零第三方依赖）。

覆盖：
  - 最小 YAML 读写：解析 devices.yaml / 写回后能原样读回（round-trip）
  - 设备端口串解析、build_device 校验
  - HTTP 接口：面板页 / 设备增删改查 / toggle / 代理状态 / 错误处理

工作台之前因为依赖 uvicorn/fastapi/pyyaml 全部缺失而无法启动，
这里用临时目录 + 随机端口跑真实服务，防止回归。
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "workbench"))

from server import app as S   # noqa: E402

PASSED = 0
FAILED = []


def check(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s %s" % (name, extra))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(base, path, method="GET", data=None, timeout=8):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    r = urllib.request.Request(base + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


# --------------------------------------------------------------------------- #
print("\n[1] 最小 YAML 读写")
# --------------------------------------------------------------------------- #
sample = """# 注释行
devices:
  - id: intent-win
    name: INTENT-win (Windows Agent)
    host: 127.0.0.1
    deployed: true
    probe_ports:
      - [6800, 对话]
      - [6801, 工具总线]
    note: 本机
  - id: intent-android
    name: INTENT-安卓 (Android)
    host: 127.0.0.1
    deployed: false
    probe_ports: []
    note: '占位: 手机上运行 qidian 端后'
api_proxy:
  base_url: http://127.0.0.1:4000
  admin_key: ''
"""
cfg = S.parse_yaml(sample)
check("解析出 2 个设备", len(cfg["devices"]) == 2, str(len(cfg["devices"])))
d0 = cfg["devices"][0]
check("id 正确", d0["id"] == "intent-win", d0["id"])
check("name 正确", d0["name"] == "INTENT-win (Windows Agent)", d0["name"])
check("deployed=True", d0["deployed"] is True, str(d0["deployed"]))
check("probe_ports 解析出 2 个", d0["probe_ports"] == [[6800, "对话"], [6801, "工具总线"]],
      str(d0["probe_ports"]))
d1 = cfg["devices"][1]
check("deployed=False", d1["deployed"] is False, str(d1["deployed"]))
check("空 probe_ports", d1["probe_ports"] == [], str(d1["probe_ports"]))
check("带引号的 note 去引号", d1["note"].startswith("占位"), d1["note"])
check("api_proxy.base_url", cfg["api_proxy"]["base_url"] == "http://127.0.0.1:4000",
      cfg["api_proxy"]["base_url"])

out = S.dump_yaml(cfg)
cfg2 = S.parse_yaml(out)
check("round-trip: 设备一致", cfg["devices"] == cfg2["devices"])
check("round-trip: api_proxy 一致", cfg["api_proxy"] == cfg2["api_proxy"])
check("写回含注释头", out.startswith("#"))

# --------------------------------------------------------------------------- #
print("\n[2] 设备字段处理")
# --------------------------------------------------------------------------- #
ports = S.parse_ports("6800:对话, 6801:工具总线")
check("端口串解析", ports == [[6800, "对话"], [6801, "工具总线"]], str(ports))
check("中文分隔符", S.parse_ports("6800、6801") == [[6800, "端口"], [6801, "端口"]])
try:
    S.parse_ports("99999:x")
    check("超范围端口应报错", False)
except ValueError:
    check("超范围端口应报错", True)
try:
    S.parse_ports("abc")
    check("非法端口应报错", False)
except ValueError:
    check("非法端口应报错", True)

dev, err = S.build_device({"name": "A", "host": "1.2.3.4", "ports": "80:x"}, "a")
check("build_device 正常", err is None and dev["id"] == "a" and dev["probe_ports"] == [[80, "x"]],
      str(err))
dev, err = S.build_device({"host": "1.2.3.4"}, "a")
check("缺名称应报错", err is not None and "名称" in err, str(err))
dev, err = S.build_device({"name": "A"}, "a")
check("缺地址应报错", err is not None and "地址" in err, str(err))

# --------------------------------------------------------------------------- #
print("\n[3] HTTP 接口（真实服务，临时配置 + 随机端口）")
# --------------------------------------------------------------------------- #
tmp = tempfile.mkdtemp()
orig_config_path = S.CONFIG_PATH
orig_web_dir = S.WEB_DIR
try:
    cfg_path = os.path.join(tmp, "devices.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(sample)
    S.CONFIG_PATH = S.Path(cfg_path)

    port = free_port()
    srv = S.ThreadingHTTPServer(("127.0.0.1", port), S._Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.4)
    base = "http://127.0.0.1:%d" % port

    st, body = req(base, "/")
    check("GET / 返回面板页", st == 200 and "text/html" not in body[:20] and len(body) > 100,
          "status=%s" % st)

    st, body = req(base, "/api/devices")
    data = json.loads(body)
    check("GET /api/devices 200", st == 200, "status=%s" % st)
    check("返回 2 个设备", len(data["devices"]) == 2, str(len(data["devices"])))
    check("含 config_path", "config_path" in data, str(list(data.keys())))

    st, body = req(base, "/api/devices", "POST",
                   {"name": "新设备", "host": "127.0.0.1", "ports": "8080:面板",
                    "note": "临时", "deployed": True})
    data = json.loads(body)
    check("POST 新增 200", st == 200, "status=%s %s" % (st, body[:120]))
    new_id = data.get("device", {}).get("id", "")
    check("新增后有 id", bool(new_id), new_id)

    st, body = req(base, "/api/devices", "POST", {"id": new_id, "name": "重复", "host": "1.1.1.1"})
    check("重复 id → 400", st == 400 and "已存在" in body, "status=%s" % st)

    st, body = req(base, "/api/devices", "POST", {"name": "缺地址"})
    check("缺地址 → 400", st == 400 and "地址" in body, "status=%s" % st)

    st, body = req(base, "/api/devices/%s" % new_id, "PUT",
                   {"name": "改名", "host": "127.0.0.1", "ports": "9000:新", "deployed": False})
    check("PUT 修改 200", st == 200, "status=%s %s" % (st, body[:120]))
    check("PUT 后端口已更新",
          json.loads(body)["device"]["probe_ports"] == [[9000, "新"]],
          body[:160])

    st, body = req(base, "/api/devices/%s/toggle" % new_id, "POST")
    check("toggle 200", st == 200 and "deployed" in body, "status=%s" % st)

    st, body = req(base, "/api/devices", "GET")
    check("新增设备已落盘", any(d["id"] == new_id for d in json.loads(body)["devices"]))

    st, body = req(base, "/api/devices/%s" % new_id, "DELETE")
    check("DELETE 200", st == 200 and json.loads(body)["removed"] == new_id, body[:120])

    st, body = req(base, "/api/devices/nosuch", "DELETE")
    check("删除不存在 → 404", st == 404, "status=%s" % st)

    st, body = req(base, "/api/proxy")
    check("GET /api/proxy 200", st == 200 and "base_url" in body, "status=%s" % st)

    st, body = req(base, "/api/proxy/key", "POST")
    check("POST /api/proxy/key 200", st == 200 and "ok" in body, "status=%s" % st)

    st, body = req(base, "/api/nope")
    check("未知路径 → 404", st == 404 and "detail" in body, "status=%s" % st)

    srv.shutdown()
    srv.server_close()
finally:
    S.CONFIG_PATH = orig_config_path
    S.WEB_DIR = orig_web_dir
    shutil.rmtree(tmp, ignore_errors=True)

# --------------------------------------------------------------------------- #
print("\n" + "=" * 50)
if FAILED:
    print("失败 %d 项: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("==== test_workbench: %d/%d 通过 ====" % (PASSED, PASSED))
