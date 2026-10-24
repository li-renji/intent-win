"""二分定位崩溃阈值：只发 3 个点，避免把 App 反复搞崩。"""
import json, time, urllib.request, urllib.error

PHONE = "http://<手机IP>:9999"
KEY = "<你的API Key>"
FILLER = "这是一段用于测试长度的中文上下文，请在回答时忽略它的具体内容。"


def call(prompt, timeout=120):
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(PHONE + "/chat", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + KEY)
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, round(time.time() - t0, 2), r.read()[:80].decode("utf-8", "ignore")
    except Exception as e:
        return False, round(time.time() - t0, 2), "%s" % type(e).__name__


def health():
    try:
        req = urllib.request.Request(PHONE + "/health", headers={"Authorization": "Bearer " + KEY})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
            return "uptime=%s chats=%s errors=%s" % (d.get("uptime"), d.get("chats"), d.get("errors"))
    except Exception as e:
        return "UNREACHABLE (%s)" % type(e).__name__


for target in [3200, 3800, 4400]:
    print("health before:", health(), flush=True)
    prompt = ("请用一句话回答：1+1等于几？\n" + FILLER * 400)[:target]
    ok, dt, msg = call(prompt)
    print("len=%5d -> %s %6.2fs %s" % (len(prompt), "OK " if ok else "CRASH", dt, msg), flush=True)
    time.sleep(3)
    print("health after :", health(), flush=True)
