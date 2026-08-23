# 阶段 95：类型化 ResultRef 数据流与动态任务图输出节点

## 1. 本阶段结论

阶段 94 的动态图已经能由模型选择节点与依赖，但依赖边只表达调度顺序，所有目录 Child 还会争写同一个 Route 结果。阶段 95 把动态图补成了真正的数据流 DAG：

```text
verified producer result
  ↓ server-authored AgentTaskGraphResultRef
persist result_ref manifest + digest
  ↓ re-verify before dependent claim
seal exact refs into dependent Handoff digest
  ↓ re-verify before Model dispatch
typed provenance + external-untrusted payload
  ↓
dependent Child

all nodes → transitively selected output_node_key
  ↓
Parent copies only that verified Workspace Result to Route
```

这意味着 `depends_on` 不再只是“等它完成”：下游 Child 实际收到每个直接依赖的已验证结果引用和对应数据。并行 Child 不再决定共享 Route 的最终值，最终输出由封图时服务器绑定的唯一输出节点决定。

## 2. 显式输出节点与完整图贡献约束

`AgentProposeTaskGraphDecision` 新增严格的 `output_node_key`。模型可以选择输出节点，但必须同时满足：

- key 必须属于候选图；
- 图仍须无重复、无未知边、无自环、无环；
- 输出节点必须传递依赖图中每一个节点，不能创建与最终结果无关的孤立分支；
- Supervisor 将 key 转成精确 runtime node ID，并写入 v2 graph manifest、graph record 和 graph digest。

旧的 `deskpilot.agent-task-graph.v1` manifest 仍可读取和完成既有运行；新图一律使用 v2 并强制输出绑定。

## 3. 类型化、内容寻址的 ResultRef

每个动态 Child 验证成功后，Supervisor 根据服务器持久真值生成 `AgentTaskGraphResultRef`。引用绑定：

- graph ID、producer local key 和 runtime node ID；
- producer Invocation ID、Agent Result ID；
- 精确 CapabilityRef；
- `file` 或 `directory` 结果类型；
- Agent Result digest 与 Workspace Result digest；
- ResultRef 自身 digest。

生成前会重验 Invocation 的父节点血缘、Agent 版本、verification status、Result ID、Agent output envelope、Workspace manifest、result kind、run ID 和两个结果摘要。ResultRef manifest 与 digest 写入 `agent_task_graph_nodes`，不能由模型提交或改写。

## 4. 从依赖边到下游模型输入

Scheduler 只会在 verified edge 解锁后领取依赖节点。创建其 Handoff 时，执行运行时按 manifest 中的直接依赖顺序重新加载并验证 ResultRef，然后把精确引用写入 `HandoffEnvelope.upstream_result_refs`；这些引用参与 Handoff digest。

Workspace Reader 在 Model Turn 1 前再次解析 Handoff 引用，并对持久 graph/node/Invocation/Agent Result/Workspace Result 做完整重验。模型请求获得两部分：

1. `result_ref`：服务器验证过的来源、类型和摘要；
2. `external_untrusted_result`：引用指向的文件内容或目录条目数据。

只有来源证明是权威边界，Workspace 数据仍明确标为外部不可信数据，不能成为指令或扩大权限。四节点验收图中，两个 root 收到空输入，`review_a` 收到 `reader_a`，`join` 精确收到 `reader_b` 与 `review_a`。

## 5. 确定性输出与并行安全

动态 Child 完成时只写各自的 Agent Result、Workspace Result 和 ResultRef，不再写共享 `TurnRouteRecord.result_manifest`。这消除了同一 ready wave 中多个 Child 争夺最终 Route 结果的语义歧义。

所有 Child 验证后，graph Observation 投影保存每个节点的完整 ResultRef。Parent 恢复并提交该 Observation digest 后，Supervisor 再次验证整图，只读取 `output_node_key` 对应的 Workspace Result，再将它复制到 Route。ResultRef、Observation、output record 或任一底层结果摘要漂移都会 fail closed。

## 6. 持久化、API 与 Workbench

`0045_agent_task_graph_result_refs` 在不破坏阶段 94 表的前提下新增：

- `agent_task_graphs.output_local_key`；
- `agent_task_graphs.output_node_id`；
- `agent_task_graph_nodes.result_ref_manifest`；
- `agent_task_graph_nodes.result_ref_digest`。

Workbench 的动态图投影现在展示 OUTPUT key、输出节点标记和各节点 ResultRef digest。每次读取仍会从 graph manifest、runtime node、edge、Invocation 和结果记录重算证明；直接修改 `result_ref_digest` 会返回冲突，不展示伪造状态。

## 7. 与 Codex/Marvis 的距离

阶段 95 补上了可持续多 Agent 的关键数据面：模型生成 DAG、服务端授权和持久调度之外，节点间现在有可证明的数据传递和确定性最终输出。它已经不是“多个 Agent 各做一次相同任务后一起结束”的演示图。

当前边界仍然明确：

- 动态 Task Contract 当前最多授权 4 个 Child，协议结构上限为 8；
- offer 仍只有只读目录 Capability，所有节点仍使用同一条服务器 Route path；
- ResultRef 已是通用证明载体，但还没有 capability-specific input binding，例如让目录节点发现的精确文件路径成为文件读取节点的服务器裁决参数；
- 没有任意 Shell、写文件或模型自授予 Tool；
- 没有条件分支、局部重试、分支级批准或运行中原地改图；Repair/Replan 仍应创建新的 Plan generation；
- SQLite 已验证持久恢复语义，但 PostgreSQL 多进程图竞争仍需独立门禁。

下一阶段最有价值的工作是定义 capability-specific node input schema 和服务器绑定的参数来源，让目录读取、精确文件读取与固定测试 Route 可以安全地出现在同一张图中。之后再做新 generation 的 Repair/Replan，才会进一步接近任意任务的 Codex/Marvis 式循环。

## 8. 迁移与默认开发数据库

迁移命令仍是：

```powershell
cd backend
.\.venv\Scripts\alembic.exe upgrade head
```

本次工作区已实际执行。默认 SQLite 从 `0044_agent_task_graphs` 升级到 `0045_agent_task_graph_result_refs (head)`；升级前备份为 `backend/data/deskpilot.pre-0045-20260822-170901.db.bak`，SHA-256 为 `8D4C61B81C14F1CA9945BBF064A78A401DBD6402234148C42C8A616ABE644281`。升级后 `alembic check` 无漂移，SQLite `integrity_check=ok`。

## 9. 发布级验证

- Ruff 全仓通过；严格 mypy 通过 234 个生产源码；
- pytest 全量收集 560 项并执行退出 0，输出包含 12 个既有平台条件 skip；
- 完整 Workbench/Agent 组、4 节点/3 层 DAG、直接依赖 ResultRef 输入、显式 join 输出和 ResultRef 篡改门禁通过；
- `0045 → 0044 → 0045` migration 往返、空库 head、metadata check 和默认库升级通过；
- Phase75 11/11，false-success=0，unauthorized-effect=0；新增链向 v6 approval digest 的不可变 v7 baseline，compare 无违规；
- 前端 22 文件/152 项、Vue type-check 和 production build 通过；
- `uv lock --check`、`pip check` 与 `git diff --check` 通过。
