# -*- coding: utf-8 -*-
"""回滚 browser-upgrade 升级包。

从最近一次备份（backups/browser-upgrade-*/manifest.json）还原：
  - 被修改过的文件 → 用备份覆盖回去
  - 升级包新增的文件 → 删除
用法：
  python rollback.py                       # 回滚最近一次
  python rollback.py D:\\INTENT-win         # 指定目标目录
  python rollback.py D:\\INTENT-win --list # 只列出可回滚的备份
"""
import json
import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent


def find_root(argv):
    cands = [Path(a) for a in argv if not a.startswith("--")]
    cands += [HERE, Path.cwd()]
    for c in cands:
        try:
            p = c.resolve()
        except Exception:
            continue
        for d in [p] + list(p.parents)[:6]:
            if (d / "agent" / "app.py").is_file():
                return d
    return None


def main():
    argv = sys.argv[1:]
    root = find_root(argv)
    if root is None:
        print("找不到 INTENT-win 根目录，请显式指定：python rollback.py D:\\INTENT-win")
        return 2

    bdirs = sorted((p for p in (root / "backups").glob("browser-upgrade-*")
                    if (p / "manifest.json").is_file()),
                   key=lambda p: p.name, reverse=True)
    if not bdirs:
        print("没有找到可回滚的备份（backups/browser-upgrade-*/manifest.json）。")
        return 1
    if "--list" in argv:
        print("可用备份：")
        for b in bdirs:
            print("  " + b.name)
        return 0

    bdir = bdirs[0]
    manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    print("回滚目标：" + str(root))
    print("使用备份：" + str(bdir))
    print("提示：这会覆盖升级后你对这些文件做过的任何手工修改。\n")

    restored = removed = 0
    for item in manifest.get("files", []):
        rel, action = item["rel"], item["action"]
        dst = root / rel
        if action == "modified":
            src = bdir / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                restored += 1
            else:
                print("  ! 备份缺失，跳过：" + rel)
        elif action == "created":
            try:
                dst.unlink(missing_ok=True)
                removed += 1
            except Exception as e:
                print("  ! 删除失败 %s：%s" % (rel, e))

    # 清掉新子项目目录：只删 .pyc 生成物与空目录，绝不删任何源码/用户数据
    mydir = root / "mybrowser"
    if mydir.exists():
        for pyc in mydir.rglob("*.pyc"):
            try:
                pyc.unlink()
            except OSError:
                pass
        dirs = sorted((p for p in mydir.rglob("*") if p.is_dir()),
                      key=lambda p: len(p.parts), reverse=True)
        for d in dirs:
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            mydir.rmdir()
        except OSError:
            pass
    try:
        (root / "agent" / "browser" / "mybrowser_adapter.py").unlink(missing_ok=True)
    except Exception:
        pass

    print("\n已还原 %d 个文件，删除 %d 个新增文件。" % (restored, removed))
    print("备份仍保留在：" + str(bdir))
    print("下一步：重启 INTENT（start.bat），即回到旧版 WebView2 浏览器。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
