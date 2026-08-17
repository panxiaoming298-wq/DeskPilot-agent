# 阶段 64：受控 MCP stdio 最小闭环

## 1. 本阶段目标

阶段 63 已建立本地知识只读闭环。本阶段按 MCP `2025-11-25` 稳定协议实现首个真实 stdio 连接，但不开放任意 Server 命令、第三方包、模型自动调用或写能力。用户在前端审阅固定 manifest 后显式启用，才能调用内置 `deskpilot.readonly-text` Server 的 `deskpilot.text.metrics`。

实现遵循官方 [Lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) 的 initialize、版本/能力协商、`notifications/initialized` 和底层 stdio 关闭流程，以及官方 [Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) 的 `tools/list`、`tools/call`、input/output Schema 和 structured content。

## 2. 受信注册与本地风险下限

唯一 Server 由应用组合根静态注册：

- `server_id = deskpilot.readonly-text`；
- 命令固定为当前 Python 的 isolated mode 加打包内脚本，API 只返回脱敏 preview，不能提交 executable、argv、cwd 或 env；
- 默认 disabled；相同状态重复提交为 no-op；
- manifest 固定声明 stdio、MCP 版本、无 network、无 filesystem roots、无 client capabilities；
- Host 不提供 roots、sampling、elicitation 或 tasks，Server 只能协商 tools；
- Tool 的本地风险下限固定为 R0，不能由 Server 的 `readOnlyHint` 降低。

MCP annotations 按官方规范只是不可信提示。Host 要求 `tools/list` 的完整定义与本地静态 manifest 精确同摘要，随后仍用本地 Pydantic 输入/输出模型校验。未知 Tool、额外参数、Schema 漂移或输出不匹配全部拒绝。

## 3. stdio 会话隔离与生命周期

每次显式调用创建一个短生命周期子进程：

1. 启动前重新计算内置 Server 脚本 SHA-256，与注册时 bundle digest 比较；
2. 使用绝对 Python 路径和 `-I`，cwd 为新临时目录；
3. 子进程只获得 SystemRoot/临时目录等最小环境和 Python 目录 PATH，不继承 API Key、Provider credential reference 或 DeskPilot session 数据；
4. 使用有界 NDJSON JSON-RPC 2.0 帧完成 initialize、initialized、tools/list、tools/call；
5. 全会话 3 秒硬 deadline，超时时先发送 `notifications/cancelled`，再关闭 stdin、terminate、kill 分级回收；
6. structured content 最大 64 KiB，Server stderr 有界读取且不进入 API/审计响应；
7. 完成后关闭 stdin 并等待子进程退出，不保留跨调用状态。

当前 `-I`、最小环境和固定 bundle 可降低注入面，但不是 Windows AppContainer 的强制 filesystem/network sandbox。此版本只运行仓库内经过摘要复核、仅使用纯文本和标准库的可信 Server；任意第三方 Server 在接入前必须复用 Runner 的受限令牌/AppContainer/Job Object 边界。

## 4. 首个只读 Server

`deskpilot.text.metrics` 接受 1～4096 字符文本，返回：

- Unicode character count；
- line count；
- Unicode `\w+` word count；
- 输入 UTF-8 SHA-256。

它不访问文件、网络、数据库、模型或环境变量，不产生副作用。结果同时使用 MCP text content 与 `structuredContent` 返回；Host 只接受并向 API 投影本地 output model 校验通过的 structured content。

调用由用户在 MCP 控制台点击触发，结果只返回该页面，不进入 TaskProcessor、Tool Registry、Policy grant 或模型上下文。因此 MCP 响应不能成为 DeskPilot Tool 授权、写路径、branch evidence 或 approval 的替代品。

## 5. 持久化状态与审计

Alembic `0028_controlled_mcp` 新增：

- `mcp_server_states`：当前 enabled、manifest digest 和 revision；manifest 变化时旧 enable 自动失效；
- `mcp_audit_state`：append-only chain 的 next sequence 与 head；
- `mcp_audit_events`：enabled、disabled、tool_called、tool_failed 的请求/结果摘要、前序摘要、事件摘要和脱敏详情。

原始文本、MCP content、异常正文、环境和命令绝对路径不进入审计。读取时从 sequence 1 重算整条 chain，并核对事件连续性和 singleton head；append 前锁 state 并复核数据库尾部，损坏时返回 `MCP_AUDIT_REJECTED`。

## 6. API 与前端

新增受本地会话与可信 Origin 保护、禁止缓存的 API：

- `GET /api/v1/mcp/servers`；
- `POST /api/v1/mcp/servers/{id}:enable|disable`；
- `POST /api/v1/mcp/servers/{id}/tools:call`；
- `GET /api/v1/mcp/audit`。

前端启用“Agent 与 MCP”导航，展示固定命令 preview、协议、网络/roots/client capabilities、本地风险下限、Schema digest、bundle digest 和状态；只有显式启用后调用按钮才可用，并明确调用会把输入发送给本地短生命周期进程。

## 7. 验证与下一步

自动化覆盖默认禁用、显式启用/调用/禁用、真实 stdio lifecycle、未知 Tool、输入输出 Schema、原文不入审计、hash-chain 篡改、超时取消与进程回收、bundle 篡改、secret environment 隔离，以及 `0028 -> 0027 -> 0028` migration 往返。

最终门禁：Ruff 全仓通过；mypy 151 个生产源码通过；pytest 421 collected、409 passed、12 skipped、1 条既有第三方 warning；Alembic `0028` head/check 和 `0028 -> 0027 -> 0028` 往返通过；`uv lock --check` 通过；前端 19 文件/138 项测试、type-check 和 production build 通过。

下一阶段进入评测与可观测最小主线：先建立版本化黄金任务 YAML Schema、确定性离线 runner、结果 manifest 和 trace record/replay，再接成功率、安全拒绝率、延迟与稳定失败分类；暂不扩大 MCP Server 来源或权限。
