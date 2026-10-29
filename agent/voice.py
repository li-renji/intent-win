# -*- coding: utf-8 -*-
"""语音转文字：用 Windows 自带语音识别（System.Speech）把麦克风说话转成文本。

设计取舍
--------
- 不引第三方库（no pip deps）：走 PowerShell + .NET 的 System.Speech，
  Windows 7 起自带，笔记本/台式机开箱即用；
- 语言跟随系统语音包：说中文需要系统里装有中文语音识别语言包
  （设置 → 时间和语言 → 语音 → 添加中文）；否则只能识别英文；
- 单次聆听有超时（默认 8 秒），说完停顿即返回，不会一直挂着；
- 任何环境问题（无麦克风/无语音包/非 Windows）都返回 ok=False + 人话错误，
  UI 据此提示，绝不静默失败。
"""

import json
import os
import subprocess
import sys

# 一次聆听的最长秒数（说话停顿会立即返回，不必等满）
DEFAULT_SECONDS = 8

_PS_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine
$rec.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$rec.SetInputToDefaultAudioDevice()
$global:VoiceText = $null
$global:VoiceErr = $null
try {
    $evt = Register-ObjectEvent -InputObject $rec -EventName SpeechRecognized -Action {
        $global:VoiceText = $EventArgs.Result.Text
    }
    $rec.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple) | Out-Null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while (-not $global:VoiceText -and $sw.Elapsed.TotalSeconds -lt %(seconds)d) {
        Start-Sleep -Milliseconds 120
    }
    $rec.RecognizeAsyncStop() | Out-Null
    try { Unregister-Event -SourceIdentifier $evt.SourceIdentifier -ErrorAction SilentlyContinue } catch {}
} catch {
    $global:VoiceErr = $_.Exception.Message
}
if ($global:VoiceErr) { Write-Output ("ERR:" + $global:VoiceErr) }
elseif ($global:VoiceText) { Write-Output ("OK:" + $global:VoiceText) }
else { Write-Output "TIMEOUT:" }
"""


def _find_powershell():
    """定位 powershell.exe：PATH 找不到时走系统常见绝对路径。"""
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


def _run_ps(script, timeout):
    """跑一段 PowerShell 脚本，返回 (stdout, stderr, returncode)。"""
    ps = _find_powershell()
    if not ps:
        return "", "找不到 powershell.exe（语音识别需要 Windows PowerShell 5.1）", -1
    cmd = [ps, "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-Command", script]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        return "", "PowerShell 执行超时", -2


def probe():
    """探测语音识别是否可用：System.Speech 程序集能否加载。"""
    if sys.platform != "win32":
        return {"ok": False, "detail": "语音识别仅支持 Windows（当前系统 %s）" % sys.platform}
    out, err, rc = _run_ps(
        "try { Add-Type -AssemblyName System.Speech; 'OK' } "
        "catch { 'ERR:' + $_.Exception.Message }", 20)
    if "OK" in out:
        return {"ok": True, "detail": "Windows 语音识别可用（说中文需系统装有中文语音包）"}
    detail = (err or out or "未知错误").strip()[:200]
    return {"ok": False, "detail": "系统语音组件加载失败: %s" % detail}


def listen(seconds=DEFAULT_SECONDS):
    """聆听一次麦克风输入，返回 {"ok", "text", "error"}。

    说完停顿会自动返回；seconds 内没说话返回 timeout（不算错误）。
    """
    if sys.platform != "win32":
        return {"ok": False, "text": "", "error": "语音识别仅支持 Windows"}
    seconds = max(3, min(int(seconds or DEFAULT_SECONDS), 60))
    script = _PS_SCRIPT % {"seconds": seconds}
    out, err, rc = _run_ps(script, seconds + 15)
    line = (out or "").strip().split("\n")[-1] if out else ""
    if line.startswith("OK:"):
        text = line[3:].strip()
        return {"ok": True, "text": text, "error": ""}
    if line.startswith("ERR:"):
        return {"ok": False, "text": "", "error": line[4:].strip()}
    if line.startswith("TIMEOUT"):
        return {"ok": False, "text": "", "error": "timeout",
                "detail": "%d 秒内没有听到声音" % seconds}
    return {"ok": False, "text": "",
            "error": "语音识别失败: %s" % (err or out or "无输出")[:200]}


if __name__ == "__main__":
    if "--probe" in sys.argv:
        print(json.dumps(probe(), ensure_ascii=False))
    elif "--listen" in sys.argv:
        sec = int(sys.argv[sys.argv.index("--listen") + 1]) if len(sys.argv) > \
            sys.argv.index("--listen") + 1 else DEFAULT_SECONDS
        print(json.dumps(listen(sec), ensure_ascii=False))
    else:
        print("用法: python voice.py --probe | --listen [秒数]")
