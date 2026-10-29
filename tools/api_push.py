# -*- coding: utf-8 -*-
"""通过 GitHub REST API 提交改动（绕开被网络阻断的 git 协议端点）。

流程（Git Data API）：
    1. 对每个文件建 blob（POST /git/blobs）
    2. 用远端当前 tree + 新 blob 建 tree（POST /git/trees）
    3. 以该 tree 建 commit（POST /git/commits）
    4. 更新分支引用（PATCH /git/refs/heads/main）

用法：
    python tools/api_push.py <base>            # 推送 base 与 origin/main 的差异
    python tools/api_push.py --all              # 推送全部文件（全量重建 tree）
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "li-renji/intent-win"
BRANCH = "main"


def get_token():
    """从 Git 凭据管理器取 github.com 的 OAuth token。"""
    p = subprocess.run(["git", "credential-manager", "get"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True)
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


TOKEN = get_token()


def req(method, path, body=None):
    url = API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "qidian-agent/1.0",
        "Content-Type": "application/json",
    })
    try:
        resp = urllib.request.urlopen(r, timeout=40)
        raw = resp.read().decode("utf-8")
        return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")[:500]
        return e.code, {"_raw": raw}
    except Exception as e:
        return 0, {"_raw": str(e)}


def get_ref_sha():
    st, d = req("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
    return d.get("object", {}).get("sha") if st == 200 else None


def get_base_tree(sha):
    st, d = req("GET", "/repos/%s/git/commits/%s" % (REPO, sha))
    return d.get("tree", {}).get("sha") if st == 200 else None


def make_blob(content_bytes):
    st, d = req("POST", "/repos/%s/git/blobs" % REPO,
                {"content": base64.b64encode(content_bytes).decode("ascii"),
                 "encoding": "base64"})
    return d.get("sha") if st == 201 else None


def build_tree(base_tree, entries):
    """entries: [(path, blob_sha_or_None)]  None 表示删除该文件。"""
    tree = []
    for path, sha in entries:
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})
    body = {"tree": tree}
    if base_tree:
        body["base_tree"] = base_tree
    st, d = req("POST", "/repos/%s/git/trees" % REPO, body)
    return d.get("sha") if st == 201 else None


def make_commit(tree_sha, msg, parent):
    body = {"message": msg, "tree": tree_sha, "parents": [parent]}
    st, d = req("POST", "/repos/%s/git/commits" % REPO, body)
    return d.get("sha") if st == 201 else None


def update_ref(commit_sha):
    st, d = req("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, BRANCH),
                {"sha": commit_sha, "force": False})
    return st == 200


def git_show(rev, path):
    p = subprocess.run(["git", "show", "%s:%s" % (rev, path)], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def diff_paths():
    """本地 HEAD 与 origin/main 之间的差异路径集合。"""
    p = subprocess.run(["git", "diff", "--name-status", "origin/main", "HEAD"],
                       capture_output=True, text=True)
    out = []
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0], parts[-1]))
    return out


def main():
    if not TOKEN:
        print("取不到 GitHub token")
        return 1
    all_mode = "--all" in sys.argv
    base = "origin/main"
    parent = get_ref_sha()
    if not parent:
        print("取不到远端分支引用")
        return 1
    base_tree = get_base_tree(parent)
    print("远端 HEAD: %s" % parent)
    print("远端 tree : %s" % base_tree)

    if all_mode:
        paths = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"],
                               capture_output=True, text=True).stdout.split()
        entries = []
        for p in paths:
            data = git_show("HEAD", p)
            if data is None:
                continue
            sha = make_blob(data)
            if not sha:
                print("blob 失败: %s" % p)
                return 1
            entries.append((p, sha))
            print("  blob %s" % p)
    else:
        entries = []
        for status, path in diff_paths():
            if status.startswith("D"):
                entries.append((path, None))
                print("  del  %s" % path)
                continue
            data = git_show("HEAD", path)
            if data is None:
                print("读取失败: %s" % path)
                return 1
            sha = make_blob(data)
            if not sha:
                print("blob 失败: %s" % path)
                return 1
            entries.append((path, sha))
            print("  blob %s" % path)

    tree_sha = build_tree(base_tree, entries)
    if not tree_sha:
        st, d = req("POST", "/repos/%s/git/trees" % REPO,
                    {"tree": [{"path": p, "mode": "100644", "type": "blob", "sha": s}
                              for p, s in entries], "base_tree": base_tree})
        print("建 tree 失败:", d)
        return 1
    print("新 tree: %s" % tree_sha)

    msg = subprocess.run(["git", "log", "-1", "--pretty=%s"], capture_output=True,
                         text=True).stdout.strip() or "update"
    commit = make_commit(tree_sha, msg, parent)
    if not commit:
        print("建 commit 失败")
        return 1
    print("新 commit: %s" % commit)

    if update_ref(commit):
        print("推送成功")
        return 0
    print("更新引用失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
