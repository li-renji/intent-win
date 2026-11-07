# -*- coding: utf-8 -*-
"""把扩展中心构建成**自包含单文件**（index.html）。

为什么
----
WorkBuddy 的本地预览（/static-html/<hash>/）只映射**入口那一个 HTML**，
同目录的其它文件和子目录一律 404——所以「目录 + 相对路径」在预览里点下载
会失败；换个服务器也可能因路径结构不同而失效。

做法：把 files/ 下所有文件以 base64 内联进 index.html，页面点下载时用
Blob 就地生成文件。这样：
- 单个 HTML 就是完整的扩展中心，双击、预览、丢给任何人都能用；
- 离线也能用（不依赖站点是否在线）；
- 站点仍保留逐文件直链，供 intent「市场 → URL 安装」使用。

用法：
    python build.py            # 由 index.src.html 生成 index.html
"""
import base64
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "index.src.html")
OUT = os.path.join(HERE, "index.html")
FILES_DIR = os.path.join(HERE, "files")
MARK = "/*__FILES__*/{}"


def collect():
    """收集要内联的文件 → {相对路径: base64}。

    包括 files/ 下的全部文件，以及站点根的 install.py（页面上的安装器按钮
    要能下载它，而它不在 files/ 里）。
    """
    out = {}
    root_install = os.path.join(HERE, "install.py")
    if os.path.isfile(root_install):
        with open(root_install, "rb") as f:
            out["install.py"] = base64.b64encode(f.read()).decode("ascii")
    for root, _dirs, files in os.walk(FILES_DIR):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, HERE).replace("\\", "/")
            with open(p, "rb") as f:
                out[rel] = base64.b64encode(f.read()).decode("ascii")
    return out


def inject_bat(text):
    """把站点地址与访问码注入 install.bat 模板（bat 需要 CRLF 换行）。"""
    base, share = "", ""
    try:
        import re
        with open(os.path.join(HERE, "install.py"), encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'^BASE_URL\s*=\s*"([^"]+)"', src, re.M)
        base = m.group(1) if m else ""
        m = re.search(r'^SHARE_CODE\s*=\s*"([^"]+)"', src, re.M)
        share = m.group(1) if m else ""
    except Exception:
        pass
    text = text.replace("{{BASE}}", base).replace("{{SHARE}}", share)
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def main():
    if not os.path.isfile(SRC):
        print("缺少模板：%s" % SRC)
        return 2
    files = collect()
    if not files:
        print("files/ 下没有文件")
        return 1
    # install.bat 里的站点地址要在构建时注入（它自己也是一个可下载文件）
    bat = os.path.join(FILES_DIR, "install.bat")
    if os.path.isfile(bat):
        with open(bat, encoding="utf-8") as f:
            injected = inject_bat(f.read())
        with open(bat, "w", encoding="utf-8", newline="") as f:
            f.write(injected)
        files["files/install.bat"] = base64.b64encode(
            injected.encode("utf-8")).decode("ascii")
    html = open(SRC, encoding="utf-8").read()
    if MARK not in html:
        print("模板里找不到占位符 %s" % MARK)
        return 1
    payload = "{" + ",".join(
        '"%s":"%s"' % (k, v) for k, v in sorted(files.items())) + "}"
    html = html.replace(MARK, payload)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    size = os.path.getsize(OUT)
    print("已生成 %s（%.1f KB，内联 %d 个文件）"
          % (os.path.basename(OUT), size / 1024.0, len(files)))
    for k in sorted(files):
        print("  · %s" % k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
