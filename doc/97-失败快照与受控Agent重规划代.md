# 阶段 97：失败快照与受控 Agent 重规划代

## 1. 本阶段结论

阶段 96 的 Parent 已能在冻结 Contract 和服务器 offer 内动态生成异构子任务 DAG，但一次协议错误会让整个 Run 终止。阶段 97 补上最小、可持续的 Repair/Replan 闭环：一个符合白名单条件的只读目录分析失败后，服务器封存失败证据，在同一不可变 Task Contract 下编译完整的新 Plan generation，并在同一事务中创建 replacement Run。

```text
Plan generation 1 / Run 1
  Parent proposes dynamic graph
  Child violates exact Route binding
  graph + node + invocation + model turn + run = failed
                     ↓ server-derived failure snapshot
atomic replan transaction
  old Plan = superseded (manifest immutable)
  old Run / graph / turns remain failed and immutable
  compile Plan generation 2 with the same Contract
  create Run 2 + nodes + edges
  reset only the reusable read-only Route projection
  persist source → target lineage digest
                     ↓
Run 2 starts a fresh Parent
  Parent may propose a different authorized DAG
  server re-offers exact Capability/Input
```

这不是把失败节点原地改回 ready，也不是复用旧 Invocation。新 Plan、Run、Node、Handoff、Invocation 和动态 graph 都有新一代身份；旧代继续提供完整失败审计证据。

## 2. 自动 Replan 的严格资格

首版只允许 `workspace_directory_analyze`，并同时要求：

- source Run 是当前 active Plan generation 对应的 `failed` Run；
- source generation 必须为 1，因此每个 Task 最多自动生成一代 replacement；
- Route 必须是 `failed`，稳定错误只能是 `AGENT_TASK_GRAPH_REJECTED`、`AGENT_ROUTE_BINDING_REJECTED` 或 `AGENT_LOOP_NO_PROGRESS`；
- 至少存在一个 failed runtime node、一个 terminal failed Invocation 和一个带相同稳定错误码的 failed Model Turn；
- Task Contract、Plan manifest、Run digest、Route parameter digest 和所有失败引用必须通过持久证明重验。

以下情况不会自动 Replan：用户停止、等待补充输入、Outcome Unknown、预算耗尽、Policy/隐私拒绝、任何写 Route，以及第二代再次失败。它们保持 blocked，避免重规划循环、扩权重试或掩盖未知副作用。

## 3. 服务器生成的失败快照

`AgentReplanFailureSnapshot` 是最小化、不可变的服务器证据，绑定：

- Task、source Run、source generation 和 source Plan digest；
- Contract version/digest；
- 精确 Route、Route parameter digest 和失败时 revision；
- 稳定错误码；
- failed node、Invocation 和 Model Turn 身份；
- snapshot digest。

模型不能提供这些可信 identity 或 digest。当前 Parent 的结构化失败已经由 Model Turn/Decision、Supervisor 拒绝和 Run 状态表达，Replan 控制面从数据库真值派生快照。后续可以允许 Parent提交“建议修复目标”，但它只能是无授权的 proposal，仍需绑定这份快照并由服务器裁决。

## 4. 原子 generation 激活

`PlanCompilationService.replan_failed_directory_analysis()` 在一个数据库事务中：

1. 锁定 PlanningState、source Run 和 Route；
2. 重验 active Contract、source Plan 和失败证据；
3. 使用同一 Contract 与受信模板编译 generation 2；
4. 将旧 Plan 状态指针标记为 superseded，但不修改旧 manifest；
5. 保存新 Plan，并创建 replacement Run、Node 和 Edge；
6. 更新 active Plan pointer；
7. 清空可重做只读 Route 的旧结果/错误并恢复 ready；
8. 保存 `AgentReplanRead` source→target 谱系与摘要。

任一步失败都会回滚，系统不会出现“active 指针已经前移但 Run 尚未创建”的半激活状态。重复或并发调用在 generation/source Run 条件上 fail closed。

## 5. 谱系证明与控制面

`0047_agent_replans` 新增 `agent_replans`，对 source Run、target Run 和 target generation 分别设置唯一约束。读取 `GET /api/v1/tasks/{task_id}/replans` 时会重新验证：

- record、manifest 和 replan digest；
- source/target Run 与 Plan generation/digest；
- source Plan 已 superseded、source Run 仍 failed；
- Contract 与 Route parameter digest 未漂移；
- 快照引用的 failed Node、Invocation 和 Model Turn 仍存在且状态一致。

Workbench 新增 `replan_failed_execution` action 和 `POST /api/v1/tasks/{task_id}/workbench:replan`。后台 Workbench Coordinator 把它视为可自动执行的安全控制动作：第一次推进若因模型协议逃逸而失败，持久 WorkItem 会重试；下一次观察到该 action 后原子 Replan，再继续运行 generation 2。前端只轮询投影，并显示 generation 1→2、稳定错误、failure snapshot digest 和 lineage digest。

## 6. 端到端证明

专项 Provider 在 generation 1 先生成合法异构图，随后让第一个 Reader 提交错误 Route binding：

- generation 1 的 graph、Parent/Child、Run 进入 failed；
- Workbench 只开放一次 Replan；
- generation 2 重新启动 Parent，生成并完成 `directory → file → directory` 异构 DAG；
- generation 1 的 graph 投影在 Replan 前后逐字段保持一致；
- 重复 Replan 返回 409；
- 修改 replan manifest 后，Planning API 返回 `409 PLANNING_PROOF_REJECTED`；
- 开启后台 Coordinator 时，无客户端 advance 也可完成失败→Replan→成功闭环。

## 7. 与 Codex/Marvis 的距离

现在系统已经可以在一次对话任务内跨 Plan generation 自我恢复：Parent 每代都能动态出图，服务器拥有任务图、能力输入、预算、调度、失败裁决和 replacement lineage。这比“同节点 retry”更接近 Codex/Marvis 的持续工作循环。

当前仍是刻意受限的最小闭环：

- Replan 只覆盖只读目录分析和三类确定的模型协议失败；
- 新一代仍使用同一受信 Plan 模板，动态变化发生在新 Parent 的运行时子图；
- 没有让模型修改 Contract、预算、风险、Provider、文件授权或输出类型；
- 没有跨代导入 verified ResultRef，也没有条件分支、节点级 repair proposal 或用户批准后的 Contract amendment；
- 不支持自由 Shell、动态 executable/argv、联网安装、目录创建、删除或覆盖。

下一阶段应把固定 Python/Node 测试设计为新的服务器绑定 CapabilityInput，使动态图能够把显式文件/目录证据连接到固定测试节点；仍由服务器固定 executable、argv、快照、断网沙箱和输出 Schema。再下一步才扩展可验证的 Parent repair advice 和跨代 verified evidence import。

## 8. 迁移与默认开发数据库

默认 SQLite 已从 `0046_agent_task_graph_capability_inputs` 升级到 `0047_agent_replans (head)`。升级前备份：

```text
backend/data/deskpilot.pre-0047-20260822-204715.db.bak
SHA-256 0E8968C9598E3AA1092A04741A060DA8B036B84E228C6968024EE00B2DBC4BBE
```

`0047 → 0046 → 0047` 往返、Alembic metadata check 和空库重复 migrate 均通过。

## 9. 验证结果

- Ruff 全仓与严格 mypy 237 个生产源码通过；
- 手动 Replan、后台自动 Replan、旧图不可变、重复拒绝和谱系篡改专项通过；
- Phase75 11/11，false-success=0，unauthorized-effect=0；新增链向 v8 approval digest 的不可变 v9 baseline，compare 无违规；
- 后端 pytest 全量收集 81 个测试文件 / 567 项并统一退出 0，包含 12 个既有平台条件 skip；
- 前端 API/Workbench 专项、22 个测试文件 / 152 项、Vue type-check 和 production build 通过；
- `pip check`、Alembic 单一 head、migration 往返、默认库升级和 diff whitespace 检查通过。
