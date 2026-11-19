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
echo "[1/4] 安装依赖..."
"$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
"$PY" -m pip install -r requirements.txt

# 3) 浏览器渲染支持（可选但推荐：政务站 WAF 需要）
#    macOS 上优先用系统 Chrome/Edge；没有则装 Playwright 自带 Chromium。
echo "[2/4] 准备浏览器渲染内核（Playwright Chromium，供智能渲染模式使用）..."
"$PY" -m playwright install chromium || echo "[WARN] Chromium 安装失败，可稍后手动运行: $PY -m playwright install chromium"

# 4) 打包为 .app
#    用 --onedir（不要 --onefile）：macOS 上 onedir 产出标准 .app 包，
#    启动更快、结构清晰、可被完整签名，稳定性明显优于 onefile。
echo "[3/4] 打包 macOS 应用..."
rm -rf build dist WordLinkScanner.spec 2>/dev/null || true
"$PY" -m PyInstaller \
  --onedir --windowed --clean --noconfirm \
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
  # 5) ad-hoc 签名：消除「应用已损坏，无法打开」（不需要开发者证书）
  echo "[4/4] 为 app 做 ad-hoc 签名..."
  xattr -cr dist/WordLinkScanner.app 2>/dev/null || true
  if codesign --force --deep --sign - dist/WordLinkScanner.app 2>/dev/null; then
    codesign --verify --deep --strict dist/WordLinkScanner.app 2>/dev/null \
      && echo "[OK] 签名校验通过" \
      || echo "[WARN] 签名校验未通过，可通过 xattr -cr 处理"
  else
    echo "[WARN] ad-hoc 签名失败（不影响本机运行，可手动执行：codesign --force --deep --sign - dist/WordLinkScanner.app）"
  fi

  echo "======================================"
  echo " BUILD OK -> dist/WordLinkScanner.app"
  echo "======================================"
  echo "用法：把 dist/WordLinkScanner.app 拖到「应用程序」文件夹即可双击运行。"
  echo "提示：首次运行若提示「无法验证开发者」，请到 系统设置 -> 隐私与安全性，"
  echo "      页面下方点「仍要打开」（macOS 15 起「右键->打开」已失效）；"
  echo "      若提示「已损坏，无法打开」，先执行："
  echo "        xattr -cr dist/WordLinkScanner.app"

  # 6) 可选：顺手生成 dmg 安装包（任何一步失败都只告警，不影响 .app 已构建成功）
  if command -v hdiutil >/dev/null 2>&1; then
    echo ""
    echo "[可选] 生成 dmg 安装包..."
    rm -rf dmg-src dist/WordLinkScanner.dmg 2>/dev/null || true
    dmg_ok=1
    mkdir -p dmg-src || dmg_ok=0
    if [ "$dmg_ok" = "1" ]; then
      # macOS 官方推荐用 ditto 复制 bundle（完整保留符号链接/扩展属性/权限）
      if command -v ditto >/dev/null 2>&1; then
        ditto dist/WordLinkScanner.app dmg-src/WordLinkScanner.app || dmg_ok=0
      else
        cp -R dist/WordLinkScanner.app dmg-src/ || dmg_ok=0
      fi
    fi
    if [ "$dmg_ok" = "1" ]; then
      ln -s /Applications dmg-src/Applications || dmg_ok=0
    fi
    if [ "$dmg_ok" = "1" ]; then
      hdiutil create -volname "WordLinkScanner" -srcfolder dmg-src \
        -ov -format UDZO dist/WordLinkScanner.dmg >/dev/null || dmg_ok=0
    fi
    if [ "$dmg_ok" = "1" ] && [ -s dist/WordLinkScanner.dmg ]; then
      hdiutil verify dist/WordLinkScanner.dmg >/dev/null 2>&1 || true
      echo "[OK] 已生成 dist/WordLinkScanner.dmg（双击打开，把 app 拖进 Applications 即可）"
    else
      echo "[WARN] dmg 生成失败，但不影响 dist/WordLinkScanner.app 的使用"
    fi
    rm -rf dmg-src 2>/dev/null || true
  fi
else
  echo "[BUILD FAILED] 未生成 dist/WordLinkScanner.app"
  exit 1
fi
