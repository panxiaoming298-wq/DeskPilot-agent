# 阶段 45：在途 Runner 取消与 Fence 语义

## 1. 本阶段目标

阶段 44 已让 graph cancel intent 停止新的 ready-set claim，但已经持有 graph/node lease、正在等待 Runner 结果的调用仍只能自然结束。阶段 45 将现有 `deskpilot.runner.v1` cancel IPC 接到受信 v2 DAG 控制面，同时保持以下安全边界：

- cancel intent 必须先成为数据库事实，之后才能向 Runner 广播；
- 只取消当前 graph fence 下、当前 node claim fence 所属的调用；
- cancel 只是一项请求，Runner 的 `cancelled/unknown/succeeded` 终态仍是权威事实；
- `file.move` 的 prepare/commit/receipt 裁决不被控制面覆盖。

## 2. 控制面时序

`TaskProcessor` 在收到任务取消后先设置运行时 stop signal。若 v2 DAG dispatcher 正在运行，则调用其 `request_cancel`；若图停在未派发/审批等待阶段，则在新的 graph lease 下直接落库 cancel intent 并执行 reducer。

在途路径严格按以下顺序执行：

1. `EffectDagDispatcher` 使用自己当前持有的 graph owner 与 fencing token 调用 `request_effect_dag_cancel`；
2. 数据库写入 `cancel_requested_at`、推进 graph revision，并产生 `effect_graph.cancel_requested` 事件；
3. dispatcher 枚举本轮实际 claim 的节点，仅把当前 `node_id + claim_fencing_token` 交给 executor；
4. executor 将 claim 映射到已经原子写为 `running` 的 Tool call 与原 Runner ID；
5. `RunnerSupervisor.cancel_call` 先校验 `expected_runner_id`，再向该 generation 的 client 发送签名 cancel IPC；
6. Runner 返回终态后，原 executor 路径仍使用相同 graph/node fence 原子提交 Tool ledger、attempt、node transition 与事件。

因此 cancel IPC 不获得写 graph 的额外权限，也不能让旧 node owner 或旧 Runner generation 修改新一代事实。

## 3. Claim 到 Runner 之间的竞态封闭

claim 已提交但 Runner call 尚未发出时存在一个窄窗口。阶段 45 使用数据库事实与进程内 claim identity 双重封闭：

- dispatcher 在 claim 后立即登记当前 node fence；
- executor 在 `start_tool_call` 前后检查该 claim 是否已经收到取消；
- `start_tool_call` 的 fenced 事务额外检查 forward graph 的 `cancel_requested_at`；
- 若 intent 已存在，调用保持在未越过 Runner 边界的状态并以确定性 `cancelled` 收敛；
- 若 `tool.started` 已提交，executor 绑定 `call_id + runner_id + node fence`，随后广播 IPC。

cancel intent 已存在时 ready-set 始终为空；dispatcher 在空 ready-set 时再次运行 reducer，避免返回旧的 active 快照。

## 4. Prepare、Commit 与 Unknown 保持不变

控制面不推断外部效果，也不把“已发送 cancel”当成“已经取消”：

| Runner/commit 边界 | 持久化结果 | graph 行为 |
| --- | --- | --- |
| prepare 前或确定未进入 commit | `cancelled` | 当前节点 cancelled，后代 skip/cancel，graph cancelled |
| 已进入 committing 且没有 durable receipt | `unknown` | 当前节点 unknown，graph 优先归约为 `blocked_unknown` |
| durable committed receipt 已存在 | `succeeded` | 成功与 effect/receipt 保留；cancel intent 只阻止/取消剩余节点 |

这复用既有 Runner controlled-commit boundary：取消广播不会将 unknown 降格为 cancelled，也不会覆盖 receipt-proven success。

补偿 dispatcher 不消费 forward graph 的任务取消信号。已经开始的 receipt-bound 补偿仍按 wave barrier 完成或进入 compensation failed/unknown 阻断，避免用户取消中断安全回滚。

## 5. Runner Generation 与失败语义

`RunnerSupervisor.cancel_call` 新增 `expected_runner_id`。若 supervisor 已换代，旧调用的取消在发送 IPC 前得到 `RUNNER_GENERATION_CHANGED`，不会误发给新 Runner。原调用路径随后按既有 generation failure 规则收敛为 unknown，不会重放。

IPC 发送本身采用 best-effort：管道关闭或 generation 丢失不会伪造 cancelled；最终仍由正在等待的调用结果、Runner 失败或启动恢复决定 durable 终态。

## 6. 验收结果

```text
Ruff:  All checks passed
mypy:  Success, 118 source files
pytest: 324 passed
Alembic: 0018_branch_decision_proofs (head), no new operations
frontend vitest: 15 files, 126 passed (workspace Node 24.14.0)
frontend type-check/build: passed
```

新增覆盖包括：

- 双根在途调用先落 graph cancel intent，再按各自 node fencing token 广播取消；
- 完整 Task API → TaskProcessor → dispatcher → ledger executor → Runner cancel 集成链；
- cancel reason 原样传递，两个调用均绑定原 Runner ID；
- 旧 Runner generation 在 IPC 发送前被拒绝；
- cancel 返回 unknown 时 graph 保持 `blocked_unknown`；
- cancel 与已提交成功竞态时保留 succeeded，剩余节点由 cancel reducer 收敛；
- 取消前文件未进入 commit 时源文件保持不变、目标不存在。

本阶段没有数据库结构变更，Alembic head 保持 `0018_branch_decision_proofs`。

## 7. 已知边界与下一步

1. 当前在途广播由持有本地 Task runtime/dispatcher 的 API 实例执行；跨 API 实例把用户取消可靠路由到远端 live graph owner，仍需持久化控制消息或外部 broker。
2. cancel intent 可停止新 forward claim；补偿继续服从 receipt-bound wave 安全语义，不提供强制中止补偿。
3. dispatcher 仍采用单图固定并发上限与整批 ready-set；下一阶段增加全局公平性、每图/每 Tool 配额、分页、backpressure 和大图压力测试。
4. Outbox DLQ/requeue/cleanup 仍缺少受保护的运维 API/UI、retention scheduler 与指标审计。
