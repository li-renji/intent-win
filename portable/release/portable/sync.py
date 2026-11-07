# -*- coding: utf-8 -*-
"""增量同步核心：安装到 U 盘、本机推送、云端拉取三处共用。

铁律：
  * 只按 sha256 判断要不要传，没变的文件一个字节都不动。
  * user 区（会话/记忆/模型/配置/浏览器登录态）只要目标已存在就不覆盖。
  * 删除只针对「上一版清单登记为 code/base」的文件；未知文件一律不碰。
"""
import os
import shutil

from manifest import is_user, norm, sha256


def atomic_copy(src, dst, hardlink=False):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isfile(dst):
        tmp = dst + ".new"
        if hardlink:
            try:
                os.link(src, tmp)
                os.replace(tmp, dst)
                return
            except OSError:
                if os.path.isfile(tmp):
                    os.remove(tmp)
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return
    if hardlink:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass
    shutil.copy2(src, dst)


def same_volume(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def sync(src, dst, src_m, dst_m=None, dry=False, keep_user=True,
         hardlink=None, channels=("code", "base", "user"), progress=None):
    """把 src 的内容按清单同步到 dst，返回统计字典。"""
    if hardlink is None:
        hardlink = same_volume(src, dst)
    st = {"new": 0, "updated": 0, "same": 0, "kept": 0,
          "removed": 0, "errors": [], "hardlink": hardlink}
    src_files = src_m.get("files", {})

    for rel, meta in src_files.items():
        ch = meta.get("ch", "code")
        if ch not in channels:
            continue
        d = os.path.join(dst, norm(rel))
        s = os.path.join(src, norm(rel))
        if ch == "user" and keep_user and os.path.isfile(d):
            st["kept"] += 1
            continue
        if os.path.isfile(d):
            try:
                if sha256(d) == meta["sha256"]:
                    st["same"] += 1
                    continue
            except OSError:
                pass
            action = "updated"
        else:
            action = "new"
        if not os.path.isfile(s):
            st["errors"].append("源缺失: " + rel)
            continue
        if not dry:
            try:
                atomic_copy(s, d, hardlink)
            except OSError as e:
                st["errors"].append("%s: %s" % (rel, e))
                continue
        st[action] += 1
        if progress:
            progress(action, rel)

    # 只删「上一版清单里登记为程序文件」且新版已移除的；用户数据永不删。
    if dst_m:
        for rel, meta in dst_m.get("files", {}).items():
            rel = norm(rel)
            if rel in src_files or is_user(rel):
                continue
            if meta.get("ch") not in ("code", "base"):
                continue
            d = os.path.join(dst, rel)
            if os.path.isfile(d):
                if not dry:
                    try:
                        os.remove(d)
                    except OSError as e:
                        st["errors"].append("删除 %s: %s" % (rel, e))
                        continue
                st["removed"] += 1
    return st


def total_size(manifest):
    return sum(m.get("size", 0) for m in manifest.get("files", {}).values())
