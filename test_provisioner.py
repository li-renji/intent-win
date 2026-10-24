"""验证 ModelProvisioner 的核心算法（续传 / 镜像切换 / 大小校验 / sha256）。
用 mock 镜像源 + 人工模拟"下载中断"场景。
"""
import hashlib
import json
import os
import shutil
import sys
import urllib.request
import urllib.error

MIRROR_PORT = 19981
WORK = r"C:/Users/renji/AppData/Local/Temp/prov_test"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 16)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download_one(url, target, expected, sha=None, fail_after=None):
    """复刻 ModelProvisioner.downloadOne 的逻辑"""
    part = target + ".part"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    resume = os.path.getsize(part) if os.path.exists(part) else 0
    if expected and resume > expected:
        os.remove(part)
        resume = 0
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "QidianMiniOS/1.0")
    if resume > 0:
        req.add_header("Range", "bytes=%d-" % resume)
    got_total = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            code = r.status
            if code not in (200, 206):
                raise RuntimeError("HTTP %s" % code)
            if code == 200 and resume > 0:
                resume = 0
            with open(part, "ab" if resume > 0 else "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got_total += len(chunk)
                    if fail_after and got_total >= fail_after:
                        raise IOError("模拟网络中断")
    except Exception as e:
        raise e
    if expected and os.path.getsize(part) != expected:
        raise RuntimeError("文件大小不符：期望 %d 实际 %d" % (expected, os.path.getsize(part)))
    if sha:
        actual = sha256(part)
        if actual.lower() != sha.lower():
            os.remove(part)
            raise RuntimeError("sha256 校验失败")
    if os.path.exists(target):
        os.remove(target)
    shutil.move(part, target)


def provision(mirrors, files, stop_after=None, label=""):
    """复刻 ModelProvisioner.provision() 的镜像切换逻辑：
       外层遍历镜像源，内层每个镜像重试 3 次，全失败才切到下一个源。
    """
    mirror_idx = 0
    for spec in files:
        target = os.path.join(WORK, spec["path"])
        if os.path.exists(target) and spec.get("bytes") and os.path.getsize(target) == spec["bytes"]:
            print("  [skip] 已存在 %s" % spec["path"])
            continue
        ok = False
        err = None
        while mirror_idx < len(mirrors):
            mirror = mirrors[mirror_idx]
            url = mirror.rstrip("/") + "/" + spec["path"]
            attempt = 0
            while attempt < 3:
                attempt += 1
                try:
                    print("  [try] %s <- %s (第%d次)" % (spec["path"], mirror, attempt))
                    download_one(url, target, spec.get("bytes", 0), spec.get("sha256"),
                                 fail_after=stop_after)
                    ok = True
                    break
                except Exception as e:
                    err = e
                    print("  [fail] %s: %s" % (type(e).__name__, e))
            if ok:
                break
            mirror_idx += 1
            if mirror_idx < len(mirrors):
                print("  [switch] 切换镜像源 -> %s" % mirrors[mirror_idx])
        if not ok:
            print("  [FATAL] %s 全部镜像源失败: %s" % (spec["path"], err))
            return False
    return True


def main():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)

    # 用例直接使用 mock 镜像实际提供的文件（否则 sha256 必然对不上）
    src = os.environ.get("MIRROR_SRC", r"C:/Users/renji/AppData/Local/Temp/mirror_src")
    if not os.path.isdir(src):
        print("!! 找不到镜像源目录:", src)
        return False
    files_spec = []
    for name in sorted(os.listdir(src)):
        p = os.path.join(src, name)
        if not os.path.isfile(p):
            continue
        files_spec.append({
            "path": name,
            "bytes": os.path.getsize(p),
            "sha256": sha256(p),
        })
    payload = open(os.path.join(src, "llm.mnn.weight"), "rb").read()

    manifest = {"files": files_spec}
    mpath = os.path.join(WORK, "model_manifest.json")
    json.dump(manifest, open(mpath, "w"))

    # 镜像 1 = 正常源；镜像 2 = 兜底源
    good = "http://127.0.0.1:%d" % MIRROR_PORT
    mirrors = [good]

    print("=== 用例1：正常下载（无 sha256 字段，靠 manifest 里的） ===")
    ok = provision(mirrors, files_spec, label="normal")
    print("  结果:", "OK" if ok else "FAIL")
    assert ok
    assert os.path.getsize(os.path.join(WORK, "llm.mnn.weight")) == files_spec[1]["bytes"]
    print("  ✓ 文件大小正确")

    print()
    print("=== 用例2：中途断网 → .part 保留 → 重试从断点续传 ===")
    for f in files_spec:
        t = os.path.join(WORK, f["path"])
        if os.path.exists(t):
            os.remove(t)
    part = os.path.join(WORK, "llm.mnn.weight.part")
    if os.path.exists(part):
        os.remove(part)
    # 先手动造一个断点文件（模拟上次下载到一半）
    with open(part, "wb") as f:
        f.write(payload[:100 * 1024])
    print("  预置断点文件: 100KB / 共 %dKB" % (len(payload) // 1024))
    ok = provision(mirrors, files_spec)
    print("  结果:", "OK" if ok else "FAIL")
    assert ok
    got = os.path.getsize(os.path.join(WORK, "llm.mnn.weight"))
    print("  ✓ 续传后完整: %d 字节" % got)
    assert got == len(payload)
    assert sha256(os.path.join(WORK, "llm.mnn.weight")) == files_spec[1]["sha256"]
    print("  ✓ sha256 匹配")

    print()
    print("=== 用例3：sha256 不匹配 → 删除损坏文件并报错 ===")
    bad = dict(files_spec[1])
    bad["sha256"] = "0" * 64
    try:
        download_one(good + "/llm.mnn.weight",
                     os.path.join(WORK, "llm.mnn.weight"), bad["bytes"], bad["sha256"])
        print("  结果: FAIL（未报错，不符合预期）")
    except RuntimeError as e:
        print("  ✓ 正确报错:", e)
        assert not os.path.exists(os.path.join(WORK, "llm.mnn.weight.part")), "损坏文件应被删除"

    print()
    print("=== 用例4：镜像 1 挂掉 → 自动切到镜像 2 ===")
    bad_mirror = "http://127.0.0.1:19999"   # 无人监听
    for f in files_spec:
        t = os.path.join(WORK, f["path"])
        if os.path.exists(t):
            os.remove(t)
    ok = provision([bad_mirror, good], files_spec)
    print("  结果:", "OK" if ok else "FAIL")
    assert ok
    print("  ✓ 镜像切换成功")

    print()
    print("=== 用例5：所有镜像都挂 → 明确失败并报错 ===")
    for f in files_spec:                       # 清掉已下载文件，确保真的走下载路径
        t = os.path.join(WORK, f["path"])
        if os.path.exists(t):
            os.remove(t)
    ok = provision([bad_mirror], files_spec)
    print("  结果:", "FAIL(符合预期)" if not ok else "FAIL(不应成功)")
    assert not ok

    shutil.rmtree(WORK, ignore_errors=True)
    print()
    print("=== 全部用例通过 ===")


if __name__ == "__main__":
    main()
