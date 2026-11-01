# Deskflow 本机 HTTP 接口

先按 [连接说明](connection.md) 核实运行进程和监听 PID，再访问 `http://127.0.0.1:8765`。仅用于本机，不开放到公网或局域网。所有 POST 使用 JSON 与 UTF-8；连接错误、HTTP 400/422、非法 JSON 都应停止该次提交并解释原因，不吞错重试。只调用用户任务需要的接口。

## 目录与深查

- GET `/health`：`ok`、`pid`、时间和桌面路径。普通检查只输出 ok/pid，避免把私人路径抄进报告。
- POST `/catalog`：可选 `category`（真实ID）、`query`（名称/摘要/标签）、`include_items`（默认true）、`include_path`（默认false）、`limit`（1–500，默认300）。返回分类、项目、人工状态与截断标记。先看大类可用 `{"include_items":false}`。会扫描并可能更新seen/摘要缓存，不能称为严格零写盘；不会重新归类。
- POST `/inspect-items`：`{"names":["示例计划.docx"],"refresh":false}`；1–50项。返回路径、快捷方式目标和用途画像；文件夹仅有界两层目录信息，不读正文。同名逐项返回并标记 `duplicate_name`。刷新不会覆盖人工摘要。
- GET `/layout`：完整分类、项目与配置，用于必要的布局诊断；可能含全部路径和元数据，不用于普通归类，也不对外发送。保存的几何按屏幕/分辨率区分，不能当作实时像素测量。

## 批量归类与元数据

POST `/organize-batch`：

```json
{"operations":[{"name":"示例计划.docx","category":"work","summary":"计划与里程碑文档","tags":["计划"],"tag_mode":"merge"}],"dry_run":true,"allow_manual":false}
```

所有例子都必须替换为目录返回的真实名称、ID。最多200项；先整批校验后一次保存。任一名称不存在、分类无效、字段非法或人工保护拒绝，整批不提交。

- `name` 必填；归类/重置要求名称唯一。
- `category` 可省略，仅改摘要/标签；`reset_category=true` 清除人工归类，恢复自动规则，与category互斥。
- `summary` 最多240字，空字符串清除人工摘要。`tags` 最多12个，每个最多32字符。
- `tag_mode`：`merge`追加去重，`remove`移除，`replace`完整替换（默认）；空数组配replace清空标签。
- `dry_run=true`：隔离校验和扫描，不保存分类、seen/摘要缓存，也不发界面更新。
- `allow_manual=false`：默认保护人工分类。用户明确授权跨分类/重置人工项才设true；使用真正JSON布尔值，字符串无效。同分类或只更新元数据仍允许。

预演通过且修改已获授权后，另发相同operations、`dry_run=false`，最后过滤回读 `/catalog`。不要用单项旧接口绕过保护。

## 分类块

POST `/categories`，最多30项，支持同一 `dry_run` 预演/提交流程：

```json
{"actions":[{"action":"create","title":"研究资料","color":"#8BD3FF"}],"dry_run":true}
```

创建提交后从返回结果取得新ID，再归类项目。重命名：`{"action":"rename","category":"实际ID","title":"长期研究"}`；删除：`{"action":"delete","category":"实际ID"}`。标题最多40字符。内置分类不能删除，自定义分类仅空块可删除。不要把删除分类扩展成删除文件。

## 布局与外观（仅用户要求时）

这些接口会保存设置或把动作交给界面线程，不支持批量接口的dry_run。先读相关状态、只发送需要改变的字段，随后核对保存状态和实际画面。`ok=true` 可能仅代表入队；不能直接宣称画面已完成。

| POST 路径 | JSON 字段与用途 |
| --- | --- |
| `/panel-mode` | `category`、`mode`：list / compact / cards |
| `/set-sorting` | `category`、`mode`：smart / name / modified / custom |
| `/reorder-item` | `category`、`name`，可选 `before`；省略before放末尾并保存人工顺序 |
| `/panel-style` | `category`；可选 `background`（#RRGGBB）、`alpha`（0.08–0.70）、`reset`（true清除后再应用给定字段） |
| `/arrange-top-right` | `{}`；按产品算法重算块尺寸并在所选屏幕排布，会改变现有位置 |
| `/move-panel` | `category`、整数 `x` / `y`；界面执行移动，遵守防重叠和屏幕规则 |
| `/settings` | 按需修改 `font_size`（8–14）、`cell_size`（44–72）、`layout_footprint`（0.45–0.95）、`alpha`（0.08–0.70）、`background`、`auto_contrast`、`target_monitor`；密度/占用或屏幕调整可能触发重排 |
| `/rescan` | `{}`；重新扫描并请求重绘，不移动文件 |
| `/open-settings` | `{}`；打开产品设置窗口 |

`alpha` 只改变底板，文字/图标不应随之变淡。不要直接提交不明来源的geometry或整份配置。屏幕坐标/显示器选择应按当前现场核对，未知时使用产品设置界面。

用户明确要求恢复原生桌面时才 POST `/show-native-icons`（`{}`）：这会关闭整理覆盖层并恢复Explorer图标。普通连接失败或分类失败不要自动调用它。

常见问题与管理员授权见 [排查说明](troubleshooting.md)。
