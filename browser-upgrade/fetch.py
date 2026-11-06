import json
import subprocess
import sys
import urllib.request

SKILL = "C:/Users/人机/AppData/Local/Programs/WorkBuddy/resources/app.asar.unpacked/resources/plugins/workbuddy-builtin/skills/library"

# 1) fresh download link
r = subprocess.run(
    [sys.executable, f"{SKILL}/drive/get_download_link.py", "--node-id", "51LgBc7tW2Ea1bUUE5kfzd"],
    capture_output=True, text=True,
)
print("=== get_download_link stdout ===")
print(r.stdout)
print("=== stderr ===", r.stderr[:300])
if r.returncode != 0:
    print("LINK_FAILED"); sys.exit(1)

url = None
for line in r.stdout.splitlines():
    if line.startswith("KS_DRIVE_DOWNLOAD"):
        idx = line.find("{")
        payload = line[idx:]
        url = json.loads(payload)["download_url"]
        break
if not url:
    print("NO_URL"); sys.exit(1)

# 2) download immediately
out = "D:/INTENT-win/browser-upgrade/browser-upgrade.zip"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=120) as resp:
    data = resp.read()
with open(out, "wb") as f:
    f.write(data)
print("=== downloaded bytes:", len(data))

# 3) sniff
magic = data[:4]
print("is_zip:", magic == b"PK\x03\x04")
if magic != b"PK\x03\x04":
    print("FIRST 300 BYTES:\n", data[:300].decode("utf-8", "replace"))
