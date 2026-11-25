#!/usr/bin/env bash
# INTENT Linux 一行命令安装器（无界面核心：终端对话 + 6800/6801/6802 三端口）
#
# 一行命令安装（新机只要 Python 3.9+，不需要 Git、不需要 sudo）：
#   curl -fsSL https://cdn.jsdelivr.net/gh/li-renji/intent-win@main/install.sh | bash
# 若上面提示连接重置，把 cdn.jsdelivr.net 换成 raw.githubusercontent.com（两者内容一致）。
# 装完以后重装/升级只需敲两个字母：iw（内置多源自动回退）
#
# 装完新开终端敲 intent 即可对话；首次聊天前在 配置.qdai 里填自己的 api_key。
#
# 可选环境变量（跑上面那行之前设置）：
#   INTENT_INSTALL_DIR   安装目录，默认 ~/.local/share/intent-win
#   INTENT_BIN_DIR       全局命令目录，默认 ~/.local/bin
#   INTENT_SOURCE_ZIP    用本地源码 zip 安装（完全离线，不连 GitHub）
#   INTENT_NO_PATH=1     不改 PATH / 不写 intent 命令
#   INTENT_SKIP_DEPS=1   不装文档库（核心对话本来就零依赖）
#   INTENT_CHECK_SOURCES=1  只探测下载源是否可达，不安装

set -e

REPO="li-renji/intent-win"
BRANCH="main"
TARGET="${INTENT_INSTALL_DIR:-$HOME/.local/share/intent-win}"
BIN_DIR="${INTENT_BIN_DIR:-$HOME/.local/bin}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/intent-install.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

say() { printf '[INTENT] %s\n' "$*"; }
die() { printf '[INTENT][ERROR] %s\n' "$*" >&2; exit 1; }

# ---------- 0 只探测源 ----------
# 多源自动回退：国内网络对 github.com 主域常做 SNI 重置，
# 故把不走主域的 codeload / jsdelivr CDN 排在前面，代理站兜底。
SOURCE_URLS=(
  "https://codeload.github.com/$REPO/zip/refs/heads/$BRANCH"
  "https://api.github.com/repos/$REPO/zipball/$BRANCH"
  "https://gh-proxy.com/https://codeload.github.com/$REPO/zip/refs/heads/$BRANCH"
)
if [ "${INTENT_CHECK_SOURCES:-0}" = "1" ]; then
  say "探测下载源（不安装）"
  for u in "${SOURCE_URLS[@]}"; do
    if curl -fsSIL --max-time 15 -o /dev/null "$u" 2>/dev/null; then
      say "  OK   $u"
    else
      say "  FAIL $u"
    fi
  done
  exit 0
fi

# ---------- 1 找 Python ----------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    PY="$c"
    break
  fi
done
[ -n "$PY" ] || die "需要 Python 3.9+，请先安装（核心链路零第三方依赖）"
say "Python: $($PY -c 'import sys; print(sys.version.split()[0])')"

# ---------- 2 下载源码 ----------
ZIP="$TMP/source.zip"
if [ -n "${INTENT_SOURCE_ZIP:-}" ] && [ -f "$INTENT_SOURCE_ZIP" ]; then
  cp "$INTENT_SOURCE_ZIP" "$ZIP"
  say "使用本地源码包：$INTENT_SOURCE_ZIP"
else
  command -v curl >/dev/null 2>&1 || die "需要 curl（或设置 INTENT_SOURCE_ZIP 离线安装）"
  got=0
  for u in "${SOURCE_URLS[@]}"; do
    say "下载源码：$u"
    if curl -fsSL --retry 2 --max-time 300 -o "$ZIP" "$u" 2>/dev/null && \
       [ "$(wc -c < "$ZIP")" -gt 1024 ]; then
      got=1
      break
    fi
  done
  [ "$got" = 1 ] || die "所有下载源都失败；可手动下载 zip 后设 INTENT_SOURCE_ZIP=/path/to/zip"
fi

# ---------- 3 解压（纯 Python，不依赖 unzip）----------
UNPACK="$TMP/unpack"
"$PY" - "$ZIP" "$UNPACK" <<'PYEOF'
import os, shutil, sys, zipfile
z, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
with zipfile.ZipFile(z) as f:
    names = [n for n in f.namelist() if not n.endswith("/")]
    # GitHub zipball 顶层是单一文件夹；找到它再剥掉
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    top = tops.pop() if len(tops) == 1 else ""
    for n in names:
        rel = n.split("/", 1)[1] if (top and n.startswith(top + "/")) else n
        if not rel:
            continue
        out = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with f.open(n) as src, open(out, "wb") as w:
            shutil.copyfileobj(src, w)
print("unpacked")
PYEOF

# ---------- 4 安装到目标目录（不覆盖本地数据 / 配置 / 运行时）----------
mkdir -p "$TARGET"
"$PY" - "$UNPACK" "$TARGET" <<'PYEOF'
import os, shutil, sys
src, dst = sys.argv[1], sys.argv[2]
skip_top = ("runtime", "logs", "backups", "data", "会话", "models", "kb", "plugin_data")
n = 0
for root, dirs, files in os.walk(src):
    rel = os.path.relpath(root, src)
    if rel == ".":
        dirs[:] = [d for d in dirs if d not in skip_top]
    for f in files:
        if f == "配置.qdai":          # 本地配置绝不覆盖
            continue
        s = os.path.join(root, f)
        d = os.path.join(dst, rel, f) if rel != "." else os.path.join(dst, f)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copyfile(s, d)
        n += 1
print("installed files:", n)
PYEOF
say "安装目录：$TARGET"

# ---------- 5 生成本地配置 ----------
if [ ! -f "$TARGET/配置.qdai" ]; then
  cp "$TARGET/配置.example.qdai" "$TARGET/配置.qdai"
  say "已生成 配置.qdai —— 首次聊天前请填自己的 api_key"
else
  say "保留已有 配置.qdai"
fi

# ---------- 6 可选文档依赖（核心对话不需要）----------
if [ "${INTENT_SKIP_DEPS:-0}" != "1" ] && [ -f "$TARGET/requirements-linux.txt" ]; then
  say "安装可选文档库（失败不影响核心对话）"
  "$PY" -m pip install --user --disable-pip-version-check -q \
      -r "$TARGET/requirements-linux.txt" 2>/dev/null || \
    say "  文档库安装失败或已跳过；核心对话零依赖，可直接用"
fi

# ---------- 7 注册全局 intent 命令 ----------
if [ "${INTENT_NO_PATH:-0}" != "1" ]; then
  mkdir -p "$BIN_DIR"
  CAT="$TARGET/chat.py"
  PYSHEBANG="$(command -v "$PY")"
  cat > "$BIN_DIR/intent" <<EOF
#!/usr/bin/env bash
# INTENT 全局命令（install.sh 生成）；改 THIS 指向的安装目录即可迁移
export no_proxy="127.0.0.1,localhost\${no_proxy:+,\$no_proxy}"
export NO_PROXY="\$no_proxy"
export PYTHONIOENCODING="utf-8"
exec "$PYSHEBANG" -u "$CAT" "\$@"
EOF
  chmod +x "$BIN_DIR/intent"
  say "已写入全局命令：$BIN_DIR/intent"

  # 超短别名 iw：重装 / 升级时只敲 iw，不用再输入那条长 URL。
  # 多个源依次尝试，任一通畅即执行；不用 github.com 主域（易被 SNI 重置）。
  cat > "$BIN_DIR/iw" <<'IWEOF'
#!/usr/bin/env bash
# INTENT 重装/升级快捷命令（等价于那条长的 curl | bash，自带多源回退）
for u in \
  "https://cdn.jsdelivr.net/gh/li-renji/intent-win@main/install.sh" \
  "https://raw.githubusercontent.com/li-renji/intent-win/main/install.sh" \
  "https://github.com/li-renji/intent-win/raw/main/install.sh"; do
  body="$(curl -fsSL --max-time 30 "$u" 2>/dev/null)"
  if [ -n "$body" ] && printf '%s' "$body" | head -1 | grep -q '^#!/usr/bin/env bash'; then
    printf '%s' "$body" | bash
    exit $?
  fi
done
echo "[INTENT] 所有下载源均失败；检查网络或代理后重试" >&2
exit 1
IWEOF
  chmod +x "$BIN_DIR/iw"
  say "已写入超短别名：$BIN_DIR/iw （以后重装/升级只敲 iw）"

  case ":${PATH}:" in
    *":$BIN_DIR:"*) say "PATH 已含 $BIN_DIR" ;;
    *)
      LINE="export PATH=\"$BIN_DIR:\$PATH\""
      for rc in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
        [ -f "$rc" ] || continue
        if ! grep -Fq "$BIN_DIR" "$rc" 2>/dev/null; then
          printf '\n# INTENT global command\n%s\n' "$LINE" >> "$rc"
        fi
        say "已把 $BIN_DIR 写入 $rc"
        break
      done
      export PATH="$BIN_DIR:$PATH"
      ;;
  esac
else
  say "INTENT_NO_PATH=1：未注册全局命令"
fi

# ---------- 8 自检（纯本地，不启引擎 / 不联网）----------
"$PY" -m py_compile "$TARGET/chat.py" "$TARGET/agent/main.py" "$TARGET/agent/tools.py" \
  || die "源码自检失败"
say "源码自检通过"

say "安装完成。新开一个终端，敲 intent 即可对话。"
say "图形界面不随 Linux 版安装（无界面核心）；首次聊天前请在 配置.qdai 填 api_key。"
