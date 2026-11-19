# 用你自己的 GitHub 账号，在云端 Mac 上免费编译出 macOS 安装包（.dmg）

这个方法**不用你装任何软件、也不用你懂命令行**：GitHub 免费提供 Mac 编译环境，
你只要把源码传到自己的仓库，点一下按钮，就能下载编译好的 macOS 安装包 `.dmg`，
双击打开后把应用拖进「应用程序」即可使用。

> 为什么可行：macOS 程序必须在 Mac 上编译，而 GitHub 提供免费的 Mac（Apple Silicon，和你的 M3 同架构）云主机。

## 第一步：建仓库并上传源码（约 3 分钟）

1. 打开 https://github.com ，登录你的账号。
2. 点右上角 `+` → `New repository`，仓库名随意（如 `wordlinkscanner`），选 **Public**，勾上
   "Add a README file"，点 `Create repository`。
3. 进入仓库页面，点 `Add file` → `Upload files`，把解压后的 `WordLinkScanner` 文件夹里
   **全部文件和子文件夹**（含 `.github` 文件夹、`assets` 文件夹）拖进去，点 `Commit changes`。
   - 注意：`.github` 是隐藏文件夹，拖整个文件夹进去即可，别漏掉。
   - 不需要上传 `build/`、`dist/`、测试文件——传核心文件即可（见下方清单）。

## 第二步：点一下按钮开始编译

1. 在仓库页面点顶部 `Actions` 标签。
2. 左侧会出现 `Build macOS App (WordLinkScanner)`，点它 → 右侧点 `Run workflow` → 再点绿色 `Run workflow`。
3. 等待 3~8 分钟（页面会实时显示进度）。

## 第三步：下载 dmg 并安装（在 Mac 上操作）

1. 编译完成后，同页面往下拉，找到 `Artifacts` 区域的 **`WordLinkScanner-macOS-dmg`**，点击下载
   （下载得到的是一个 `.zip`，里面装着 `WordLinkScanner.dmg`；Artifacts 一律用 zip 封装，这是 GitHub 的规定）。
2. 把 `WordLinkScanner.dmg` 从压缩包里解出来，**双击打开**它 —— 会弹出一个窗口，里面有
   `WordLinkScanner` 应用图标和 `Applications`（应用程序）快捷方式。
3. 把 **`WordLinkScanner` 图标拖到 `Applications` 上**，松手即完成安装（这就是常见的 Mac 安装方式）。
4. 打开「启动台」或「应用程序」文件夹，双击 `WordLinkScanner` 运行。

> **首次打开若提示「无法验证开发者」**：别点「删除」，改成 **右键点击 app 图标 → 打开 → 再点「打开」**，
> 之后系统就记住这个选择了，以后双击可正常打开。

> **如果仍提示「应用已损坏，无法打开，你应该将它移到废纸篓」**：这不是文件真的坏了，而是 macOS 给下载
> 的文件打了「隔离」标记。打开「终端」（Launchpad 里搜 `终端` 或 `Terminal`），粘贴下面一行回车即可：
>
> ```
> xattr -cr /Applications/WordLinkScanner.app
> ```
>
> 然后重新双击 app 就能运行。注意路径要对应你实际放置的位置：如果 app 还在 dmg 窗口里没拖进应用程序，
> 命令应写成 `xattr -cr /Volumes/WordLinkScanner/WordLinkScanner.app`。

## 为什么不直接给 `.app` 而是给 `.dmg`？

`.app` 其实是一个**文件夹**（内部靠符号链接组织），用 zip 压缩/解压会破坏这些链接与文件属性，
用户拿到后经常看到「应用已损坏」。`.dmg` 是 macOS 原生磁盘镜像，能完整保留 app 结构，
并且自带「拖到 Applications」的安装引导，是 Mac 上分发软件的标准做法。

## 需要上传的最小文件清单

```
app.py
scanner.py
requirements.txt
assets/forest_ant.icns
assets/forest_ant_window.png
.github/workflows/build-mac.yml      ← 关键：云端编译脚本，别漏
```

（其余文件传了也不影响。）

## 常见问题

- **Actions 页面没出现那个工作流**：确认 `.github/workflows/build-mac.yml` 已上传且路径正确（`.github` 是点开头的隐藏目录）。
- **报错 `The configuration 'macos-14-...' is not supported`**：当前配置已改为 `macos-latest`。若你之前上传的是旧版，请在仓库里打开 `.github/workflows/build-mac.yml` → 编辑 → 把 `runs-on: macos-14` 改成 `runs-on: macos-latest` → Commit，再重新 Run workflow。若 `macos-latest` 仍不支持，改成 `macos-13`（Intel 芯片，M3 上会通过 Rosetta 2 运行，功能一致）。
- **编译失败红色叉**：点进去看日志，把报错发我。
- **不想用 Public 仓库**：Private 仓库的 Actions 分钟数有免费额度，小项目够用，也可选 Private。
- **下载需要登录 GitHub**：Artifacts 需登录账号下载，属正常。
- **Artifacts 里为什么只有一个 zip？我要的是 dmg**：GitHub 规定 Artifacts 一律打包成 zip，dmg 就在这个 zip 里，解压即得 `WordLinkScanner.dmg`。
- **dmg 双击后提示「无法打开，因为它来自身份不明的开发者」**：在 dmg 上右键 → 打开 → 再点「打开」；若 dmg 已挂载，直接把 app 拖进应用程序后按上面第三步的 `xattr -cr` 处理即可。
- **能不能在 Windows 上直接运行**：不能。macOS 的 `.app` 只能在 Mac 上运行，Windows 版请用 `build.bat` 编出的 `.exe`。
- **日志里出现 `[WARN] Chromium 安装失败`**：不影响打包，只影响「智能渲染模式」；打开软件后在抓取方式里选「仅 HTTP」也能正常工作。

## 为什么不直接由我登录你的 Mac 生成？

我运行在 Windows 环境，只能操作本机文件与命令行，没有远程桌面/浏览器登录能力，无法代替你登录 Mac 图形界面。
GitHub Actions 是把"编译"这件事放到云端 Mac 上自动完成，等价于借了一台 Mac 给你，且全程你自己的账号、安全可控。
