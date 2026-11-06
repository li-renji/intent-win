# -*- coding: utf-8 -*-
"""INTENT-win 内置浏览器升级包 —— 一键安装（在【另一台机器】上运行）。

设计原则：最小侵入。只做两件事：
  1) 新增 mybrowser/ 子项目 + agent/browser/mybrowser_adapter.py（不删除任何旧文件）
  2) 把 agent/app.py、agent/main.py 里的 WebView2Adapter 换成 MyBrowserAdapter（各 2 行）
其余业务代码（上下文、模型、记忆、插件体系…）一个字节都不碰。
所有被改动/覆盖的文件先备份到 backups/，可用 rollback.py 一键回滚。

用法：
  python install.py                            # 自动探测 INTENT-win 根目录
  python install.py D:\\INTENT-win              # 显式指定目标目录
  python install.py D:\\INTENT-win --dry-run    # 只预演，不写任何文件
  python install.py D:\\INTENT-win --core-only # 只换引擎，不同步提示文案/配置
  python install.py D:\\INTENT-win --full-app  # 用本机 app.py/main.py 整份覆盖，体验 100% 对齐

默认只替换 2 行接线，最小侵入；--full-app 会连带同步「标题栏直达导航 / 启动预热 /
窗口 show()」等体验改动，但会覆盖目标机的 app.py，仅在确认目标机无独有改动时使用。

Windows 控制台若中文乱码，先执行：chcp 65001
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / "payload"

IMPORT_NEW = b"from .browser.mybrowser_adapter import MyBrowserAdapter"
IMPORT_PATTERNS = [
    rb"from\s+\.browser\.webview2\s+import\s+WebView2Adapter",
    rb"from\s+\.webview2\s+import\s+WebView2Adapter",
    rb"from\s+\.browser\s+import\s+WebView2Adapter",
]
CTOR_PAT = rb"\bWebView2Adapter\s*\(\s*cfg\s*\)"
CTOR_NEW = b"MyBrowserAdapter(cfg)"

CONFIG_BLOCK = """
# ---- MyBrowser 专属（自研浏览器，由 browser-upgrade 升级包追加）----
mybrowser_ui=true
mybrowser_proxy=direct
mybrowser_preheat=true
mybrowser_serve=false
mybrowser_serve_port=9223
"""


def find_root(argv):
    """定位 INTENT-win 根目录：命令行参数 > 脚本所在目录 > 当前工作目录。"""
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


def nl_of(data):
    return b"\r\n" if b"\r\n" in data else b"\n"


class Installer:
    def __init__(self, root, dry=False, core_only=False, full_app=False):
        self.root = root
        self.dry = dry
        self.core_only = core_only
        self.full_app = full_app
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.bdir = root / "backups" / ("browser-upgrade-" + self.stamp)
        self.touched = {}          # rel -> "modified" | "created"
        self.log = []
        self.warns = []
        self.errors = []

    def note(self, s):
        self.log.append(s)
        print("  " + s)

    def mark(self, rel):
        """登记将被改动的文件，并在真正写入前备份原始版本。"""
        rel = rel.replace("\\", "/")
        if rel in self.touched:
            return
        exists = (self.root / rel).is_file()
        self.touched[rel] = "modified" if exists else "created"
        if exists and not self.dry:
            dst = self.bdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.root / rel, dst)

    def put(self, rel, data):
        """字节级写入（不改动文件其余任何字节）。"""
        if self.dry:
            return
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    # ---------- 1. 复制新增文件 ----------
    def copy_payload(self):
        print("\n[1/5] 复制浏览器文件")
        if not PAYLOAD.is_dir():
            self.errors.append("payload/ 目录缺失，升级包不完整")
            return
        files = sorted(p for p in PAYLOAD.rglob("*") if p.is_file())
        changed = 0
        for src in files:
            rel = src.relative_to(PAYLOAD).as_posix()
            data = src.read_bytes()
            dst = self.root / rel
            if dst.is_file() and dst.read_bytes() == data:
                continue          # 已一致，跳过（重复运行不产生新备份）
            self.mark(rel)
            self.put(rel, data)
            changed += 1
        self.note("新增/覆盖 %d 个文件，%d 个已是最新跳过"
                  % (changed, len(files) - changed))

    # ---------- 2. 核心接线：换引擎 ----------
    def rewire(self, rel):
        p = self.root / rel
        if not p.is_file():
            self.note("%s 不存在，跳过" % rel)
            return
        data = p.read_bytes()
        if b"MyBrowserAdapter" in data:
            self.note("%s 已是新引擎，跳过（幂等）" % rel)
            return
        self.mark(rel)
        imp_hits = 0
        for pat in IMPORT_PATTERNS:
            data, n = re.subn(pat, lambda m: IMPORT_NEW, data)
            if n:
                imp_hits += n
                break
        data, ctor_hits = re.subn(CTOR_PAT, lambda m: CTOR_NEW, data)
        if imp_hits == 0:
            self.warns.append("%s 没找到 WebView2Adapter 的 import 行（版本可能不同），请手动检查" % rel)
        if ctor_hits == 0:
            self.warns.append("%s 没找到 WebView2Adapter(cfg) 实例化行，请手动检查" % rel)
        self.put(rel, data)
        self.note("%s：import 替换 %d 处，实例化替换 %d 处" % (rel, imp_hits, ctor_hits))

    def do_rewire(self):
        print("\n[2/5] 切换浏览器引擎（核心，只改 2 行/文件）")
        self.rewire("agent/app.py")
        self.rewire("agent/main.py")

    def do_full_app(self):
        """用参考文件整份覆盖，实现与源机 100% 一致（含直达导航 / 预热 / show）。"""
        print("\n[2/5] 整份覆盖 app.py / main.py（--full-app）")
        ref = HERE / "reference" / "agent"
        if not ref.is_dir():
            self.errors.append("reference/ 缺失，无法使用 --full-app；请改用默认模式")
            return
        for rel in ("agent/app.py", "agent/main.py"):
            src = ref / Path(rel).name
            if not src.is_file():
                self.errors.append("参考文件缺失：" + str(src))
                continue
            self.mark(rel)
            self.put(rel, src.read_bytes())
            self.note("%s：已用源机版本整份覆盖（原文件已备份）" % rel)

    # ---------- 3. 可选：同步给模型看的提示文案 ----------
    def sync_tools_py(self):
        rel = "agent/tools.py"
        p = self.root / rel
        if not p.is_file():
            return
        data = p.read_bytes()
        if "NAVIGATE <url或搜索词>".encode("utf-8") in data:
            self.note("%s 已是新版描述，跳过" % rel)
            return
        old = '"NAVIGATE <url> 导航；SNAPSHOT 取可交互元素编号表+正文摘要；"'.encode("utf-8")
        if old not in data:
            self.warns.append("%s 未匹配到旧版描述，跳过（不影响功能）" % rel)
            return
        nl = nl_of(data).decode("ascii")
        new = nl.join([
            '"NAVIGATE <url或搜索词> 直接导航（网址开网页、搜索词开搜索结果页，直达不绕主页）；"',
            '                "SEARCH <关键词> 直接搜索并打开结果页；"',
            '                "SNAPSHOT 取可交互元素编号表+正文摘要；"',
        ]).encode("utf-8")
        self.mark(rel)
        self.put(rel, data.replace(old, new, 1))
        self.note("%s：补上 NAVIGATE 直达 / SEARCH 指令说明" % rel)

    def sync_prompts_py(self):
        rel = "agent/prompts.py"
        p = self.root / rel
        if not p.is_file():
            return
        data = p.read_bytes()
        if "不要先 SNAPSHOT 主页".encode("utf-8") in data:
            self.note("%s 已是新版提示，跳过" % rel)
            return
        old = '"只要正文用READ（最省token）。"'.encode("utf-8")
        if old not in data:
            self.warns.append("%s 未匹配到 FRAG_BROWSER_USAGE 锚点，跳过（不影响功能）" % rel)
            return
        nl = nl_of(data).decode("ascii")
        extra = nl.join([
            '"只要正文用READ（最省token）。"',
            '    "用户要『打开/搜索网页』时，直接 NAVIGATE <网址或搜索词> 或 SEARCH <关键词> "',
            '    "打开浏览器并直达目标，不要先 SNAPSHOT 主页再手动找搜索框输入（那样慢且多一步）。"',
        ]).encode("utf-8")
        self.mark(rel)
        self.put(rel, data.replace(old, extra, 1))
        self.note("%s：补上「直达导航，不要先 SNAPSHOT 主页」提示" % rel)

    def sync_config(self):
        rel = "配置.qdai"
        p = self.root / rel
        if not p.is_file():
            self.warns.append("未找到 配置.qdai，跳过配置追加（适配器有默认值，不影响运行）")
            return
        raw = p.read_bytes()
        if b"mybrowser_ui" in raw:
            self.note("%s 已含 MyBrowser 配置，跳过" % rel)
            return
        self.mark(rel)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        nl = "\r\n" if "\r\n" in text else "\n"
        block = CONFIG_BLOCK.strip("\n").replace("\n", nl) + nl
        if text and not text.endswith(("\n", "\r")):
            text += nl
        self.put(rel, (text + block).encode("utf-8"))
        self.note("%s：追加 MyBrowser 配置段（不覆盖任何原有项）" % rel)

    def do_optional(self):
        print("\n[3/5] 同步提示文案与配置（尽力而为，匹配不到就跳过）")
        if self.core_only:
            self.note("--core-only：跳过")
            return
        self.sync_tools_py()
        self.sync_prompts_py()
        self.sync_config()

    # ---------- 4. 依赖检查 ----------
    def check_deps(self):
        print("\n[4/5] 检查运行依赖")
        import importlib.util
        for mod in ("clr", "webview"):
            try:
                ok = importlib.util.find_spec(mod) is not None
            except Exception:
                ok = False
            self.note("Python 包 %-8s %s" % (mod, "OK" if ok else "缺失"))
            if not ok:
                self.warns.append("缺少 %s —— 请在目标机器执行：pip install pythonnet pywebview" % mod)
        dll_ok = False
        try:
            import webview
            lib = os.path.join(os.path.dirname(webview.__file__), "lib")
            dll_ok = all(os.path.exists(os.path.join(lib, f)) for f in (
                "Microsoft.Web.WebView2.WinForms.dll",
                "Microsoft.Web.WebView2.Core.dll"))
        except Exception:
            pass
        self.note("WebView2 托管 DLL  %s" % ("OK" if dll_ok else "缺失（pywebview 应自带）"))
        if not dll_ok:
            self.warns.append("WebView2 托管 DLL 未找到 —— 重装 pywebview：pip install --force-reinstall pywebview")
        self.note("Edge WebView2 Runtime 由 Windows 提供（Win10/11 一般已内置）")

    # ---------- 5. 轻量验证 ----------
    def verify(self):
        print("\n[5/5] 验证（不弹窗、不联网）")
        if self.dry:
            self.note("dry-run：跳过验证")
            return
        code = (
            "from mybrowser.agent_tool import MyBrowserTool; "
            "from agent.browser.mybrowser_adapter import MyBrowserAdapter; "
            "print('INTERFACE_OK' if all(hasattr(MyBrowserAdapter, m) for m in "
            "('execute','available','start','show')) else 'INTERFACE_MISSING')"
        )
        try:
            r = subprocess.run([sys.executable, "-c", code], cwd=str(self.root),
                               capture_output=True, text=True, timeout=40)
            out = (r.stdout or "").strip()
            if "INTERFACE_OK" in out:
                self.note("导入链路 + 适配器接口：OK")
            else:
                self.errors.append("导入验证失败：" + (out or (r.stderr or "")).strip()[-500:])
        except Exception as e:
            self.errors.append("导入验证异常：%s" % e)

    # ---------- 汇总 ----------
    def finish(self):
        if not self.dry and self.touched:
            self.bdir.mkdir(parents=True, exist_ok=True)
            (self.bdir / "manifest.json").write_text(json.dumps({
                "timestamp": self.stamp,
                "root": str(self.root),
                "files": [{"rel": k, "action": v} for k, v in sorted(self.touched.items())],
            }, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n" + "=" * 62)
        if self.dry:
            print("预演完成（未写入任何文件）。涉及 %d 个文件。" % len(self.touched))
        else:
            print("安装完成。改动 %d 个文件，备份位于：" % len(self.touched))
            print("  " + str(self.bdir))
        if self.warns:
            print("\n提醒：")
            for w in self.warns:
                print("  ! " + w)
        if self.errors:
            print("\n错误：")
            for e in self.errors:
                print("  X " + e)
            print("\n可用 rollback.py 回滚后重试。")
            return 1
        if not self.dry:
            print("\n下一步：")
            print("  1) 关闭正在运行的 INTENT，重新执行 start.bat")
            print("  2) 打开软件，点「浏览器」看是否出现自研多标签界面")
            print("  3) 想做完整自检：python -m mybrowser.selftest（会启动无头 Edge）")
            print("  4) 想回滚：python rollback.py")
        print("=" * 62)
        return 0


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    core_only = "--core-only" in argv
    full_app = "--full-app" in argv
    root = find_root(argv)
    if root is None:
        print("找不到 INTENT-win 根目录（需包含 agent/app.py）。")
        print("请把本包解压到 INTENT-win 目录下，或显式指定：")
        print("  python install.py D:\\INTENT-win")
        return 2
    print("目标目录：" + str(root))
    print("模式：" + ("预演(dry-run)" if dry else "正式安装")
          + (" + 整份覆盖 app.py/main.py" if full_app else " + 最小接线替换"))
    it = Installer(root, dry=dry, core_only=core_only, full_app=full_app)
    it.copy_payload()
    if full_app:
        it.do_full_app()
    else:
        it.do_rewire()
    it.do_optional()
    it.check_deps()
    it.verify()
    return it.finish()


if __name__ == "__main__":
    sys.exit(main())
