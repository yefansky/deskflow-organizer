# DESKFLOW 智能桌面整理器

DESKFLOW 是面向 Windows 的桌面覆盖层与托盘管理器。它扫描桌面文件并按类别显示透明整理块，不会为了排版移动文件；用户明确删除时，文件进入 Windows 回收站。

## 安装

支持 Windows 10/11 x64，**不需要预装 Python**。

1. 在本仓库 Releases 页面下载 `DESKFLOW-windows-x64-Setup.exe`。
2. 双击安装，默认安装到 `%LOCALAPPDATA%\Programs\DeskflowOrganizer`，无需管理员权限。
3. 安装后桌面有 `DESKFLOW 管理器` 快捷方式，也可通过开始菜单启动。管理器默认驻留右下角托盘。

安装包包含主程序、运行依赖和 `DeskflowMCP.exe`。首次安装需要下载完整安装包，安装过程无需联网安装 Python 依赖。

## 更新

应用启动约 12 秒后自动检查 GitHub 最新正式 Release，也可从管理器或托盘手动“检查更新”。发现新版本后提示确认，下载并验证 SHA-256，等待旧进程退出，再安装并重启。升级保留桌面分类、布局、标签和管理器设置。更新失败的交接日志位于 `%LOCALAPPDATA%\DeskflowOrganizer\updates\版本号\`。

仓库目前私有，更新需要本机安装 [GitHub CLI](https://cli.github.com/) 并执行 `gh auth login`，登录有仓库读取权限的账号。应用不内置开发者凭据；若仓库以后公开，可直接通过 HTTPS 更新。后台检查失败只显示状态，手动检查会展示具体错误。

v0.2 的 ZIP 更新器仍可下载 `DESKFLOW-windows-x64.zip`，它现在是迁移到 EXE 版的兼容包。已有 Python 快捷方式和 MCP 脚本入口会转发到安装后的 EXE。旧 `.venv` 不自动清理，以免破坏仍引用它的客户端配置。

## 文件右键菜单

整理块内右键文件或快捷方式会显示 Windows 原生文件菜单，包括关联程序提供的“编辑”、属性和第三方扩展；Shift+右键可显示扩展操作。菜单依据实际文件生成，快捷方式不会被替换为目标文件。

菜单末尾的“DESKFLOW 整理”保留打开所在位置、放回原生桌面等待整理、归到其他分类、取消手动归类、重命名和移到回收站。原生菜单的重命名也使用整理器的改名流程，保留分类、标签和块内顺序。系统菜单加载失败时会说明原因并显示原有备用菜单，可通过打开所在位置进入资源管理器。

## 让代码 agent 操作桌面

安装版自带标准输入输出 MCP 服务，入口为安装目录的 `DeskflowMCP.exe`。它支持目录读取、摘要、标签、批量归类、分类管理、布局、样式、排序和位置调整，服务仅连接本机。

配置示例和操作顺序见 [MCP 接入说明](docs/MCP.md)。旧版 `scripts/mcp_server.py` 配置也可继续使用。更新时 MCP 连接会退出以释放文件占用，升级后在客户端重新连接即可。

## 卸载

通过 Windows“已安装的应用”、开始菜单或管理器中的卸载入口执行。卸载移除程序和启动入口，**保留用户设置，不删除桌面文件**。从旧版迁移后遗留的 Python 虚拟环境可能仍在安装目录中，需要停止旧客户端引用后再清理。

## 自动构建发布

- 推送或合并到 `main`：测试、冻结 GUI/MCP、构建 EXE、真实安装与两版升级验证，通过后自动上传 GitHub Release。
- Pull Request：执行相同构建验证，产物保存在 Actions，不发布 Release。
- 手动运行：在 main 上构建发布。
- 推送 `v*` 标签：标签必须与源码 VERSION 一致。

主干自动版本使用 `VERSION` 的主次版本加 Actions 运行序号，例如 `0.3.12`。Release 先以草稿上传 EXE、ZIP 和各自 SHA-256，全部成功后才发布；客户端只接受更高版本。完整安装包是 `DESKFLOW-windows-x64-Setup.exe`，ZIP 用于旧版本迁移。

## 本地开发与构建

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller==6.20.0
.\.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_*.py" -v
.\.venv\Scripts\pythonw.exe scripts\deskflow_manager.py --start
# 安装 Inno Setup 6 后：
.\packaging\build.ps1 -Python .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe packaging\verify_bundle.py dist\DESKFLOW
```

`packaging/verify_install.py` 仅在一次性的 Windows Actions 环境运行，验证真实安装、桌面快捷方式、旧版到新版升级、MCP 文件占用释放、旧配置兼容和卸载保留设置。本机冻结启动检查不启动桌面覆盖层、不改动实际桌面。
