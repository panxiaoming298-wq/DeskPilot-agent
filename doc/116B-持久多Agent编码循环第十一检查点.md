# 阶段 116B：持久多 Agent 编码循环第十一检查点

## 目标与结论

本检查点把第十检查点中已由用户 fresh-confirmed 的 `WorkspaceCodingWritePlanBinding` 接入现有 TaskLoop，闭合以下 LOCAL-only 三轮对话纵切：

1. Explorer 封存项目 Catalog，用户确认候选文件集；
2. Reader TaskLoop 生成 verified ResultRef，零工具 Change Proposer 形成无写权限提案；
3. 用户再次 exact 确认后，第三个 Task 自动激活固定写 Plan，并完成 Coordinator、Reader、Patch Planner、Patch approval、固定 Test、Git approval/commit 与 Delivery。

写 Plan 没有自己的步骤状态机。`WorkspaceCodingWritePlanBinding` 和节点 proof 只保存不可变授权与映射；执行、并发、租约、Attempt、Invocation、验证、Repair、终态和恢复仍以既有 TaskLoop/Run/Node/VerifiedResult 为唯一真值。因此本检查点让项目更接近 Codex 类“可持续对话、可恢复、多 Agent 编码执行”，没有转向多个模型自由聊天或旁路 Shell 编排。

## 不可变授权与持久化

- `confirmed_change_proposal` 成为 TaskLoop 的第三种受信来源，与 ModelPlanner 来源和 `confirmed_file_set` Reader 来源共用同一执行主干。
- `WorkspaceCodingWriteNodeProof` 对每个 runnable node 绑定 write binding、Change Proposal、原 Reader file-set/snapshot、Catalog/project/ecosystem、确认消息、recipe/parameters/changes、Contract/Plan/node、Agent/Prompt/Capability 和 tool authority。
- TaskLoop execution 与 ModelPlanner node binding 增加专用 write binding/proof 列；每个节点必须有且只有一种来源授权，旧来源的 manifest/digest 保持兼容。
- Activation 原子锁定 exact write binding、创建现有 Run/TaskLoop/event/node bindings；没有另建写计划进度表，也没有伪造 ModelPlanner Offer、Draft 或 Step。
- Alembic `0065_confirmed_change_task_loop` 增加三来源约束、外键和唯一性；存在 confirmed-write 执行记录时 downgrade 拒绝，避免删除执行证明。

## 执行、恢复与失败语义

- Activation、Agent claim、Capability claim、恢复和 Agent ResultRef bridge 每次重新构建当前 authority bundle，验证 Proposal、fresh confirmation、recipe/parameters、Contract/Plan/node、Catalog、project path、snapshot、Agent/Prompt、Capability 和 node proof。
- `BoundCapabilityInput` 与 Agent input/context/result 摘要链包含 exact write proof；Patch Planner 只能提交 `changes_json` 中对应路径的精确 replacement，不能增加路径、命令、环境变量或依赖。
- verified 节点在重启后直接复用 ResultRef；未启动节点从原 TaskLoop 继续。`running`、租约过期和 outcome unknown 仍执行 `NO_AUTOMATIC_REPLAY`。
- Patch 后的每次 claim 不会错误要求原始文件摘要不变。运行时只在同一 TaskLoop 中识别唯一 verified `patch_receipt`，再从不可变 Reader ResultRef 重建每个已确认文件的完整预期内容；Catalog 形状、未确认路径或完整文件内容出现其他漂移时继续 fail closed。
- 并行 Patch Planner batch 中，一个 sibling 被拒绝时不会抢先把共享 Run 置失败并阻断另一个 sibling 落证。两个已 claim Agent 先分别持久化成功、拒绝或 outcome-unknown 证据，再统一终止 Run；Patch 与后续节点保持未启动。
- Workbench 继续投影同一个 TaskLoop，不增加独立 API 或前端页面。确认写 Plan 在 TaskLoop 创建前显示 planned，激活后进入既有执行/审批/Delivery 投影。

## 端到端验收

新增/扩展隔离 Python Git 仓库用例，使用同一 conversation 的三个 Task 走完 Explorer→Reader→Change Proposal→fresh confirmation→write TaskLoop。用例证明：

- 确认前第三个 Task 没有 Run，确认写 Plan 激活后只有一个 Run；
- 所有 runnable node 都有 exact `WorkspaceCodingWriteNodeProof`，Agent 与 Capability input 的 proof 互斥且完整；
- 激活后路径漂移在新 Attempt 前拒绝，proof digest 篡改在重启后拒绝；
- 首个 Agent candidate 后重建 runtime，不重复已完成工作；
- 多行源文件只修改 Proposal 指定片段，其他行保持并进入 post-patch 全文件验证；
- 完整 Coordinator/双 Reader/双 Patch Planner/Patch/Test/Git/Delivery 成功，两个审批仍由用户显式确认；
- 并行后期 Planner 的 reject/outcome-unknown 均保存独立终态，后续 Patch、审批、写入、测试和 Delivery 均不启动。

## 迁移与验证

默认 SQLite 升级前已备份到 `backend/data/backups/deskpilot.pre-0065-confirmed-change-task-loop.db`，SHA-256 为 `620DF4944BEB9E2DC6E3C27AD6C6B851F7A853996D76FFF640541BFD9F35489B`；备份不提交。当前唯一 head 为 `0065_confirmed_change_task_loop`，`alembic current/check`、SQLite integrity/foreign-key、0064↔0065 往返和有数据 downgrade guard 均通过。

最终门禁覆盖 Ruff、strict mypy、依赖锁与包一致性、完整 migration、Task Workbench、116B exploration/resilience、Windows Evaluation v2、Phase75 v21、wheel Prompt 33/33，以及前端 24 文件 / 165 项、type-check/build。默认后端实际收集 827 项，最终代码冻结后的单进程统一运行 `815 passed + 12 skipped`、失败/错误为 0，仅保留 1 条既有 Starlette TestClient/httpx 弃用警告。PostgreSQL/RabbitMQ 外部 cohort 未配置时继续按既有 marker 安全 skip；没有把 SQLite/Fake 结果描述成真库、真消息队列或真实模型质量。

## 方向判断与下一步

项目方向没有跑偏。116B 的 LOCAL-only 核心主纵切已经从“预编译图能运行”推进为“用户连续三轮对话形成逐级授权，进程可重启，多个职责 Agent 串并行协作，副作用经审批并有验证回执”。这是 Codex 类持久编码 Agent 所需的运行时骨架。

但本检查点不宣称与 Codex 等价，也不宣称 116C 或生产完成。仍缺真实模型在真实仓库上的成功率、长时间运行、上下文质量和成本证据；115B 的 Candidate/Judge、代码出站、费用、真人评审和激活授权也未提供。

下一批不应继续增加新的执行状态机或自由 Shell。优先顺序是：

1. 用既有 Workbench/API 在隔离的真实 Python/Node 仓库做三轮对话用户验收和重启/长时间 soak，修复可用性与恢复缺口；
2. 增加版本化真实仓库黄金任务，但在未获 115B 授权前仍使用 LOCAL/Fake/recorded，不产出 cloud 生产结论；
3. 获得五项外部授权后执行 115B live cohort、Admission/activation，再进入 116C 真实模型质量验收；
4. create/rename、依赖安装、push/PR 等能力必须作为未来独立、显式授权范围，不能借本检查点扩权。
