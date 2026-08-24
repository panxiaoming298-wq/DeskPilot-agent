# DeskPilot Frontend

Vue 3 + TypeScript 本地控制台，当前包含：

- 最多三个活动任务同时运行；每个任务拥有独立的事件 cursor、连接、预算、待审批/待输入和未读状态。
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

普通网页开发仍使用 `pnpm dev`。安装 Rust/MSVC 构建环境后，可运行：

```powershell
pnpm desktop:dev
```

生成 Windows NSIS 安装包：

```powershell
pnpm desktop:build
```

桌面生产构建会用锁定的 Python 依赖冻结 FastAPI/Runner 后端，再将固定 sibling sidecar 与 Tauri/NSIS 一起打包。sidecar 只监听 `127.0.0.1:8000`，由 Rust supervisor 启动、限次退避重启并在明确退出时停止。关闭主窗口只隐藏到托盘，后台任务继续；托盘可恢复窗口、查看活动任务数和明确退出。它不是 Windows Service，也不承诺机器重启后无人值守继续。

前端启动后自动从受信任的 `/api/v1/session` 获取进程级令牌并只保存在内存中。REST 自动添加 Bearer token，WebSocket 通过子协议认证；API 重启导致令牌失效时会自动重新建立会话。事件连接恢复后会从最后一个 `seq` 继续接收。服务端 Problem Details 的 `detail` 会作为可读错误展示。

任务控制请求不做乐观状态切换。Pause/Resume/Cancel 必须同时绑定 exact Task ID 和当前 `last_event_seq`；缺少 revision 返回 `428`，过期 revision 返回 `409`。成功后使用服务端完整 Task 快照，响应中断时先查询任务真值再决定提示，不会盲目重放非幂等 Resume。桌面重启后会恢复最新的三个未完成任务并从各自 cursor 重连。

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

组件测试使用 Vitest、Vue Test Utils 和 jsdom；阶段 114 共 24 个测试文件、165 项，覆盖三任务槽位、独立 cursor/重连、焦点切换、重启恢复、exact revision 控制和 Tauri 托盘计数桥接。执行 Vite/Vitest 需要 Node 20.19+。
