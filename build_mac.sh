#!/bin/bash
# Word 超链接关键字扫描器 —— macOS 一键构建脚本（Apple Silicon / Intel 均可）
# 在本 Mac 上原生编译，产出 dist/WordLinkScanner.app
set -e
cd "$(dirname "$0")"

echo "======================================"
echo " WordLinkScanner  macOS 构建"
echo "======================================"

# 1) 选择 Python（需要带 tkinter；python.org 与 Homebrew 官方版都带）
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import tkinter" >/dev/null 2>&1; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "[ERROR] 没找到带 tkinter 的 Python。"
  echo "请安装 python.org 官方 Python 3.10~3.12（安装时勾选 Tcl/Tk），或："
  echo "  brew install python-tk@3.12"
  echo "安装后重新双击本脚本。"
  exit 1
fi
echo "[OK] Python: $($PY --version 2>&1) ($PY)"

# 2) 安装依赖
echo "[1/3] 安装依赖..."
"$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
"$PY" -m pip install -r requirements.txt

# 3) 浏览器渲染支持（可选但推荐：政务站 WAF 需要）
#    macOS 上优先用系统 Chrome/Edge；没有则装 Playwright 自带 Chromium。
echo "[2/3] 准备浏览器渲染内核（Playwright Chromium，供智能渲染模式使用）..."
"$PY" -m playwright install chromium || echo "[WARN] Chromium 安装失败，可稍后手动运行: $PY -m playwright install chromium"

# 4) 打包为 .app
echo "[3/3] 打包 macOS 应用..."
rm -rf build dist WordLinkScanner.spec 2>/dev/null || true
"$PY" -m PyInstaller \
  --onefile --windowed --clean --noconfirm \
  --name WordLinkScanner \
  --icon=assets/forest_ant.icns \
  --add-data "assets/forest_ant_window.png:assets" \
  --add-data "assets/forest_ant.icns:assets" \
  --collect-data certifi \
  --hidden-import=soupsieve \
  --hidden-import=charset_normalizer \
  --hidden-import=openpyxl \
  --collect-all playwright \
  --hidden-import=greenlet \
  app.py

echo ""
if [ -d "dist/WordLinkScanner.app" ]; then
  echo "======================================"
  echo " BUILD OK -> dist/WordLinkScanner.app"
  echo "======================================"
  echo "用法：把 dist/WordLinkScanner.app 拖到「应用程序」文件夹即可双击运行。"
  echo "提示：首次运行若被 Gatekeeper 拦截，右键 -> 打开；或执行："
  echo "  xattr -cr dist/WordLinkScanner.app"
else
  echo "[BUILD FAILED] 未生成 dist/WordLinkScanner.app"
  exit 1
fi
