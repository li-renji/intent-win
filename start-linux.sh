#!/usr/bin/env bash
# INTENT Linux 版启动器（无界面核心：终端一问一答 + 6800/6801/6802 三端口）
#
# 用法：
#   chmod +x start-linux.sh
#   ./start-linux.sh              连续对话（输入 exit 退出）
#   ./start-linux.sh 你好         问一句就退出
#   ./start-linux.sh --status     只看引擎状态
#   ./start-linux.sh --runtime    看运行时自检
#
# 前置：Python >= 3.9。核心链路只用标准库，不装任何依赖也能跑对话；
#       需要读 PDF/Word/Excel/PPT 才装：python3 -m pip install -r requirements-linux.txt

set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# 优先用系统 python3；可用 PYTHON 环境变量覆盖
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[失败] 找不到 $PY，请先安装 Python 3.9+ 或设置 PYTHON=/path/to/python3" >&2
  exit 1
fi

# 关掉本机代理，避免 127.0.0.1 的三端口请求被代理劫持成 502
export no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
export NO_PROXY="$no_proxy"
export PYTHONIOENCODING="utf-8"

exec "$PY" -u chat.py "$@"
