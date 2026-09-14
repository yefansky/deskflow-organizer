# DESKFLOW MCP 接入

安装版自带 `DeskflowMCP.exe`，代码 agent 不需要额外安装 Python。它通过标准输入输出（stdio）提供 MCP 服务，再调用本机 `127.0.0.1:8765` 的桌面整理接口。无需把本机端口开放到局域网或互联网。

## 配置

将下面示例里的 `你的用户名` 换成实际 Windows 用户目录；自定义安装位置时使用对应绝对路径。JSON 配置通常不展开 `%LOCALAPPDATA%`，请使用真实路径。

```json
{
  "mcpServers": {
    "deskflow": {
      "command": "C:\\Users\\你的用户名\\AppData\\Local\\Programs\\DeskflowOrganizer\\DeskflowMCP.exe",
      "args": []
    }
  }
}
```

使用 TOML 的客户端可配置：

```toml
[mcp_servers.deskflow]
command = 'C:\Users\你的用户名\AppData\Local\Programs\DeskflowOrganizer\DeskflowMCP.exe'
args = []
```

保存后重新连接该 MCP 服务器。应用升级会关闭 MCP 连接，以释放安装文件；升级完成后在 agent 客户端重新连接即可。

## 使用顺序

1. 调用 `open_desktop_manager` 启动桌面整理器。
2. 调用 `get_desktop_catalog` 查看分类、文件摘要和人工归类。
3. 需要进一步了解文件时调用 `inspect_desktop_items`。
4. 用 `organize_desktop_items` 批量提交分类、摘要和标签，再读取目录复核。

也支持分类管理、单项归类、块内排序、项目顺序、块位置、布局、透明度和样式调整，以及恢复原生桌面图标。完整参数以客户端的 `tools/list` 返回为准。

默认保留 `manual=true` 的人工归类，除非用户要求调整。整理分类只更新显示和配置；MCP 不提供任意命令执行，也没有删除文件工具。

## 无法连接时

- 找不到 exe：核对安装路径，确认安装的是 EXE 版本。
- 提示桌面整理器未运行：先调用 `open_desktop_manager`，待启动后再次读取目录。
- 更新后连接断开：重新连接 MCP；更新时进程退出是为了解除文件占用。
- 私有仓库更新失败：本机需要 GitHub CLI 并已通过 `gh auth login` 登录具有仓库读取权限的账号；应用不会内置开发者凭据。
