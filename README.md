# DESKFLOW 智能桌面整理器

DESKFLOW 是面向 Windows 的桌面覆盖层与托盘管理器。它扫描桌面文件并按类别显示透明整理块，不会为了排版擅自移动文件；用户明确删除时，文件进入 Windows 回收站。

## 安装

支持 Windows 10/11，需要 Python 3.11 或 3.12，并需要联网下载 Python 依赖。

1. 在仓库的 **Actions** 页面下载最新 `deskflow-windows` 构建产物，或在 **Releases** 页面下载版本 ZIP。
2. 解压 ZIP。
3. 双击 `安装DESKFLOW.bat`。

安装器会把程序复制到 `%LOCALAPPDATA%\Programs\DeskflowOrganizer`，在该目录创建独立 Python 环境并安装依赖，然后启动管理器。安装不需要管理员权限。管理器会创建桌面快捷方式和桌面右键入口。

日常启动可使用桌面的 `DESKFLOW 管理器` 快捷方式，也可双击安装包里的 `启动DESKFLOW.bat`。管理器内可控制自动启动、透明度、字号、布局密度和显示屏。

卸载时双击 `卸载DESKFLOW.bat`。默认同时删除 DESKFLOW 配置，但不会删除任何桌面文件；如需保留设置，可在 PowerShell 中运行：

```powershell
.\uninstall.ps1 -KeepSettings
```

## 本地开发与测试

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_*.py" -v
.\.venv\Scripts\pythonw.exe scripts\deskflow_manager.py --start
```

MCP 服务入口是 `scripts/mcp_server.py`。若要让本地智能代理连接已经运行的 DESKFLOW，可把该脚本配置为标准输入输出（stdio）MCP 服务器。

## 自动构建

GitHub Actions 在以下情况运行 Windows 构建：

- 向 `main` 提交或合并代码；
- 发起 Pull Request；
- 手动点击 `Run workflow`；
- 推送形如 `v0.1.0` 的版本标签。

流水线安装正式依赖、执行 Python 编译检查与全部单元测试，并生成带 SHA-256 校验文件的 `DESKFLOW-windows-x64.zip`。版本标签还会自动创建 GitHub Release。
