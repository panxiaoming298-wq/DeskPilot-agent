# DeskPilot Frontend

Vue 3 + TypeScript 本地控制台，当前包含：

- 任务创建、状态快照、结构化计划和实时事件时间线。
- 运行中任务暂停、暂停任务恢复、所有非终态任务取消及取消二次确认。
- 断线状态、1/2/4/8 秒恢复退避、手动重连、会话重认证和按事件序号续传提示。
- 工具失败、取消和结果不确定事件使用独立危险态标签；`tool.unknown` 不会被呈现为可安全重试。
- `tool.unknown` 到达后自动查询持久化 Runner receipt，并以 committed/no-receipt/query-failed 三态证据卡展示；原调用和人工裁决不会被前端改写。
- committed `file.move` 证据可二次确认创建服务端派生的反向任务；创建后切换到新任务，并在新审批卡核对路径与版本。
- 独立“历史与对账”工作台提供有界任务历史、Reconciliation 状态筛选、证据刷新、不可改写人工裁决，以及原任务/新 attempt/compensation 血缘导航。
- 裁决和后继任务均采用二次确认、稳定幂等键与服务端快照更新；活动任务结束前禁止切换到其他历史任务。
- Provider Catalog、健康检查和能力概览。
- Fake/OpenAI-compatible Provider 创建与完整重新配置。
- Provider 启停、默认切换、删除确认与脱敏审计时间线。
- ETag 乐观并发、幂等写入和 `412` 冲突刷新提示。

## 启动

先启动 `backend`，再执行：

```powershell
pnpm install
pnpm dev
```

访问 `http://127.0.0.1:5173`。Vite 会将 `/api` 和 WebSocket 代理到 `http://127.0.0.1:8000`。

## 桌面壳

普通网页开发仍使用 `pnpm dev`。安装 Rust/MSVC 构建环境后，可在后端已启动的情况下运行：

```powershell
pnpm desktop:dev
```

生成 Windows NSIS 安装包：

```powershell
pnpm desktop:build
```

桌面生产构建会连接 `http://127.0.0.1:8000`。当前 Tauri 只负责窗口和前端资源，Python 后端仍按原方式独立启动，尚未打包为 sidecar。

前端启动后自动从受信任的 `/api/v1/session` 获取进程级令牌并只保存在内存中。REST 自动添加 Bearer token，WebSocket 通过子协议认证；API 重启导致令牌失效时会自动重新建立会话。事件连接恢复后会从最后一个 `seq` 继续接收。服务端 Problem Details 的 `detail` 会作为可读错误展示。

任务控制请求不做乐观状态切换。Pause/Resume/Cancel 成功后使用服务端完整 Task 快照；响应中断或 `409` 时先查询任务真值再决定提示，尤其不会盲目重放非幂等 Resume。API 重启后内存检查点丢失时，暂停任务会保持暂停并提示取消后重新创建。

## 模型凭据

设置页不会接收 API Key。Windows 下先在 `backend/` 中执行：

```powershell
.\.venv\Scripts\python.exe -m deskpilot.credential_cli store CLOUD_CHAT
```

然后创建 Provider，并选择 `windows_credential_manager`、填写引用标识符 `CLOUD_CHAT`。公开 Catalog、审计和前端状态均不包含 endpoint、凭据引用或密钥。

现有 OpenAI-compatible Provider 的编辑采用完整重新配置。由于 API 刻意不回传 endpoint 和凭据引用，保存时必须重新填写这些连接字段。

## 验证

```powershell
pnpm test
pnpm type-check
pnpm build
```

组件测试使用 Vitest、Vue Test Utils 和 jsdom；当前 21 个文件、141 个用例额外覆盖长期记忆来源/Provider 使用展示、待确认提案、类型化新建和两次显式删除确认。阶段 74 未修改前端；CompactionSnapshot API 已为后续统一工作台提供 source/coverage/conflict/stale/parent 数据。执行 Vite/Vitest 需要 Node 20.19+。
