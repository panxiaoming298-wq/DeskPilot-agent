# 阶段 47：跨实例 Graph 取消控制邮箱

## 1. 本阶段目标

阶段 45 已能在当前 API 进程内按 graph/node fence 取消在途 Runner call，阶段 46 又保证了 backpressure waiter 不会伪造 node claim；但取消请求若落到另一个 API 实例，那个实例没有目标任务的进程内 runtime，也无法调用远端实例的 `EffectDagDispatcher`。

阶段 47 在不改变 prepare/commit/unknown、Runner generation、graph lease 和 node claim fence 语义的前提下，完成以下闭环：

- 取消命令先进入独立的持久化 graph control mailbox；
- control message 定向到数据库中当前 live graph owner/fence；
- 只有精确匹配该 owner generation 的 API router 可以 claim；
- live owner 复用原 dispatcher，保持 intent-before-IPC 和 node fence 精确命中；
- 目标 lease 过期后，其他实例可以用更高 graph fence 接管并安全收敛图；
- 消息 claim 自带单调 delivery fence，过期旧消费者不能 ack 新一轮投递；
- API 等待超时只返回“命令仍在途”，不丢弃或盲目重建命令。

## 2. 为什么不复用现有 Outbox

现有 Outbox 是任务事实的竞争消费队列：多个 publisher 中只有一个实例 claim 一条消息，随后只发布到该实例的进程内 `EventBroker`。它适合将已提交事实可靠交给本地 WebSocket 投影，但不能保证“所有 API 实例都收到一条定向控制消息”。

因此本阶段没有把取消伪装成普通任务事件，而是新增 `tool_effect_graph_controls`。每个 API 都轮询同一个 mailbox，但只能 claim 与自身 DAG owner ID 以及当前数据库 graph fence 同时匹配的记录。该表是控制投递真值，Outbox 继续只承载事实事件，两者职责不混用。

当前实现使用数据库轮询，不依赖外部 broker；这给 SQLite 开发环境提供了完整语义，也为后续替换成带唤醒能力的外部 transport 保留了稳定的持久化协议。

## 3. 内容寻址命令与状态机

取消消息 ID 为：

```text
egc_<sha256(graph_id + separator + "cancel")>
```

数据库同时以 `(graph_id, command)` 唯一约束兜底。一次 graph 生命周期只有一条 cancel 控制记录；并发、重试或响应丢失都会读取同一记录，不会产生第二次控制副作用。首次请求的 `reason`、请求摘要和 requester 保留为审计事实，后续不同 reason 不改写已存在命令。

状态机为：

```text
pending -> processing -> applied
              |
              +-> pending       handler 可重试
              +-> superseded    owner/fence 已变化

superseded -> pending           重新路由到当前 live owner/fence
```

路由字段绑定 `target_owner_id + target_fencing_token`。投递 claim 又绑定 `claim_owner_id + claim_fencing_token + claim_expires_at`；每次重新 claim 都递增消息 fence。旧 claim 即使稍后恢复，也无法 `mark_applied` 或覆盖新 owner 的状态。

`request_digest` 绑定 schema version、graph ID、command 和首次 reason。邮箱只保存控制元数据，不保存 Tool 参数、授权或敏感文件内容。

## 4. 路由与接管流程

每个 API lifespan 创建一个 router，其 owner ID 与 `TaskProcessor.dag_owner_id` 完全相同。一次跨实例取消按以下顺序执行：

1. 请求实例按 task 找到 graph，并在同一数据库中插入或读取内容寻址 cancel record；
2. router 从 graph lease 读取当前 live `owner_id + fencing_token`，把 pending/superseded 消息定向到该 generation；
3. 所有 API 都可扫描 mailbox，但只有 owner ID 与 graph 当前 live fence 同时匹配的实例能 CAS claim；
4. owner router 将 claim 交给本进程 `TaskProcessor.apply_effect_graph_control`；处理器再次核对目标 owner 和 fence，并要求目标 runtime 仍存在；
5. active dispatcher 在该 graph fence 下先落 `cancel_requested_at`，再取消尚未 claim 的 admission waiter，最后按当前 node claim fence 广播 Runner cancel；
6. worker 收敛后 router 幂等取消 Task，以消息 claim fence 写入 `applied`，记录实际使用的 graph fence；
7. 请求实例轮询到 `applied` 后才返回正常 Task 快照。

若消息没有 live target，例如 owner 已退出且 lease 已过期，任一 router 可以竞争取得新的 graph lease。只有成功取得更高 fencing token 的实例会重新定向并 claim 该消息。该实例若没有进程内 runtime，则不会猜测或重放 Runner call，而只在新 fence 下持久化 graph cancel intent、运行 reducer 并取消 Task；遗留 running/unknown Tool 的既有恢复规则仍决定最终事实。

若旧 owner 在接管后恢复，它持有的 graph fence 已失效，graph mutation 会被数据库拒绝。若 control claim TTL 过期，重新投递会递增消息 fence，旧 ack 同样被拒绝。

## 5. Runner 与提交边界保持不变

跨 API 路由没有新增绕过现有执行协议的取消路径：

- **intent-before-IPC**：live owner 仍调用 `EffectDagDispatcher.request_cancel`，先提交 graph cancel intent，再触发 executor cancel；
- **node fence**：只取消 dispatcher 当前登记的 `node_id + claim_fencing_token`，旧 node owner 不能提交终态；
- **Runner generation**：`LedgerBoundEffectNodeExecutor` 继续把原 `runner_id` 传给 `RunnerSupervisor.cancel_call(expected_runner_id=...)`，generation 不匹配时发送前拒绝；
- **prepare/commit/unknown**：确定未 commit 可返回 cancelled；进入 committing 而无 receipt 仍是 unknown；可验证 receipt 仍保留 succeeded/effect，control message 不覆盖 Runner 事实；
- **任务尚无 graph**：没有 graph owner 或在途 DAG call 可路由，普通 Task cancel 仍直接执行，不因 mailbox 返回 500。

HTTP 请求等待有界。超过 `DESKPILOT_EFFECT_GRAPH_CONTROL_REQUEST_TIMEOUT_SECONDS` 时返回 `503 EFFECT_GRAPH_CONTROL_PENDING` 和稳定 `control_id`。这只表示同步等待结束；数据库命令仍会继续路由。客户端应先 GET Task 对账，再决定是否重试同一 cancel。

## 6. 迁移与配置

新增 Alembic `0019_graph_control_mailbox`，创建 `tool_effect_graph_controls`，包含：

- task/graph 外键与 `(graph_id, command)` 唯一约束；
- command/status/version/target-pair 检查约束；
- owner/fence 定向字段；
- claim owner、TTL 和单调 delivery fence；
- attempt、backoff、error code、applied graph fence 与时间戳；
- route 和过期 claim 索引。

新增启动配置：

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `DESKPILOT_EFFECT_GRAPH_CONTROL_POLL_INTERVAL_SECONDS` | 0.05 | mailbox 轮询及请求端状态复核间隔 |
| `DESKPILOT_EFFECT_GRAPH_CONTROL_CLAIM_TTL_SECONDS` | 15 | 一次消息 delivery claim 的数据库 TTL |
| `DESKPILOT_EFFECT_GRAPH_CONTROL_REQUEST_TIMEOUT_SECONDS` | 30 | Cancel API 同步等待 fenced ack 的上限 |

消息 claim 和 graph lease 是两层不同的 fence：前者保护控制投递/ack，后者保护图和节点事实写入，不能互相替代。

## 7. 验收结果

```text
Ruff:  All checks passed
mypy:  Success, 122 source files
pytest: 339 passed
Alembic: 0019_graph_control_mailbox (head), no new operations
frontend vitest: 15 files, 126 passed (workspace Node 24.14.0)
frontend type-check/build: passed
```

新增覆盖包括：

- 两个独立 FastAPI 实例共享数据库，请求实例没有 runtime，owner 实例持有 graph lease 和两条在途 Runner call；远端 cancel 精确到达 owner，requester Runner 零调用；
- 两次 Runner cancel IPC 前均已能读取 graph `cancel_requested_at`，graph fence 与路由时捕获的 fence 一致；
- 并发重复请求只由 owner handler 应用一次，reason、node fence 和 Runner generation 精确传递；
- 目标 owner/fence 变化后旧 graph mutation 和旧 control ack 均被拒绝，消息重新定向到新 owner/fence；
- delivery claim 过期后消息 fence 递增，旧消费者无法确认新投递；
- 请求进程退出后，持久化的无目标 cancel 仍可由新 router 取得 graph lease 并收敛；
- `head -> 0018 -> head` 往返、表/索引/外键精确检查与 metadata drift 检查通过；
- 阶段 45/46 的 Runner cancel 三态、generation、graph/node fence、backpressure 和 ready proof 回归继续通过。

## 8. 已知边界与下一步

1. mailbox 当前依赖数据库轮询，没有外部 broker 的主动唤醒；取消延迟下界受 poll interval 和数据库负载影响。
2. claim 使用通用 CAS 路径，尚未增加 PostgreSQL 专用 `SKIP LOCKED/RETURNING` 批量 graph-control claim，也未在真实 PostgreSQL 上做进程杀死和网络分区故障注入。
3. graph control 目前只定义 cancel；没有受保护的查询/重放/retention 运维 API、指标和告警。超时后的控制可由稳定 `control_id` 审计，但尚未投影到前端。
4. DAG admission 仍只在单 API 进程内全局；多实例总容量和公平性尚未由数据库协调。
5. ready v3 页限制单次 proof payload，但完整 membership 仍以 O(V+E) 重算。

下一阶段入口：**数据库支持的集群级 DAG admission 与跨实例公平容量，随后推进增量 ready 投影；保持 graph-control owner/fence、claim-before-runner、prepare/commit/unknown 和取消语义。**
