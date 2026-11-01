# Deskflow 配套 skill

这是随软件安装的独立能力包，包含接入步骤、图标分类方法、摘要/标签语义、预演与回读核验。普通本机代码 agent 可直接读取，不需要私人知识库、Python开发环境。

## 在哪里找到

默认安装目录为 `%LOCALAPPDATA%\Programs\DeskflowOrganizer`，自定义安装请使用 DESKFLOW.exe 的实际目录。入口是 `skills/deskflow-organizer/SKILL.md`；程序根 README 和 AI接入说明均有链接，开始菜单提供“Deskflow AI 使用说明”。

可复制给 agent：

> 找到正在运行的 Deskflow，从其产品目录读取 skills/deskflow-organizer/SKILL.md。按说明查看我指定的图标、保护人工归类，先预演再执行已授权的整理，最后回读核验。不要安装全局 skill 或修改 agent 配置。

## 包内文件

- [SKILL.md](SKILL.md)：实际整理流程与分类判断。
- [连接说明](references/connection.md)：进程发现、PID核对、本机请求及中文编码示例。
- [接口参考](references/api.md)：新版本机HTTP接口、参数和验证边界。

- [常见问题](references/troubleshooting.md)：公共图标管理员授权/UAC、权限、显示、连接、更新及安全处理。

默认保留在产品目录按需读取，不自动写入任何 agent 全局目录。需要客户端原生 skill 发现时，用户可自行选择将整个 `deskflow-organizer` 文件夹交给其客户端支持的技能导入功能，或放入该客户端明确支持的目录；发现规则因客户端而异。移动本包后仍通过运行进程找产品目录。仅远程云端运行且不能读本机文件/访问本机接口的 agent 无法直接控制此桌面。

本包只含通用文档和虚构示例，不含桌面清单、运行日志、个人账号、令牌、机器地址或用户布局。升级会更新产品目录内的文档；自行复制的副本需由用户同步更新。
