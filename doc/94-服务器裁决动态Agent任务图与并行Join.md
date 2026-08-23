# 阶段 94：服务器裁决动态 Agent 任务图与并行 Join

## 1. 本阶段结论

DeskPilot 已从阶段 93 的“模型选择一个预编译 Child slot”升级为“模型提出完整 DAG，服务器将其绑定为可执行图”。当前目录 Route 的新运行链是：

```text
Workspace Coordinator 1.1.0
  └─ propose_task_graph(nodes, dependencies, budgets)
       ↓ Supervisor offer / validate / bind / seal
  ready roots ──→ bounded parallel wave
       ↓ verified-edge unlock
  dependent children ──→ later waves
       ↓ every child verified
  one immutable graph Observation (join)
       ↓ reclaim same Parent Invocation
  Parent submit_result → final acceptance → delivery
```

“动态”指节点数、节点 key 和依赖边由模型在运行时选择，不再出现在预编译 Plan 中。“任意”仅限服务器 offer 所划定的 Agent/Capability/隐私/预算区域；模型不能借画图获得新权限。

## 2. 完整 DAG 决定协议

`AgentProposeTaskGraphDecision` 是一个严格结构化决定，一次包含完整候选图。每个节点声明：

- 图内唯一 `local_key`；
- 目标 Capability，而不是自由 Agent 版本；
- 有界 objective 和只能从 offer 中选择的 context refs；
- 显式 `depends_on`；
- 节点预算切片。

Pydantic 层先拒绝重复 key、未知依赖、自环和循环。这仍只是不受信提议，不会直接创建 Node 或 Invocation。

## 3. Supervisor 权限裁决与预算守恒

`AgentSupervisorRuntime` 向 Parent 提供当次 offer，并在接受决定前重新读取服务器真值。它核对：

1. 当前 Task Contract/Plan/run digest 没有漂移；
2. Parent 是 Registry 中精确 Contract/Prompt digest 的 Coordinator；
3. `may_delegate_to` 与 `may_receive_from` 双向成立，且禁止自委派；
4. 隐私模式允许，Capability 在 Task Contract 中且可运行；
5. 当前动态区域只允许无 workspace write、无额外 Tool grant 的只读 Agent；
6. 节点数不超过 Parent handoff policy、Parent 预算、Task handoff 与 Plan node 上限；
7. 每个节点预算不超过目标 Agent Contract；
8. 既有 Plan 节点加新图的 model/tool/token/wall/retry/cost 总和不超过 Task Contract。

通过后，Supervisor 选择精确 `workspace_reader@1.2.0`，生成内容寻址的 graph/node/binding ID，把候选 key 转为 runtime node ID，并在同一事务内写入图、Node 和 Edge。任何裁决失败都不会留下部分子图。

## 4. 持久图、Ready wave 与 verified join

`0044_agent_task_graphs` 新增：

- `agent_task_graphs`：Parent/Decision/binding、不可变 manifest、graph digest、节点数、最大深度和 join Observation；
- `agent_task_graph_nodes`：图内 key 到 runtime Node/Invocation/Result 的绑定、分配预算和状态；
- `workspace_agent_results`：子 Agent 的结构化 Workspace Result 与 digest，供 join 重验；
- `agent_decisions.kind=propose_task_graph`。

Scheduler 一次领取一个有界 ready wave，同波中无依赖的 Child 以 `asyncio.gather` 并发推进。每个 Child 只有在 Result 已持久、确定性验证通过后，才通过统一 verified-edge reducer 解锁直接后继。

最后一个 Child 验证后，Supervisor 逐节点核对 Invocation 血缘、Agent Result、Workspace Result 和 digest，再生成唯一 graph Observation。Parent 只能提交该 Observation digest，不能用部分结果或未验证结果提前成功。

## 5. 恢复、停止与证明重验

- Parent 封图后进入 `waiting_children`；应用重启后可继续领取 ready Child，并续接原 Parent Invocation/attempt。
- 停止 Run 会 fence 未完成 Node/Invocation，同时取消活动 graph 和 graph node。
- 一个 Child 终态失败时，graph 失败，未开始兄弟节点取消，Parent 不会产生 false success。
- Workbench 每次读取都重算 proposal/manifest/node/edge/Agent/Capability/budget/Invocation/Result/Observation 证明，并重新验证 DAG 无环与最大深度。
- 结果投影、graph digest、依赖边或血缘被篡改时返回冲突，不展示伪造图。

## 6. 已验证的动态拓扑

专项验收 Provider 运行时生成下列 4 节点、3 层图：

```text
reader_a ──→ review_a ──┐
                              ├──→ join
reader_b ──────────────┘
```

运行时 ready wave 为 `1 Parent → 2 roots → 1 → 1 → 1 Parent resume`，四个 Child 占用的预算与 Task Contract 上限精确守恒，最后图和所有节点均为 `consumed`。

Phase75 因 Registry/Prompt/Plan cohort 的有意变化新增不可变 v6 baseline；`previous_baseline_digest` 精确绑定 v5 approval digest，零容忍阈值不变。`0044 → 0043 → 0044` 迁移往返也已作为独立门禁。

## 7. 与 Codex/Marvis 的真实差距

本阶段已具有 Codex/Marvis 式系统的一个关键骨架：模型生成图、服务器授权、持久并行调度、verified join、跨重启续接和可审计控制面。但它还不是通用编码 Agent：

- 当前 Task Contract 最多授权 4 个动态 Child，通用协议的结构上限是 8；
- 当前动态 offer 只包含只读 Workspace Directory Capability，不包含 shell、写文件、测试或联网研究；
- 依赖边现在表示验证后调度关系，还没有把上游结果作为类型化输入交给下游子 Agent；
- Parent 只能一次封定完整图，不支持运行中原地改图；Repair/Replan 应创建新 generation；
- 当前 join 要求全部 Child 验证通过，没有 quorum/可选分支或分支级用户控制；
- SQLite 适合开发和恢复语义；多进程高并发仍需要用 PostgreSQL 真库做新的动态图竞争门禁。

后续应先把动态 offer 推广到目录、文件、固定测试和研究等多个受控 Capability，并定义类型化上游 Result Ref。之后再实现以新 Plan generation 为边界的 Repair/Replan、分支级停止/重试/授权和 PostgreSQL 并发验收，这才会继续向可持续编码 Agent 推进。

## 8. 迁移

阶段 94 的实际启动前置为：

```powershell
cd backend
.\.venv\Scripts\python.exe -m alembic upgrade head
```

该命令只把开发库 schema 从旧 head 升级到 `0044_agent_task_graphs`，不会自动开启新的写权限或删除任务数据。

本次实际工作区已完成升级：升级前默认库为 `0043_agent_delegations`，已保存同目录备份 `deskpilot.pre-0044-20260822-155150.db.bak`；备份 SHA-256 与升级前源库一致。升级后 `alembic current` 为 `0044_agent_task_graphs (head)`，`alembic check` 无待生成操作，SQLite `integrity_check=ok`。

## 9. 发布级验证

- Ruff 全生产码与测试通过；严格 mypy 通过 233 个生产源码；
- pytest 全量收集 558 项并以退出码 0 完成，包含按环境条件跳过的既有实库/外部运行时门禁；
- 4 节点/3 层动态 DAG、同波双根节点领取、越权拒绝、停止取消、跨重启续接和证明篡改均通过；
- `0044 → 0043 → 0044` 往返、空库 head 和 metadata check 通过；
- Phase75 11/11，false-success=0，unauthorized-effect=0，v6 baseline compare 无违规；
- 前端 22 个文件/152 项通过，Vue type-check 和 production build 通过；
- `uv lock --check`、`pip check` 和 `git diff --check` 通过。
