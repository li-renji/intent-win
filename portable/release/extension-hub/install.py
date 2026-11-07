# -*- coding: utf-8 -*-
"""intent-win 扩展一键安装器。

用法（在下载下来的 install.py 所在目录执行）：

    python install.py                          # 自动探测 intent-win 根目录，安装全部扩展
    python install.py --root D:\\intent-win    # 明确指定根目录
    python install.py --only ajue,ajue-listen  # 只装指定扩展（按 id 过滤）
    python install.py --base https://...        # 换一个下载源（默认用脚本内置的站点地址）

它做三件事
----------
1. 把站点上的 ``.qdskill`` / ``.qdplugin`` 放进 ``<root>/skills/`` 和 ``<root>/plugins/``；
2. 把配套的运行时代码（``tools/*.py``、``plugins/ajue/``）放进 ``<root>/`` 对应位置；
3. 打印还需要你手动做的事（例如重启 intent 让扩展生效）。

注意
----
- 只装扩展，**不会碰**你的 ``配置.qdai``（里面是 API Key）、``plugin_data/``
  （聊天记录 / 日记 / 记忆）、会话文件——那些永远留在你自己机器上。
- 装完请重启 intent：技能摘要与插件注册都在启动时完成。
"""
import argparse
import os
import sys
import urllib.request

# 站点根地址与访问码（部署时回填）
#   注意：这个站点的文件访问**必须带访问码**，否则一律 403。
BASE_URL = "https://intent-ext-hub.app.workbuddy.host"
SHARE_CODE = "6G7CZFN4KM"

# 站点上的 manifest 文件名（统一拓展中心：自制 + 框架全部收纳）
SKILLS = ["workbench.qdskill", "docgen.qdskill", "rea.qdskill", "browser-guide.qdskill"]
PLUGINS = ["ajue.qdplugin", "genoffice.qdplugin", "workbench.qdplugin",
           "minimind.qdplugin", "vscode-ide.qdplugin", "sysinfo.qdplugin",
           "yaogaochaole.qdplugin"]

# 配套运行时代码
CODE_TOOLS = ["genoffice_adapter.py", "plugin_launch.py"]
CODE_AJUE = ["ajue_core.py", "listen.py", "runner.py", "tts.py",
             "persona.json", "enable_autostart.py", "pet.html", "README.md"]
# 工作台完整运行时（workbench 插件与技能都依赖它）
CODE_WORKBENCH = ["server/app.py", "web/index.html", "devices.yaml",
                 "start.cmd", "start.sh", "wb_start.cmd", "wb_stop.cmd"]


def find_root(explicit=None):
    """定位 intent-win 根目录（含 agent/ 与 plugins/ 的那一层）。"""
    if explicit:
        p = os.path.abspath(explicit)
        return p if os.path.isdir(os.path.join(p, "agent")) else None
    for cand in (r"D:\intent-win", r"%INTENT_ROOT%", r"C:\intent-win"):
        if os.path.isdir(os.path.join(cand, "agent")):
            return cand
    cur = os.path.abspath(os.getcwd())
    while True:
        if (os.path.isdir(os.path.join(cur, "agent"))
                and os.path.isdir(os.path.join(cur, "plugins"))):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def fetch(url, dest):
    """下载一个文件到 dest，返回字节数。失败抛异常。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "intent-ext-installer"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def _split_base(base):
    """把 base 拆成 (origin, query)；query 里通常带 sharecode 访问码。

    这样 ``--base https://x/?sharecode=y`` 和 ``--base https://x`` 都能用。
    """
    base = (base or "").strip()
    q = ""
    if "?" in base:
        base, q = base.split("?", 1)
    return base.rstrip("/"), q


def _file_url(base, q, rel):
    """拼出站点上一个文件的完整下载地址（带访问码）。"""
    url = base + "/files/" + rel.replace("\\", "/")
    if q:
        url += "?" + q
    return url


def put(base, q, rel, dest, ok, fail):
    """下载站点文件 rel 到 dest（目标是相对 root 的路径）。"""
    url = _file_url(base, q, rel)
    try:
        n = fetch(url, dest)
        ok.append((rel, dest, n))
    except Exception as e:
        fail.append((rel, str(e)))


def main():
    ap = argparse.ArgumentParser(description="intent-win 扩展一键安装器")
    ap.add_argument("--root", default=None, help="intent-win 根目录（默认自动探测）")
    ap.add_argument("--base", default=BASE_URL, help="站点根地址")
    ap.add_argument("--only", default="", help="只装指定 id，逗号分隔")
    args = ap.parse_args()

    base, q = _split_base(args.base or BASE_URL)
    if not base:
        print("没填站点地址，请用 --base https://... 指定，或重新从站点下载。")
        return 2
    if not q:
        q = ("sharecode=" + SHARE_CODE) if SHARE_CODE else ""

    root = find_root(args.root)
    if not root:
        print("没找到 intent-win 的安装目录。请用 --root 指定，例如：")
        print(r"    python install.py --root D:\intent-win")
        return 2
    print("目标目录：%s" % root)
    print("下载源  ：%s" % base)
    print("-" * 56)

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    want = lambda name: (not only) or (name in only)

    ok, fail = [], []
    for fn in SKILLS:
        if want(fn[:-len(".qdskill")]):
            put(base, q, "skills/" + fn, os.path.join(root, "skills", fn), ok, fail)
    for fn in PLUGINS:
        if want(fn[:-len(".qdplugin")]):
            put(base, q, "plugins/" + fn, os.path.join(root, "plugins", fn), ok, fail)
    # 运行时代码总是跟着装（体积很小，缺了扩展就跑不起来）
    for fn in CODE_TOOLS:
        put(base, q, "code/tools/" + fn, os.path.join(root, "tools", fn), ok, fail)
    for fn in CODE_AJUE:
        put(base, q, "code/plugins/ajue/" + fn,
            os.path.join(root, "plugins", "ajue", fn), ok, fail)
    # 工作台运行时：放到 <root>/workbench/ 对应位置（纯标准库，不依赖 uvicorn/fastapi）
    for fn in CODE_WORKBENCH:
        put(base, q, "code/workbench/" + fn,
            os.path.join(root, "workbench", fn), ok, fail)

    print("成功 %d 个：" % len(ok))
    for rel, dest, n in ok:
        print("  ✓ %-38s %6.1f KB" % (rel.replace("code/", ""), n / 1024.0))
    if fail:
        print("失败 %d 个：" % len(fail))
        for rel, err in fail:
            print("  ✗ %-38s %s" % (rel, err))
    print("-" * 56)
    print("接下来：")
    print("  1) 重启 intent（技能摘要与插件注册都在启动时完成）；")
    print("  2) 打开「设置 → 市场」能看到刚装的扩展；")
    print("  3) 想让阿觉常驻监听：说「阿觉开机自启」，"
          "或在桌宠「设置 → 监听」里打开开关。")
    if not ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
