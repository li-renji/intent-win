# -*- coding: utf-8 -*-
"""开发者 · 生成云端更新源并推送到 GitHub（「发布新版本.cmd」调用）。

流程：
  1. 重新打一个全新出厂包
  2. 把其中的 code 通道文件镜像到 portable\\release\\
     （runtime/ 底座不上传，约 1.9GB；配置.qdai 等用户数据不上传）
  3. 写 release\\manifest.json（sha256 清单）与 release\\version.json
  4. git 提交 + 用 tools\\api_push.py 经 GitHub Data API 推送到 main
     （git 协议端点在国内常被代理拦，Data API 走 api.github.com 可用）

之后任何 U 盘点 UPDATE.cmd 都会从
    https://raw.githubusercontent.com/li-renji/intent-win/main/portable/release/
拉增量。

用法：
    python portable\\publish.py --stage                 只生成更新源
    python portable\\publish.py --publish --dry-run    只看将要执行的命令
    python portable\\publish.py --publish              真正发布到 GitHub
    python portable\\publish.py --no-build             用现有 payload
"""
import argparse
import json
import os
import subprocess
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.path.join(SRC, "portable")
sys.path.insert(0, PORT)

import manifest as mf

PAYLOAD = os.path.join(PORT, "dist", "INTENT-Portable-Setup", "payload")
RELEASE = os.path.join(PORT, "release")
REPO = "li-renji/intent-win"
RAW = "https://raw.githubusercontent.com/%s/main/portable/release/" % REPO


def stage(payload=PAYLOAD, release=RELEASE):
    m = mf.load(os.path.join(payload, mf.MANIFEST))
    if not m:
        raise SystemExit("[发布] payload 缺 manifest.json，请先运行 factory.py")
    code = {rel: meta for rel, meta in m["files"].items()
            if meta.get("ch") == "code" and not mf.is_user(rel)}
    os.makedirs(release, exist_ok=True)
    old = mf.load(os.path.join(release, mf.MANIFEST)) or {"files": {}}

    removed = 0
    for rel in old.get("files", {}):
        if rel not in code:
            p = os.path.join(release, mf.norm(rel))
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
    copied = 0
    for rel in code:
        s = os.path.join(payload, mf.norm(rel))
        d = os.path.join(release, mf.norm(rel))
        os.makedirs(os.path.dirname(d), exist_ok=True)
        if os.path.isfile(d) and mf.sha256(d) == code[rel]["sha256"]:
            continue
        with open(s, "rb") as f:
            data = f.read()
        tmp = d + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, d)
        copied += 1

    out = {"version": m["version"], "base_version": m["base_version"],
           "files": code, "count": len(code)}
    mf.save(release, out)
    mf.save_version(release, m["version"], m["base_version"], "portable release")
    total = sum(v.get("size", 0) for v in code.values())
    print("[发布] 更新源：%s" % release)
    print("[发布] 版本 %s / 底座 %s / 云端程序文件 %d 个 / %.2f MB"
          % (m["version"], m["base_version"], len(code), total / 1024 ** 2))
    print("[发布] 本次写入 %d 个、清理 %d 个、未变 %d 个"
          % (copied, removed, len(code) - copied))
    print("[发布] 云端地址：%s" % RAW)
    return out


def verify_index_bytes(release=RELEASE, sample=12):
    """git add 之后，抽查索引里的 blob 是否与磁盘逐字节一致。

    若仓库开了 autocrlf / .gitattributes 的 eol 规则，blob 会被改行尾，
    U 盘 UPDATE.cmd 下载后 sha256 校验会全部失败。这里提前拦住。
    """
    m = mf.load(os.path.join(release, mf.MANIFEST)) or {"files": {}}
    paths = [k for k in m["files"] if os.path.splitext(k)[1].lower()
             in (".cmd", ".py", ".txt", ".vbs", ".inf", ".json")]
    paths += ["manifest.json", "version.json"]
    bad = []
    for rel in paths[:sample] + paths[-sample:]:
        git_path = "portable/release/" + mf.norm(rel)
        r = subprocess.run(["git", "show", ":" + git_path], cwd=SRC,
                           capture_output=True)
        if r.returncode != 0:
            continue                    # 没进索引，稍后 add 会处理
        disk = os.path.join(release, mf.norm(rel))
        if os.path.isfile(disk) and r.stdout != open(disk, "rb").read():
            bad.append(rel)
    return bad


def publish(dry=False):
    m = mf.load(os.path.join(RELEASE, mf.MANIFEST))
    if not m:
        raise SystemExit("[发布] 请先 --stage")
    msg = "chore: portable release %s" % m["version"]
    cmds = [["git", "add", "-A", "portable/release"],
            ["git", "commit", "-m", msg],
            [sys.executable, os.path.join(SRC, "tools", "api_push.py")]]
    for c in cmds:
        print("  > " + " ".join(c))
    if dry:
        print("[发布] 预演完成，未改动仓库/远端。")
        return 0
    add = subprocess.run(cmds[0], cwd=SRC, capture_output=True, text=True,
                         errors="replace")
    if add.returncode != 0:
        print("[发布] git add 失败：%s" % (add.stdout + add.stderr)[-300:])
        return 1
    bad = verify_index_bytes()
    if bad:
        print("[发布] 已中止：.gitattributes 未生效，%d 个文件在 git 里被改了字节。" % len(bad))
        print("       例：%s" % ", ".join(bad[:4]))
        print("       请确认 .gitattributes 里 portable/release/** -text 已提交，再重试。")
        return 1
    print("[发布] 字节保真检查通过（索引 blob 与盘上文件一致）")
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=SRC)
    if staged.returncode == 0:
        print("[发布] 更新源无新增改动，跳过本次提交")
    else:
        r = subprocess.run(cmds[1], cwd=SRC, capture_output=True, text=True,
                           errors="replace")
        if r.returncode != 0:
            print("[发布] 提交失败：%s" % (r.stdout + r.stderr).strip()[-300:])
            return 1
    r = subprocess.run(cmds[2], cwd=SRC, capture_output=True, text=True, errors="replace")
    print(r.stdout[-1500:] or r.stderr[-800:])
    if r.returncode != 0:
        print("[发布] GitHub 推送失败（可稍后重试：python tools\\api_push.py）")
        return 1
    print("[发布] 已推送。U 盘点 UPDATE.cmd 即可拉到 %s" % m["version"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    a = ap.parse_args(argv)
    if not a.no_build:
        import factory
        factory.build()
    stage()
    if a.publish:
        return publish(dry=a.dry_run)
    if a.stage or not a.publish:
        print("[发布] 下一步：python portable\\publish.py --publish")
    return 0


if __name__ == "__main__":
    sys.exit(main())
