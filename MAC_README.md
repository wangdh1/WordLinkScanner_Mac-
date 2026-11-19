# WordLinkScanner macOS 版 —— 构建说明

这个软件用 Python + tkinter 编写。**macOS 应用必须在 Mac 本机编译**（PyInstaller 不能跨平台交叉编译），
所以这里给你的是「源码 + 一键构建脚本」。在你的 MacBook Air M3 上跑一次脚本，就会生成原生 `.app`。

## 前提条件（只需做一次）

1. 安装 **带 tkinter 的官方 Python 3.10~3.12**：
   - 方式 A（推荐）：到 python.org 下载 macOS 安装包，安装时保持默认选项（含 Tcl/Tk）。
   - 方式 B：Homebrew 用户执行 `brew install python-tk@3.12`。
   - 注意：macOS 系统自带的 `/usr/bin/python3` 通常**不带 tkinter**，脚本会自动跳过它去找带 tkinter 的版本。

2. 首次双击 `.command` 若被系统拦截：**macOS 15（Sequoia）起「右键 → 打开」已失效**，请到
   **系统设置 → 隐私与安全性 → 页面下方点「仍要打开」**；或者更省事的做法——终端执行
   ```
   chmod +x "一键编译Mac版.command"
   xattr -cr "一键编译Mac版.command"
   ```
   然后再双击（或在终端直接 `bash build_mac.sh`，效果完全相同、完全绕过这个拦截）。

## 构建步骤

1. 把整个文件夹（保持现有目录结构）拷贝到 Mac 上，例如放到 `~/WordLinkScanner`。
2. **双击 `一键编译Mac版.command`**，会自动：
   - 检测带 tkinter 的 Python；
   - 安装依赖；
   - 安装 Playwright Chromium（智能渲染模式用，政务站 WAF 需要；失败不阻塞构建）；
   - 用 `--onedir` 模式打包生成 `dist/WordLinkScanner.app`（macOS 上比 `--onefile` 更稳定、启动更快）；
   - 对 app 做 ad-hoc 签名（`codesign --sign -`），避免出现「应用已损坏」；
   - 顺带尝试生成 `dist/WordLinkScanner.dmg` 安装镜像（可选步骤，失败不影响 .app）。
3. 编译完成后，把 `dist/WordLinkScanner.app` 拖到「应用程序」文件夹，即可双击运行。

> 如果偏好命令行：终端进入文件夹后执行 `bash build_mac.sh`，效果完全相同。

> **dmg 是可选产物**：本地构建只要 `.app` 就能用；dmg 主要给「需要分发给别人」的场景。
> GitHub Actions 云端构建（见 `GITHUB_BUILD_README.md`）默认只产出 dmg，因为只有 dmg 能可靠地
> 保留 app 包结构、避免「已损坏」。

## 常见问题

- **首次打开提示"无法验证开发者"／"Apple 无法验证此 App 是否包含恶意软件"**：
  **macOS 15（Sequoia）起「右键 → 打开」的绕过方式已被 Apple 移除**，主方案改为：
  先双击一次 app（提示被阻止时点「完成」/「好」）→ **系统设置 → 隐私与安全性** →
  页面往下滚到「安全性」区域 → 点 **「仍要打开」** → 输入密码/指纹确认，之后即可正常启动。
- **提示"应用已损坏，无法打开，你应该将它移到废纸篓"**（多见于把 app 用 zip 传给别人后）：
  这不是文件坏了，而是 macOS 给下载文件加了隔离标记。**先执行下面一行，再双击**：
  ```
  xattr -cr /Applications/WordLinkScanner.app
  ```
  这条命令也是上面「无法验证开发者」场景的**并列解法**（效果等同去设置里点「仍要打开」）。
  路径换成 app 实际所在位置；如果 app 还在 dmg 挂载的窗口中（或本地构建后仍留在 `dist/` 里），
  路径分别是 `/Volumes/WordLinkScanner/WordLinkScanner.app`、`dist/WordLinkScanner.app`。
- **提示缺少 tkinter**：说明选到的 Python 不带 Tcl/Tk，按上面「前提条件」装官方 Python 后重试。
- **浏览器渲染不工作**：脚本已自动安装 Playwright Chromium；若仍失败，终端执行
  `<你的python> -m playwright install chromium`；软件里也可把抓取方式切到「仅 HTTP」。
- **图标**：打包已内置森林蚂蚁 `.icns`，Dock 与 Finder 均显示该图标。

## 与 Windows 版的差异

- 功能、字段、导出格式（12 列 Excel/CSV）完全一致。
- 渲染通道：Windows 版优先用系统 Edge/Chrome；macOS 版用 Playwright 自带 Chromium（脚本已安装）。
- 产物形式：Windows 是单个 `.exe`；macOS 是 `.app` 应用包（CI 额外打成 `.dmg` 分发，避免 zip 破坏 app）。

## 文件清单（构建需要的最小集）

```
app.py, scanner.py, requirements.txt,
assets/forest_ant.ico, assets/forest_ant.icns, assets/forest_ant_window.png,
build_mac.sh, 一键编译Mac版.command, MAC_README.md,
.gitignore, .gitattributes
```

> `.gitignore` 用来挡住 `build/`、`dist/`、`__pycache__/`、`*.spec` 等构建产物，
> 避免它们被误传进仓库分发（那正是「拿到旧配置、编出坏包」的根源）；
> `.gitattributes` 锁定脚本为 LF 行尾，防止在 Windows 上 checkout 后脚本变成 CRLF、
> 到 macOS 上执行报 `bad interpreter: /bin/bash^M`。
