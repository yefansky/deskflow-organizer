# DESKFLOW MCP 接入与能力发现

安装版自带 `DeskflowMCP.exe`，其他代码 agent 无需安装 Python、Git 或开发环境。服务使用标准 MCP **stdio**，只通过 `127.0.0.1:8765` 访问本机整理器。这个 HTTP 端口是内部应用 API，不是 Streamable HTTP MCP 地址；不要把它直接配置为 MCP URL，也不需要开放外网端口。

## 生成当前机器配置

在 PowerShell 运行一次：

```powershell
& "$env:LOCALAPPDATA\Programs\DeskflowOrganizer\DeskflowMCP.exe" --print-config
```

程序只输出配置 JSON 后退出，不启动桌面、不监听端口、不写客户端配置。输出使用实际绝对路径，支持中文和空格；自定义安装位置时执行该目录下的 EXE。把输出合并到所用 agent 客户端的 MCP 配置即可：

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

客户端配置中的 JSON 路径通常不展开 `%LOCALAPPDATA%`，应优先复制程序生成的结果。使用 TOML 的客户端填写同一绝对路径：

```toml
[mcp_servers.deskflow]
command = 'C:\Users\你的用户名\AppData\Local\Programs\DeskflowOrganizer\DeskflowMCP.exe'
args = []
```

MCP 标准化的是**接入后的能力发现**；客户端配置文件位置由各客户端决定。DESKFLOW 不会擅自修改其他 agent 的配置。已经配置过旧入口的客户端继续可用；源码开发者也可运行 `python scripts/mcp_server.py --print-config`，生成当前 Python 及脚本的绝对路径配置。正式安装用户使用 EXE。

## agent 如何自动发现方法

1. 客户端启动服务并发送 `initialize`；服务返回 `tools`、`resources`、`prompts` 能力及 `instructions`。
2. 发送 `notifications/initialized` 通知，然后用 `tools/list` 获取完整工具名、中文语义、参数 JSON Schema 和只读提示。当前共有 **18 个工具**，保留既有 17 个名字。
3. `resources/list` 可发现 `deskflow://guide/organizing`，通过 `resources/read` 读取整理方法。`prompts/list` / `prompts/get` 提供 `organize-desktop` 工作流。无需安装某个专用 skill 才能发现这些说明。
4. 协议协商支持 `2024-11-05`、`2025-03-26`、`2025-06-18`；不支持的版本返回 `2025-06-18` 供客户端判断是否继续。未知协议方法返回 `-32601`，未知工具/非法参数返回 `-32602`；应用未启动、业务校验等执行失败使用工具结果 `isError=true`，内容里附结构化错误。JSON-RPC 通知不回复。

静态能力发现不启动桌面整理器。需要实际桌面数据时，先调用 `open_desktop_manager`，待应用就绪再读取；不会在只读调用失败时自动改变桌面可见性。

## 分类与排布感知

| 需要了解的内容 | 工具与边界 |
| --- | --- |
| 分类 ID、名称、用途、数量、模式、排序；图标顺序、摘要、标签、人工归类 | `get_desktop_catalog`，支持分类/关键词/数量过滤，默认不返回路径 |
| 块位置、大小、各分辨率布局档案、显示模式与排序 | `get_desktop_arrangement`，只返回保存的布局配置，不带文件路径/摘要；位置不是当前窗口像素测量，未保存的自动位置可能为空 |
| 个别文件/目录用途、快捷方式目标、同名歧义 | `inspect_desktop_items`，最多50项；按需使用，避免为普通整理深查全部文件 |
| 完整模型诊断 | `desktop_layout`，返回完整配置，普通整理优先使用以上紧凑工具 |

`get_desktop_catalog` 的 `truncated=true` 表示结果超出上限；请按分类进一步读取。布局工具输出 `coordinate_source=saved_configuration`，并保留每个布局档案的坐标系；不要把不同显示器/分辨率档案混用为当前窗口位置。

## 预演、批量提交与人工习惯

建议先读目录，再按需深查，最后使用一次 `organize_desktop_items` 原子提交。批次中任意项目非法时整批拒绝，不写一部分。`manage_desktop_categories` 同样支持原子批量和 `dry_run`。

```json
{
  "name": "organize_desktop_items",
  "arguments": {
    "operations": [
      {"name": "会议记录.txt", "category": "work", "summary": "本周会议纪要", "tags": ["会议"]}
    ],
    "dry_run": true
  }
}
```

确认计划符合用户意图后，以相同 operations、`dry_run=false` 提交，再读目录复核。预演会在隔离快照中扫描和校验，既不保存分类，也不保存扫描产生的 seen/摘要缓存，不发送界面更新事件。

- `manual=true` 表示已有人工固定分类。批量工具默认拒绝把它移到别类或恢复自动规则；同分类和只改摘要/标签仍允许。
- 只有用户**明确要求覆盖人工分类**时，agent 才应设置 `allow_manual=true`。服务只能检查这个布尔参数，无法自行证明人类授权。字符串 `"true"` / `"false"` 都无效。
- `assign_desktop_item` 是旧的显式人工固定原语；自动整理应优先使用带默认人工保护的批量工具。不要把它用作绕过 manual 保护的途径。
- 标签支持 `tag_mode=replace/merge/remove`；批次在 Store 锁内校验、合并和提交，避免并行批量标签覆盖。`reset_category=true` 与 `category` 不能同时提供。
- 分类操作只更新虚拟显示和本地配置，不移动、改名、删除真实文件；没有任意命令执行或文件删除工具。自定义分类仅空块可删除，内置分类不删除。
- 排序、模式、样式、移动块等工具会修改布局。先依据用户要求读取相关状态，完整参数以 `tools/list` 为准。移动块遵守应用的防重叠和持久化规则。

## 诊断与更新

正常运行 stdout 只写 MCP 协议。需要诊断时在客户端 args 中加入 `["--log-level", "debug"]`（也支持 info/warn/error）。日志在应用同目录，包含启动日期、进程与角色，达到200 KiB轮转；每行包含日期时间、线程号、级别和内容。MCP 调用记录工具名、耗时，执行异常附堆栈，不主动记录参数和桌面项目内容；业务异常文本可能包含项目名，日志仍按本机私人材料处理。启动管理器的子进程输出与协议 stdout 隔离。

- 找不到 EXE：核对实际安装目录；源码版本的 Python 配置和正式 EXE 配置不要混用。
- 工具结果提示应用未运行：先调用 `open_desktop_manager`，待启动后重试。
- 更新断开连接：升级会通知 MCP 退出以释放安装文件，完成后在客户端重新连接。
- 更新下载失败：安装版通过内置匿名 HTTPS 下载公开 GitHub Release；网络或限流失败时稍后重试，无需 GitHub 登录、Git 或 GitHub CLI。

协议依据：[MCP 生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)、[stdio 传输](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)、[工具与错误处理](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)、[资源发现](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)。
