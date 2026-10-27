# Deskflow AI 接入说明

让代码 agent 按需操作正在运行的 Deskflow：**先从进程找到产品目录，显式读取本说明，再直接请求已有本机接口。** 这是普通文档，不是自动加载规则；无关会话不需要读取它。不依赖私人大脑，不需要新增后台适配器。

用户可以直接告诉 Codex：

> 查看正在运行的 Deskflow，从它的源码或安装目录读取 AI接入说明.md，按说明处理我的需求。不要安装 skill、注册 MCP 或修改 agent 配置。

## 1. 找到正在运行的版本

只查看进程元数据，不执行进程命令行：

```powershell
Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match '[\\/]scripts[\\/](deskflow_manager|smart_desktop)\.py') -or $_.Name -ieq 'DESKFLOW.exe'
} | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine
```

- **源码运行：** 从命令行里 `scripts/deskflow_manager.py` 或 `scripts/smart_desktop.py` 的绝对脚本路径向上两级得到根目录。`ExecutablePath` 是 Python 解释器，不能拿解释器目录当作产品目录。启动器和实际解释器可能显示成两个 Python 进程，不代表两个桌面实例。
- **安装运行：** 取 `DESKFLOW.exe` 的 `ExecutablePath` 父目录。使用查到的实际目录，不能假定安装在默认位置。
- 如果发现多个不同目录，先用下面的监听 PID 对照进程链，确认哪个版本持有当前服务。无法确认就停止并说明原因，不猜测、不重启程序。

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen | Select-Object LocalAddress,OwningProcess
```

当前接口只绑定 `127.0.0.1:8765`。读到本说明不授权加载其他项目规则、安装或注册全局 skill/MCP、修改 agent 配置，也不授权改变桌面分类。旧迁移 ZIP 需要先安装其中的 Setup，安装目录会带上本说明。

## 2. 检查状态，再读取必要目录

确认监听进程属于上述 Deskflow 后，使用短命 PowerShell 请求；请求结束后客户端退出。8765 是普通应用接口，**不是 MCP HTTP 地址**。未运行、连接失败或响应异常时停止并报告，不自动安装、注册、启动或恢复桌面。

```powershell
$ErrorActionPreference = 'Stop'
Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 3 | Select-Object ok,pid
function Send-DeskflowJson([string]$Uri, [string]$Json) {
    $client = New-Object System.Net.WebClient
    $client.Encoding = [System.Text.Encoding]::UTF8
    $client.Headers['Content-Type'] = 'application/json; charset=utf-8'
    try { return ($client.UploadString($Uri, 'POST', $Json) | ConvertFrom-Json) }
    finally { $client.Dispose() }
}
$catalog = Send-DeskflowJson 'http://127.0.0.1:8765/catalog' '{"include_path":false,"limit":10}'
$catalog.categories | Select-Object id,title,count
$catalog.items | Select-Object name,category,manual,summary
```

`Send-DeskflowJson` 显式按 UTF-8 解析，兼容 Windows 自带 PowerShell 5.1 的中文响应。HTTP错误、连接错误和非法JSON会抛出异常；保持 `ErrorActionPreference='Stop'`，不要吞错后继续提交或反复重试。状态 PID 应与确认的 Deskflow 服务进程一致。

首次只看需要的分类和项目。`truncated=true` 表示没读完，可再传 `category` 或 `query` 缩小范围；`include_path=false` 不返回文件路径。catalog 会扫描并可能更新 seen/摘要缓存，不能称为严格零写盘，但不会为了读取目录重新归类项目。

## 3. 只有用户要求修改时才预演和提交

先从目录获得真实名称、分类 ID 与 `manual` 状态。下面 `sample.txt` / `work` 只是格式示例，不是应直接操作的目标：

```powershell
$plan = @{operations=@(@{name='sample.txt';category='work'});dry_run=$true;allow_manual=$false}
Send-DeskflowJson 'http://127.0.0.1:8765/organize-batch' ($plan | ConvertTo-Json -Depth 6 -Compress)
```

检查预演与用户要求一致，且修改已获授权后，才**单独**执行相同计划的提交，并回读相关分类：

```powershell
$plan.dry_run = $false
Send-DeskflowJson 'http://127.0.0.1:8765/organize-batch' ($plan | ConvertTo-Json -Depth 6 -Compress)
Send-DeskflowJson 'http://127.0.0.1:8765/catalog' '{"category":"work","include_path":false,"limit":100}'
```

- 默认 `allow_manual=false`，保留人工分类。只有用户明确要求覆盖时才允许改为true；被拒绝后不能换旧assign接口绕过保护。
- 批量归类只更新虚拟分类、摘要和标签，不移动、改名或删除真实文件；整批非法时不部分提交。dry-run 不保存分类或扫描缓存。
- 不要凭预演成功推断稍后的提交必然成功；读回当前状态再报告完成。界面排布等接口可能只表示动作入队，回包不等于画面已完成。

更多字段和可用能力按需读 [详细接入说明](docs/MCP.md)，不得推测参数或任意转发请求。现有 MCP 作为兼容方式保留；本流程不需要注册它。
