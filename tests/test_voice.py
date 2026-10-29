# -*- coding: utf-8 -*-
"""语音转文字测试：环境探测与优雅降级。

真实识别需要麦克风 + Windows 语音包，沙箱里无法验证收音，
这里验证：① 探测返回结构正确；② 非 Windows / 异常路径不崩溃、
统一返回 ok=False + 人话错误（UI 据此提示，绝不静默失败）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import voice                                    # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s %s" % ("[PASS]" if cond else "[FAIL]", name, detail))


def run():
    # ---- 探测 ----
    p = voice.probe()
    check("探测：返回 ok 字段", isinstance(p, dict) and "ok" in p)
    check("探测：带说明", bool(p.get("detail")))
    if sys.platform == "win32":
        check("探测：Windows 上尝试加载", isinstance(p.get("ok"), bool))
    else:
        check("探测：非 Windows 明确不可用", p.get("ok") is False)

    # ---- listen 的健壮性（无麦克风环境也必须优雅返回） ----
    r = voice.listen(2)
    check("聆听：返回三字段", set(r) >= {"ok", "text", "error"})
    if not r.get("ok"):
        check("聆听：失败带原因", bool(r.get("error")))
        check("聆听：失败不带半截文本", r.get("text") == "")
    else:
        check("聆听：成功带文本", isinstance(r.get("text"), str))

    # ---- powershell 定位 ----
    ps = voice._find_powershell()
    check("定位：找到 powershell 或明确为空", ps is None or os.path.exists(ps),
          str(ps))

    ok_all = all(r[1] for r in RESULTS)
    print("\n==== test_voice: %d/%d 通过 ====" % (
        sum(1 for r in RESULTS if r[1]), len(RESULTS)))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(run())
