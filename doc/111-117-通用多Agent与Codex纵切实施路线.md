# 阶段 111～117：通用多 Agent、Codex 纵切与 Edge / 记事本实施路线

## 1. 分离式执行顺序

本路线在阶段 77～110 全量门禁与 `codex/stage-110` checkpoint 通过后执行。阶段 115A 完成后，依据 [ADR-016](ADR-016-115B生产门与116开发纵切解耦.md) 将生产 cloud 授权门与本地长循环开发门解耦：

```text
111 通用任务提案
  → 112 通用持久任务循环
  → 113 Codex 类安全编码工具
  → 114 并行任务与窗口后台运行
  → 115A Cloud Release / Calibration / Admission 代码底座
  → 116A 受控编码工具面
  → 116B 持久多 Agent 编码循环
  → 115B 真实 Cloud capture / 真人评审 / Production Admission
  → 116C 真实模型黄金任务与生产质量验收
  → 117 Edge + Windows 记事本安全纵切
```

每阶段从上一阶段已通过的提交建立独立 `codex/stage-NNN` 分支。阶段结束必须重新取得默认后端全量、Phase75、前端和受影响外部/Windows 门禁的一次完整退出 0，使用中文本地提交，不自动 push。

任一门禁失败就停止推进。不通过改 baseline、隐藏重试、删除失败证明、放宽 Policy/Approval 或扩大模型权限强行过关。定向复跑只用于诊断，不能替代阶段全量结果。

## 2. 阶段 111：通用任务提案与 Capability Offer

> 实施状态（2026-08-24）：阶段 111 代码、`0051` 迁移、前端投影、CI、全量/外部门禁与中文提交均已完成；精确结果见[阶段 111：通用任务提案与 Capability Offer](111-通用任务提案与Capability-Offer.md)。

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

> 实施状态（2026-08-24）：112A 与 112B 已分别形成中文里程碑提交；112C 的精确 Patch/Approval、receipt 对账、有界 Repair、no-progress/预算终止及 `0054` 迁移均已完成。阶段 112 默认后端、Phase75、前端、PostgreSQL 11/11 与 RabbitMQ 1/1 总门禁已通过，未 push；实际边界与验证记录见[阶段 112：通用持久任务循环](112-通用持久任务循环.md)。下一步创建 `codex/stage-113`。

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
- 实际实现使用持久 `task_loop_capability_approvals` 与 `task_loop_cycle_events`：审批绑定 exact Task/revision/node/attempt/preview/manifest/fresh confirmation；Patch 写入后崩溃按同 attempt receipt 对账；非审批节点的 Repair 只能消费原 Plan retry/预算，最多两次，审批副作用不自动重放。

本阶段仍不开放任意 Shell、模型安装依赖、项目删除或登录态浏览器。

## 4. 阶段 113：Codex 类安全编码工具

> 实施状态（2026-08-25）：阶段 113 已完成并通过总门禁。项目根限定搜索/批读、Git 只读、六个服务器 Python/Node Command Profile、断网临时快照、内容寻址回执和 planner-only 单步骤 TaskLoop 均已落地；默认后端 `760 passed + 12 skipped`、严格 mypy 282 个源码、Alembic `0055`、Phase75/baseline、前端、PostgreSQL 11/11 与 RabbitMQ 1/1 通过。实际边界与失败记录见[阶段 113：Codex 类安全编码工具](113-Codex类安全编码工具.md)。下一步创建 `codex/stage-114`。

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

> 实施状态（2026-08-25）：阶段 114 已完成并通过总门禁。三槽位 Task runtime collection、独立 cursor/连接/审批/未读/预算投影、持久 control reservation 绑定的 exact Task/revision 控制、重启恢复、Tauri 托盘和受监督冻结 sidecar 均已落地；默认后端 `763 passed + 12 skipped`、前端 24 文件 / 165 项、Rust/sidecar/NSIS、PostgreSQL 11/11 与 RabbitMQ 1/1 全部通过。实际边界和验收记录见[阶段 114：并行任务与窗口后台运行](114-并行任务与窗口后台运行.md)。按用户指令，本次在阶段 114 checkpoint 停止，不执行 115～117。

- 前端从单一 `task` 状态升级为按 Task ID 管理的任务集合。
- 每个 Task 拥有独立事件 cursor、连接、预算、待审批、待输入与未读状态。
- 至少三个 Task 可并行；切换焦点不停止后台任务。
- `approve / pause / resume / cancel` 必须绑定准确 Task ID 与 revision，拒绝 stale UI 操作。

Tauri 增加托盘和受监督本地后端 sidecar：

- 关闭主窗口默认隐藏到托盘，任务继续执行；
- 托盘可恢复窗口、查看活动任务并执行明确退出；
- 显式退出后，未完成任务依靠持久状态在下次启动恢复。

本阶段不实现 Windows Service，也不承诺机器重启后无人值守继续。验收增加 Rust `fmt/clippy/test`、Tauri/NSIS build 和三任务断线重连。

## 6. 阶段 115：真实 Cloud Agent 与 Calibration v3

> 实施状态（2026-08-25）：115A 与 115B 的授权/工件准备已在 `codex/stage-115` 形成内部 checkpoint；Release hash chain、cloud-only 三角色 2.0.0、闭合 companion、Calibration v3、exact 三角色 Admission builder 与 Task privacy-compatible binding 已通过 783 项默认后端总门禁及专项自动化。真实 Provider/Judge-human capture、Production Admission 和 activation 尚未获得授权，因此阶段 115 未完成；但该外部生产门不再阻塞 116A/116B 的 LOCAL-only 开发，详见 [ADR-016](ADR-016-115B生产门与116开发纵切解耦.md)。

阶段 115 不再以“生命周期和合成证据已经实现”作为完成条件。它必须把至少一个真实三角色 cohort 安全激活到生产运行时，才能支持 116C 的真实模型质量声明。116A/116B 可以先证明本地运行时、工具和安全语义，但不得以 Fake、recorded 或未校准候选冒充生产质量。

### 115A：Release lifecycle 与三角色身份

- 新增独立 Agent Release Manifest 与显式 activation channel；最高 SemVer 不再自动成为 preferred。
- 新增不可原地修改的 cloud-only 通用 Turn Planner、Dynamic Coordinator 与 Patch Planner 版本。
- Calibration v3 从固定两角色升级为显式三角色 cohort，并保持 v1/v2 工件完整兼容。
- Release、rollback、disable、expiry 与 replacement 都必须产生不可变审计事件；旧 Agent 版本继续支持既有 Run 收尾，但不能被静默提升为 preferred。

### 115B：真实校准与 Production Admission

真实 capture 只能在用户明确选择 Provider/model、允许的数据分类与出站范围、费用上限和真人评审安排后执行。授权具备后必须完成：

1. exact Planner、Coordinator、Patch Planner identity 与闭合 Handoff companion；
2. 使用生产请求构造器执行真实 Provider capture，不以 Fake/recorded 输出替代；
3. 独立 Judge 盲审、两名真人主审与必要的第三仲裁；
4. Calibration v3 baseline compare、Phase109 Admission 与最长 90 天有效期；
5. cloud Task Contract、逐 Turn Provider authority 与用户对当前任务数据出站的显式同意；
6. 激活、进程重启后恢复、禁用、过期和回滚的运行时验收。

没有 Admission 时所有候选保持 disabled，本地稳定版本仍是 preferred。若真实 Provider、费用或真人评审尚未获得授权，可以完成 115A 并形成内部 checkpoint，但**不得把阶段 115 标记为完成，也不得宣称 cloud Agent 已达到生产质量**。不得提交凭据、原始敏感样本或 Fake 生产证据。

缺少上述授权时允许创建 `codex/stage-116-dev` 并实施 116A/116B；开发路径只能使用 LOCAL-only Agent 和离线/固定测试 Provider，不能生成 Production Admission、不能设置 production activation 开关，也不能进入 116C 的真实模型验收。

阶段 115 的发布门禁除既有 Phase75 零容忍项外，必须证明真实 cohort 没有 unauthorized effect、identity drift、未授权数据出站或 Judge 替代人工授权。

## 7. 阶段 116：Codex 类真实仓库长循环

阶段 116 的唯一目标是闭合一个用户可感知的 Codex 纵向切片：用户在同一会话持续提出、补充和修订编码目标，系统在真实 Python/Node 仓库中完成调查、计划、多 Agent 协作、修改、测试、失败修复和证据交付。

### 116A：受控编码工具面

> 实施授权：不依赖 115B 外部授权，立即在 `codex/stage-116-dev` 推进；cloud-only 2.0.0 cohort 继续 disabled。

> 首个检查点（2026-08-25）：`WorkspaceCommandPlan` 的冻结 Contract 与服务器 Compiler 已实现，只接受 Task/计划代、结构化项目目标和注册 Profile ID，并生成绑定 Catalog/Profile 摘要的失败即停步骤链；持久 TaskLoop 多步执行仍是下一纵切，详见[阶段 116A 检查点](116A-服务器编译WorkspaceCommandPlan.md)。

- 保留项目根、symlink/reparse、预算、Policy 与 Approval 边界，把搜索/批读、精确 Patch、固定测试扩展为同一隔离 Workspace 内的多文件编辑闭环。
- 新增服务器编译的 `WorkspaceCommandPlan`。模型只能选择注册操作和结构化目标，不能提供 executable、任意 argv、cwd、环境变量或 shell 字符串。
- 支持受控本地 Git branch、status、diff 与 commit；branch/commit 必须绑定 exact workspace revision、diff digest、测试证据和用户确认，继续禁止自动 push、force、reset-hard 与修改 hooks/config。
- 依赖变更必须先生成 manifest/lockfile/egress preview，再经独立审批；安装只在隔离快照、允许的 registry 和费用/网络预算内执行，不能静默修改原仓库或全局工具链。
- 支持 staged create/rename 与精确文件删除；删除只在可丢弃快照中执行，写回前必须展示清单并取得新确认，禁止递归目录删除。

### 116B：持久多 Agent 编码循环

> 实施授权：不依赖 115B 外部授权。自动化可以使用 LOCAL-only/Fake Provider 证明持久化、并行、fencing、审批和恢复语义，但不能证明真实模型质量。

> 第一检查点（2026-08-26）：同一持久 TaskLoop 已走通两个独立 Reader 并行调查、双 verified ResultRef join、精确双文件 Patch 确认、服务器固定 Test、一次已知失败 Repair、final acceptance 和结构化 Delivery 证据持久化。重启恢复不重跑已验证 Reader，失败 ResultRef 不解锁后继。尚未闭合同会话 amendment 的旧 generation/lease fencing、真实版本化 Patch Planner Model Handoff 和用户可见 Delivery 投影，因此 116B 仍为进行中。详见[第一检查点文档](116B-持久并行编码循环第一检查点.md)。

> 第二检查点（2026-08-26）：两个真实持久 Patch Planner Model Turn/Handoff 已接入 Reader 与 Patch Capability 之间，只有与服务器封存变更完全一致的 `PATCH_PROPOSAL` ResultRef 才能进入写入 join；同会话新指令会先 cancel/fence 旧 Run、封存 TaskLoop 终止事件，再由 `0059` 把源 Contract/Plan/Execution 绑定到后继 Task/用户消息；成功 Delivery 已通过既有 Workbench/API 返回脱敏 diff、测试、风险和回滚点。Dynamic Coordinator 模型图提案和更真实的有界仓库任务仍待后续，因此 116B 继续标记为进行中。详见[第二检查点文档](116B-持久并行编码循环第二检查点.md)。

> 第三/第四检查点（2026-08-26）：受约束 Coordinator 已以持久 `COORDINATION_PLAN` ResultRef 确认服务器封存图；固定测试通过后再经第二次 exact approval，在服务器命名新分支上完成 hook/signing/push-disabled commit。分支创建、暂存和已提交中间态均可以内容寻址回执对账，不盲目重放。详见[第三检查点](116B-持久并行编码循环第三检查点.md)与[第四检查点](116B-持久并行编码循环第四检查点.md)。

> 第五检查点（2026-08-26）：原双文件图已推广为 Python/Node 各 2～8 文件的服务器预编译变体。N>2 使用独立 bounded Coordinator v2，Reader/Planner 分批并行上限仍为 2，exact path/节点/提案/Git 摘要形成同一证明链；Delivery v3 与 `0060_workspace_coding_bounded_files` 保存 3～8 文件语义，原双文件合同/摘要/Delivery v2 保持不变。当前仍缺 Node/八文件端到端对称证明和受控探索到新文件集计划代的收窄边界，因此 116B 继续标记为进行中。详见[第五检查点文档](116B-持久并行编码循环第五检查点.md)。

> 第六检查点（2026-08-26）：Node 八文件已走通完整 Coordinator→分批 Reader/Planner→Patch/Test→Git→Delivery 链，并在每个并行波次后从持久状态重建 Coordinator；pending binding 篡改在 Attempt 前拒绝，后期 Planner 波次的失败与 outcome unknown 全部持久终结且不启动 Patch。bounded Coordinator 1.1 补足上限输出预算，历史 1.0 按 exact binding 保持可读。当前下一缺口是“受控探索→候选文件集→用户确认→新不可变计划代”，因此 116B 仍为进行中。详见[第六检查点文档](116B-持久并行编码循环第六检查点.md)。

> 第七检查点（2026-08-27）：服务器已能封存 Python/Node 项目的 2～256 文件元数据 Catalog，验证 `builtin.workspace_coding_explorer@1.0.0` 身份下的 2～8 文件无权限 Proposal，并要求同会话 exact 用户确认后原子创建新的 generation-1 R0 Reader Plan。snapshot/proposal/binding、逐候选 Plan-node mapping 和 Workbench 投影均内容寻址，项目或跨表证据漂移在恢复时拒绝。当前仍缺标准 Explorer Invocation/Model Turn、Reader TaskLoop activation 与 Reader verified join 后的独立 Patch 再确认，因此 116B 继续标记为进行中。详见[第七检查点文档](116B-持久多Agent编码循环第七检查点.md)。

> 完成检查点（2026-08-29）：第八至第二十一检查点继续闭合标准 Explorer/Reader/Change Proposal/写 TaskLoop、真实安装工具链、步骤间恢复、三任务公平并发以及并发强杀故障域。最终在 `concurrency=2` 两个真实 pytest 运行时强杀 sidecar，只有两个已领取 Attempt 收敛为 unknown 且不重放，未领取同伴跨代完成 Delivery；AppContainer journal 与 Attempt/ResultRef 全部可对账。至此下列 116B LOCAL-only 运行时完成口径已全部满足，真实模型质量仍由 115B/116C 生产门阻断。详见[第二十一检查点文档](116B-持久多Agent编码循环第二十一检查点.md)。

- Turn Planner、Dynamic Coordinator、Explorer/Reader、Patch Planner、Test Runner 与独立 Verifier 通过版本化 Contract/Handoff 协作；至少证明两个独立 Child 并行调查和一个依赖 verified ResultRef 的 join。
- 同一会话的新用户消息可以补充约束、纠正方向或要求停止；服务器封存旧 generation/lease，生成新的不可变计划代，不把迟到结果绑定到新计划。
- 执行循环覆盖 `Inspect → Plan → Delegate → Patch → Test → Repair → Verify → Deliver`，测试失败只能在总预算、最大计划代和 no-progress 约束内继续。
- 关闭窗口、事件流断线、API/sidecar 重启后从持久 checkpoint 续接；outcome unknown 的写入、安装或 Git 操作不得透明重放。
- 最终交付必须包含 diff、变更文件清单、执行过的测试、失败/修复历史、剩余风险和可回滚点；模型总结不能替代这些证据。

### 116C：真实仓库黄金任务与验收

> 生产质量门：涉及真实 Candidate/Judge、真实模型成功率、Production Admission 和 cloud activation 的部分必须等待 115B 五项外部授权闭合。115B 前可以先冻结任务、harness、阈值和离线安全预检，但不得发布真实模型通过结论。

- 建立至少 20 个版本化真实仓库任务，覆盖 Python/Node、单/多文件修改、测试失败修复、用户中途改意、重启恢复和多 Agent 并行调查。
- capture 前冻结模型、Agent identity、Prompt、工具版本、任务输入、重复次数和成功阈值；默认质量门槛为端到端任务成功率不低于 80%。
- `false-success=0`、`unauthorized-effect=0`、越界路径/网络/Git 写入为零容忍；失败必须以可检查终态交付，禁止靠隐藏重试或人工改库通过。
- 至少一条桌面端验收从自然语言目标开始，连续多轮完成真实仓库修复，并在明确批准后写回与创建本地 commit；不自动 push。

## 8. 阶段 117：Edge + 记事本安全纵向切片

### 117A：Browser Agent

- 使用独立 DeskPilot Microsoft Edge Profile，用户只在可见窗口手动登录。
- 默认域名 allowlist 为空；自动验收使用本地 loopback 页面。
- 首版支持导航、DOM 读取、截图和表单预填。
- `submit / upload / download / publish` 分别要求绑定 origin、目标与内容摘要的新审批。
- Cookie、密码、验证码与 2FA 不进入模型；验证码或权限弹窗立即进入等待用户。

### 117B：Notepad Agent

- 只允许 Windows 系统记事本和语义 UIA selector，禁止任意坐标点击。
- 支持发现、启动、激活、输入，以及经审批保存到允许目录。
- 未保存关闭、覆盖文件或异常对话框必须暂停。
- 成功必须由窗口状态或文件内容摘要验证，不能把 UI 点击成功当作任务正确。

新增本地 Browser Profile/域名允许管理接口和 Browser/App action receipt。Browser Operator 与 Notepad Operator 都是 LOCAL-only Agent；网页、DOM 与 UI 文本始终按不可信输入处理。任务仍通过通用 Workbench、Capability、Policy 与 Approval 执行。

系统设置、多应用编排、管理员操作、支付、验证码绕过和跨端控制移至阶段 118 以后。

## 9. Baseline、提交与外部边界

- Agent/Contract/Plan 变化导致 Phase75 digest 漂移时，只能在 11/11、false-success=0、unauthorized-effect=0 且人工确认差异符合预期后追加新不可变 baseline；禁止覆盖旧版本。
- 每阶段同步 README、`项目进度.md` 与阶段文档。
- 112、113、115、116、117 可以按内部里程碑做中文提交，但阶段结束仍需一次全量门禁。
- 不自动 push。真实 cloud capture 只能在明确的 Provider、数据出站、费用和真人评审授权后执行；模型输出、UI 点击或 Judge 结果都不能视为权限或任务正确性的证明。
- 115B 缺少外部授权不阻塞 116A/116B 的 LOCAL-only 开发；它仍严格阻塞 production cloud activation 与 116C 的真实模型质量结论。
