# -*- coding: utf-8 -*-
r"""GenOffice 适配器：把 intent-win 插件传入的参数转成 genoffice CLI 调用。

接口（与 plugins/genoffice.qdplugin 对应）：
    python genoffice_adapter.py <type> <infile> <out>
      type   : docx | xlsx | pptx
      infile : 内容源文件路径（docx/pptx 用 Markdown，xlsx 用 JSON 数组或 CSV）
      out    : 输出 Office 文件路径

前置：GenOffice 需要先构建 CLI（首次使用一次性）：
    cd D:\genoffice
    npm install --ignore-scripts
    npm run build -w @genoffice/cli
构建产物在 D:\genoffice\packages\cli\bin\genoffice（或 .cjs）。
"""
import json
import os
import shutil
import subprocess
import sys

GENOFFICE_DIR = r"D:\genoffice"
CLI_DIR = os.path.join(GENOFFICE_DIR, "packages", "cli")
NODE = shutil.which("node") or "node"


def _find_cli():
    # 构建产物在 packages/cli/dist/genoffice.cjs（build.mjs 输出），优先用它
    candidates = [
        os.path.join(CLI_DIR, "dist", "genoffice.cjs"),
        os.path.join(CLI_DIR, "bin", "genoffice.cjs"),
        os.path.join(CLI_DIR, "bin", "genoffice"),
        os.path.join(CLI_DIR, "genoffice.cjs"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


def main():
    if len(sys.argv) < 4:
        _emit({"status": "error", "code": 1,
               "message": "用法: genoffice_adapter.py <type> <infile> <out>"})
        return

    typ = sys.argv[1].strip().lower()
    infile = sys.argv[2].strip()
    out = sys.argv[3].strip()

    if typ not in ("docx", "xlsx", "pptx"):
        _emit({"status": "error", "code": 1,
               "message": "type 必须是 docx / xlsx / pptx，收到: %s" % typ})
        return

    if not os.path.isfile(infile):
        _emit({"status": "error", "code": 2,
               "message": "输入文件不存在: %s" % infile})
        return

    cli = _find_cli()
    if not cli:
        _emit({
            "status": "error", "code": 3,
            "message": ("GenOffice CLI 尚未构建。请在 D:\\genoffice 执行：\n"
                        "  npm install --ignore-scripts\n"
                        "  npm run build -w @genoffice/cli\n"
                        "构建完成后本工具即可生成文档。")
        })
        return

    # 确保输出目录存在
    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    cmd = [NODE, cli, "create", "--type", typ, "--from", infile, "--out", out]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=180, cwd=GENOFFICE_DIR)
    except subprocess.TimeoutExpired:
        _emit({"status": "error", "code": 4,
               "message": "GenOffice 执行超时（180 秒）"})
        return
    except Exception as e:  # noqa: BLE001
        _emit({"status": "error", "code": 5, "message": "运行失败: %s" % e})
        return

    out_text = (proc.stdout or "").strip()
    err_text = (proc.stderr or "").strip()
    if proc.returncode == 0 and (os.path.isfile(out) or out_text):
        _emit({
            "status": "ok",
            "output_path": out,
            "summary": out_text or ("已生成: " + out),
            "stderr": err_text or None,
        })
    else:
        _emit({
            "status": "error", "code": proc.returncode or 6,
            "message": (out_text + "\n" + err_text).strip() or "GenOffice 无输出",
        })


if __name__ == "__main__":
    main()
