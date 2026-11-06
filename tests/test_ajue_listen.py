# -*- coding: utf-8 -*-
"""阿觉「常驻监听 + 两层唤醒」的单元测试。

不依赖真实麦克风：音频源与 ASR 都用 mock 注入，因此可以在任何机器上跑。
覆盖：唤醒词识别、语义确认（防误唤醒）、能量 VAD、重采样、监听状态机
流转（命中唤醒词→调用阿觉；未命中→不调用）、以及 listen 配置的持久化。
"""
import array
import json
import math
import os
import random
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "plugins", "ajue"))

import listen  # noqa: E402


def speech_pcm(seconds=0.3, rate=listen.SAMPLE_RATE, freq=320.0):
    """合成一段「有人说话」的 16bit 单声道 PCM（带底噪，贴近真实语音）。"""
    n = int(rate * seconds)
    a = array.array("h", [0]) * n
    for i in range(n):
        v = math.sin(2 * math.pi * freq * i / rate) * 11000 + \
            random.randint(-1500, 1500)
        a[i] = max(-32768, min(32767, int(v)))
    return a.tobytes()


def silence_pcm(seconds=0.3, rate=listen.SAMPLE_RATE):
    return b"\x00" * int(rate * seconds) * 2


class FakeAudio:
    """假麦克风：按顺序吐出预设音频块，用完后持续返回静音。"""

    def __init__(self, chunks, stop_flag=None):
        self.chunks = list(chunks)
        self.stop_flag = stop_flag if stop_flag is not None else {}
        self.i = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, chunk=None):
        if self.stop_flag.get("stop"):
            raise KeyboardInterrupt        # 让监听线程干净退出
        if self.i < len(self.chunks):
            c = self.chunks[self.i]
            self.i += 1
            return c
        return silence_pcm(0.3)


class FakeASR:
    """假识别器：按调用顺序返回预设文本。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    def transcribe(self, audio):
        t = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return t


class FakeApi:
    """假阿觉 API：记录收到的对话。"""

    def __init__(self):
        self.core = types.SimpleNamespace(config={})
        self.chats = []
        self.on_chat = None

    def chat(self, text):
        self.chats.append(text)
        if self.on_chat:
            self.on_chat()
        return "好的"

    def set_ajue_config(self, patch):
        self.core.config.update(patch or {})
        return {"ok": True}


# ---------------------------------------------------------------- 唤醒词
class TestWakeWord(unittest.TestCase):
    def test_contains_wakeword(self):
        self.assertTrue(listen.contains_wakeword("阿觉，帮我查下天气", ["阿觉"]))
        self.assertTrue(listen.contains_wakeword("小阿觉在吗", ["阿觉", "小阿觉"]))
        self.assertFalse(listen.contains_wakeword("今天天气不错", ["阿觉"]))
        self.assertFalse(listen.contains_wakeword("", ["阿觉"]))
        self.assertFalse(listen.contains_wakeword("阿觉", []))

    def test_strip_wakeword(self):
        self.assertEqual(listen.strip_wakeword("阿觉，帮我查下天气", ["阿觉"]),
                         "帮我查下天气")
        self.assertEqual(listen.strip_wakeword("阿觉阿觉在吗", ["阿觉", "阿觉阿觉"]),
                         "在吗")

    def test_semantic_judge_requires_wakeword(self):
        # 叫了名字 + 有实质内容 → 确认
        self.assertTrue(
            listen.semantic_judge("阿觉，帮我看看今天天气", ["阿觉"]))
        # 只叫了名字没内容 → 不算有效指令
        self.assertFalse(listen.semantic_judge("阿觉", ["阿觉"]))
        # 关键防误唤醒：跟别人聊时间，不该被当成在叫它
        self.assertFalse(
            listen.semantic_judge("我们明天几点开会", ["阿觉"]))
        self.assertFalse(
            listen.semantic_judge("这附近有什么好吃的", ["阿觉"]))

    def test_semantic_judge_without_wakeword_mode(self):
        # 「无唤醒词」模式：像在问/下指令才触发
        self.assertTrue(
            listen.semantic_judge("现在几点了", ["阿觉"], require_wakeword=False))
        # 注意：无唤醒词模式明显更宽松（连「我们几点开会」也会触发），
        # 这正是它更容易误唤醒的原因——默认模式已关闭它。
        self.assertTrue(
            listen.semantic_judge("我们明天几点开会", ["阿觉"],
                                  require_wakeword=False))


# ---------------------------------------------------------------- VAD
class TestEnergyVAD(unittest.TestCase):
    def test_detects_speech_and_silence(self):
        vad = listen.EnergyVAD(2)
        self.assertTrue(listen.vad_has_speech(vad, speech_pcm(0.3)))
        self.assertFalse(listen.vad_has_speech(vad, silence_pcm(0.3)))

    def test_too_short_buffer_is_not_speech(self):
        vad = listen.EnergyVAD(2)
        self.assertFalse(listen.vad_has_speech(vad, b"\x00" * 10))

    def test_make_vad_falls_back_without_webrtcvad(self):
        vad = listen._make_vad(2)
        # 装了 webrtcvad 就用它，没装就回退 EnergyVAD，两者都能判语音
        self.assertTrue(listen.vad_has_speech(vad, speech_pcm(0.3)))


# ---------------------------------------------------------------- 重采样
class TestResample(unittest.TestCase):
    def test_length_ratio(self):
        src = speech_pcm(1.0, rate=48000)
        out = listen._resample(src, 48000, 16000)
        self.assertAlmostEqual(len(out) / 2 / 16000, 1.0, delta=0.02)

    def test_same_rate_is_identity(self):
        src = speech_pcm(0.1)
        self.assertEqual(listen._resample(src, 16000, 16000), src)


# ---------------------------------------------------------------- 状态机
class TestListenerStateMachine(unittest.TestCase):
    def _make_listener(self, chunks, asr_texts, cfg=None, api=None):
        api = api or FakeApi()
        stop_flag = {}
        fa = FakeAudio(chunks, stop_flag)
        fasr = FakeASR(asr_texts)
        base = {"enabled": True, "power_policy": "always",
                "wake_words": ["阿觉"]}
        base.update(cfg or {})
        lis = listen.Listener(
            api, tempfile.gettempdir(),
            audio_source=lambda: fa,
            asr_factory=lambda: fasr,
            config_getter=lambda: dict(base),
            config_setter=lambda patch: None)
        lis._test_stop_flag = stop_flag
        return lis, api, stop_flag

    def _wait_chats(self, api, timeout=6.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if api.chats:
                return True
            time.sleep(0.05)
        return False

    def test_wakeword_triggers_chat(self):
        lis, api, stop = self._make_listener(
            chunks=[speech_pcm(0.3), silence_pcm(0.5)],
            asr_texts=["阿觉 帮我看看今天天气"])
        api.on_chat = lambda: stop.update({"stop": True})
        lis.start()
        self.assertTrue(self._wait_chats(api), "命中唤醒词后应当调用阿觉")
        lis.stop()
        self.assertIn("阿觉", api.chats[0])

    def test_non_wakeword_speech_does_not_trigger(self):
        lis, api, stop = self._make_listener(
            chunks=[speech_pcm(0.3), silence_pcm(0.5)],
            asr_texts=["我们明天几点开会"])
        lis.start()
        time.sleep(1.2)
        lis.stop()
        self.assertEqual(api.chats, [], "没叫名字时不应触发（防误唤醒）")

    def test_silence_never_triggers(self):
        lis, api, stop = self._make_listener(
            chunks=[silence_pcm(0.5)], asr_texts=["阿觉 在吗"])
        lis.start()
        time.sleep(1.0)
        lis.stop()
        self.assertEqual(api.chats, [], "纯静音时不应触发")

    def test_disabled_never_triggers(self):
        lis, api, stop = self._make_listener(
            chunks=[speech_pcm(0.3)], asr_texts=["阿觉 在吗"],
            cfg={"enabled": False})
        lis.start()
        time.sleep(0.8)
        lis.stop()
        self.assertEqual(api.chats, [])

    def test_start_stop_and_status(self):
        lis, api, stop = self._make_listener(chunks=[silence_pcm(0.5)],
                                             asr_texts=[""])
        self.assertFalse(lis.is_running())
        lis.start()
        self.assertTrue(lis.is_running())
        st = lis.status()
        self.assertTrue(st["running"])
        lis.stop()
        self.assertFalse(lis.is_running())

    def test_default_config_shape(self):
        cfg = listen.default_config()
        self.assertIn("wake_words", cfg)
        self.assertIn("power_policy", cfg)
        self.assertIn("asr_model", cfg)
        self.assertFalse(cfg["enabled"])


# ---------------------------------------------------------------- 配置持久化
class TestConfigPersist(unittest.TestCase):
    def test_listen_config_is_saved_to_disk(self):
        import ajue_core
        obj = ajue_core.Ajue.__new__(ajue_core.Ajue)
        obj.config = {"model": "", "diary_model": "", "diary_time": "12:00"}
        obj.data_root = tempfile.mkdtemp()
        obj.set_config({"listen": {"enabled": True,
                                   "wake_words": ["阿觉", "小阿觉"]}})
        self.assertTrue(obj.config["listen"]["enabled"])
        self.assertEqual(obj.config["listen"]["wake_words"],
                         ["阿觉", "小阿觉"])
        with open(os.path.join(obj.data_root, "ajue_config.json"),
                  encoding="utf-8") as f:
            saved = json.load(f)
        self.assertTrue(saved["listen"]["enabled"])

    def test_listener_reads_config_and_writes_back(self):
        api = FakeApi()
        api.core.config = {"listen": {"enabled": True, "asr_model": "tiny"}}
        lis = listen.Listener(api, tempfile.gettempdir())
        cfg = lis.cfg()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["asr_model"], "tiny")
        # 未指定的值回落默认值
        self.assertEqual(cfg["power_policy"], "ac_only")
        lis.set_config({"asr_model": "small"})
        self.assertEqual(api.core.config["listen"]["asr_model"], "small")


class TestHomophoneTolerance(unittest.TestCase):
    """同音字容错：真机实测 faster-whisper 会把「阿觉」听成「阿爵」。"""

    def test_homophone_still_wakes(self):
        self.assertTrue(listen.contains_wakeword("阿爵，帮我查下天气", ["阿觉"]))
        self.assertTrue(listen.contains_wakeword("小阿爵在吗", ["阿觉"]))
        # 仍然不能误触发
        self.assertFalse(listen.contains_wakeword("我们明天几点开会", ["阿觉"]))
        self.assertFalse(listen.contains_wakeword("阿胶怎么吃", ["阿觉"]))

    def test_strip_homophone(self):
        self.assertEqual(listen.strip_wakeword("阿爵，帮我查下天气", ["阿觉"]),
                         "帮我查下天气")

    def test_to_simplified(self):
        self.assertEqual(listen._to_simplified("幫我查一下天氣"),
                         "帮我查一下天气")
        self.assertEqual(listen._to_simplified("已是简体中文"), "已是简体中文")


class TestFuzzyAndGuards(unittest.TestCase):
    def test_non_strict_homophone_is_not_matched(self):
        # 「阿卷」不是「阿觉」的严格同音：不做近似匹配，避免误触发
        self.assertFalse(listen.contains_wakeword("阿卷你在吗", ["阿觉"]))

    def test_mute_guard_estimate(self):
        self.assertEqual(listen.Listener._speak_seconds(""), 1.5)
        self.assertGreater(listen.Listener._speak_seconds("好的"), 1.0)
        self.assertLessEqual(listen.Listener._speak_seconds("啊" * 500), 30.0)

    def test_asr_receives_hotwords(self):
        """唤醒词必须作为热词偏置传给识别器，否则很容易被听成同音字。"""
        captured = {}

        def factory(**kw):
            captured.update(kw)
            return FakeASR(["阿觉 在吗"])

        api = FakeApi()
        fa = FakeAudio([speech_pcm(0.3)])
        lis = listen.Listener(
            api, tempfile.gettempdir(),
            audio_source=lambda: fa,
            asr_factory=factory,
            config_getter=lambda: {"enabled": True, "power_policy": "always",
                                   "wake_words": ["阿觉"]})
        lis.start()
        time.sleep(1.0)
        lis.stop()
        self.assertEqual(captured.get("hotwords"), ["阿觉"])

    def test_mute_guard_engages_after_reply(self):
        """阿觉说完话要进入静音护栏，否则会自己唤醒自己。"""
        api = FakeApi()
        stop = {}
        fa = FakeAudio([speech_pcm(0.3), silence_pcm(0.5)], stop)
        fasr = FakeASR(["阿觉 帮我看看今天天气"])
        lis = listen.Listener(
            api, tempfile.gettempdir(),
            audio_source=lambda: fa,
            asr_factory=lambda **kw: fasr,
            config_getter=lambda: {"enabled": True, "power_policy": "always",
                                   "wake_words": ["阿觉"]})
        api.on_chat = lambda: stop.update({"stop": True})
        lis.start()
        t0 = time.time()
        while not api.chats and time.time() - t0 < 6:
            time.sleep(0.05)
        lis.stop()
        self.assertTrue(api.chats, "应当触发一次对话")
        self.assertGreater(lis._mute_until, time.time() - 2,
                           "回复后应处于静音护栏窗口内")


class TestDiskConfig(unittest.TestCase):
    """监听配置以**磁盘**为准：intent 里的工具改了它，桌宠要能立刻看见。"""

    def _runner(self):
        import runner
        return runner

    def test_read_write_listen_cfg(self):
        r = self._runner()
        d = tempfile.mkdtemp()
        self.assertTrue(r._write_listen_cfg(
            d, {"enabled": True, "wake_words": ["阿觉", "小阿觉"]}))
        cfg = r._read_listen_cfg(d)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["wake_words"], ["阿觉", "小阿觉"])

    def test_write_preserves_other_config(self):
        r = self._runner()
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ajue_config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"model": "xxx", "diary_time": "09:00"}, f)
        r._write_listen_cfg(d, {"enabled": True})
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["model"], "xxx")
        self.assertEqual(saved["diary_time"], "09:00")
        self.assertTrue(saved["listen"]["enabled"])

    def test_listener_sees_external_change(self):
        """外部进程改了磁盘配置 → 监听线程下一轮就能看到（否则工具等于白做）。"""
        r = self._runner()
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ajue_config.json")

        def write(cfg):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"listen": cfg}, f, ensure_ascii=False)

        write({"enabled": False})
        lis = listen.Listener(None, d,
                              config_getter=lambda: r._read_listen_cfg(d))
        self.assertFalse(lis.cfg()["enabled"])
        write({"enabled": True, "wake_words": ["阿觉", "小阿觉"]})
        self.assertTrue(lis.cfg()["enabled"], "外部改了配置，监听线程要能看见")
        self.assertEqual(lis.cfg()["wake_words"], ["阿觉", "小阿觉"])


if __name__ == "__main__":
    unittest.main()
