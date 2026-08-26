# 阶段 116B：持久多 Agent 编码循环第八检查点

## 1. 检查点结论

第八检查点完成了第七检查点留下的第一个真实执行缺口：`builtin.workspace_coding_explorer@1.0.0` 不再由调用方直接提交一个看似可信的 Decision，而是通过现有 `AgentExecutionRun → AgentNode → AgentInvocation → AgentModelTurn → AgentDecision → AgentResult` 主干持久运行。只有这条链完整结束、所有内容摘要与绑定重新验证通过后，Binder 才会原子生成候选文件集 Proposal。

这一步让项目更接近 Codex 类持久多 Agent 的核心，不是因为又增加了一个 Runtime 类，而是因为探索模型现在也服从统一的可恢复执行语义：运行事实写入数据库，模型输出不是权限，未知结果不自动重放，进程重启后可以重验同一条 Run/Turn/Result 证明链。

本检查点仍不是 116B 完成态。确认后的 generation-1 Reader Plan 尚未激活到 TaskLoop，Reader verified join 尚未接到 Patch 候选与新的写入确认，探索入口也尚未接入统一 Turn Planner/前端动作。

## 2. 标准持久 Explorer 执行链

- 新增 `WorkspaceCodingExplorerRuntime`，使用现有 `PlanCompilationService` 和 `AgentExecutionRuntime` 原子激活一个固定 generation-1 Plan：Explorer、确定性 Final Acceptance、Delivery。
- Explorer 固定为 LOCAL、R0、零工具、零 Capability、零上游 ResultRef、一次模型调用、零自动重试；它不能读取文件正文、调用 Shell、修改文件、运行测试、操作 Git、联网、安装依赖或发起 Handoff。
- 模型请求只包含服务器封存的规范相对路径、逐文件 proof digest、snapshot/catalog/source-message 等内容摘要，以及严格输出 Schema；不把项目源文件正文发送给模型。
- Fake Provider 通过同一 Model Gateway/Model Loop 产生 `propose_file_set` Decision，用于 LOCAL-only 隔离门禁；业务层没有直接伪造成功 Decision 的旁路。
- Explorer 成功后，Reducer 在同一数据库事务中持久化 Proposal、Turn proof 和 Agent Result，并验证 Invocation、ModelTurn、Decision、Explorer node、Final Acceptance、Delivery 与 ExecutionRun 全部收敛到一致成功状态。

## 3. 不可变 Run/Turn 证明

Alembic head 升级为 `0062_workspace_coding_explorer_turns`，新增两张不可变证明表：

- `workspace_coding_explorer_run_bindings`：绑定 exact snapshot、Explorer Contract、generation-1 Plan、ExecutionRun、Explorer node、Agent/Prompt、Catalog 和源消息摘要。
- `workspace_coding_explorer_turn_proofs`：绑定 exact Proposal、Run binding、Invocation、ModelTurn、AgentDecision、AgentResult、请求/响应/Schema 与 Result evidence 摘要。

`agent_decisions.decision_kind` 约束同步加入 `propose_file_set`。已有历史记录继续按原约束读取；存在 Explorer Run binding、Turn proof 或 `propose_file_set` Decision 时，migration downgrade fail closed，避免丢失执行证明。

`get_binding`、`get_turn_proof`、`get_proposal` 和 Workbench 读取都重新回查源 Contract/Plan、ExecutionRun/node、Invocation、ModelTurn、Decision manifest、AgentResult envelope/evidence 及项目快照。任一行、摘要或映射漂移都会拒绝，不依赖进程内对象。

## 4. 失败、恢复与未知结果

- 模型返回不符合候选文件规范、越权 route 或无法验证的 Decision 时，ModelTurn、Invocation 和 ExecutionRun 收敛到终态，Proposal 不会落库。
- snapshot 在派发前或模型返回后发生路径、文件 proof、Catalog、源消息漂移时 fail closed，不把旧输出授权给新项目状态。
- Provider outcome unknown 保留 `outcome_unknown` Invocation/ModelTurn；重启后不会透明重放相同 Explorer 调用，也不会创建 Proposal。
- Workbench 从实际 Run/Turn 状态投影 `snapshot_ready`、`explorer_ready`、`explorer_blocked`、`proposal_ready` 或 `confirmed_read_only_plan`，unknown 明确显示为阻断态。
- 原先 `submit_proposal(snapshot_id, decision)` 的无证明入口现在始终拒绝；它不能绕开 Invocation/ModelTurn/AgentResult 证明链。

## 5. Workbench 与兼容边界

现有 `WorkspaceCodingExplorationWorkbench` 增加可空 Explorer Run/Invocation/Turn、proof digest 和状态字段。旧 snapshot 或未启动的历史数据仍可显示 `snapshot_ready`；第七检查点已经确认的历史 Proposal/Reader Plan 继续可读，不要求不安全回填。

本检查点没有新增独立 API、第二套节点状态机或前端页面。`app.state.workspace_coding_explorer_runtime` 只提供组合根内的实际执行服务，后续应由统一 Turn Planner/Workbench 动作调用，而不是建立平行任务系统。

## 6. 验收结果

- 探索专项 4 项通过，覆盖真实持久 Explorer 成功与重启恢复、exact Turn proof、错误确认回滚、snapshot 漂移、Proposal/Run binding/Turn proof 篡改、无证明旁路拒绝和 outcome-unknown 重启不重放。
- 相关 Agent Registry、阶段 76 Workbench 与探索联合回归通过；migration 专项 48 项通过。
- 默认后端实际收集 820 项，统一运行 `808 passed + 12 skipped`、失败/错误为 0，仅保留既有 Starlette/httpx 弃用 warning。
- Ruff 全仓通过；strict mypy 通过 300 个生产源码；`uv lock --check`、60 个 Python 包 `pip check` 与 `git diff --check` 通过。
- SQLite/Alembic 已覆盖唯一 head `0062_workspace_coding_explorer_turns` 的 upgrade/current/check、空库建表、往返与有记录 downgrade guard。
- Windows Evaluation v2 compare 通过，report digest=`c84b8b20d72bb60a2d014b75a736f77357f995716e0c2e3836a983e4317e70cc`；Phase75 v20 compare 通过，report digest=`65f2195aacb8a5cc22603b9b5a387ef0681a3d28dac20c6b352cf4a89908b043`；两套 baseline 哈希均未修改。
- wheel 内 Prompt resource `31/31`；前端未修改，24 个测试文件 / 165 项、type-check 与 production build 通过。
- 当前机器没有 Docker，且未配置 PostgreSQL/RabbitMQ 专用测试 URL；外部 cohort 按默认规则安全 skip，本检查点不冒充真库、真消息队列或真实模型质量门禁。

## 7. 方向校准

项目方向没有跑偏，而且比第七检查点更接近 Codex 类持久多 Agent：

1. Explorer 已成为真实、可恢复的持久 Agent Turn，而不是 Binder 的外部参数。
2. Exploration、Proposal 和后继授权继续分层；模型输出仍不能直接取得文件读取或写入权限。
3. Run、Invocation、Turn、Decision、Result 和 Proposal 可跨进程重验，未知结果不会被“重试成功”掩盖。
4. 所有执行仍复用现有 Agent Runtime/Plan/Workbench 真值，没有为新角色建立第二套调度状态机。

仍需防止两个偏航方向：一是继续增加 Registry 名称、表或抽象，却不把确认后的 Reader 真正接入 TaskLoop；二是为了“像 Codex”过早开放自由 Shell、任意路径、依赖安装或自动 push。下一纵切必须继续做真实纵向执行，而不是扩大无证明工具面。

## 8. 下一执行入口

下一检查点按以下顺序推进：

1. 把 snapshot prepare、Explorer activation/run 和 Proposal 展示接入统一 Turn Planner/Workbench 动作；不新增平行 API 或独立前端页面。
2. 将用户确认后的每个 Reader mapping 编译为 exact `BoundCapabilityInput`/node proof，并在同一事务激活现有 TaskLoop；重启不得重复已经 verified 的 Reader。
3. 只有全部 Reader ResultRef 完成 verified join，才允许生成精确 Patch 候选并请求新的写入确认；探索确认不能继承为写权限。
4. 写入确认后才复用已有 Patch/Test/一次 Repair/Git/Delivery 链，并继续保留 outcome unknown 不自动重放。
5. 自由 Shell、模型提供 argv/env、依赖安装、自动 push、cloud activation 和 116C 真实模型质量结论继续不进入下一 LOCAL-only 检查点。

这一执行入口完成前，116B 继续标记为进行中。
