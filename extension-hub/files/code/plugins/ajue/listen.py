# -*- coding: utf-8 -*-
"""阿觉的「耳朵」：常驻实时监听 + 两层唤醒。

设计目标（来自产品需求）
------------------------
1. **长时间在线**：插着充电器时后台常驻实时监听；未插电自动暂停（省电）。
2. **两层唤醒**（兼顾体验与开销）：
   - 第一层（极低功耗）：未唤醒时只做「语音帧静默检测」（VAD），**不解析内容**，
     不跑 ASR，因此几乎不占 CPU / 电量。
   - 第二层（语义确认）：检测到人声后，取一小段用 faster-whisper 检测唤醒词
     （默认「阿觉 / 阿觉阿觉」，可配置）；命中后继续录整句，转写完成后用
     轻量语义判断确认「这真是在叫我、且需要回应」，避免把「和别人聊天 /
     电视声」误判成指令。
3. **唤醒后完整承接能力**：确认唤醒 → 调 ``api.chat(text)``，复用已有的
   流式回复 + 软化人格 + 时间感知 + TTS 播报；如需执行快捷动作也走同一管线。
4. **隐身运行**：全程无主动弹窗，仅在收到有效指令 / 主动问询时通过 TTS 输出。

工程约束
--------
- 重依赖（sounddevice / webrtcvad / faster-whisper）一律**延迟导入**：只有真正
  start() 且启用监听时才 import；未启用时本模块可被任何地方 import 而不报错，
  也不会拖垮宿主 / 桌宠的启动。
- 音频源（AudioSource）与识别器（ASR）都是**可注入**的，单测用 mock 注入，
  无需真实麦克风即可覆盖状态机 / 唤醒判断 / 语义判断 / 接线逻辑。
"""

import json
import os
import threading
import time

# 国内直连 HuggingFace 常常不稳定（新版 xet 后端还会直接 401）：
# 默认走国内镜像并关掉 xet 加速后端。必须在 import faster_whisper 之前生效，
# 所以放在模块级——用户若已自己设过这些变量则不覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# --------------------------------------------------------------------------
# 状态常量
# --------------------------------------------------------------------------
STATE_IDLE = "idle"        # 未唤醒：只做 VAD 静默检测，不解析内容
STATE_WAKING = "waking"    # 检测到人声：用短片段检测唤醒词
STATE_LISTENING = "listening"  # 命中唤醒词：继续录整句并转写
STATE_JUDGE = "judge"      # 整句转写完成：语义判断是否真在呼叫阿觉
STATE_ACTIVE = "active"     # 确认唤醒：调 api.chat 输出
STATE_PAUSED = "paused"     # 未插电 / 未启用：暂停监听

# 采样参数（16kHz 单声道 16bit，webrtcvad 与 faster-whisper 都吃这套）
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 4800       # 每次读取的采样数 = 300ms
FRAME_MS = 30              # webrtcvad 帧长
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2   # 960 字节


# --------------------------------------------------------------------------
# 默认配置（可被 ajue_config.json 的 listen.* 覆盖）
# --------------------------------------------------------------------------
def default_config():
    return {
        "enabled": False,                       # 总开关（设置里打开）
        "wake_words": ["阿觉", "阿觉阿觉"],       # 唤醒词（可改、可多个）
        "asr_model": "base",                    # faster-whisper 模型尺寸
        "power_policy": "ac_only",              # ac_only=仅插电监听；always=始终监听
        "vad_aggressiveness": 2,                # 0~3，越大越「宁可漏检也不误检」
        "wake_phrase_seconds": 1.5,             # 唤醒词检测取多长的前缀片段
        "sentence_timeout": 1.2,                # 整句录制：静音多久算说完
        "max_sentence_seconds": 15,             # 单句最长录制（防卡死）
        "semantic_judge": True,                 # 是否做语义二次确认
    }


# --------------------------------------------------------------------------
# 音频源抽象（可注入）。默认实现用 sounddevice（延迟导入）。
# --------------------------------------------------------------------------
class AudioSource:
    """真实麦克风音频源：16kHz / 单声道 / int16 PCM。

    设备原生采样率常见 44.1k/48k。Windows 的共享模式一般能直接按 16k 打开
    （由系统自动重采样，零额外 CPU）；万一打不开，就用原生率采集后本地
    线性重采样到 16k（faster-whisper 与 VAD 都要求 16k）。
    """

    def __init__(self, rate=SAMPLE_RATE, chunk=CHUNK_SAMPLES):
        self.rate = rate
        self.chunk = chunk
        self._stream = None
        self._native = rate

    def __enter__(self):
        import sounddevice as sd
        for cand in (self.rate, 48000, 44100):
            try:
                st = sd.RawInputStream(
                    samplerate=cand, blocksize=self.chunk,
                    dtype="int16", channels=1)
                st.start()
                self._stream = st
                self._native = cand
                break
            except Exception:
                continue
        if self._stream is None:
            raise RuntimeError("没有可用的麦克风输入设备")
        return self

    def read(self, chunk=None):
        """返回一个 chunk 的 int16 PCM 字节（长度 = chunk*2）。"""
        data, _ = self._stream.read(chunk or self.chunk)
        data = bytes(data)
        if self._native != self.rate:
            data = _resample(data, self._native, self.rate)
        return data

    def __exit__(self, *a):
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


def _resample(audio_bytes, src_rate, dst_rate):
    """int16 PCM 线性重采样（把 44.1k/48k 降到 16k）。"""
    if src_rate == dst_rate or not audio_bytes:
        return audio_bytes
    import array
    src = array.array("h")
    src.frombytes(audio_bytes[:len(audio_bytes) // 2 * 2])
    if len(src) < 2:
        return audio_bytes
    ratio = float(dst_rate) / float(src_rate)
    n_dst = max(1, int(len(src) * ratio))
    dst = array.array("h", [0]) * n_dst
    last = len(src) - 1
    for i in range(n_dst):
        x = i / ratio
        i0 = int(x)
        i1 = i0 + 1 if i0 < last else last
        frac = x - i0
        dst[i] = int(src[i0] * (1 - frac) + src[i1] * frac)
    return dst.tobytes()


# --------------------------------------------------------------------------
# ASR 抽象（可注入）。默认实现用 faster-whisper（延迟导入）。
# --------------------------------------------------------------------------
class WhisperASR:
    """faster-whisper 流式（句级）识别器。

    ``hotwords`` 是**热词偏置**：把唤醒词直接喂给模型做解码偏置，能显著提升
    「阿觉」这类专有词的识别正确率（纯靠声学很容易听成「阿爵」「阿卷」）。
    """

    def __init__(self, model_size="base", hotwords=None):
        self.model_size = model_size
        self.hotwords = [w for w in (hotwords or []) if w]
        self._model = None

    def _ensure(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            # CPU 推理；int8 量化足够日常，速度优先
            self._model = WhisperModel(
                self.model_size, device="cpu",
                compute_type="int8", cpu_threads=max(1, os.cpu_count() or 1))
        return self._model

    def transcribe(self, audio_bytes):
        """audio_bytes: 16k int16 PCM。返回文本（可能为空）。"""
        if not audio_bytes:
            return ""
        import numpy as np
        audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(
            np.float32) / 32768.0
        model = self._ensure()
        kw = {}
        if self.hotwords:
            # faster-whisper 1.x 的 hotwords 只接受字符串（内部会 .strip()），
            # 多个热词用空格连接
            kw["hotwords"] = " ".join(self.hotwords)
        segments, _ = model.transcribe(
            audio_np, language="zh", vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=400),
            # 用一句话引导模型输出简体中文（否则偶尔会吐繁体）
            initial_prompt="以下是普通话对话，请用简体中文转写。",
            **kw)
        text = "".join(s.text for s in segments).strip()
        return _to_simplified(text)


# --------------------------------------------------------------------------
# VAD 工具（语音活动检测：只判断「有没有人在说话」，不解析内容）
#
# 优先用 webrtcvad（更准）；**没装时自动回退到零依赖的能量 VAD**。
# 之所以要有回退：webrtcvad 是 C 扩展，在没装 Windows SDK 的机器上编译不过
# （找不到 io.h），而 VAD 只是「静默检测」这一层，用 RMS 音量 + 过零率
# 完全够用，且零依赖、CPU 开销极小——正合「未唤醒时不解析内容、超低功耗」
# 的要求。
# --------------------------------------------------------------------------
class EnergyVAD:
    """零依赖能量 VAD：RMS 音量 + 过零率（ZCR）。"""

    def __init__(self, aggressiveness=2):
        # aggressiveness 越大越保守：能量阈值抬高、更难被判成语音
        self.energy_threshold = 0.010 * (1.6 ** max(0, int(aggressiveness)))
        self.zcr_min = 0.02
        self.zcr_max = 0.45

    def has_speech(self, audio_bytes, rate=SAMPLE_RATE):
        """整块快速判定：先算 RMS（安静时直接否掉），过阈值再算过零率。"""
        import array
        n = len(audio_bytes) // 2
        if n < 16:
            return False
        s = array.array("h")
        s.frombytes(audio_bytes[:n * 2])
        total = 0
        for v in s:
            total += v * v
        rms = (total / float(n)) ** 0.5 / 32768.0
        if rms < self.energy_threshold:
            return False                      # 安静/底噪 → 直接否掉（最省 CPU）
        crossings = 0
        prev = s[0] < 0
        for i in range(1, n):
            cur = s[i] < 0
            if cur != prev:
                crossings += 1
                prev = cur
        zcr = crossings / float(n)
        return self.zcr_min <= zcr <= self.zcr_max


def _make_vad(aggressiveness=2):
    """优先 webrtcvad；没装就用零依赖能量 VAD。"""
    try:
        import webrtcvad
        return webrtcvad.Vad(aggressiveness)
    except Exception:
        return EnergyVAD(aggressiveness)


def vad_has_speech(vad, audio_bytes, threshold=0.3):
    """判断一段音频里是否「有人说话」（不解析内容，仅静默检测）。"""
    if len(audio_bytes) < FRAME_BYTES:
        return False
    fast = getattr(vad, "has_speech", None)
    if callable(fast):
        try:
            return bool(fast(audio_bytes))
        except Exception:
            return False
    speech = 0
    total = 0
    for i in range(0, len(audio_bytes) - FRAME_BYTES + 1, FRAME_BYTES):
        frame = audio_bytes[i:i + FRAME_BYTES]
        try:
            if vad.is_speech(frame, SAMPLE_RATE):
                speech += 1
        except Exception:
            pass
        total += 1
    return total > 0 and (speech / total) >= threshold


# --------------------------------------------------------------------------
# 电量守护（WMI 查询 Win32_Battery）
# --------------------------------------------------------------------------
def on_ac_power():
    """是否插着外接电源。无电池（台式机）视为插电。异常时保守返回 True。"""
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-WmiObject Win32_Battery | "
             "Select-Object -ExpandProperty BatteryStatus"],
            capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            line = line.strip()
            if line in ("2", "6", "7"):   # 2=外接电源 6/7=全满且接电
                return True
            if line == "1":               # 1=放电（在用电池）
                return False
        return True                       # 无电池对象 → 台式机，视为插电
    except Exception:
        return True


# --------------------------------------------------------------------------
# 唤醒词 / 语义判断
# --------------------------------------------------------------------------
def _pinyin(text):
    """中文转拼音（无声调）。装了 pypinyin 才有效，否则返回 None。"""
    if not text:
        return None
    try:
        from pypinyin import lazy_pinyin
        return "".join(lazy_pinyin(text))
    except Exception:
        return None


def _to_simplified(text):
    """繁体转简体（装了 zhconv 才转，否则原样返回）。"""
    if not text:
        return text
    try:
        from zhconv import convert
        return convert(text, "zh-cn")
    except Exception:
        return text


def contains_wakeword(text, wake_words):
    """唤醒词匹配：**精确 + 拼音同音容错**。

    为什么必须有拼音容错：faster-whisper 常把「阿觉」听成同音的「阿爵」「阿月」
    （真机验证实测过），纯精确匹配会导致**永远唤不醒**。拼音序列相同即命中：
    阿觉 / 阿爵 → ajue。

    这里只做**严格同音**匹配，不做「近似」匹配——实测近似相似度无法可靠区分
    「阿卷」（该命中）与「啊，就是…」（不该命中），与其留一个不可靠的开关、
    白白引入误触发，不如把风险彻底去掉。识别不稳时的正解是：换一个更大的
    ASR 模型，或在设置里把常被听到的写法直接补进唤醒词。
    """
    if not text:
        return False
    t = text.lower()
    tp = None
    for w in wake_words:
        if not w:
            continue
        if w.lower() in t:
            return True
        wp = _pinyin(w)
        if not wp:
            continue
        if tp is None:
            tp = _pinyin(text) or ""
        if tp and wp in tp:
            return True
    return False


def _strip_by_pinyin(text, word, wp):
    """按拼音等值删掉 text 中与 word 同音的那几个字（阿爵 → 阿觉 的容错）。"""
    n = len(word)
    if not text or n == 0:
        return text
    for i in range(len(text) - n + 1):
        if _pinyin(text[i:i + n]) == wp:
            return text[:i] + text[i + n:]
    return text


def strip_wakeword(text, wake_words):
    """去掉文本里出现的唤醒词，返回「真正的指令 / 问题」部分。"""
    t = text
    for w in wake_words:
        if w:
            t = t.replace(w, "")
    # 拼音容错：ASR 听成同音字时，把对应片段也去掉
    for w in wake_words:
        if not w:
            continue
        wp = _pinyin(w)
        if wp:
            t = _strip_by_pinyin(t, w, wp)
    return t.strip(" ，。,.！!？?~～”“\"' \n\t")


def semantic_judge(text, wake_words, llm=None, require_wakeword=True):
    """第二层：确认「这真是在叫阿觉、且需要回应」。

    默认口径（对应「关键词 + 语义确认」方案）：
      - **必须先叫了名字**（第一层已命中），再判断去掉名字后还有没有实质内容；
      - 没叫名字 → 一律不触发。这条很关键：否则「我们明天几点开会」这种
        你跟别人的日常对话会被误判成在叫它（几点/怎么/什么 等疑问词太常见）。

    ``require_wakeword=False`` 时才启用「无唤醒词」模式：只要像在问问题 /
    下指令就触发（更接近「像人一样不用叫名字」，但误触发率更高）。
    """
    stripped = strip_wakeword(text, wake_words)
    if contains_wakeword(text, wake_words):
        return bool(stripped)          # 叫了名字 + 有实质内容 → 确认
    if require_wakeword:
        return False                   # 没叫名字 → 默认不触发（防误唤醒）
    question_cues = ("吗", "？", "?", "怎么", "什么", "为什么", "几点",
                     "多少", "哪", "谁", "能不能", "可以", "帮", "请", "打开",
                     "查一下", "搜", "记一下", "提醒", "设置")
    return any(c in text for c in question_cues)


# --------------------------------------------------------------------------
# 监听守护
# --------------------------------------------------------------------------
class Listener:
    """阿觉常驻监听守护。"""

    def __init__(self, api, data_dir,
                 audio_source=None, asr_factory=None,
                 config_getter=None, config_setter=None,
                 on_state_change=None):
        self.api = api
        self.data_dir = data_dir
        self._cfg_get = config_getter or (
            lambda: (api.core.config.get("listen") if api and api.core
                     else {}))
        self._cfg_set = config_setter or (
            lambda patch: api.set_ajue_config({"listen": patch}) if api else None)
        self._audio_cls = audio_source or AudioSource
        self._asr_factory = asr_factory or (lambda: WhisperASR("base"))
        self._on_state = on_state_change   # 可选：状态变化推给 UI
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = STATE_IDLE
        # 静音护栏：阿觉说话期间忽略麦克风输入（防止自己唤醒自己）
        self._mute_until = 0.0
        self._status = {"running": False, "state": STATE_IDLE,
                        "ac_power": None, "asr_loaded": False,
                        "error": None}

    # ---- 配置 ----
    def cfg(self):
        c = default_config()
        try:
            c.update(self._cfg_get() or {})
        except Exception:
            pass
        # 列表型字段兜底
        if not isinstance(c.get("wake_words"), (list, tuple)) or \
                not c["wake_words"]:
            c["wake_words"] = default_config()["wake_words"]
        return c

    def set_config(self, patch):
        try:
            self._cfg_set(patch or {})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- 生命周期 ----
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return {"ok": True, "already_running": True}
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ajue-listen", daemon=True)
        self._thread.start()
        return {"ok": True}

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3)
        self._thread = None
        self._set_state(STATE_IDLE)
        self._patch_status(running=False)
        return {"ok": True}

    # ---- 状态维护（线程安全） ----
    def _set_state(self, s):
        with self._lock:
            if self.state == s:
                return          # 状态没变就不通知前端（避免每 300ms 刷一次 JS）
            self.state = s
        self._patch_status(state=s)
        if self._on_state:
            try:
                self._on_state(s)
            except Exception:
                pass

    def _patch_status(self, **kw):
        with self._lock:
            self._status.update(kw)

    def status(self):
        with self._lock:
            return dict(self._status)

    # ---- 主循环 ----
    def _run(self):
        self._patch_status(running=True, error=None)
        cfg = self.cfg()
        try:
            asrc = self._audio_cls().__enter__()
        except Exception as e:
            self._patch_status(running=False,
                               error="麦克风打开失败：%s" % e)
            return
        try:
            vad = _make_vad(int(cfg.get("vad_aggressiveness", 2)))
            asr = None  # 懒加载：第一次真正需要识别时才建 Whisper 模型
            while not self._stop.is_set():
                cfg = self.cfg()
                # 未启用 → 暂停
                if not cfg.get("enabled"):
                    self._set_state(STATE_PAUSED)
                    self._patch_status(ac_power=on_ac_power())
                    if self._stop.wait(2):
                        break
                    continue
                # 电量策略：只在「仅插电监听」模式下才查 WMI（避免无谓开销）
                if cfg.get("power_policy", "ac_only") == "ac_only":
                    ac = on_ac_power()
                    self._patch_status(ac_power=ac)
                    if not ac:
                        self._set_state(STATE_PAUSED)
                        if self._stop.wait(30):
                            break
                        continue

                # ---- IDLE：只做 VAD 静默检测，不解析内容 ----
                self._set_state(STATE_IDLE)
                try:
                    chunk = asrc.read()
                except Exception as e:
                    self._patch_status(error="读取麦克风失败：%s" % e)
                    if self._stop.wait(2):
                        break
                    continue
                if time.time() < self._mute_until:
                    continue  # 阿觉正在说话 → 丢弃这段音频（防自唤醒）
                if not vad_has_speech(vad, chunk):
                    continue  # 没声音 → 继续静默检测（极低开销）

                # ---- 检测到人声：第一层唤醒词检测 ----
                self._set_state(STATE_WAKING)
                if asr is None:
                    # 把唤醒词作为热词偏置喂给识别器 → 大幅提升唤醒词识别率
                    try:
                        asr = self._asr_factory(hotwords=cfg.get("wake_words"))
                    except TypeError:
                        asr = self._asr_factory()
                    self._patch_status(asr_loaded=True)
                # 取一段前缀用于检测唤醒词
                prefix = chunk + self._collect_fixed(
                    asrc, vad, float(cfg.get("wake_phrase_seconds", 1.5)))
                wtext = asr.transcribe(prefix)
                if not contains_wakeword(wtext, cfg["wake_words"]):
                    # 不是叫阿觉（可能是和别人说话 / 电视声）→ 回到 IDLE
                    continue

                # ---- 命中唤醒词：继续录整句 ----
                self._set_state(STATE_LISTENING)
                rest = self._collect_until_silence(
                    asrc, vad,
                    timeout=float(cfg.get("sentence_timeout", 1.2)),
                    max_seconds=float(cfg.get("max_sentence_seconds", 15)))
                full = asr.transcribe(prefix + rest)

                # ---- 第二层语义确认 ----
                self._set_state(STATE_JUDGE)
                if not full:
                    continue
                if cfg.get("semantic_judge", True):
                    ok = semantic_judge(full, cfg["wake_words"])
                else:
                    ok = bool(strip_wakeword(full, cfg["wake_words"]))
                if not ok:
                    continue  # 唤醒词被误检 / 没有实质指令 → 丢弃

                # ---- 确认唤醒：交给阿觉主脑 ----
                self._set_state(STATE_ACTIVE)
                reply = ""
                try:
                    reply = self.api.chat(full) or ""
                except Exception as e:
                    self._patch_status(error="对话失败：%s" % e)
                # 静音护栏：阿觉自己说话时麦克风会录到它的声音，不屏蔽就会
                # 形成「自己唤醒自己」的循环。这里按回复字数估算说话时长。
                self._mute_until = time.time() + self._speak_seconds(reply)
        except (KeyboardInterrupt, SystemExit):
            # 正常退出（Ctrl+C / 被要求停止），安静收尾
            pass
        finally:
            try:
                asrc.__exit__(None, None, None)
            except Exception:
                pass
            self._set_state(STATE_IDLE)
            self._patch_status(running=False, asr_loaded=False)

    # ---- 采集辅助 ----
    @staticmethod
    def _speak_seconds(text):
        """估算阿觉把这段话说完要多久（中文语速约 5~6 字/秒）+ 一点余量。"""
        if not text:
            return 1.5
        return min(30.0, len(text) / 5.5 + 1.5)

    def _collect_fixed(self, asrc, vad, seconds):
        """持续采集直到累计 seconds 秒，或中间出现明显静音（人说完唤醒词）。"""
        buf = b""
        steps = max(1, int(seconds * 1000 / (CHUNK_SAMPLES / SAMPLE_RATE * 1000)))
        for _ in range(steps):
            try:
                chunk = asrc.read()
            except Exception:
                break
            buf += chunk
            # 后半段出现静音就提前结束（避免录到过长）
            if len(buf) > FRAME_BYTES * 3 and not vad_has_speech(
                    vad, buf[-FRAME_BYTES * 6:]):
                break
        return buf

    def _collect_until_silence(self, asrc, vad, timeout=1.2, max_seconds=15):
        """持续采集直到连续 timeout 秒无语音，或达到 max_seconds 上限。"""
        buf = b""
        silent_chunks = 0
        need_silent = max(1, int(timeout * 1000 /
                                 (CHUNK_SAMPLES / SAMPLE_RATE * 1000)))
        max_chunks = max(1, int(max_seconds * 1000 /
                                (CHUNK_SAMPLES / SAMPLE_RATE * 1000)))
        spoke = False
        for _ in range(max_chunks):
            try:
                chunk = asrc.read()
            except Exception:
                break
            buf += chunk
            if vad_has_speech(vad, chunk):
                spoke = True
                silent_chunks = 0
            else:
                silent_chunks += 1
                if spoke and silent_chunks >= need_silent:
                    break
        return buf


# --------------------------------------------------------------------------
# 自检（命令行）：python listen.py --probe
# 仅验证模块可导入 / 工具函数正常，不碰麦克风。
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    print("listen 模块导入 OK")
    print("默认配置:", json.dumps(default_config(), ensure_ascii=False))
    print("电量(插电?):", on_ac_power())
    print("唤醒词检测:", contains_wakeword("阿觉帮我查下天气", ["阿觉"]))
    print("语义判断:", semantic_judge("阿觉，今天心情不太好", ["阿觉"]))
    print("语义判断(非指令):", semantic_judge("今天天气真不错啊", ["阿觉"]))
