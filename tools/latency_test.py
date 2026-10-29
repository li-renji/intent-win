"""测试手机本地 AI 对不同 prompt 长度的响应时间，并检测闪退。"""
import json, time, urllib.request, urllib.error, os

PHONE = "http://192.168.3.4:9999"
KEY = (os.environ.get("PHONE_KEY") or "").strip() or (open(pkp).read().strip() if os.path.exists(pkp := os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone.key")) else "")
FILLER = "这是一段用于测试长度的中文上下文，请在回答时忽略它的具体内容。"


def call(prompt, timeout=120):
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(PHONE + "/chat", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + KEY)
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, round(time.time() - t0, 2), r.read()[:120].decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return False, round(time.time() - t0, 2), "HTTP %d %s" % (e.code, e.read()[:100].decode("utf-8", "ignore"))
    except Exception as e:
        return False, round(time.time() - t0, 2), "%s: %s" % (type(e).__name__, str(e)[:100])


def health():
    try:
        req = urllib.request.Request(PHONE + "/health", headers={"Authorization": "Bearer " + KEY})
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception as e:
        return "UNREACHABLE (%s)" % type(e).__name__


print("起始 health:", health(), flush=True)
for n in [10, 100, 500, 1000, 2000, 4000, 8000, 16000]:
    prompt = ("请用一句话回答：1+1等于几？\n" + FILLER * (n // 25))[: n * 2]
    ok, dt, msg = call(prompt)
    print("len=%6d chars -> %s %6.2fs  %s" % (len(prompt), "OK " if ok else "ERR", dt, msg.replace("\n", " ")[:90]), flush=True)
    print("   health:", health()[:150], flush=True)
