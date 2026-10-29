# -*- coding: utf-8 -*-
"""云端 API 接入的端到端测试（不需要真实联网）。

用一个本地假服务器冒充厂商：
  - /v1/models 返回不同形状的清单
  - /v1/chat/completions 校验 Key 与模型名
覆盖：清单解析、档位判定、Key 校验、无清单降级、手动填名验证。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import provider  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    shape = "openai"
    good_key = "sk-good"

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
        if self.path.rstrip("/") not in ("/v1/models", "/models"):
            self._send(404, {"error": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != self.good_key:
            self._send(401, {"error": {"message": "invalid api key"}})
            return
        s = self.shape
        if s == "openai":
            self._send(200, {"object": "list", "data": [
                {"id": "gpt-4o-mini", "object": "model", "owned_by": "acme"},
                {"id": "acme-pro-max", "object": "model", "owned_by": "acme"},
                {"id": "acme-standard", "object": "model", "owned_by": "acme"},
                {"id": "acme-reasoner-r1", "object": "model", "owned_by": "acme"},
            ]})
        elif s == "variants":
            self._send(200, {"models": [
                {"name": "acme-lite-nano"},
                {"name": "acme-ultra"},
                "acme-plain-string",
            ]})
        elif s == "nested":
            self._send(200, {"data": {"models": ["acme-flash", "acme-pro"]}})
        elif s == "mapping":
            self._send(200, {"acme-map-model": {"owned_by": "acme"}})
        elif s == "empty":
            self._send(200, {"object": "list", "data": []})
        else:
            self._send(200, {"whatever": 1})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != self.good_key:
            self._send(401, {"error": {"message": "invalid api key"}})
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        model = payload.get("model", "")
        if model not in ("acme-standard", "acme-pro-max"):
            self._send(404, {"error": {"message": "model %s not found" % model}})
            return
        self._send(200, {"choices": [{"message": {"content": "pong"}}]})


class ProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.srv.server_address[1]
        cls.base = "http://127.0.0.1:%d/v1" % cls.port
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _Handler.shape = "openai"

    # ---------- 档位判定 ----------
    def test_tier_flagship(self):
        for mid in ("acme-pro", "gpt-4o", "claude-opus-4", "some-ultra",
                    "qwen-max", "acme-pro-max"):
            t = provider.tier_of(mid)
            self.assertEqual(t["tier"], provider.TIER_FLAGSHIP, mid)
        self.assertEqual(provider.tier_of("acme-pro")["label"], "旗舰")

    def test_tier_economy(self):
        for mid in ("acme-lite", "acme-nano", "gpt-4o-mini", "claude-haiku",
                    "acme-flash", "acme-turbo", "qwen2.5-7b", "model-14b"):
            t = provider.tier_of(mid)
            self.assertEqual(t["tier"], provider.TIER_ECONOMY, mid)
        self.assertEqual(provider.tier_of("acme-lite")["label"], "轻量")

    def test_tier_balanced(self):
        for mid in ("acme-standard", "qwen-plus", "glm-4", "baichuan-4"):
            t = provider.tier_of(mid)
            self.assertEqual(t["tier"], provider.TIER_BALANCED, mid)

    def test_tier_light_wins_over_flag(self):
        # o1-mini 必须判成轻量，不能被 "o1" 抢先命中成旗舰
        self.assertEqual(provider.tier_of("o1-mini")["tier"], provider.TIER_ECONOMY)
        self.assertEqual(provider.tier_of("gpt-4.1-nano")["tier"], provider.TIER_ECONOMY)
        # 带版本号的 mini 仍是轻量
        self.assertEqual(provider.tier_of("gpt-4o-mini-2024")["tier"], provider.TIER_ECONOMY)

    def test_tier_turbo_override(self):
        # turbo 一般是便宜档，但 gpt-4-turbo 是旗舰
        self.assertEqual(provider.tier_of("acme-turbo")["tier"], provider.TIER_ECONOMY)
        self.assertEqual(provider.tier_of("gpt-4-turbo")["tier"], provider.TIER_FLAGSHIP)

    def test_tier_vendor_metadata(self):
        t = provider.tier_of("weird-name", {"tier": "economy"})
        self.assertEqual(t["tier"], provider.TIER_ECONOMY)
        t = provider.tier_of("weird-name", {"level": "flagship"})
        self.assertEqual(t["tier"], provider.TIER_FLAGSHIP)

    def test_tier_note_reasoning(self):
        self.assertIn("推理", provider.tier_of("acme-reasoner-r1")["note"])

    # ---------- 清单解析 ----------
    def test_parse_openai_shape(self):
        got = provider._parse_models({"data": [{"id": "a"}, {"id": "b"}]})
        self.assertEqual([g["id"] for g in got], ["a", "b"])

    def test_parse_variants(self):
        got = provider._parse_models({"models": [{"name": "x"}, "y"]})
        self.assertEqual([g["id"] for g in got], ["x", "y"])

    def test_parse_nested(self):
        got = provider._parse_models({"data": {"models": ["p", "q"]}})
        self.assertEqual([g["id"] for g in got], ["p", "q"])

    def test_parse_mapping(self):
        got = provider._parse_models({"acme-x": {"owned_by": "acme"}})
        self.assertEqual(got[0]["id"], "acme-x")

    def test_parse_dedup(self):
        got = provider._parse_models({"data": [{"id": "a"}, {"id": "a"}]})
        self.assertEqual(len(got), 1)

    # ---------- 接口校验 ----------
    def test_check_ok(self):
        r = provider.check_endpoint(self.base, "sk-good")
        self.assertTrue(r["ok"], r["message"])
        self.assertTrue(r["key_ok"])
        self.assertFalse(r["needs_manual"])
        self.assertEqual(len(r["models"]), 4)
        ids = [m["id"] for m in r["models"]]
        self.assertIn("acme-pro-max", ids)
        # 旗舰排在前面
        self.assertEqual(r["models"][0]["tier"], provider.TIER_FLAGSHIP)

    def test_check_bad_key(self):
        r = provider.check_endpoint(self.base, "sk-bad")
        self.assertFalse(r["ok"])
        self.assertFalse(r["key_ok"])
        self.assertFalse(r["needs_manual"])
        self.assertIn("API Key 无效", r["message"])

    def test_check_empty_key(self):
        r = provider.check_endpoint(self.base, "")
        self.assertFalse(r["ok"])
        self.assertIn("API Key", r["message"])

    def test_check_empty_base(self):
        r = provider.check_endpoint("", "sk-good")
        self.assertFalse(r["ok"])
        self.assertIn("接口地址", r["message"])

    def test_check_shape_variants(self):
        for shape, expect_n in (("variants", 3), ("nested", 2), ("mapping", 1)):
            _Handler.shape = shape
            r = provider.check_endpoint(self.base, "sk-good")
            self.assertTrue(r["ok"], "%s -> %s" % (shape, r["message"]))
            self.assertEqual(len(r["models"]), expect_n, shape)

    def test_check_needs_manual_when_no_list(self):
        _Handler.shape = "empty"
        r = provider.check_endpoint(self.base, "sk-good")
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_manual"])
        self.assertIn("手动填写模型名", r["message"])

    def test_check_needs_manual_when_404(self):
        # 服务端没有 /models 路由 -> 404 -> 走手动
        r = provider.check_endpoint("http://127.0.0.1:%d/nope/v1" % self.port, "sk-good")
        self.assertTrue(r["needs_manual"])

    def test_check_network_fail(self):
        r = provider.check_endpoint("http://127.0.0.1:1/v1", "sk-good")
        self.assertFalse(r["ok"])
        self.assertIn("网络", r["message"])

    # ---------- 手动填名后的真实验证 ----------
    def test_probe_model_ok(self):
        r = provider.probe_model(self.base, "sk-good", "acme-standard")
        self.assertTrue(r["ok"], r["message"])

    def test_probe_model_wrong_name(self):
        r = provider.probe_model(self.base, "sk-good", "no-such-model")
        self.assertFalse(r["ok"])
        self.assertIn("模型", r["message"] + "模型")

    def test_probe_model_bad_key(self):
        r = provider.probe_model(self.base, "sk-bad", "acme-standard")
        self.assertFalse(r["ok"])
        self.assertIn("API Key", r["message"])

    def test_probe_model_missing_args(self):
        r = provider.probe_model("", "", "")
        self.assertFalse(r["ok"])

    # ---------- 计价提取 ----------
    def test_extract_price_per_token(self):
        pin, pout = provider._extract_price({"pricing": {"prompt": 0.0000015,
                                                        "completion": 0.000002}})
        self.assertAlmostEqual(pin, 1.5, places=3)
        self.assertAlmostEqual(pout, 2.0, places=3)

    def test_extract_price_per_million(self):
        pin, pout = provider._extract_price({"pricing": {"prompt": 1.5,
                                                        "completion": 6.0}})
        self.assertAlmostEqual(pin, 1.5, places=3)
        self.assertAlmostEqual(pout, 6.0, places=3)

    def test_extract_price_absent(self):
        self.assertEqual(provider._extract_price({"id": "x"}), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
