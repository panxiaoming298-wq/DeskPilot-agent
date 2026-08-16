# 39. 版本化 Tool effect graph、原子节点 transition 与 Saga 补偿

## 1. 阶段结果

DeskPilot 已把阶段 38 的固定单 Tool 执行切片扩展为版本化、可查询的 Tool effect graph。每个图明确保存 graph/node/edge/attempt/effect/compensation 身份；图状态和节点状态不再只存在于 TaskProcessor 内存或事件文本中。

新增显式 `file_move_saga` 入口用于验证有界多步副作用：一次任务可包含 2～10 个互不重叠的单文件移动，所有路径仍只来自结构化用户请求并在任务创建前规范化，不从 goal、模型输出或历史事件提取。每个 forward 节点独立执行 Policy、一次性审批、Runner 调用和持久化 commit receipt。

确定性失败发生在后续节点时，系统只对已有回执证明的前置效果按逆序规划补偿。每个 compensation 都产生新的 call/attempt/effect、随机高熵 Tool 幂等键、Policy 请求、一次性审批、Runner commit 和 receipt；不会改写原 forward 调用账本，也不会复用原审批。

## 2. 图模型与身份

Alembic `0013_tool_effect_graphs` 新增：

| 表 | 用途 |
| --- | --- |
| `tool_effect_graphs` | 每任务一个 `deskpilot.tool-effect-graph.v1` 图及当前执行模式/终态 |
| `tool_effect_nodes` | 有序 Tool 节点、Contract 摘要、补偿策略和节点状态 |
| `tool_effect_edges` | forward success 依赖与反向 compensation order 边 |
| `tool_effect_attempts` | `forward`/`compensation` attempt 与唯一 Tool call 绑定 |
| `tool_effects` | receipt-bound applied effect 及 `compensates_effect_id` 血缘 |
| `tool_effect_transitions` | 与 TaskEvent 一一绑定的 append-only 节点 transition journal |

graph、node、edge、attempt、call 和 effect 身份都由版本、任务和节点身份确定性派生。compensation 与 forward 使用不同的 attempt/call/effect 身份；底层 `tool_calls` 的 compensation 使用新的 attempt 序号，避免覆盖原调用。

图定义和查询投影不保存原始 Tool arguments、文件路径或幂等键。`GET /api/v1/tasks/{task_id}/effect-graph` 只返回节点 key、Tool/Contract 摘要、状态、边、attempt/call/effect/receipt 身份及 transition 证明。原始多步请求继续只存在于 DPAPI 保护的 runtime checkpoint 中。

## 3. 原子节点 transition

每次 `effect.node.started`、attempt requested/running/succeeded/failed/unknown、compensation started/completed/failed 和 graph completed/compensated 都在一个数据库事务内同时完成：

1. 追加 TaskEvent；
2. 追加 Outbox；
3. 更新 node/graph mutable projection、revision 和 `last_event_seq`；
4. 写入 `tool_effect_transitions`，唯一绑定 `event_id` 与 graph 内 `event_seq`；
5. 在适用时更新 attempt 状态并创建 effect/compensation lineage。

因此查询到的节点状态总能追溯到同事务提交的事件；不存在“节点已变但 transition 事件未提交”的状态。启动恢复把遗留 running Tool 收敛为 `unknown` 时，也会在同一事务把 attempt/node/graph 收敛为 `blocked_unknown`。

Tool call 账本事件与后续节点 transition、以及 transition 与受保护 TaskCheckpoint 仍是相邻事务。中间崩溃时会通过 checkpoint/event/call/graph 绑定 fail closed，不会猜测或重放；本阶段消除了节点 projection 自身的事件窗口，但尚未把所有安全账本合并为一个统一事务。

## 4. 多步 forward 语义

`file_move_saga` 当前是受信应用定义的有序图，不接受模型生成路径或条件边：

- 创建前规范化全部 source/destination；每个 source 必须是普通文件，每个 destination 必须不存在且同卷；
- 所有节点资源路径必须互不重叠，避免隐式读写依赖和别名绕过；
- 每个节点只在前一节点存在 receipt-bound applied effect 后开始；
- 每个节点重新投影资源版本、重新评估 Policy、重新审批和提交；
- 节点全部成功后 graph 进入 `succeeded`，任务才进入 `succeeded`。

连续两次 API 重启测试证明：任务可分别停在第一和第二个 pending approval，重启后按 checkpoint 中的 graph/node/mode 与数据库 graph/call/approval 事实恢复，不重建路径、不重复 forward call。

## 5. Saga 补偿语义

当后续 forward 节点在副作用边界前或 Runner 返回确定失败时：

1. 失败节点进入 `failed`，graph 切换到 `compensating`；
2. 只选择已有 committed receipt 的前置节点；
3. 按 `compensation_order` 逆序逐个处理；
4. reverse source 版本从原 receipt 的 `resource_versions_after.destination` 取得；
5. reverse source/destination 由受保护原请求与 receipt 共同推导；
6. 每个反向移动重新执行资源版本校验、Policy、一次性审批、Runner commit 与 receipt 投影；
7. 原 effect 变为 `compensated`，新 compensation effect 记录 `compensates_effect_id`；
8. 全部完成后 graph 为 `compensated`，原任务仍以 `SAGA_COMPENSATED` 失败终结，因为原目标没有完成。

不可补偿节点显式使用 `compensation_strategy=none`。当前公开多步写入口只允许 receipt-bound `file.move` 节点，不允许构造混合不可补偿写图；只读 `computer.disk_usage` 节点标记为 `none` 且不会产生外部补偿。后续引入混合 Tool 图时，任何已应用的 `none` 节点都必须把图收敛为 `blocked_non_compensable`，不得跳过后继续声称已补偿。

## 6. Unknown 阻断

forward 或 compensation 一旦为 `unknown`：

- attempt 进入 `unknown`；
- forward node 进入 `unknown`，compensation node 进入 `compensation_unknown`；
- graph 原子进入 `blocked_unknown` 并记录 failure node；
- 现有 ToolReconciliation 继续承载人工证据和裁决；
- 不启动后继 forward，不启动或继续自动 compensation，也不把幂等键当作重放授权。

启动恢复遇到已派发 running call 时同样执行上述阻断语义。只有后续显式 reconciliation 协议能决定是否允许新 attempt 或新的补偿任务；本阶段不会从 graph 状态推断外部效果。

## 7. API 与验收

新增接口：

```text
POST /api/v1/tasks
  tool_request.kind = file_move_saga

GET /api/v1/tasks/{task_id}/effect-graph
```

验证结果：

```text
Ruff:  All checks passed
mypy:  Success, 107 source files
pytest: 288 passed
Alembic: 0013_tool_effect_graphs (head), no new upgrade operations
frontend vitest: 15 files, 122 passed
frontend type-check/build: passed (workspace Node 24.14.0)
```

新增自动化覆盖：两个 forward 节点的独立 call/effect/receipt、图查询不泄露路径、transition 与事件序号一一对应、后续资源失效后逆序补偿、compensation 新审批/新回执/血缘、Policy 拒绝同步终止绑定图、Runner unknown 零补偿，以及连续两次 API 重启精确恢复不同节点。

## 8. 已知边界与下一步

1. 当前多步图是受信应用生成的有序 DAG 切片，不支持任意分支、并行节点或模型生成条件边。
2. 节点 transition journal 已原子化；Tool call 账本、graph transition 和受保护 TaskCheckpoint 尚未全部合并为同一事务。
3. 当前公开多步写图只包含可回执补偿的 `file.move`；混合不可补偿节点尚未开放。
4. 补偿本身失败或 unknown 会终止自动编排；不实现“补偿的再补偿”。
5. graph 仍以单 API 进程为目标，没有跨实例 graph lease、claim 或 fencing token。
6. 下一阶段应增加 graph lease/所有权与 CAS revision，统一 Tool ledger + node transition + protected checkpoint 的事务命令，并为 reconciliation 裁决后的 graph 继续/终结定义显式协议。
