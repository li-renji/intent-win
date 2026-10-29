# -*- coding: utf-8 -*-
"""知识库测试：扫描 / 检索 / 统计 / 跳过规则 / 工具入口。

kb 索引的是「电脑里有哪些文件」，不读内容 —— 快且省 token；
手机端经 PC 文件桥 /kb/search 共用同一份索引。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import kb as kb_mod                              # noqa: E402
from agent.config import load_config                        # noqa: E402
from agent.tools import ToolBus                             # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s %s" % ("[PASS]" if cond else "[FAIL]", name, detail))


def _mk_tree(root):
    os.makedirs(os.path.join(root, "docs"), exist_ok=True)
    os.makedirs(os.path.join(root, "node_modules"), exist_ok=True)   # 应跳过
    for f in ("年度报告.docx", "照片.png", "会议纪要.txt", "报告-备份.txt"):
        with open(os.path.join(root, f), "w", encoding="utf-8") as fh:
            fh.write("x")
    with open(os.path.join(root, "node_modules", "报告.js"), "w") as fh:
        fh.write("x")
    with open(os.path.join(root, "docs", "调研报告.pdf"), "w") as fh:
        fh.write("x")
    with open(os.path.join(root, "install.log"), "w") as fh:   # SKIP_EXTS 应跳过
        fh.write("x")


def run():
    root = tempfile.mkdtemp()
    _mk_tree(root)
    store = kb_mod.KnowledgeBase(root)

    # ---- 扫描 ----
    st = store.scan(roots=[(root, 6)])
    check("扫描：成功", st.get("ok"), json.dumps(st, ensure_ascii=False))
    s = store.stats()
    check("扫描：跳过 node_modules 与 .log", s["count"] == 5, "count=%d" % s["count"])
    check("扫描：索引落盘", os.path.exists(store.index_path))
    check("扫描：刚扫完不过期", not store.is_stale())

    # ---- 检索 ----
    hits = store.search("报告")
    check("检索：命中 3 条", len(hits) == 3, str(hits))
    check("检索：不含跳过目录", not any("node_modules" in h for h in hits))
    check("检索：结果带路径和大小", "—" in hits[0] and "B)" in hits[0])
    check("检索：多词交集", len(store.search("报告 调研")) == 1)
    check("检索：limit 生效", len(store.search("报告", limit=1)) == 1)
    check("检索：空词返回空", store.search("") == [])
    check("检索：无匹配返回空", store.search("不存在的词xyz") == [])

    # ---- 陈旧判定 ----
    idx = json.load(open(store.index_path, encoding="utf-8"))
    idx["scanned_at"] = time.time() - 48 * 3600      # 假装两天前扫的
    with open(store.index_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False)
    check("陈旧：超 24h 判定过期", store.is_stale())
    store.scan(roots=[(root, 6)])
    check("陈旧：重扫后恢复", not store.is_stale())

    # ---- kb 工具入口 ----
    bus = ToolBus(load_config())
    ok, r = bus.invoke("kb", {"op": "search", "kw": "报告"})
    # 注意：这里查的是项目自身索引（可能还没建），只要不报错即为通
    check("工具：kb search 可调", ok)
    ok, r = bus.invoke("kb", {"op": "stats"})
    check("工具：kb stats 可调", ok and ("已索引" in r or "尚未建立" in r))
    ok, r = bus.invoke("kb", {"op": "bogus"})
    check("工具：未知 op 有提示", ok and "未知 op" in r)

    ok_all = all(r[1] for r in RESULTS)
    print("\n==== test_kb: %d/%d 通过 ====" % (
        sum(1 for r in RESULTS if r[1]), len(RESULTS)))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(run())
