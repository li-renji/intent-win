# -*- coding: utf-8 -*-
"""Api 层的云端接入接口测试（api_probe / api_probe_model / api_add_profile）。

用假的 Api 实例（不启窗口、不起服务）打后端方法，
再用一个本地假厂商服务端点验证整条链路。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.app import Api  # noqa: E402


class _Vendor(BaseHTTPRequestHandler):
    """假厂商：/v1/models 给清单，/v1/chat/completions 校验 Key+模型。"""
    good_key = "sk-vendor-ok"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") != "/v1/models":
            self._send(404, {"error": "no such route"})
            return
        if self.headers.get("Authorization", "") != "Bearer " + self.good_key:
            self._send(401, {"error": {"message": "bad key"}})
            return
        self._send(200, {"object": "list", "data": [
            {"id": "vendor-flash-lite", "owned_by": "vendor"},
            {"id": "vendor-pro-max", "owned_by": "vendor"},
            {"id": "vendor-standard", "owned_by": "vendor"},
        ]})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": "no such route"})
            return
        if self.headers.get("Authorization", "") != "Bearer " + self.good_key:
            self._send(401, {"error": {"message": "bad key"}})
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        if payload.get("model") != "vendor-standard":
            self._send(404, {"error": {"message": "model not found"}})
            return
        self._send(200, {"choices": [{"message": {"content": "ok"}}]})


class _FakeCfg(dict):
    """最小配置：只需要 api_add_profile 用到的字段。"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ApiProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Vendor)
        cls.port = cls.srv.server_address[1]
        cls.base = "http://127.0.0.1:%d/v1" % cls.port
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        # 每次都用独立的临时项目根，避免污染真实 配置.qdai
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="qdapi_")
        self.cfg = {"project_root": self.tmp, "llm_mode": "cloud",
                    "base_url": "", "model": "", "api_key": "",
                    "llm_base_url": "", "llm_model": "", "profiles": ""}
        # 只借用 Api 的这几个方法，不跑 __init__（避免拉起浏览器/引擎）
        self.api = Api.__new__(Api)
        self.api.cfg = self.cfg

    def test_api_probe_returns_models(self):
        r = self.api.api_probe(self.base, "sk-vendor-ok")
        self.assertTrue(r["ok"], r["message"])
        self.assertEqual(len(r["models"]), 3)
        self.assertFalse(r["needs_manual"])
        tiers = {m["id"]: m["tier"] for m in r["models"]}
        self.assertEqual(tiers["vendor-pro-max"], "flagship")
        self.assertEqual(tiers["vendor-flash-lite"], "economy")
        self.assertEqual(tiers["vendor-standard"], "balanced")
        # 旗舰排最前
        self.assertEqual(r["models"][0]["tier"], "flagship")

    def test_api_probe_bad_key(self):
        r = self.api.api_probe(self.base, "sk-wrong")
        self.assertFalse(r["ok"])
        self.assertFalse(r["needs_manual"])
        self.assertIn("API Key", r["message"])

    def test_api_probe_manual_fallback(self):
        # 地址指向一个没有 /models 路由的路径 -> 需要手填模型名
        r = self.api.api_probe("http://127.0.0.1:%d/nope/v1" % self.port, "sk-vendor-ok")
        self.assertTrue(r["needs_manual"])
        self.assertFalse(r["ok"])

    def test_api_probe_model_ok(self):
        r = self.api.api_probe_model(self.base, "sk-vendor-ok", "vendor-standard")
        self.assertTrue(r["ok"], r["message"])

    def test_api_probe_model_wrong(self):
        r = self.api.api_probe_model(self.base, "sk-vendor-ok", "nope-model")
        self.assertFalse(r["ok"])

    def test_api_add_profile_creates_and_enables(self):
        r = self.api.api_add_profile(self.base, "sk-vendor-ok", "vendor-pro-max",
                                     "厂商旗舰", "flagship")
        self.assertTrue(r["ok"], r.get("error"))
        prof = r["profile"]
        self.assertEqual(prof["model"], "vendor-pro-max")
        self.assertEqual(prof["name"], "厂商旗舰")
        self.assertEqual(prof["tier"], "flagship")
        self.assertEqual(prof["llm_mode"], "cloud")
        # 已落盘
        path = os.path.join(self.tmp, "配置.qdai")
        self.assertTrue(os.path.exists(path))
        content = open(path, encoding="utf-8").read()
        self.assertIn("vendor-pro-max", content)
        # 顶层配置已切到这个模型
        self.assertEqual(self.cfg["model"], "vendor-pro-max")
        self.assertEqual(self.cfg["base_url"], self.base)
        # profiles 里有这一条
        profs = json.loads(self.cfg["profiles"])
        self.assertEqual(len(profs), 1)

    def test_api_add_profile_auto_name(self):
        r = self.api.api_add_profile(self.base, "sk-vendor-ok", "vendor-standard")
        self.assertTrue(r["ok"])
        self.assertIn("vendor-standard", r["profile"]["name"])
        self.assertIn("127.0.0.1", r["profile"]["name"])

    def test_api_add_profile_infers_tier(self):
        # 不传 tier 时应按名字推断
        r = self.api.api_add_profile(self.base, "sk-vendor-ok", "vendor-flash-lite")
        self.assertTrue(r["ok"])
        self.assertEqual(r["profile"]["tier"], "economy")

    def test_api_add_profile_dedup(self):
        self.api.api_add_profile(self.base, "sk-vendor-ok", "vendor-standard", "A")
        r2 = self.api.api_add_profile(self.base, "sk-vendor-ok2", "vendor-standard", "B")
        self.assertTrue(r2["ok"])
        profs = json.loads(self.cfg["profiles"])
        self.assertEqual(len(profs), 1, "同址同模型只应有一条")
        self.assertEqual(profs[0]["api_key"], "sk-vendor-ok2", "重复添加应更新 Key")

    def test_api_add_profile_rejects_empty(self):
        r = self.api.api_add_profile("", "k", "")
        self.assertFalse(r["ok"])
        self.assertIn("不能为空", r["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
