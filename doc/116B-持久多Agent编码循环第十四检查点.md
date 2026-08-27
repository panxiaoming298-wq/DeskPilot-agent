# 阶段 116B：持久多 Agent 编码循环第十四检查点

## 目标与结论

本检查点复用第十三检查点的版本化 Workspace Coding 黄金任务与现有 Conversation/Workbench 公共 API，把已经存在于 TaskLoop、Attempt、ResultRef 和 WorkspaceCommandPlan 中的失败恢复语义提升为真实 API 进程级证据。没有新增执行状态机、命令 API、前端页面或模型权限。

当前结论仍限定为 LOCAL-only/Fake/recorded runtime 与隔离测试项目。第十三检查点的真实 AppContainer pytest 继续负责证明默认测试执行链；第十四检查点使用记录式 Command Runtime 做可重复故障注入，不把它冒充真实 Ruff/mypy 质量或长时间生产 soak。

## 版本化韧性资产

- 新增 `deskpilot.workspace-coding-resilience-suite.v1`，以 `workspace_suite_digest` 精确绑定第十三检查点的 `workspace_coding_v1.yaml`，基础黄金任务变化后韧性计划会 fail closed。
- 场景固定项目路径、两个唯一 Python Command Profile、三轮稳定重启、一次已知失败、预期第二 Attempt、完整 proof 漂移矩阵和 outcome-unknown 错误码。
- proof 漂移顺序固定为 Catalog、选中 Profile、project path、node spec、bound input；缺失、重复、乱序或新增字段均由冻结 Schema 拒绝。
- 资产只描述故障与验收期望，不授予 executable、argv、cwd、环境变量或任何执行权限。

## 公共 API Repair 与有界重启 soak

- 测试进程通过 `POST /conversation-turns` 创建普通对话任务，由记录式本地 Planner 从服务器 Offer 中选择 `python.ruff.v1` 与 `python.mypy.v1`，再经现有 Workbench 自动形成并激活一个严格串行 WorkspaceCommandPlan。
- 第一 Profile 返回一个已验证的已知失败；失败 ResultRef 保持不可变，第二 Profile 继续 pending。
- API 连续销毁并重建三次，每次只用 Workbench GET 恢复完全相同的节点状态、Attempt 计数和失败回执；Planner 调用数保持 1，Command Runtime 调用数保持 1。
- 后续进程经公共 `workbench:advance` 记录 `repair_started/repair_completed`，仅把失败节点恢复为 ready；再下一进程执行第二 Attempt 并通过，随后才解锁第二 Profile。
- 最终调用顺序精确为 Ruff 失败、Ruff Repair 通过、mypy 通过；首节点 `attempt_count=2` 且失败回执计数仍为 1。

这里完成的是三轮进程重建的有界 restart soak，不是数小时或数日的墙钟稳定性结论。真实长时间 soak 仍属于后续 cohort。

## 五类 proof 漂移

同一公共 API 任务在首个 Command Profile ready 后停止进程，分别注入以下漂移并重启：

1. 改变一个未选中的 Profile，使整个服务器 Catalog digest 漂移；
2. 改变已选中的 Ruff Profile；
3. 重命名 exact project directory；
4. 改变持久 node spec digest；
5. 改变持久 bound input manifest。

五项均在 Command Runtime 首次调用前返回稳定 409；Runtime 调用数为 0，Planner 没有重放。数据库 node/input 修改仅用于故障注入，执行和观察仍只经公共 API；测试不把篡改接口暴露给产品。

## 强制终止与 NO_AUTOMATIC_REPLAY

- 记录式 Ruff Profile 在持久 Attempt 已进入 `running` 后阻塞，父测试进程强杀真实 Uvicorn 子进程。
- 为避免等待生产 600 秒 claim 窗口，夹具在进程已消失后以合法内容摘要把该 Attempt 的租约推进到已过期；这等价于故障时钟前移，不伪造 candidate、receipt 或 ResultRef。
- 新 API 进程执行现有 `recover_expired()`，依据 Command Profile 的 `NO_AUTOMATIC_REPLAY` 收敛为 `outcome_unknown / CAPABILITY_OUTCOME_UNKNOWN_AFTER_LEASE`，节点与执行失败，第二个 Profile 不启动。
- 随后三轮进程重启中，节点状态、Attempt 计数、错误码和 Runtime 调用数保持不变；被中断的 Ruff Profile 始终只调用一次。

固定 Python/Node 测试 Capability 仍保留内容寻址、断网、临时快照条件下的 `DETERMINISTIC_RETRY`；本检查点没有为了制造 unknown 而错误改变其安全重试合同。unknown 场景选择本来就声明 `NO_AUTOMATIC_REPLAY` 的 Command Profile。

## 装配边界

`create_app()` 新增可选、受协议约束的 `CommandProfileCatalog` 与 `WorkspaceCommandPort` 注入点，供真实进程测试使用。未注入时仍构建原有服务器 Catalog 与 `WorkspaceCommandRuntime`，生产默认、Profile 内容、Capability Registry 和 Workbench API 均不改变。

故障服务器位于 `tests/fixtures`，不会进入生产包。它只能返回绑定当前 snapshot/Profile 的严格 `WorkspaceCommandRead`，仍没有模型自定义进程字段。

## 验证结果

- 第十四检查点专项 8/8 通过：严格 cross-digest 资产、跨进程 Repair/soak、五类 proof 漂移、强杀后的 outcome unknown 禁止重放。
- 第十三检查点默认黄金任务 3/3 再次通过，包括真实 AppContainer pytest 与三个 Uvicorn 进程的 Patch/Git 审批恢复。
- Capability 执行、MultiStepPlan/Planner Workbench 和既有 116B Explorer/编码循环/韧性联合回归通过。
- Ruff 全仓、strict mypy 307 个生产源码、`uv lock --check` 与 60 包 `pip check` 通过。
- 默认后端只读收集为 110 个测试文件 / 841 项，完整单进程运行到 100%、退出码为 0，冻结结果为 `829 passed + 12 skipped`、失败/错误为 0；仅保留 1 条既有 Starlette TestClient/httpx 弃用警告。12 项均是未配置 PostgreSQL/RabbitMQ 专用环境时的既有外部 cohort 安全跳过。
- Windows Evaluation v2 compare 无违规，report digest=`d0a316629fedc78b172b0d392637f6ca2e49e7f23434ec63e91cc181b4959bda`；Phase75 v21 compare 无违规，report digest=`805d03c4f4ab5eedb82bb877b4980fa583c7ee700a891b5286ab1bea13d95d53`，immutable baseline 未修改。
- Alembic/SQLite 的唯一/current head 均为 `0065_confirmed_change_task_loop`，`alembic check` 无新增 upgrade 操作；本检查点不新增 migration，完整 migration 测试 50 项已包含在后端全量中。
- wheel 构建通过，包含 Prompt 资源 33/33、`workspace_coding_v1.yaml` 和 `workspace_coding_resilience_v1.yaml` 各 1 份，测试故障服务器/fixture 为 0 份；`git diff --check` 通过。前端未修改，本批次不重复冒充执行上一检查点已通过的 24 文件 / 165 项、type-check/build 门禁。

## 方向判断与下一步

项目没有跑偏。本检查点强化的是 Codex 类持久 Agent 的核心质量：模型调用不重放、失败证据不可变、进程可替换、恢复决策由持久 proof 决定、未知结果不被成功或可重试伪装。它没有用更多角色名、更多页面或自由 Shell制造“多 Agent”表象。

下一批应在相同 suite 上增加真实墙钟 soak、受监督 sidecar 被终止后的恢复、更多可抛弃中型 Python/Node 仓库，以及多任务并发下的公平性与资源上限。仍不得开放自由 Shell、依赖安装、自动 push 或 cloud activation；115B 的外部授权未闭合前不执行 116C 真实模型质量结论。
