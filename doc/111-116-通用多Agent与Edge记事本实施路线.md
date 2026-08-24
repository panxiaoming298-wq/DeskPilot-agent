# 阶段 111～116：通用多 Agent 与 Edge / 记事本实施路线

## 1. 固定执行顺序

本路线在阶段 77～110 全量门禁与 `codex/stage-110` checkpoint 通过后执行，顺序固定为：

```text
111 通用任务提案
  → 112 通用持久任务循环
  → 113 Codex 类安全编码工具
  → 114 并行任务与窗口后台运行
  → 115 Cloud 候选生命周期 / Calibration v3
  → 116 Edge + Windows 记事本安全纵切
```

每阶段从上一阶段已通过的提交建立独立 `codex/stage-NNN` 分支。阶段结束必须重新取得默认后端全量、Phase75、前端和受影响外部/Windows 门禁的一次完整退出 0，使用中文本地提交，不自动 push。

任一门禁失败就停止推进。不通过改 baseline、隐藏重试、删除失败证明、放宽 Policy/Approval 或扩大模型权限强行过关。定向复跑只用于诊断，不能替代阶段全量结果。

## 2. 阶段 111：通用任务提案与 Capability Offer

> 实施状态（2026-08-24）：阶段 111 代码、`0051` 迁移、前端投影和 CI 已完成，本地全量门禁取得统一退出 0；精确结果见[阶段 111：通用任务提案与 Capability Offer](111-通用任务提案与Capability-Offer.md)。完成中文阶段提交并建立 `codex/stage-112` 后，按本路线进入 112A。

### 2.1 路由与运行时

- 保留现有 15 条确定性 Route；规则成功路由时完全跳过模型，旧 route manifest digest 与行为保持兼容。
- 增加 LOCAL-only `builtin.turn_planner@1.0.0` 和独立、持久的 `TurnPlannerRuntime`。
- Planner 不依赖已存在的 Plan/Invocation，不接入 Agent Model Loop 的递归入口，避免形成“为创建 Plan 必须先有 Invocation”的循环。
- 提取版本化 `RouteRecipeCatalog`：旧 v1 route manifest 只用于历史记录重放，新版本修正 Route/Capability 映射而不改写旧 digest。

### 2.2 服务器 Offer 与模型输出

规则无法路由时，后台 Workbench 执行 `interpret_turn`。服务器先生成 opaque Offer，每个 Offer 预编译并绑定：

- exact Task Contract、Agent、Prompt 与 Provider；
- Capability/输入 Schema、Policy、预算和 Workspace；
- trusted recipe、输出 Schema和不可变 digest。

模型只能引用 `offer_key`。模型提供的参数必须逐字来自已持久化用户消息，不能生成路径、命令、URL、凭据或授权事实。输出只允许 `propose_steps / needs_input / unsupported`：

- 单步骤提案由服务器 trusted recipe 生成计划；
- 多步骤提案保存为 `MULTI_STEP_PLAN_DEFERRED`，交给阶段 112；
- 模型提案和 Judge 结论都不授予 Capability、Policy 许可或审批。

### 2.3 持久证明与投影

新增迁移 `0051`，至少保存：

- Capability Offer；
- Planner Run；
- Adjudication；
- Plan Binding；
- Turn Route 的可空 planner provenance。

现有写 API 保持兼容。Workbench 增加 `interpreting`、`interpret_turn` 与可空 `turn_planning` 投影。Planner timeout、Schema 错误、unknown offer、Provider 不可用或绑定漂移时，必须保存失败证明并原样回退确定性结果；不自动重放模型调用。

公开 Workbench 投影必须与内部 proof 模型分离，只展示状态、数量、opaque digest 和稳定原因；不得向 API/前端泄露完整 Offer、用户参数、模型 response/proposal manifest、claim owner、Provider 私有配置或预编译计划正文。

## 3. 阶段 112：通用持久任务循环

阶段 112 分三个可独立提交的内部里程碑：

### 112A：版本化多步骤 Planner

> 实施状态（2026-08-24）：112A 实现、独立 staged 里程碑门禁与中文提交 `完成阶段112A多步骤计划检查点` 已完成，未 push；112B 的通用 Execute/Verify 实现与本地里程碑门禁已通过，下一步进入 112C。实际边界与验证记录见[阶段 112：通用持久任务循环](112-通用持久任务循环.md)。阶段 112 的 PostgreSQL/RabbitMQ 外部门禁在 112C 结束后统一执行。

- 整体规划支持 1～8 个服务器 Offer：单步骤继续走阶段 111 已验收的 trusted recipe 路径，112A 的 TaskLoop 多步骤入口只消费 2～8 步 `MULTI_STEP_PLAN_DEFERRED`，不改旧 digest/行为。
- 每一步只引用 Offer 与已持久输入；服务器重新验证消息片段、Offer、recipe、exact Contract/Capability/Policy/预算绑定，全程不进行第二次 Provider 调用。
- `ModelPlannerComposer` 生成 `model_planner` DraftPlan 和 expected generation-1 ExecutablePlan preview；112A 只保存 Draft/preview，不创建 PlanningState、PlanGeneration、ExecutionRun、Invocation 或 Tool call。
- `0052_model_planner_task_loop` 保存 TaskLoop、两事件 Observe→Plan 链、Draft 和逐 step binding；Workbench 只公开状态、数量和摘要，不泄露输入、Offer、Contract 或 Plan 正文。
- 进入 112B 前必须把每个 composite node 绑定到精确 source step，并按 `组合 Contract ∩ source-step 权限` 执行，同时重新验证当前 runtime/Executor 资格；不能只凭组合 Contract 或静态 CapabilityCatalog 激活。

### 112B：Capability Executor Registry 与通用 reducer

- 用 Executor Registry 和通用 reducer 替代 Workbench 中的大量 Route 分支。
- 研究、知识、MCP、Workspace 读取与固定测试可以组合。
- 节点只能消费类型匹配、digest 闭合的 verified ResultRef；Memory、Summary、MCP 或 UI 状态不能替代 verified edge。
- 激活必须在 expected generation-1 Plan、逐 source-step 权限/输入绑定与当前 runtime eligibility 原子闭合之后发生；112A Draft 本身不具有执行权限。
- `0053_task_loop_execution` 保存逐节点 authority/eligibility binding、generation-1 execution/event、attempt 与 verified ResultRef；Workbench 通过 `advance_task_loop` 每次只提交一个持久 reducer command。
- Capability 与 Agent 执行均在事务外做受控工作、事务内复核 owner/fence 和结果证明；重启从 planned/active execution 的持久真值恢复，不重放 Turn Planner Provider 或 outcome unknown 副作用。

### 112C：Patch/Test/Approval 与 Repair

- 把现有精确 Patch、固定 Test、逐节点 Approval 和三代预算守恒 Repair 接入通用循环。
- Workbench 增加可空 `task_loop` 投影，展示 `Observe → Plan → Execute → Verify → waiting_user → Repair`。
- 支持重启恢复、no-progress 检测、总预算耗尽和稳定终止。

本阶段仍不开放任意 Shell、模型安装依赖、项目删除或登录态浏览器。

## 4. 阶段 113：Codex 类安全编码工具

### 4.1 只读项目能力

- 项目根目录限定的递归搜索、批量读取；
- Git `status / diff / log` 只读能力；
- 拒绝 reparse point、junction 与 symlink 逃逸；
- Git 固定关闭 hooks、external diff、textconv 和 pager，并限制文件数、字节数、行数和执行时间。

### 4.2 服务器命令 Profile

首批只注册：

- Python：pytest、Ruff、mypy；
- Node/pnpm：test、type-check、build。

模型只能选择 `command_profile_id`，不能提供 executable、argv、cwd 或环境变量。命令在断网临时项目快照内执行，回执绑定 snapshot digest、工具链、Profile、输出摘要、exit code、timeout、truncation 与 cancellation。

失败结果进入阶段 112 Repair。代码写入继续复用精确 Patch、staging、manifest 与用户确认。首版不支持任意 Shell、Git commit/push/reset、模型新增依赖、联网安装或项目目录删除。

## 5. 阶段 114：并行任务与窗口后台运行

- 前端从单一 `task` 状态升级为按 Task ID 管理的任务集合。
- 每个 Task 拥有独立事件 cursor、连接、预算、待审批、待输入与未读状态。
- 至少三个 Task 可并行；切换焦点不停止后台任务。
- `approve / pause / resume / cancel` 必须绑定准确 Task ID 与 revision，拒绝 stale UI 操作。

Tauri 增加托盘和受监督本地后端 sidecar：

- 关闭主窗口默认隐藏到托盘，任务继续执行；
- 托盘可恢复窗口、查看活动任务并执行明确退出；
- 显式退出后，未完成任务依靠持久状态在下次启动恢复。

本阶段不实现 Windows Service，也不承诺机器重启后无人值守继续。验收增加 Rust `fmt/clippy/test`、Tauri/NSIS build 和三任务断线重连。

## 6. 阶段 115：Cloud 候选与 Calibration v3

- 新增独立 Agent Release Manifest 与显式 activation channel；最高 SemVer 不再自动成为 preferred。
- 新增不可原地修改的 cloud-only 通用 Turn Planner、Dynamic Coordinator 与 Patch Planner 版本。
- Calibration v3 从固定两角色升级为显式三角色 cohort，并保持 v1/v2 工件完整兼容。

只有以下条件同时成立才可激活 cloud 候选：

1. exact 三角色 identity；
2. 闭合 Handoff companion；
3. Phase109 Admission；
4. cloud Task Contract；
5. 用户对数据出站的显式同意。

没有 Admission 时所有候选保持 disabled，本地稳定版本仍是 preferred。本阶段先完成生命周期、v3 Schema、固定测试与合成证据回放；真实 Provider/Judge capture、费用、真人评审与 production Admission 到达外部授权点后暂停。不得提交凭据或 Fake 生产证据。真实 cohort 未完成不阻塞阶段 116 的本地能力开发。

## 7. 阶段 116：Edge + 记事本安全纵向切片

### 116A：Browser Agent

- 使用独立 DeskPilot Microsoft Edge Profile，用户只在可见窗口手动登录。
- 默认域名 allowlist 为空；自动验收使用本地 loopback 页面。
- 首版支持导航、DOM 读取、截图和表单预填。
- `submit / upload / download / publish` 分别要求绑定 origin、目标与内容摘要的新审批。
- Cookie、密码、验证码与 2FA 不进入模型；验证码或权限弹窗立即进入等待用户。

### 116B：Notepad Agent

- 只允许 Windows 系统记事本和语义 UIA selector，禁止任意坐标点击。
- 支持发现、启动、激活、输入，以及经审批保存到允许目录。
- 未保存关闭、覆盖文件或异常对话框必须暂停。
- 成功必须由窗口状态或文件内容摘要验证，不能把 UI 点击成功当作任务正确。

新增本地 Browser Profile/域名允许管理接口和 Browser/App action receipt。Browser Operator 与 Notepad Operator 都是 LOCAL-only Agent；网页、DOM 与 UI 文本始终按不可信输入处理。任务仍通过通用 Workbench、Capability、Policy 与 Approval 执行。

系统设置、多应用编排、管理员操作、支付、验证码绕过和跨端控制移至阶段 117 以后。

## 8. Baseline、提交与外部边界

- Agent/Contract/Plan 变化导致 Phase75 digest 漂移时，只能在 11/11、false-success=0、unauthorized-effect=0 且人工确认差异符合预期后追加新不可变 baseline；禁止覆盖旧版本。
- 每阶段同步 README、`项目进度.md` 与阶段文档。
- 112、113、116 可以按内部里程碑做中文提交，但阶段结束仍需一次全量门禁。
- 不自动 push，不执行真实 cloud capture，不把模型输出、UI 点击或 Judge 结果视为权限或任务正确性的证明。
