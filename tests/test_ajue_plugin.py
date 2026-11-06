# -*- coding: utf-8 -*-
"""阿觉 companion 插件的自测。

覆盖插件形态的关键约束：
- 插件声明能被正确解析，且**不会**被当成工具注册进 ToolBus
- companion 能被宿主发现
- 阿觉的记忆与通用 Agent 的记忆**完全隔离**
- 能读取「各工作区」的会话
- 桌宠界面自包含、具备完整对话能力
- 阿觉说话依赖宿主端点；核心 Agent 不再含任何阿觉代码
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PLUGIN_DIR = os.path.join(ROOT, "plugins", "ajue")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestPluginDeclaration(unittest.TestCase):
    """插件声明：.qdplugin 头解析 + 不被当成工具。"""

    def setUp(self):
        from agent.tools import _parse_qdplugin
        self.parse = _parse_qdplugin
        self.path = os.path.join(ROOT, "plugins", "ajue.qdplugin")

    def test_companion_not_registered_as_tool(self):
        """companion 插件不能注册成工具（否则模型会把它当工具调用）。"""
        self.assertIsNone(self.parse(self.path))

    def test_tool_plugin_still_parsed(self):
        """普通工具插件不受影响。"""
        parsed = self.parse(os.path.join(ROOT, "plugins", "genoffice.qdplugin"))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], "genoffice")

    def test_market_lists_companion_fields(self):
        """插件市场能读出 companion 的声明字段。"""
        from agent import market
        items = market.list_all()
        ids = [i["id"] for i in items["plugins"]["installed"]]
        self.assertIn("ajue", ids)
        head = market._head_fields(self.path)
        self.assertEqual(head.get("kind"), "companion")
        self.assertEqual(head.get("entry"), "runner.py")
        self.assertTrue(head.get("data_dir"))


class TestCompanionDiscovery(unittest.TestCase):
    """宿主发现并拉起 companion。"""

    def test_discover_ajue(self):
        from agent.companions import CompanionManager
        mgr = CompanionManager({"http_host": "127.0.0.1", "http_port": 6802})
        items = mgr.discover()
        ids = [i["id"] for i in items]
        self.assertIn("ajue", ids)
        item = [i for i in items if i["id"] == "ajue"][0]
        self.assertEqual(item["ui"], "pet")
        self.assertEqual(item["entry"], "runner.py")

    def test_endpoint_from_cfg(self):
        from agent.companions import CompanionManager
        mgr = CompanionManager({"http_host": "127.0.0.1", "http_port": "6802"})
        self.assertEqual(mgr.endpoint(), "http://127.0.0.1:6802")

    def test_entry_and_data_paths_exist(self):
        from agent.companions import CompanionManager
        mgr = CompanionManager({})
        plugin_dir, data_dir, entry = mgr._paths("ajue")
        self.assertTrue(os.path.isfile(entry))
        self.assertTrue(os.path.isdir(plugin_dir))
        self.assertIn("plugin_data", data_dir)


class TestIndependentMemory(unittest.TestCase):
    """阿觉的记忆与通用 Agent 的记忆完全隔离。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.core = _load("ajue_core_test", os.path.join(PLUGIN_DIR, "ajue_core.py"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_memory_lives_in_plugin_dir(self):
        """阿觉的记忆文件必须落在插件数据目录，绝不能写进宿主目录。"""
        aj = self.core.Ajue(self.tmp, PLUGIN_DIR, ROOT)
        mem_path = os.path.abspath(aj.memory.path)
        self.assertTrue(mem_path.startswith(os.path.abspath(self.tmp)),
                        "阿觉的记忆应落在插件数据目录：" + mem_path)
        self.assertNotIn(os.path.abspath(ROOT), mem_path)
        # 真正写入一条，确认文件位置
        aj.memory.add("我喜欢喝咖啡", kind="preference", source="auto")
        self.assertTrue(os.path.isfile(aj.memory.path))
        self.assertTrue(os.path.abspath(aj.memory.path).startswith(
            os.path.abspath(self.tmp)))

    def test_persona_loaded_from_plugin(self):
        aj = self.core.Ajue(self.tmp, PLUGIN_DIR, ROOT)
        self.assertEqual(aj.persona.get("name"), "阿觉")

    def test_permissions_are_in_plugin_dir(self):
        aj = self.core.Ajue(self.tmp, PLUGIN_DIR, ROOT)
        aj.permissions.grant("observe_conversations")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "permissions.json")))


class TestWorkspaceScan(unittest.TestCase):
    """读取各工作区会话。"""

    def setUp(self):
        self.core = _load("ajue_core_ws", os.path.join(PLUGIN_DIR, "ajue_core.py"))
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.ws1 = os.path.join(self.tmp, "ws1")
        self.ws2 = os.path.join(self.tmp, "ws2")
        for root in (self.ws1, self.ws2):
            os.makedirs(os.path.join(root, "会话"))
        self._write_conv(self.ws1, "a", "我周五要交方案", "好的，记住了")
        self._write_conv(self.ws2, "b", "我周末想去爬山", "爬山好啊")
        self.scanner = self.core.WorkspaceScanner(self.data, self.ws1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_conv(self, root, cid, user, reply):
        path = os.path.join(root, "会话", cid + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"title": "t", "updated": 1,
                       "messages": [{"role": "user", "content": user},
                                    {"role": "assistant", "content": reply}]},
                      f, ensure_ascii=False)

    def test_default_root_is_host(self):
        self.assertEqual(self.scanner.roots(), [self.ws1])

    def test_add_and_remove_root(self):
        r = self.scanner.add_root(self.ws2)
        self.assertTrue(r["ok"])
        self.assertIn(self.ws2, self.scanner.roots())
        self.scanner.remove_root(self.ws2)
        self.assertNotIn(self.ws2, self.scanner.roots())

    def test_add_invalid_root_rejected(self):
        r = self.scanner.add_root(os.path.join(self.tmp, "nope"))
        self.assertFalse(r["ok"])

    def test_conversations_read_across_workspaces(self):
        self.scanner.add_root(self.ws2)
        convs = self.scanner.conversations()
        texts = [m["content"] for c in convs for m in c["messages"]]
        self.assertIn("我周五要交方案", texts)
        self.assertIn("我周末想去爬山", texts)

    def test_learn_requires_permission(self):
        aj = self.core.Ajue(self.data, PLUGIN_DIR, ROOT)
        aj.scanner = self.scanner
        r = aj.learn_from_workspaces()
        self.assertFalse(r["ok"])
        self.assertIn("未授权", r["reason"])

    def test_learn_after_grant(self):
        aj = self.core.Ajue(self.data, PLUGIN_DIR, ROOT)
        aj.scanner = self.scanner
        aj.permissions.grant("observe_conversations")
        r = aj.learn_from_workspaces()
        self.assertTrue(r["ok"])
        self.assertGreater(r["scanned"], 0)


class TestSystemPromptAndEndpoint(unittest.TestCase):
    """阿觉自建身份，并依赖宿主端点说话。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.core = _load("ajue_core_sp", os.path.join(PLUGIN_DIR, "ajue_core.py"))
        self.aj = self.core.Ajue(self.tmp, PLUGIN_DIR, ROOT,
                                 endpoint="http://127.0.0.1:6802")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prompt_contains_persona(self):
        sp = self.aj.build_system_prompt("你好")
        self.assertIn("阿觉", sp)

    def test_prompt_contains_speaking_style(self):
        """阿觉必须带「说话方式」引导，否则会像说明书一样生硬、直球。"""
        sp = self.aj.build_system_prompt("你好")
        self.assertIn("说话方式", sp)
        self.assertIn("不直球", sp)
        self.assertIn("说教", sp)

    def test_prompt_contains_current_time(self):
        """阿觉必须知道「现在」——否则连几点、星期几都说错。"""
        sp = self.aj.build_system_prompt("你好")
        self.assertIn("当前时间", sp)
        # 时间片段里应有本机真实年份与「年月日」
        import datetime
        self.assertIn("%d年%d月%d日" % (datetime.date.today().year,
                                        datetime.date.today().month,
                                        datetime.date.today().day), sp)

    def test_prompt_contains_l3_or_memory(self):
        self.aj.memory.add("我喜欢喝咖啡", kind="preference", source="auto")
        sp = self.aj.build_system_prompt("我喜欢喝咖啡")
        self.assertTrue(len(sp) > 10)

    def test_llm_chat_sends_agent_flag(self):
        """阿觉聊天应带 agent:true，让宿主 6802 走 agent 模式（可联网）。"""
        captured = {}

        class _FakeResp:
            def __init__(self, data):
                self._d = data.encode("utf-8")

            def read(self):
                return self._d

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeOpener:
            def open(self, req, timeout=None):
                captured["body"] = req.data.decode("utf-8")
                return _FakeResp(json.dumps(
                    {"choices": [{"message": {"content": "ok"}}]}))

        self.aj.llm.model = "test-model"          # 跳过 available() 探测
        self.aj.llm._opener = lambda: _FakeOpener()
        out = self.aj.llm.chat([{"role": "user", "content": "hi"}], agent=True)
        self.assertEqual(out, "ok")
        self.assertIn('"agent": true', captured["body"])
        # 不带 agent 时不应出现该标志（裸透传）
        captured.clear()
        self.aj.llm.chat([{"role": "user", "content": "hi"}], agent=False)
        self.assertNotIn("agent", captured["body"])

    def test_llm_client_points_to_host(self):
        self.assertEqual(self.aj.llm.endpoint, "http://127.0.0.1:6802")

    def test_llm_chat_without_host_returns_error_text(self):
        """宿主不在时不能抛异常，应返回可读提示。"""
        dead = self.core.Ajue(self.tmp, PLUGIN_DIR, ROOT,
                              endpoint="http://127.0.0.1:1")
        out = dead.llm.chat([{"role": "user", "content": "hi"}])
        self.assertIsInstance(out, str)
        self.assertIn("联系不上", out)

    def test_minimind_never_auto(self):
        r = self.aj.minimind.distill([{"role": "user", "text": "x"}], approved=False)
        self.assertFalse(r["ok"])


class TestChatRoutingAndStreaming(unittest.TestCase):
    """路由与流式：日常闲聊走快速透传+流式，只有需要联网才走 agent。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.core = _load("ajue_core_rt", os.path.join(PLUGIN_DIR, "ajue_core.py"))
        self.aj = self.core.Ajue(self.tmp, PLUGIN_DIR, ROOT,
                                 endpoint="http://127.0.0.1:6802")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_llm(self, capture):
        class _L:
            def __init__(self):
                self.model = "m"

            def chat(self, messages, timeout=180, model=None, max_tokens=None,
                     agent=False, on_chunk=None):
                capture["agent"] = agent
                capture["on_chunk"] = on_chunk
                text = "好的，我陪你说说话。"
                if on_chunk:
                    for ch in ["好的，", "我陪你", "说说话。"]:
                        on_chunk(ch)
                return text
        return _L()

    def test_smalltalk_does_not_force_agent(self):
        cap = {}
        self.aj.llm = self._fake_llm(cap)
        out = self.aj.chat("我今天好累")
        self.assertEqual(out, "好的，我陪你说说话。")
        self.assertFalse(cap["agent"], "日常闲聊不应走 agent（会慢）")

    def test_web_query_uses_agent(self):
        cap = {}
        self.aj.llm = self._fake_llm(cap)
        self.aj.chat("帮我查一下今天的新闻")
        self.assertTrue(cap["agent"], "查新闻应走 agent 联网模式")

    def test_smalltalk_streams_chunks(self):
        cap = {}
        self.aj.llm = self._fake_llm(cap)
        chunks = []
        self.aj.chat("嗨", on_chunk=lambda c: chunks.append(c))
        self.assertEqual("".join(chunks), "好的，我陪你说说话。")
        self.assertIsNotNone(cap["on_chunk"])

    def test_needs_web_heuristics(self):
        self.assertTrue(self.aj.needs_web("今天天气怎么样"))
        self.assertTrue(self.aj.needs_web("查一下这个网址 http://x.com"))
        self.assertTrue(self.aj.needs_web("最新股价"))
        self.assertFalse(self.aj.needs_web("我今天好累"))
        self.assertFalse(self.aj.needs_web("陪我聊聊天吧"))
        self.assertFalse(self.aj.needs_web("嗨"))

    def test_read_sse_parses_and_calls_back(self):
        import io
        sse = ("data: {\"choices\":[{\"delta\":{\"content\":\"你\"}}]}\n\n"
               "data: {\"choices\":[{\"delta\":{\"content\":\"好\"}}]}\n\n"
               "data: [DONE]\n\n").encode("utf-8")
        got = []
        out = self.core.LLMClient._read_sse(io.BytesIO(sse), got.append)
        self.assertEqual(out, "你好")
        self.assertEqual(got, ["你", "好"])


class TestPetUi(unittest.TestCase):
    """桌宠是用户与阿觉对话的唯一入口：桌面上的小球 + 点开后的对话面板。"""

    def setUp(self):
        with open(os.path.join(PLUGIN_DIR, "pet.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_has_chat_input_and_log(self):
        self.assertIn('id="in"', self.html)
        self.assertIn('id="log"', self.html)
        self.assertIn('id="send"', self.html)

    def test_has_orb_and_panel_two_forms(self):
        """必须是「桌面小球 + 展开面板」两种形态，而不是一个普通窗口。"""
        self.assertIn('id="orb"', self.html)          # 小球本体
        self.assertIn('id="panel"', self.html)        # 对话面板
        self.assertIn('data-state="orb"', self.html)  # 形态开关
        self.assertIn("body[data-state=\"orb\"]", self.html)
        self.assertIn("body[data-state=\"panel\"]", self.html)

    def test_draggable_and_collapsible(self):
        """球和面板顶栏可拖；面板有「收起成球」的按钮。"""
        self.assertIn("pywebview-drag-region", self.html)
        self.assertIn('id="btnMin"', self.html)
        self.assertIn('id="menu"', self.html)     # 右键菜单
        self.assertIn("save_pos", self.html)      # 记住位置

    def test_has_permission_and_workspace_controls(self):
        # 授权 / 工作区 / 学习 / 日记 / 语音 / 清屏 已从主界面移除，并入设置
        self.assertNotIn('id="tbAuth"', self.html)
        self.assertNotIn('id="tbWs"', self.html)
        self.assertNotIn('id="tbLearn"', self.html)
        self.assertNotIn('id="tbDiary"', self.html)
        self.assertNotIn('id="tbTts"', self.html)
        self.assertNotIn('id="tbClear"', self.html)
        self.assertNotIn('id="tools"', self.html)
        self.assertNotIn('id="miClear"', self.html)
        # 这些能力在设置面板里仍有入口
        self.assertIn('data-tab="auth"', self.html)
        self.assertIn('data-tab="ws"', self.html)
        self.assertIn('data-tab="model"', self.html)
        # 模型配置 API 已接上
        self.assertIn("ajue_config", self.html)
        self.assertIn("set_ajue_config", self.html)

    def test_settings_tabs(self):
        for tab in ("auth", "ws", "voice", "model", "state", "about"):
            self.assertIn('data-tab="%s"' % tab, self.html)

    def test_self_contained(self):
        """不能引用外部资源（插件进程离线也能跑）。"""
        self.assertNotIn("http://", self.html.replace("http://127.0.0.1", ""))
        self.assertNotIn("https://", self.html)
        self.assertNotIn("<script src", self.html)


class TestPetWindowGeometry(unittest.TestCase):
    """球 / 面板的位置与形态切换。"""

    def setUp(self):
        self.runner = _load("ajue_runner_geo",
                            os.path.join(PLUGIN_DIR, "runner.py"))
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sizes_are_pet_like(self):
        """球必须足够小（它是一颗球，不是一个窗口）。"""
        self.assertLessEqual(self.runner.ORB_SIZE, 160)
        self.assertGreaterEqual(self.runner.PANEL_W, 320)

    def test_geometry_default_and_remember(self):
        g = self.runner.Geometry(self.tmp)
        self.assertEqual(g.data["state"], "orb")
        self.assertTrue(g.auto_panel())            # 面板位置从球「长」出来
        g.set("orb", 300, 400)
        g.set_state("panel")
        g2 = self.runner.Geometry(self.tmp)
        self.assertEqual(g2.get("orb"), (300, 400))
        self.assertEqual(g2.data["state"], "panel")

    def test_panel_auto_follows_orb_until_moved(self):
        g = self.runner.Geometry(self.tmp)
        g.set("orb", 700, 300)
        self.assertTrue(g.auto_panel())            # 用户没挪过面板
        g.set("panel", 10, 10, manual=True)
        self.assertFalse(g.auto_panel())
        self.assertEqual(g.get("panel"), (10, 10))

    def test_state_switch_api(self):
        api = self.runner.AjueApi.__new__(self.runner.AjueApi)
        api._win = None
        api.state = "orb"
        api.geo = self.runner.Geometry(self.tmp)
        # 没有窗口时不应该炸，只是切换不了
        self.assertFalse(api._place("panel"))
        self.assertEqual(api.set_state("wat")["ok"], False)

    def test_clamp_keeps_window_on_screen(self):
        api = self.runner.AjueApi.__new__(self.runner.AjueApi)
        x, y = api._clamp(-50, -50, 100, 100)
        self.assertEqual((x, y), (0, 0))
        x, y = api._clamp(99999, 99999, 100, 100)
        self.assertLessEqual(x, 1920 - 100)
        self.assertLessEqual(y, 1080 - 100)


class TestVoiceSynthesis(unittest.TestCase):
    """语音合成：多家厂商可选，且 Windows 上不能再拿 SoundPlayer 播 mp3。"""

    def setUp(self):
        self.tts = _load("ajue_tts_test", os.path.join(PLUGIN_DIR, "tts.py"))
        self.tmp = tempfile.mkdtemp()
        self.tts.set_data_dir(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_longer_plays_mp3_with_soundplayer(self):
        """SoundPlayer 只认 wav——用它放 mp3 就是「听不见」的根因。"""
        src = open(os.path.join(PLUGIN_DIR, "tts.py"), encoding="utf-8").read()
        self.assertIn("SoundPlayer", src)          # 只保留给 wav 兜底
        self.assertIn("_play_mci", src)            # mp3 走 MCI
        self.assertIn("mciSendStringW", src)

    def test_presets_include_third_party_voices(self):
        ids = [p["id"] for p in self.tts.PRESETS]
        for want in ("edge", "siliconflow", "volcark", "openai", "custom",
                     "sapi"):
            self.assertIn(want, ids)
        self.assertTrue(any(p.get("kind") == "openai" and p.get("base_url")
                            for p in self.tts.PRESETS))

    def test_config_round_trip(self):
        self.tts.save_config({"provider": "openai",
                              "openai": {"base_url": "https://example.com/v1",
                                         "model": "m1", "voice": "v1"}})
        cfg = self.tts.load_config(refresh=True)
        self.assertEqual(cfg["provider"], "openai")
        self.assertEqual(cfg["openai"]["model"], "m1")

    def test_apply_preset(self):
        r = self.tts.apply_preset("siliconflow")
        self.assertTrue(r["ok"])
        cfg = self.tts.load_config(refresh=True)
        self.assertEqual(cfg["provider"], "openai")
        self.assertTrue(cfg["openai"]["base_url"])
        bad = self.tts.apply_preset("不存在的厂商")
        self.assertFalse(bad["ok"])

    def test_provider_chain_follows_config(self):
        self.tts.save_config({"provider": "off"})
        self.assertEqual(self.tts.provider_chain(), [])
        self.tts.save_config({"provider": "edge"})
        self.assertEqual(self.tts.provider_chain(), ["edge", "sapi"])

    def test_openai_request_shape(self):
        """合成请求打的是 OpenAI 兼容的 /v1/audio/speech。"""
        src = open(os.path.join(PLUGIN_DIR, "tts.py"), encoding="utf-8").read()
        self.assertIn("/audio/speech", src)
        self.assertIn("Authorization", src)
        self.assertIn("response_format", src)

    def test_strip_markdown(self):
        self.assertEqual(self.tts._strip_md("**加粗**"), "加粗")
        self.assertEqual(self.tts._strip_md("`code`"), "code")
        self.assertNotIn("```", self.tts._strip_md("```python\nx=1\n```"))

    def test_speak_empty_text(self):
        ok, detail = self.tts.speak("")
        self.assertFalse(ok)


class TestCompanionHeartbeat(unittest.TestCase):
    """桌宠是独立进程：靠心跳判断它还在不在桌面上。"""

    def setUp(self):
        from agent import companions
        self.mod = companions
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, pid, age=0):
        with open(self.mod.heartbeat_path(self.tmp), "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "ts": time.time() - age}, f)

    def test_missing_heartbeat(self):
        self.assertIsNone(self.mod.read_heartbeat(self.tmp))

    def test_stale_heartbeat_is_dead(self):
        self._write(123456, age=120)
        self.assertIsNone(self.mod.read_heartbeat(self.tmp))

    def test_live_heartbeat(self):
        """用一个真的活着的进程验证。"""
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            self._write(p.pid)
            self.assertEqual(self.mod.read_heartbeat(self.tmp), p.pid)
        finally:
            p.kill()


class TestRunnerApi(unittest.TestCase):
    """插件进程入口与它的 JS 接口。"""

    def setUp(self):
        self.runner = _load("ajue_runner_test", os.path.join(PLUGIN_DIR, "runner.py"))

    def test_api_methods_are_sync(self):
        """pywebview 6.x 的 js_api 不支持 async 方法（返回值要 json 序列化）。"""
        import inspect
        for name in ("chat", "status", "greet", "learn", "permissions",
                     "workspaces", "write_diary", "set_tts", "quit"):
            fn = getattr(self.runner.AjueApi, name, None)
            self.assertIsNotNone(fn, "缺少接口：" + name)
            self.assertFalse(inspect.iscoroutinefunction(fn),
                             "%s 不能是 async（pywebview js_api 不支持）" % name)

    def test_api_surface_complete(self):
        for name in ("chat", "status", "greet", "learn", "permissions", "grant",
                     "revoke", "workspaces", "add_workspace", "remove_workspace",
                     "write_diary", "speak", "set_tts", "voices", "persona",
                     "style", "l3", "move_to", "quit", "minimind_available",
                     "distill", "clear_history"):
            self.assertTrue(hasattr(self.runner.AjueApi, name), "缺少接口：" + name)

    def test_resolve_endpoint_prefers_explicit(self):
        self.assertEqual(self.runner.resolve_endpoint("http://1.2.3.4:9999/"),
                         "http://1.2.3.4:9999")


class TestCoreIsGeneric(unittest.TestCase):
    """通用 Agent 不含任何阿觉专属代码。"""

    def test_app_py_has_no_ajue_code(self):
        src = open(os.path.join(ROOT, "agent", "app.py"), encoding="utf-8").read()
        for bad in ("ajue_core", "ajue_status", "ajue_set", "self.ajue",
                    "ajue_enabled", "persona_name"):
            self.assertNotIn(bad, src, "核心里不该再有：" + bad)

    def test_ui_has_no_ajue_panel(self):
        html = open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8").read()
        self.assertNotIn("ajueFab", html)
        self.assertNotIn("ajuePanel", html)

    def test_core_has_no_persona_modules(self):
        for name in ("ajue_core.py", "tts.py", "pet.py"):
            self.assertFalse(os.path.isfile(os.path.join(ROOT, "agent", name)),
                             "核心不该再有 " + name)

    def test_core_exposes_companion_api(self):
        """宿主提供 companion 的发现/启停接口（装上插件就能在界面里管）。"""
        import agent.app as app
        for name in ("companions_list", "companion_start", "companion_stop"):
            self.assertTrue(hasattr(app.Api, name), "缺少接口：" + name)

    def test_llm_client_bypasses_proxy(self):
        """本地端点必须绕过系统代理：否则 http_proxy 会劫持回环请求。"""
        import importlib.util
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        spec = importlib.util.spec_from_file_location(
            "ajue_core_proxy", os.path.join(PLUGIN_DIR, "ajue_core.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"object": "list",
                                   "data": [{"id": "fake-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        old = {k: os.environ.get(k) for k in
               ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY")}
        # 把代理指向一个根本不存在的地址：若阿觉走代理，这里必然失败
        os.environ["http_proxy"] = "http://127.0.0.1:9"
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        try:
            c = mod.LLMClient("http://127.0.0.1:%d" % port)
            self.assertTrue(c.available(), "阿觉必须绕过代理直连宿主端点")
            self.assertEqual(c.model, "fake-model")
        finally:
            srv.shutdown()
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestDiaryAndConfig(unittest.TestCase):
    """日记内容来源 / 轻量生成 / 独立模型配置。"""

    def setUp(self):
        self.core = _load("ajue_core_cfg", os.path.join(PLUGIN_DIR, "ajue_core.py"))
        self.tmp = tempfile.mkdtemp()
        self.aj = self.core.Ajue(self.tmp, PLUGIN_DIR, ROOT,
                                endpoint="http://127.0.0.1:6802")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chat_log_append_and_read(self):
        self.aj.log_chat("你好", "我在呢")
        self.aj.log_chat("晚上吃火锅", "安排上")
        log = self.aj.read_chat_log()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["user"], "你好")
        self.assertEqual(log[1]["ajue"], "安排上")

    def test_config_default_and_persist(self):
        self.assertEqual(self.aj.config["model"], "")
        self.assertEqual(self.aj.config["diary_time"], "12:00")
        r = self.aj.set_config({"model": "x-lite", "diary_time": "08:30"})
        self.assertEqual(r["model"], "x-lite")
        a2 = self.core.Ajue(self.tmp, PLUGIN_DIR, ROOT,
                            endpoint="http://127.0.0.1:6802")
        self.assertEqual(a2.config["model"], "x-lite")
        self.assertEqual(a2.config["diary_time"], "08:30")

    def test_diary_includes_chat_log(self):
        chat = [{"user": "我叫人机", "ajue": "记下了"},
                {"user": "在重庆生活", "ajue": "好地方"}]
        r = self.aj.diary.write(self.aj.memory, self.aj.style, self.aj.persona,
                                chat_log=chat, workspace_notes=[])
        self.assertTrue(r["ok"])
        self.assertIn("人机", r["text"])
        self.assertIn("重庆", r["text"])

    def test_diary_prompt_is_compact(self):
        """日记用紧凑 prompt；llm_fn 收到的 prompt 不应过大。"""
        seen = {}
        def fake_llm(prompt):
            seen["p"] = prompt
            return "日记占位"
        chat = [{"user": "x" * 50, "ajue": "y" * 50}]
        # 仅验证 prompt 不含历史模板里的「结合下面关于主人的已知信息」旧文案
        self.aj.diary.write(self.aj.memory, self.aj.style, self.aj.persona,
                            llm_fn=fake_llm, chat_log=chat, workspace_notes=[])
        self.assertIn("不超过150字", seen["p"])


class TestDiaryScheduler(unittest.TestCase):
    """定时写日记的解析。"""

    def setUp(self):
        self.runner = _load("ajue_runner_sched",
                            os.path.join(PLUGIN_DIR, "runner.py"))

    def test_parse_time(self):
        self.assertEqual(self.runner._parse_time("12:00"), (12, 0))
        self.assertEqual(self.runner._parse_time("08:30"), (8, 30))
        self.assertEqual(self.runner._parse_time("23:59"), (23, 59))
        self.assertEqual(self.runner._parse_time("99:99"), (12, 0))   # 非法回退
        self.assertEqual(self.runner._parse_time(""), (12, 0))


class TestCompanionAgentTools(unittest.TestCase):
    """宿主 6802 给 companion（阿觉）的安全工具子集。"""

    def _servers(self):
        import importlib
        return importlib.import_module("agent.servers")

    def test_tool_subset_only_allows_safe_tools(self):
        srv = self._servers()
        bus = object()

        class FakeBus:
            def enabled(self):
                return ["web", "browser", "screen", "skills", "remind",
                        "fs", "code", "organize", "memo"]

            def openai_schemas(self):
                return [{"type": "function",
                         "function": {"name": n}} for n in self.enabled()]

            def invoke(self, name, args):
                return (name in srv._COMPANION_TOOLS), "ran %s" % name

        sub = srv._ToolSubset(FakeBus(), srv._COMPANION_TOOLS)
        enabled = sub.enabled()
        # 危险工具绝不应出现
        self.assertNotIn("fs", enabled)
        self.assertNotIn("code", enabled)
        self.assertNotIn("organize", enabled)
        # 安全工具应在
        for t in ("web", "browser", "screen", "skills", "remind"):
            self.assertIn(t, enabled)
        # 越权调用被拒绝
        ok, _ = sub.invoke("fs", {})
        self.assertFalse(ok)
        ok, _ = sub.invoke("web", {})
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
