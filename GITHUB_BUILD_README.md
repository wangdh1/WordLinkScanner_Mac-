# 用你自己的 GitHub 账号，在云端 Mac 上免费编译出 .app

这个方法**不用你装任何软件、也不用你懂命令行**：GitHub 免费提供 Mac 编译环境，
你只要把源码传到自己的仓库，点一下按钮，就能下载编译好的 macOS 版 `.app`。

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

## 第三步：下载 .app

1. 编译完成后，同页面往下拉，找到 `Artifacts` 区域的 `WordLinkScanner-macOS-ARM64`，点击下载。
2. 解压后得到 `WordLinkScanner.app`，拖进 Mac 的「应用程序」即可双击运行。
   - 若提示"无法验证开发者"：右键 → 打开，或终端执行 `xattr -cr WordLinkScanner.app`。

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
- **编译失败红色叉**：点进去看日志，把报错发我。
- **不想用 Public 仓库**：Private 仓库的 Actions 分钟数有免费额度，小项目够用，也可选 Private。
- **下载需要登录 GitHub**：Artifacts 需登录账号下载，属正常。

## 为什么不直接由我登录你的 Mac 生成？

我运行在 Windows 环境，只能操作本机文件与命令行，没有远程桌面/浏览器登录能力，无法代替你登录 Mac 图形界面。
GitHub Actions 是把"编译"这件事放到云端 Mac 上自动完成，等价于借了一台 Mac 给你，且全程你自己的账号、安全可控。
