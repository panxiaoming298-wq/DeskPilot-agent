# 42. DAG 并行 Dispatcher、图级 Reducer 与可靠消息投递

## 1. 阶段结果

本阶段将阶段 41 的 v2 DAG ready-set/claim 原语接成可执行的应用层 dispatcher，并补齐 node claim 续租、图级终态归约、skip/cancel 传播和内容寻址的并行补偿波次证明。

Outbox 现在为每次投递生成独立 delivery ID，有界重试耗尽后进入 DLQ，并提供显式重新入队和已发布消息清理。新增消费端 Inbox 在同一数据库事务内占用 `consumer_name + message_id` 并运行 handler，使 broker 重发不会重复提交数据库副作用。

PostgreSQL 路径已加入 `asyncpg` 运行依赖，Outbox 使用单条 `FOR UPDATE SKIP LOCKED` CTE + `UPDATE ... RETURNING` 批量 claim，DAG node 使用 `SKIP LOCKED` 批量加锁后一次 `UPDATE ... RETURNING` 发放 fence。SQLAlchemy PostgreSQL 方言编译测试会稳定断言这些原生语义。

## 2. v2 DAG Dispatcher 与并行 Runner 边界

`EffectDagDispatcher` 每轮执行：

1. 使用数据库时间获取 graph lease，并按 TTL/3 续租；
2. 持久化当前 ready-set 证明；
3. 按每图并发上限从证明集合原子 claim 节点；
4. 使用 `asyncio.gather` 并行跨越 Runner adapter 边界；
5. 每个在途节点独立按 TTL/3 续租，心跳不改变 fencing token；
6. 结果提交同时核对 graph fence 和 node fence，然后重新执行 reducer。

`RunnerEffectNodeExecutor` 是实际 `RunnerSupervisor.call_tool` 的 adapter。原始参数、授权 grant、幂等数据不进入 DAG 定义，必须由受信应用的 `EffectNodeRequestResolver` 在 claim 后即时提供。当前默认 `TaskProcessor` 仍只生成 v1 受信 `file_move` saga；v2 没有开放给模型或通用 API，因此不会因并行能力扩大现有写路径权限。

Runner adapter 异常不会被伪装成确定失败，dispatcher 将其收敛为 `unknown`；随后 reducer 将图置为 `blocked_unknown`，仍禁止透明重放。

## 3. Node claim 心跳与 fencing

`tool_effect_nodes` 新增 `claim_heartbeat_at`。续租命令只在以下事实同时成立时延长 expiry：

- graph owner/fence 仍有效；
- node owner/fence 精确匹配；
- node claim 尚未过期；
- 节点仍是可执行在途状态。

续租不增加 fence；只有新 claim/reclaim 才单调增加 fence。节点终态提交会清空 owner/acquired/heartbeat/expiry，但保留历史 fencing token。

自动化使用 1 秒 graph/node TTL 和 1.1 秒 Runner 调用，证明两个并行根节点在原 TTL 跨越后仍能使用同一 fence 合法提交，join 只在两者完成后进入下一轮。

## 4. 图级终态与 skip/cancel reducer

v2 新增 node `skipped/cancelled` 和 graph `cancelled` 终态。图取消是持久化的 `cancel_requested_at` 意图，ready-set 在该意图存在后立即停止发放新 claim。

Reducer 在 graph lease/fence 保护下迭代至不动点：

- 前驱为 failed/unknown/cancelled/skipped 时，未启动后继转为 `skipped`；
- 取消意图下，已 ready 但未启动的节点转为 `cancelled`，其后继转为 `skipped`；
- 任一 unknown 导出 `blocked_unknown`；
- 失败后有不可补偿的成功节点导出 `blocked_non_compensable`；
- 全部可补偿时进入 `compensating`；
- 无在途节点且全部成功、取消或确定失败时，分别归约为 `succeeded/cancelled/failed`。

每个 skip/cancel 节点仍写入 TaskEvent、Outbox 和 append-only effect transition，图级终态变化另写 `effect_graph.reduced`。

## 5. 并行补偿计划

图进入 `compensating` 后，调度器持久化 `tool_effect_compensation_plans`。计划从 forward success edge 的反向依赖生成最大并行波次：先补偿没有已应用后继的 sink，再移除该波并继续。

例如 `left + right -> join`，补偿波次为：

```text
wave 0: join
wave 1: left, right
```

证明包含 graph revision/event-seq、候选节点状态/revision/last-event-seq 和波次，规范 JSON 的 SHA-256 作为 plan ID 的内容地址。本阶段完成计划与审计证明，实际补偿 wave 的 Runner 并行消耗、每 wave barrier 和补偿失败降级仍是下一阶段。

## 6. Delivery、Inbox 与 DLQ

`outbox_messages` 新增：

| 字段 | 语义 |
| --- | --- |
| `delivery_id` | 每次 claim 生成的投递尝试身份 |
| `delivery_attempted_at` | 数据库时间的尝试时刻 |
| `dead_lettered_at` | 达到有界尝试次数后的 DLQ 时刻 |
| `dead_letter_reason` | 有界、脱敏的最后失败摘要 |

Publisher 向支持 envelope 的 broker 传递 `delivery_id/message_id/topic/attempt/attempted_at/payload`；现有 WebSocket broker 解包后仍只广播 `TaskEventRead`，因此前端协议不变。

Inbox 用 logical `message_id` 去重，而不用每次变化的 delivery ID。首次 handler 失败会回滚 Inbox 占用；成功后的任意重投只返回 `duplicate=true`。

`DESKPILOT_OUTBOX_MAX_ATTEMPTS` 默认为 8。达到上限后消息离开 claim 集合，不再阻塞同 task 后继序列；只有显式 `requeue_dead_letter` 才清除 DLQ 事实并从第一次重试。`cleanup_published` 和 Inbox `cleanup` 要求调用者给出明确 retention boundary。

## 7. PostgreSQL 原生 claim

SQLite 仍使用短事务候选查询 + 逐行 CAS，用于当前单机测试。PostgreSQL 方言分支使用：

- Outbox：带同 task 早期消息 `NOT EXISTS` 约束的候选 CTE，`FOR UPDATE OF outbox_messages SKIP LOCKED`，单条 `UPDATE ... RETURNING`；
- DAG node：对已由 ready-set 证明的 node ID 子集执行 `FOR UPDATE OF tool_effect_nodes SKIP LOCKED`，必须全部加锁才执行批量 `UPDATE ... RETURNING node_id/revision/fence`，否则整事务回滚。

自动化已使用 PostgreSQL dialect 编译并断言 `SKIP LOCKED`、`RETURNING`、顺序屏障和 fence 递增。当前开发机没有 PostgreSQL 服务或 Docker，因此尚未宣称完成真实 PostgreSQL 双实例压力、进程杀死和网络分区验收。

## 8. 迁移与验收

Alembic head 为 `0016_dag_dispatch_delivery`，增加 graph cancel intent、node heartbeat、compensation plan、Outbox delivery/DLQ 字段和 Inbox 表。自动化完成 `head -> 0015 -> head` 往返，并使用 `alembic check` 证明 ORM metadata 无新升级操作。

```text
Ruff:  All checks passed
mypy:  Success, 115 source files
pytest: 308 passed
Alembic: 0016_dag_dispatch_delivery (head), round-trip passed, no new operations
frontend vitest: 15 files, 126 passed
frontend type-check/build: passed (workspace Node 24.14.0)
```

新增 8 项后端用例：并行根节点/续租/join、失败后 skip 与补偿波次、cancel reducer、Inbox logical-message 去重/清理、Outbox DLQ/显式重入队、PostgreSQL Outbox claim SQL、PostgreSQL DAG claim SQL，以及迁移往返/metadata 无漂移。

## 9. 已知边界与下一步

1. 把受信 v2 业务计划接入 `TaskProcessor`，将每个并行 Runner 节点完整绑定 Tool ledger、Policy/Approval、attempt/effect 和 receipt；仍不允许模型生成写路径。
2. 按已持久化的 compensation waves 执行真实并行补偿，增加 wave barrier、claim/fence、新审批/新回执和失败/unknown 降级。
3. 增加条件边、显式 branch-decision 证明、在途 Runner cancel IPC，以及全局公平性、每图/每 Tool 并发限制和大图分页。
4. 为 DLQ/requeue/cleanup 增加受保护的运维 API、retention scheduler、指标和审计；对外部 broker 进行真实 ack-before-crash 故障注入。
5. 在可用 PostgreSQL 测试服务上运行双 API/dispatcher 实例压力和 crash/timeout/network 故障矩阵，校验隔离级别、锁竞争、公平性与恢复延迟。
