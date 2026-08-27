# 阶段 116B：持久多 Agent 编码循环第十检查点

## 1. 检查点结论

第十检查点已闭合 `verified Reader ResultRef → 无写权限 Change Proposal Model Turn → 新的 exact 用户确认 → 后继 generation-1 写 Plan`。Reader Task 不被改写：它在原 generation-1 只读 TaskLoop 成功后，以同一 Task 的 generation-2 Contract/Plan 运行一个零工具 Change Proposer；用户确认后则创建第三个 Task，并持久化既有 `workspace_coding_loop` 的完整写计划。

这一纵切没有新增第二套执行状态机。提案继续复用 `PlanCompilation → ExecutionRun → Invocation → ModelTurn → AgentDecision/Result`，后继写计划继续复用既有 Contract/Plan；四张新表只保存不可变授权、证据映射和摘要，不保存另一套步骤状态。

本检查点仍不标记整个 116B 完成。后继写 Plan 被明确保留在 `planned`，Workbench 禁止自动 `START_EXECUTION`；把该确认绑定激活为现有 TaskLoop 的第三种受信来源，并走通 Patch/Test/Repair/Git/Delivery，是下一检查点。

## 2. Reader 证据到无权变更提案

- 新增 LOCAL-only `builtin.workspace_change_proposer@1.0.0`。它只有一次模型调用、零工具、零 Handoff、R0、无 Capability；Contract 明确禁止 Patch、Test、Git、Shell、网络、依赖安装和自动 push。
- `WorkspaceCodingChangeRunBinding` 精确绑定 confirmed file-set、成功 Reader execution、terminal event、完整 ResultRef 集、Contract v2、Draft/Plan generation 2、Run、proposer node 与 Agent Contract/Prompt digest。
- ModelRequest 只消费 exact Reader 输出和原任务目标；每个 proposed replacement 必须保持原文件集顺序，并绑定 ResultRef/result/version digest，`old_text` 必须在对应已验证内容中恰好出现一次。
- Proposal、Model Turn proof 与 Agent Result 在同一 reducer 事务持久化；Result 的 limitation 明确记录 `proposal_has_no_execution_authority`。
- 已持久 Proposal 可跨重启读取，不重复已成功 Model Turn；`running` 或 `outcome_unknown` 不自动重放。文件、snapshot、Reader result、Plan、Agent、Request/Response、Decision 或 proof 任一漂移都 fail closed。

## 3. 新确认与后继写 Plan

- 唯一接受文本为 `确认变更提案：{proposal_id}`。确认必须来自同一 conversation 的新 user Task/message，消息时间严格晚于 Proposal，并重验 message digest、Task goal 和 Proposal 全链证明。
- 文件集确认不会继承为 Patch 权限；后继计划使用新的 Task、Contract v1 和 Plan generation 1。
- 服务器将 Proposal 中每个 exact path/old/new replacement 固化进 `workspace_coding_loop` 参数与 recipe/binding manifest；模型不能增加路径、修改 executable/argv/env 或改变图结构。
- Contract、Draft、Plan 与 `WorkspaceCodingWritePlanBinding` 在同一数据库事务持久化。事务失败不会留下半个写计划；同一 Proposal 的幂等重试必须命中同一 successor/message，竞争的第二个后继 Task 会被拒绝。
- Workbench 复用现有 Task 投影与 conversation-turn 接口展示 Proposal 和确认文本，不新增独立 API 或前端页面。确认后的后继 Task 显示为 `planned`，但本检查点显式禁用自动执行。

## 4. 数据库与兼容

Alembic head 升级为 `0064_workspace_coding_change_proposals`，新增四张不可变证明表：

- `workspace_coding_change_run_bindings`
- `workspace_coding_change_proposals`
- `workspace_coding_change_turn_proofs`
- `workspace_coding_write_plan_bindings`

迁移不虚构历史数据。旧 Agent/Task/Workbench 摘要在新字段缺失时保持原材料；已结束的 Reader TaskLoop 在其 Plan 被 generation 2 supersede 后仍可按 exact terminal history 读取。存在 Change Proposal Run 或后继写 Plan 证明时 downgrade 拒绝，避免丢失授权链。

默认 SQLite 在升级前备份为 `backend/data/backups/deskpilot.pre-0064-workspace-coding-change-proposals.db`，SHA-256 为 `B145D8164765AD03360168E5A28D5C157DE21C1F5EDE20C7C069BE346F129F90`；升级后唯一/current head 为 `0064_workspace_coding_change_proposals`，`alembic check` 无待生成操作，`integrity_check=ok` 且 foreign-key 零违规。

## 5. 验收证据

- 阶段专项 9/9 通过，覆盖两文件端到端 Proposal、重启幂等、历史 Reader 可读、新确认、后继 Plan 持久化但零 Run/零写入、竞争后继拒绝、Reader 文件漂移、Turn proof 篡改和 outcome-unknown 不重放。
- migration 专项完整通过，覆盖空库、upgrade/current/check、`0063 ↔ 0064` 往返与有记录 downgrade guard；Task Workbench 全文件回归通过。
- Ruff 全仓、strict mypy 304 个生产源码、`uv lock --check`、60 个 Python 包 `pip check` 通过；wheel 包含 33/33 Prompt resource，并包含 Change Proposer JSON/TXT。
- Windows Evaluation v2 compare 通过，report digest=`785dc2cfa1ef2a0c2e98312982e3b08f877daf9ad4e34a88cacef5973a1d3466`。新增 Agent/Prompt 导致受控的 Plan/Cohort digest 变化，因此追加链向 v20 的不可变 Phase75 v21；11/11、false-success=0、unauthorized-effect=0，report digest=`805d03c4f4ab5eedb82bb877b4980fa583c7ee700a891b5286ab1bea13d95d53`。
- 前端未改，24 个测试文件 / 165 项、type-check 与 production build 通过。
- 默认后端实际收集 826 项，最终代码冻结后的单进程统一运行 `814 passed + 12 skipped`、失败/错误为 0；仅保留 1 条既有 Starlette TestClient/httpx 弃用警告。12 个 skip 来自未配置的 PostgreSQL/RabbitMQ 专用外部 cohort，不冒充真库、真消息队列、真模型质量或生产激活。

## 6. 方向校准与下一步

项目没有跑偏。相较第九检查点，本次把“模型可读证据”推进成“模型可提出修改，但仍不能执行”，再由一个新的用户回合生成可审计的写计划。这比直接把 Explorer 或 Reader 输出升级为 Patch 权限更接近 Codex 类持久 Agent：对话、计划代、模型回合、权限边界和恢复证据都是持久真值，模型输出本身不是授权。

下一检查点应只做一条纵切：

1. 将 `WorkspaceCodingWritePlanBinding` 作为现有 TaskLoop 的受信来源激活，不伪造 ModelPlanner Offer/Draft，也不新增状态机。
2. Activation 与每次 claim 重验 Proposal、fresh confirmation、recipe/parameters、Contract/Plan/node、Catalog/Agent/Capability 和项目 snapshot。
3. 复用既有 2～8 文件 Coordinator/Reader/Patch Planner/Patch/Test/一次 Repair/Git/Delivery；已确认 `changes_json` 是唯一写入候选，不能重新让模型扩展路径或 replacement。
4. 重启不重复已验证步骤；Patch/Test/Git 的 running/outcome-unknown 继续 `NO_AUTOMATIC_REPLAY`。
5. 完成统一 conversation/Workbench 端到端验收后，再判断 116B 是否达到 LOCAL-only 完成口径。

自由 Shell、依赖安装、自动 push/PR、cloud activation 和 116C 真实模型质量结论继续不进入下一检查点。115B 的外部 Provider/Judge-human/出站/费用/激活授权仍是独立生产门。
