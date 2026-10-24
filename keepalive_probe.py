import json, time, urllib.request, urllib.error, datetime

PHONE = "http://<手机IP>:9999"
KEY = "<你的API Key>"

def probe(path, timeout=6):
    t0 = time.time()
    try:
        req = urllib.request.Request(PHONE + path, headers={"Authorization": "Bearer " + KEY})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, round(time.time() - t0, 2), r.read()[:160].decode("utf-8", "ignore")
    except Exception as e:
        return False, round(time.time() - t0, 2), type(e).__name__ + ": " + str(e)[:80]

log = []
for i in range(24):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    ok, dt, msg = probe("/health")
    line = f"{ts} health {'OK ' if ok else 'ERR'} {dt}s {msg}"
    print(line, flush=True)
    log.append(line)
    time.sleep(8)

print("\n=== 保活 3 分钟后测对话 ===", flush=True)
ok, dt, msg = probe("/health")
print(f"health {'OK' if ok else 'ERR'} {dt}s {msg}", flush=True)
body = json.dumps({"model": "qwen2.5-1.5b-mnn", "messages": [{"role": "user", "content": "说一句话"}]}).encode()
t0 = time.time()
try:
    req = urllib.request.Request(PHONE + "/v1/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        print(f"chat OK {round(time.time()-t0,2)}s {r.read()[:300].decode('utf-8','ignore')}", flush=True)
except Exception as e:
    print(f"chat ERR {round(time.time()-t0,2)}s {e}", flush=True)

okc = sum(1 for l in log if " OK " in l)
print(f"\n统计: {okc}/{len(log)} 次 health 成功", flush=True)
