# -*- coding: utf-8 -*-
"""把出厂包 payload/ 安装到 U 盘根目录。

用法（一般由包根目录的「安装到U盘.cmd」调用）：
    python install.py                     自动找可移动盘
    python install.py --drive E:          指定盘符
    python install.py --list              只列出可用磁盘
    python install.py --dry-run           预演，不写任何文件

安装到空盘 = 全新、零个人数据。目标盘若已有 INTENT，会话/记忆/配置/
模型一律保留（等同一次增量更新），不会清空别人的东西。
"""
import argparse
import ctypes
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _bind_portable(payload):
    p = os.path.join(payload, "portable")
    if p not in sys.path:
        sys.path.insert(0, p)


def drives():
    """返回 [{letter, type, free, total}]，type: removable/fixed/cdrom/other。"""
    k32 = ctypes.windll.kernel32
    bitmask = k32.GetLogicalDrives()
    k32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    k32.GetDriveTypeW.restype = ctypes.c_uint
    types = {2: "removable", 3: "fixed", 4: "remote", 5: "cdrom", 6: "ramdisk"}
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    out = []
    for i in range(26):
        if not (bitmask >> i) & 1:
            continue
        root = "%s:\\" % chr(65 + i)
        t = types.get(k32.GetDriveTypeW(ctypes.c_wchar_p(root)), "other")
        f = t2 = 0
        if k32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(root),
                                   ctypes.byref(free), ctypes.byref(total), None):
            f, t2 = free.value, total.value
        out.append({"letter": chr(65 + i), "root": root, "type": t,
                    "free": f, "total": t2})
    return out


def pick(payload_m, argv_drive=None, allow_fixed=False):
    from sync import total_size
    need = total_size(payload_m) + 200 * 1024 * 1024
    all_d = drives()
    if argv_drive:
        letter = argv_drive.strip().rstrip("\\/:")[0].upper()
        d = next((x for x in all_d if x["letter"] == letter), None)
        if not d:
            raise SystemExit("[安装] 找不到盘符 %s:" % letter)
        if d["type"] not in ("removable", "fixed") and not allow_fixed:
            raise SystemExit("[安装] %s: 不是可写磁盘（%s）" % (letter, d["type"]))
        if letter == os.environ.get("SystemDrive", "C:").rstrip(":").upper() and not allow_fixed:
            raise SystemExit("[安装] 拒绝装到系统盘 %s:，请指定 U 盘盘符" % letter)
        return d, need
    cands = [d for d in all_d if d["type"] == "removable" and d["free"] >= need]
    if len(cands) == 1:
        return cands[0], need
    print("[安装] 检测到 %d 个可用可移动磁盘：" % len(cands))
    for i, d in enumerate(cands, 1):
        print("  %d) %s:  可用 %.1f GB / 共 %.1f GB"
              % (i, d["letter"], d["free"] / 1024 ** 3, d["total"] / 1024 ** 3))
    if not cands:
        print("[安装] 没找到空间足够的可移动磁盘（需 %.2f GB）。" % (need / 1024 ** 3))
        print("       可手动指定：安装到U盘.cmd --drive E:")
        raise SystemExit(2)
    try:
        n = int(input("请选择序号（直接回车取消）: ").strip() or "0")
    except (EOFError, ValueError):
        n = 0
    if not 1 <= n <= len(cands):
        raise SystemExit("[安装] 已取消")
    return cands[n - 1], need


def install(payload, target, manifest, dry=False, collect=None, hardlink=None):
    """返回统计。collect: 可选回调，用于验收收集写入的文件列表。"""
    _bind_portable(payload)
    import shortcuts
    import sync
    from manifest import save, save_version

    t0 = time.time()
    dst_m = None
    mp = os.path.join(target, "manifest.json")
    if os.path.isfile(mp):
        try:
            with open(mp, "r", encoding="utf-8") as f:
                dst_m = json.load(f)
        except (OSError, ValueError):
            dst_m = None

    # 目标已有 INTENT -> 保留用户数据；空盘 -> 全新安装
    keep_user = dst_m is not None
    st = sync.sync(payload, target, manifest, dst_m, dry=dry,
                   keep_user=keep_user, hardlink=hardlink, progress=collect)
    if dry:
        st["seconds"] = round(time.time() - t0, 2)
        return st

    # 清单不参与传输（sync 已跳过），在这里落到目标盘
    save(target, manifest)
    save_version(target, manifest["version"], manifest["base_version"],
                 "installed from factory package")
    ok, msg = shortcuts.make(target)
    st["shortcuts"] = ok
    st["shortcuts_msg"] = msg
    st["seconds"] = round(time.time() - t0, 2)
    return st


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--payload", default=os.path.join(os.path.dirname(HERE), "payload"))
    ap.add_argument("--drive", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-fixed", action="store_true")
    ap.add_argument("--hardlink", action="store_true",
                    help="同盘验收用：强制硬链接，避免复制 1.9GB")
    a = ap.parse_args(argv)

    if a.list:
        for d in drives():
            print("%s:  %-10s %.1f / %.1f GB"
                  % (d["letter"], d["type"], d["free"] / 1024 ** 3,
                     d["total"] / 1024 ** 3))
        return 0

    payload = os.path.abspath(a.payload)
    if not os.path.isfile(os.path.join(payload, "START.cmd")):
        raise SystemExit("[安装] 找不到 payload\\START.cmd：%s" % payload)
    _bind_portable(payload)
    from manifest import load
    m = load(os.path.join(payload, "manifest.json"))
    if not m:
        raise SystemExit("[安装] payload 缺 manifest.json，请先运行 factory.py 重新打包")

    d, need = pick(m, a.drive, a.allow_fixed)
    target = d["root"]
    existing = os.path.isfile(os.path.join(target, "INTENT-PORTABLE.marker"))
    print("[安装] 目标 %s  可移动=%s  可用 %.1f GB  需要 %.2f GB"
          % (target, d["type"] == "removable", d["free"] / 1024 ** 3,
             need / 1024 ** 3))
    print("[安装] 模式：%s" % ("保留已有数据（增量安装）" if existing else "全新出厂安装"))
    if not a.dry_run and d["type"] != "removable" and not a.allow_fixed:
        raise SystemExit("[安装] 目标不是可移动磁盘，已拒绝（--allow-fixed 可强制）")

    st = install(payload, target, m, dry=a.dry_run,
                 hardlink=True if a.hardlink else None)
    print("[安装] %s：新增 %d / 更新 %d / 未变 %d / 保留用户数据 %d / 删除 %d / 耗时 %.1fs"
          % ("预演" if a.dry_run else "完成", st["new"], st["updated"],
             st["same"], st["kept"], st["removed"], st["seconds"]))
    if st.get("shortcuts") is False:
        print("[安装] 快捷方式生成失败：%s" % st.get("shortcuts_msg"))
    for e in st["errors"][:5]:
        print("  ! " + e)
    if not a.dry_run:
        print("[安装] 现在可以双击 %s 根目录的「终端对话.lnk」或「图形界面.lnk」" % target)
    return 0 if not st["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
