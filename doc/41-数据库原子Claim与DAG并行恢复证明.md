# 41. 数据库原子 Claim、Outbox Fencing 与 DAG 并行恢复证明

## 1. 阶段结果

DeskPilot 已将 graph lease 和后台 claim 的时间真值改为共享数据库时间，并为 Outbox 与 v2 Tool effect DAG 节点增加持久化 owner、expiry 和单调 fencing token。多个 API 实例可以同时竞争待发布消息或 ready node，但只有成功 CAS claim 且仍持有当前 fence 的实例能够提交确认或节点 transition。

阶段 39～40 的 `deskpilot.tool-effect-graph.v1` 线性 saga 协议保持兼容。新增 `deskpilot.tool-effect-graph.v2` 表达受信应用生成的通用无环依赖，可持久化 ready-set 和 join 前驱证明，并可在 worker 崩溃、claim 过期和 API 重启后安全重新 claim 并行节点。

## 2. 数据库权威时间

`database_utc_now` 从当前事务的数据库连接读取 UTC 时间：

- SQLite 通过 `strftime(..., 'now')` 取得毫秒精度的数据库时间；
- 其他 SQLAlchemy 方言使用 `CURRENT_TIMESTAMP`；
- graph lease 的 acquire/renew/fence、Outbox claim/ack/retry 和 DAG node claim 均不再依赖 API 进程的本地时钟判定所有权。

测试会把应用 `utc_now` 故意改到 2000 年，并证明新 graph lease 仍使用当前数据库时间。这消除了同一数据库的多实例因进程时钟漂移产生的提前接管或过期不接管。

## 3. 多实例 Outbox claim 与 fencing

Alembic `0015_database_claims_dag` 为 `outbox_messages` 增加：

| 字段 | 语义 |
| --- | --- |
| `claim_owner_id` | 发布实例 ID |
| `claim_acquired_at` | 本次 claim 的数据库时间 |
| `claim_expires_at` | 可被其他实例接管的边界 |
| `claim_fencing_token` | 每次成功 claim 单调递增，永不回退 |

Publisher 先在短事务中选取可用消息，然后通过 `message_id + published_at + available_at + old fence + expired/unowned claim` 执行条件 UPDATE。只有更新了一行的实例才能跨出事务调用 broker。

broker 返回后的 success/failure 确认再次核对 owner、fence 和 live expiry。如果旧 publisher 在停顿后恢复，而消息已被新 publisher 接管，旧 fence 的 ack 更新不到任何行，记为 `fenced`，不得覆盖新 owner 的结果。

同一 task 内仍按 event sequence 投递：只要存在更早的未发布消息，后续消息就不进入本次 claim。语义仍是 at-least-once；broker 已接收但数据库 ack 前进程崩溃仍可能重发，消费端继续以 `event_id` 或 `task_id + seq` 去重。

## 4. 并发幂等冲突归一化

进程内 `asyncio.Lock` 只能减少单实例竞争，不再被视为幂等正确性边界。现在以数据库唯一回执为最终裁决：

- Tool `key_required` 首次占用发生唯一键/序列化竞争时，回滚后重读回执，对另一 call 稳定返回 `TOOL_IDEMPOTENCY_KEY_ALREADY_USED`；
- Reconciliation 的 resolve、new attempt、compensation 和 graph recovery 在数据库冲突后有界重试，由持久化 fingerprint 决定 replay 或 `IDEMPOTENCY_KEY_REUSED`；
- Provider 管理 commit 对唯一键、数据库忙和 catalog CAS 竞争先重读持久化响应，同 key 成功竞争返回 `replayed=true`，不同请求仍保留语义冲突。

自动化使用两个独立 TaskService 实例同时提交 Tool 幂等键和 Reconciliation 新 attempt，证明仅一个首次写成功，另一个收敛为可重试的 replay 或稳定领域冲突，不暴露 SQLAlchemy/SQLite 异常。

## 5. v2 DAG 定义与 ready-set

`EffectDagNodeDefinition` 在旧节点合约上增加不可变 `depends_on` 节点键。创建 v2 图前必须证明：

1. 节点数为 1～20，`node_key` 和 `step_id` 唯一；
2. 所有依赖存在、不重复、不自指；
3. Kahn 拓扑遍历能消费全部节点，否则在写库前拒绝环；
4. 每个依赖同时形成 forward `success` edge 和反向 `compensation_order` edge。

ready-set 仅包含：

- `pending` 且所有 success 前驱均为 `succeeded` 的节点；
- 或已为 `active`、但无 claim/无 expiry/claim 已过期的可恢复节点；
- 仍有 live claim、前驱未全部成功或已终态的节点不在 ready-set。

## 6. 内容寻址 ready-set 证明

`tool_effect_ready_set_checkpoints` 持久化：

- `graph_id + graph_revision + event_seq`；
- ready node 的 ID、key、status、revision、last event seq；
- 该节点上一个 claim fence/expiry；
- 每个 join 前驱的 ID、key、`succeeded` 状态、revision 和 last event seq；
- 上述规范 JSON 的 SHA-256 `proof_digest`，以及生成证明时的数据库时间。

claim 命令必须同时证明：当前 graph lease/fence 有效，checkpoint 的 graph revision/event seq 未变，重算 ready-set 的 digest 一致，所有请求节点都在证明集合中。然后一次 graph revision CAS 与多次 node revision/old-fence CAS 在同一事务中把整个子集改为 `active`，为每个节点发放新 fence，并写入 `effect.node.claimed` 或 `effect.node.reclaimed` transition/event/Outbox。任一 CAS 失败都回滚全部子集。

节点结果通过 `transition_claimed_effect_node` 提交，同时核对 graph fence 和 node fence。并行根节点可独立终结；只有全部 join 前驱均已成功，新 checkpoint 才会将 join 节点纳入 ready-set。

## 7. 跨重启并行恢复证明

自动化覆盖一个 `left + right -> join` DAG：

1. 首个实例证明 `left/right` 同时 ready，并以 fence 1 批量 claim；
2. 模拟 worker 崩溃并使两个 claim 过期；
3. 新 TaskService 实例从数据库重算 ready-set，证明两个 `active` 节点可恢复；
4. 新 worker 取得 fence 2；旧 worker 用 fence 1 提交被 `EFFECT_NODE_FENCE_REJECTED` 拒绝；
5. 两个新 claim 分别提交 `succeeded`；
6. 重算 checkpoint 只返回 `join`，并包含两个 succeeded 前驱的版本证明。

因此恢复不是根据内存 future 或单个 `current_node_id` 猜测，而是重读持久化 graph/node/edge/transition，并由新的数据库 claim fence 重新建立唯一写权。

## 8. 验收

```text
Ruff:  All checks passed
mypy:  Success, 110 source files
pytest: 300 passed
Alembic: 0015_database_claims_dag (head), no new upgrade operations
frontend vitest: 15 files, 126 passed
frontend type-check/build: passed (workspace Node 24.14.0)
```

新增覆盖包括：数据库时间不受应用时钟污染、双 Outbox publisher 唯一 claim、过期接管与旧 ack fence 拒绝、跨 TaskService Tool 幂等键竞争归一化、跨实例 Reconciliation 唯一新 attempt、v2 DAG 环拒绝、双根并行 claim、崩溃 reclaim、旧 node fence 拒绝和 join 前驱证明。

## 9. 已知边界与下一步

1. SQLite 适用于当前单机共享库多实例证明；不宣称在网络分区下提供分布式共识。生产多主需用 PostgreSQL 等数据库的 row lock/`SKIP LOCKED`/`RETURNING` 语义做方言级验收。
2. Outbox fencing 防止旧 owner 改写数据库，但 broker publish 与数据库 ack 仍不是同一 ACID 事务；后续需要 delivery ID、消费端 inbox、死信和清理策略。
3. v2 当前只支持无条件 success edge 和并行 join；条件分支、skip/cancel 传播、图级终态 reducer 与并行补偿计划尚未实现。
4. TaskProcessor 的当前业务路径仍使用 v1 线性 saga；v2 已建立可测试的持久化调度原语，但还未把任意并行 Tool 开放给模型或用户。
5. node claim 现有 TTL 与接管 fence，还需要独立续租/心跳、调度公平性、并发上限和大图分页。
6. 下一阶段应接通 v2 DAG dispatcher 与并行 Runner，增加 node claim 续租、图级终态/skip/cancel reducer 和并行补偿计划；同时为 Outbox 增加 delivery/inbox/DLQ 证明，并在 PostgreSQL 上验证原生批量 claim。
