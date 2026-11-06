# -*- coding: utf-8 -*-
"""阿觉的「声音」：文本转语音（TTS）。

这一版重点解决两件事
--------------------
1. **播不出来**：旧实现在 Windows 上用 ``System.Media.SoundPlayer`` 去放 mp3，
   而 SoundPlayer **只认 wav**，所以「合成成功、但听不见」。现在改成优先用
   ``winmm`` 的 MCI 接口播放（支持 mp3），中文路径也不会被截断。
2. **音色太单一**：除了微软 Edge 的免费在线音色，新增了一类通用通道——
   **OpenAI 兼容的 /v1/audio/speech**（硅基流动、火山方舟、302.AI、OpenAI…
   都提供这种接口，很多还有免费额度），填好地址/密钥/模型/音色即可用。

配置落在 ``plugin_data/ajue/tts.json``，桌宠的「语音」面板可以直接改。

典型用法：
    import tts
    tts.set_data_dir(DATA_DIR)
    tts.speak("今天也要照顾好自己呀。")
    tts.speak_async("好的，我记下来了。")
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

# 微软 Edge 在线音色（免费，音质最自然）
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

_lock = threading.Lock()
_http_lock = threading.Lock()

_DATA_DIR = None
_cfg_cache = None

# HTTP 超时（秒）
HTTP_TIMEOUT = 60


# --------------------------------------------------------------------------
# 供应商预设：都是「免费或有大额免费额度、便宜」的一档。
# base_url 填到能拼出 `<base_url>/audio/speech` 的那一级（通常是以 /v1 结尾）。
# 具体可用的 model / voice 以厂商当期文档为准，填错时错误信息会原样带回。
# --------------------------------------------------------------------------
PRESETS = [
    {
        "id": "edge",
        "name": "微软 Edge 在线语音（免费）",
        "kind": "edge",
        "base_url": "",
        "model": "",
        "voice": DEFAULT_VOICE,
        "note": "免密钥、免注册，音色最自然；需要联网。",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动 SiliconFlow（有免费额度）",
        "kind": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "FunAudioLLM/CosyVoice2-0.5B",
        "voice": "anna",
        "voices": ["anna", "alex", "bella", "benjamin", "charles", "claire",
                   "david", "diana"],
        "note": "注册即送额度，音色偏日常口语。",
    },
    {
        "id": "volcark",
        "name": "火山方舟 · 豆包语音",
        "kind": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-tts",
        "voice": "zh_female_wanwanxiaohe_mars_bigtts",
        "note": "豆包同款语音，按量计费很便宜；model 需填方舟上的接入点 ID。",
    },
    {
        "id": "openai",
        "name": "OpenAI 官方 TTS",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini-tts",
        "voice": "alloy",
        "voices": ["alloy", "ash", "coral", "echo", "fable", "onyx",
                   "nova", "sage", "shimmer"],
        "note": "效果最好，但需付费且需自行解决网络可达。",
    },
    {
        "id": "custom",
        "name": "自定义（其他 OpenAI 兼容厂商）",
        "kind": "openai",
        "base_url": "",
        "model": "",
        "voice": "",
        "note": "任何提供 POST <base>/audio/speech 的服务都能填进来。",
    },
    {
        "id": "sapi",
        "name": "Windows 系统语音（离线兜底）",
        "kind": "sapi",
        "base_url": "",
        "model": "",
        "voice": "",
        "note": "不联网也能说话，音色偏机械；仅在 Windows 上可用。",
    },
]

# edge-tts 里比较常用、且中文友好的一批音色
EDGE_ZH_VOICES = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-YunjianNeural",
    "zh-CN-YunxiaNeural", "zh-CN-YunyangNeural", "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural", "zh-HK-WanLungNeural", "zh-TW-HsiaoChenNeural",
    "zh-CN-XiaoyiNeural", "zh-CN-YunyeNeural",
]


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------
DEFAULTS = {
    "provider": "auto",        # auto | edge | openai | sapi | off
    "preset": "edge",          # 当前套用的预设（只是为了方便回填）
    "voice": DEFAULT_VOICE,
    "rate": "+0%",             # edge 专用语速
    "volume": "+0%",           # edge 专用音量
    "openai": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "voice": "",
        "format": "mp3",
        "speed": 1.0,
    },
}


def set_data_dir(path):
    """指定插件数据目录（tts.json 存在这里）。"""
    global _DATA_DIR, _cfg_cache
    _DATA_DIR = path
    _cfg_cache = None


def _cfg_path():
    d = _DATA_DIR or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "tts.json")


def _deep_merge(base, patch):
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(refresh=False):
    global _cfg_cache
    if _cfg_cache is not None and not refresh:
        return _cfg_cache
    cfg = json.loads(json.dumps(DEFAULTS))          # 深拷贝
    try:
        with open(_cfg_path(), encoding="utf-8") as f:
            cfg = _deep_merge(cfg, json.load(f))
    except Exception:
        pass
    _cfg_cache = cfg
    return cfg


def save_config(patch):
    """合并写入并返回最新配置。找不到数据目录也不抛异常。"""
    global _cfg_cache
    cfg = _deep_merge(load_config(refresh=True), patch or {})
    _cfg_cache = cfg
    try:
        with open(_cfg_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cfg


def _host_api_key():
    """OpenAI 兼容通道没填密钥时，借用宿主 Agent 已配好的那把（同一个生态）。"""
    try:
        from agent.config import load_config as _host_cfg
        return str(_host_cfg().get("api_key") or "")
    except Exception:
        return ""


def openai_cfg():
    """返回 OpenAI 兼容通道的完整参数（含密钥补全）。"""
    cfg = load_config()
    oa = dict(cfg.get("openai") or {})
    key = (oa.get("api_key") or "").strip()
    if not key:
        key = _host_api_key()
    return {
        "base_url": (oa.get("base_url") or "").strip().rstrip("/"),
        "api_key": key,
        "model": (oa.get("model") or "").strip(),
        "voice": (oa.get("voice") or "").strip(),
        "format": (oa.get("format") or "mp3").strip() or "mp3",
        "speed": float(oa.get("speed") or 1.0),
        "key_source": "plugin" if (oa.get("api_key") or "").strip()
                      else ("host" if key else "none"),
    }


# --------------------------------------------------------------------------
# 可用性
# --------------------------------------------------------------------------
def edge_available():
    try:
        import importlib
        importlib.import_module("edge_tts")
        return True
    except Exception:
        return False


def _find_powershell():
    import shutil
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    if exe:
        return exe
    windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    for cand in (os.path.join(windir, "System32", "WindowsPowerShell", "v1.0",
                              "powershell.exe"),
                 os.path.join(windir, "SysNative", "WindowsPowerShell", "v1.0",
                              "powershell.exe")):
        if os.path.exists(cand):
            return cand
    return None


def sapi_available():
    return sys.platform == "win32" and _find_powershell() is not None


def openai_available():
    c = openai_cfg()
    return bool(c["base_url"] and c["api_key"] and c["model"])


def provider_chain():
    """按当前 provider 设置，返回真正会依次尝试的通道列表。"""
    p = (load_config().get("provider") or "auto").strip().lower()
    if p in ("off", "none", "disable"):
        return []
    if p == "edge":
        return ["edge", "sapi"]
    if p == "openai":
        return ["openai", "edge", "sapi"]
    if p == "sapi":
        return ["sapi"]
    # auto
    chain = []
    if openai_available():
        chain.append("openai")
    if edge_available():
        chain.append("edge")
    if sapi_available():
        chain.append("sapi")
    return chain


def available():
    return bool(provider_chain())


def info():
    """给 UI 用的一份快照。"""
    cfg = load_config()
    oa = openai_cfg()
    return {
        "available": available(),
        "provider": cfg.get("provider", "auto"),
        "preset": cfg.get("preset", "edge"),
        "voice": cfg.get("voice", DEFAULT_VOICE),
        "rate": cfg.get("rate", "+0%"),
        "volume": cfg.get("volume", "+0%"),
        "presets": PRESETS,
        "openai": {k: v for k, v in oa.items() if k != "api_key"},
        "openai_has_key": bool(oa.get("api_key")),
        "edge": edge_available(),
        "sapi": sapi_available(),
        "openai_ready": openai_available(),
        "chain": provider_chain(),
        "config_path": _cfg_path(),
    }


# --------------------------------------------------------------------------
# 播放：Windows 走 winmm MCI（支持 mp3 + 中文路径），其它平台尽力而为
# --------------------------------------------------------------------------
def _ext_for(fmt):
    fmt = (fmt or "mp3").lower()
    return "." + ("mp3" if fmt in ("mp3", "mpeg") else fmt)


def _write_temp(data, ext):
    fd, path = tempfile.mkstemp(suffix=ext, prefix="ajue_tts_")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _play_mci(path):
    """Windows：用 winmm 的 MCI 播放 mp3/wav。返回 (ok, detail)。"""
    if sys.platform != "win32":
        return False, "MCI 仅支持 Windows"
    try:
        import ctypes
        winmm = ctypes.windll.winmm
        winmm.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                         ctypes.c_uint, ctypes.c_void_p]
        winmm.mciSendStringW.restype = ctypes.c_uint

        alias = "ajue_%d_%d" % (os.getpid(), int(time.time() * 1000) % 100000)

        def cmd(s):
            buf = ctypes.create_unicode_buffer(512)
            code = winmm.mciSendStringW(s, buf, 511, None)
            return code, buf.value

        ext = os.path.splitext(path)[1].lower()
        mci_type = "waveaudio" if ext == ".wav" else "mpegvideo"
        code, msg = cmd('open "%s" type %s alias %s' % (path, mci_type, alias))
        if code:
            return False, "MCI 打不开音频（%d %s）" % (code, msg)
        try:
            winmm.mciSendStringW("set %s time format milliseconds" % alias,
                                 None, 0, None)
            _dur = ctypes.create_unicode_buffer(64)
            winmm.mciSendStringW("status %s length" % alias, _dur, 63, None)
            try:
                length_ms = int(_dur.value or 0)
            except ValueError:
                length_ms = 0
            limit = 240000 if not length_ms else int(length_ms * 3 + 15000)

            code, msg = cmd("play %s from 0 notify" % alias)
            if code:
                return False, "MCI 播放失败（%d %s）" % (code, msg)

            # 轮询播放状态：既知道什么时候放完，也能在卡住时超时退出
            waited, step = 0, 100
            while waited < limit:
                buf = ctypes.create_unicode_buffer(64)
                winmm.mciSendStringW("status %s mode" % alias, buf, 63, None)
                mode = (buf.value or "").strip().lower()
                if mode in ("stopped", "not ready", "") and waited > 300:
                    break
                time.sleep(step / 1000.0)
                waited += step
            return True, "已播放"
        finally:
            try:
                cmd("stop %s" % alias)
            except Exception:
                pass
            cmd("close %s" % alias)
    except Exception as e:
        return False, "MCI 播放异常：%s" % e


def _play_file(path):
    """跨平台播放音频文件：优先能同步等待的方式（避免还没放完就被删）。"""
    try:
        if sys.platform == "win32":
            ok, detail = _play_mci(path)
            if ok:
                return ok, detail
            # 兜底：wav 还能用 SoundPlayer
            if os.path.splitext(path)[1].lower() == ".wav":
                try:
                    p = subprocess.Popen(
                        ["powershell", "-NoProfile", "-NonInteractive",
                         "-Command",
                         "(New-Object Media.SoundPlayer '%s').PlaySync()"
                         % path.replace("'", "''")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    p.wait(timeout=180)
                    return True, "已播放（SoundPlayer 兜底）"
                except Exception as e:
                    return False, "%s；SoundPlayer 兜底也失败：%s" % (detail, e)
            return False, detail
        elif sys.platform == "darwin":
            subprocess.run(["afplay", path], check=False, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "已播放"
        else:
            for cmd in (["ffplay", "-nodisp", "-autoexit", "-loglevel",
                         "quiet", path],
                        ["mpg123", "-q", path],
                        ["paplay", path],
                        ["xdg-open", path]):
                try:
                    subprocess.run(cmd, check=False, timeout=180,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    return True, "已播放（%s）" % cmd[0]
                except Exception:
                    continue
            return False, "没有可用的播放器（可安装 ffmpeg / mpg123）"
    except Exception as e:
        return False, "播放失败：%s" % e


# --------------------------------------------------------------------------
# 合成：edge / openai / sapi
# --------------------------------------------------------------------------
def _synth_edge(text, voice, rate, volume):
    """合成到内存字节：成功返回 (data, ext, detail)，失败返回 (None, '', msg)。"""
    try:
        import asyncio
        import edge_tts
    except Exception as e:
        return None, "", "未安装 edge-tts：pip install edge-tts（%s）" % e
    try:
        async def _run():
            comm = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
            chunks = []
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        with _http_lock:
            data = asyncio.run(_run())
        if not data:
            return None, "", "edge-tts 没有返回音频数据"
        return data, ".mp3", "edge-tts"
    except Exception as e:
        return None, "", "edge-tts 合成失败：%s" % e


def _synth_openai(text):
    """OpenAI 兼容 /v1/audio/speech。"""
    c = openai_cfg()
    if not (c["base_url"] and c["model"]):
        return None, "", "还没填 接口地址 / 模型"
    key = c["api_key"]
    if not key:
        return None, "", "还没填 API Key（可留空借用宿主 Agent 的密钥）"
    import urllib.request

    url = c["base_url"] + "/audio/speech"
    payload = {
        "model": c["model"],
        "input": text,
        "voice": c["voice"] or "alloy",
        "response_format": c["format"],
    }
    try:
        sp = float(c["speed"])
        if abs(sp - 1.0) > 1e-6:
            payload["speed"] = sp
    except Exception:
        pass
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % key,
                 "Accept": "audio/*"})
    # 走厂商外网时**不要**绕过代理：国内访问海外接口常常需要环境变量里的代理
    try:
        with _http_lock:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = resp.read()
                ctype = (resp.headers.get("Content-Type") or "").lower()
    except Exception as e:
        detail = getattr(e, "read", None)
        extra = ""
        if detail:
            try:
                extra = " · " + detail().decode("utf-8", "ignore")[:200]
            except Exception:
                extra = ""
        return None, "", "TTS 接口报错：%s%s" % (e, extra)

    if not data:
        return None, "", "TTS 接口返回空内容"
    if b"<html" in data[:200].lower() or data[:1] == b"{":
        return None, "", "TTS 接口返回的不是音频（多半是地址/模型/密钥不对）：%s" \
                         % data[:120].decode("utf-8", "ignore")

    if "wav" in ctype:
        ext = ".wav"
    elif "ogg" in ctype or "opus" in ctype:
        ext = ".ogg"
    elif "flac" in ctype:
        ext = ".flac"
    else:
        ext = _ext_for(c["format"])
    return data, ext, "TTS 接口"


def _speak_sapi(text):
    """Windows 离线兜底：System.Speech 直接朗读（不产生文件）。"""
    if sys.platform != "win32":
        return False, "SAPI 仅支持 Windows"
    ps = _find_powershell()
    if not ps:
        return False, "找不到 powershell.exe"
    safe = (text or "").replace("'", "''").replace('"', "'")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female,"
        " [System.Speech.Synthesis.VoiceAge]::Adult, 0,"
        " (New-Object System.Globalization.CultureInfo('zh-CN'))) } catch {} ; "
        "$s.Rate = 1; "
        "$s.Speak('%s')" % safe
    )
    try:
        p = subprocess.Popen([ps, "-NoProfile", "-NonInteractive", "-Command",
                              script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.wait(timeout=180)
        return True, "已用系统语音朗读"
    except Exception as e:
        return False, "SAPI 朗读失败：%s" % e


# --------------------------------------------------------------------------
# 对外主入口
# --------------------------------------------------------------------------
def _strip_md(text):
    """去掉 markdown 噪音，让朗读不像在读代码。"""
    t = text
    t = re.sub(r"```[\s\S]*?```", "。", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"\*(.+?)\*", r"\1", t)
    t = re.sub(r"#{1,6}\s*", "", t)
    t = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", t)
    t = re.sub(r"^\s*[>#*\-|]+\s*", "", t, flags=re.M)
    t = re.sub(r"\n{2,}", "。", t)
    t = t.replace("\n", "，")
    return t.strip() or text


def speak(text, voice=None):
    """朗读一段文本。返回 (ok, detail)。

    内部按 provider_chain() 依次尝试：配置里的厂商 → edge-tts → 系统语音。
    """
    text = (text or "").strip()
    if not text:
        return False, "空文本"
    clean = _strip_md(text)[:1500]
    cfg = load_config()
    voice = voice or cfg.get("voice") or DEFAULT_VOICE

    errors = []
    for ch in provider_chain():
        if ch == "openai":
            data, ext, who = _synth_openai(clean)
            if data:
                path = _write_temp(data, ext)
                try:
                    ok, detail = _play_file(path)
                finally:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                if ok:
                    return True, "%s 合成并播放成功" % who
                errors.append("%s：%s" % (who, detail))
                continue
            errors.append("OpenAI 兼容通道：%s" % who)
            continue
        if ch == "edge":
            data, ext, who = _synth_edge(clean, voice,
                                         cfg.get("rate") or "+0%",
                                         cfg.get("volume") or "+0%")
            if data:
                path = _write_temp(data, ext)
                try:
                    ok, detail = _play_file(path)
                finally:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                if ok:
                    return True, "已用「%s」开口（%s）" % (voice, who)
                errors.append("%s：%s" % (who, detail))
                continue
            errors.append(who)
            continue
        if ch == "sapi":
            ok, detail = _speak_sapi(clean)
            if ok:
                return True, detail
            errors.append(detail)

    if not errors:
        return False, ("当前没有任何可用 TTS：Windows 系统语音也不可用。"
                       "可 pip install edge-tts，或在桌宠「语音」里"
                       "填一家 OpenAI 兼容厂商。")
    return False, "；".join(errors)


def speak_async(text, voice=None):
    threading.Thread(target=lambda: speak(text, voice), daemon=True).start()
    return True, "已在后台朗读"


# --------------------------------------------------------------------------
# 音色列表
# --------------------------------------------------------------------------
def list_voices():
    """给 UI 用：[{id, name, note}]。"""
    out = []
    chain = provider_chain()
    if "openai" in chain:
        for v in _openai_voices():
            out.append({"id": v, "name": v, "kind": "API"})
    if "edge" in chain or edge_available():
        if edge_available():
            try:
                import asyncio
                import edge_tts

                async def _run():
                    vs = await edge_tts.list_voices()
                    return [{"id": v["ShortName"],
                             "name": v.get("FriendlyName", v["ShortName"]),
                             "kind": "Edge"}
                            for v in vs if str(v.get("Locale", ""))
                            .lower().startswith("zh")]
                out.extend(asyncio.run(_run()))
                return out
            except Exception:
                pass
        for v in EDGE_ZH_VOICES:
            out.append({"id": v, "name": v, "kind": "Edge"})
    if not out and sapi_available():
        out.append({"id": "sapi", "name": "系统默认语音", "kind": "系统"})
    return out


def _openai_voices():
    c = openai_cfg()
    vs = []
    for p in PRESETS:
        if p.get("kind") == "openai" and p.get("base_url") \
                and c["base_url"].rstrip("/") == p["base_url"].rstrip("/"):
            vs.extend(p.get("voices") or [])
    cur = c.get("voice")
    if cur and cur not in vs:
        vs.insert(0, cur)
    return vs


# --------------------------------------------------------------------------
def apply_preset(preset_id):
    """套用一个预设（只覆盖通道相关的几个字段）。"""
    pid = (preset_id or "").strip()
    for p in PRESETS:
        if p.get("id") == pid:
            kind = p.get("kind")
            if kind == "openai":
                patch = {
                    "preset": pid,
                    "provider": "openai",
                    "openai": {
                        "base_url": p.get("base_url", ""),
                        "model": p.get("model", ""),
                        "voice": p.get("voice", ""),
                        "format": "mp3",
                    },
                }
            elif kind == "sapi":
                patch = {"preset": pid, "provider": "sapi"}
            else:
                patch = {"preset": pid, "provider": "edge",
                         "voice": p.get("voice") or DEFAULT_VOICE}
            return {"ok": True, "config": save_config(patch)}
    return {"ok": False, "error": "没有这个预设：%s" % pid}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="阿觉 TTS 自检")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--say", default=None)
    ap.add_argument("--info", action="store_true")
    a = ap.parse_args()
    set_data_dir(a.data_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "plugin_data", "ajue"))
    if a.info:
        print(json.dumps(info(), ensure_ascii=False, indent=2))
    elif a.say:
        print(speak(a.say))
    else:
        print(json.dumps({"available": available(), "chain": provider_chain()},
                         ensure_ascii=False))
