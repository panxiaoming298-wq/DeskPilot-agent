# 阶段 49：增量 Ready 投影与 v4 分页证明

## 1. 本阶段目标

阶段 46 的 ready v3 已把单页 proof 限制在有界节点数内，但每次生成或校验一页仍会把整个 DAG 的 node、edge 和 branch decision 载入应用进程，再执行 O(V+E) membership 重算。阶段 48 把 Runner 容量提升到集群级以后，这个全图重算成为大图调度的下一处放大点。

阶段 49 将“依赖是否满足”物化为数据库侧增量投影，并保持：

- ready proof 仍绑定当前 graph fence、事件事实、节点 revision、前驱 proof 和 branch-decision proof；
- admission 仍发生在 node claim 之前，claim 事务继续校验 capacity proof；
- node claim TTL 继续使用数据库时间，崩溃后的 ACTIVE node 可在过期后重新进入 ready；
- branch decision 仍是不可改写、内容寻址的事实，未选路径继续由 reducer 收敛为 skipped；
- graph cancel、补偿、prepare/commit/receipt/unknown 和跨 API graph-control 语义不变。

## 2. 数据库投影模型

Alembic `0021_incremental_ready` 新增两张表。

`tool_effect_dag_ready_states` 是每张 DAG 的投影 head，保存：

- 单调 projection revision；
- 已消费的 graph event sequence；
- 由重建快照或后续事件链推进的 `content_digest`；
- 最近完整重建时间和更新时间。

`tool_effect_dag_ready_nodes` 为每个节点保存：

- graph/node/ordinal 身份；
- 尚未成功的直接前驱数 `remaining_predecessors`；
- 尚未产生决定的条件数 `unresolved_branches`；
- 是否已有条件明确拒绝该路径；
- 行 revision、内容摘要和更新时间。

数据库索引按 graph、branch rejected、两个计数和 ordinal 排列。ready 查询只需连接 node 当前状态与 claim expiry，即可筛选：

```text
branch_rejected = false
remaining_predecessors = 0
unresolved_branches = 0
node.status in (pending, active)
claim 未占用或已按数据库时钟过期
```

投影只表示依赖条件，不复制 Tool 参数、授权、receipt 或敏感路径。

## 3. 初始化、增量推进与修复

新 DAG 在 node/edge 定义提交事务内执行一次 O(V+E) 初始化：

- 普通与条件依赖都计入 remaining predecessors；
- 已成功前驱不计入 remaining；
- 无 decision 的条件计入 unresolved；
- 已决定但 outcome 不匹配的条件标记 rejected；
- 每行生成绑定计数、ordinal 和 revision 的摘要；
- state digest 绑定有序行摘要和当前 graph event。

稳态不再重扫完整图：

- node 首次进入 succeeded 时，只对其直接 outgoing success/conditional edge 的目标计数减一；
- succeeded 在补偿阶段离开时，对直接目标计数加一；
- branch decision 只更新同一 source/key 的条件目标：unresolved 减一，未选 outcome 标记 rejected；
- claim、reclaim、cancel、skip、终态与 compensation 事件即使不改变依赖计数，也会推进 projection state hash-chain；
- graph lease 心跳没有新增 graph event，不推进投影，也不会使语义仍有效的 ready proof 无端失效。

每次投影更新和 node/graph 事实处于同一数据库事务。若旧数据库升级后还没有投影，或 state event/行数与 graph 不一致，第一次 ready checkpoint 会执行一次完整重建；之后恢复增量路径。重建不是猜测 Tool 结果，只从持久化 graph/node/edge/decision 真值重新派生计数。

## 4. Ready v4 分页证明

ready v4 不再构造完整 ready-node Python tuple。一次 checkpoint 执行：

1. 校验 live graph owner/fence；
2. 读取或一次性修复 projection state；
3. 在数据库内 COUNT 当前 ready rows；
4. 按 ordinal、cursor 和 page size 只取当页 node；
5. 只读取当页节点的 incoming edge、前驱 node 和相关 branch decision；
6. 复核投影行摘要、所有前驱 succeeded、所有条件 outcome 精确匹配；
7. 生成 v4 membership/page digest 并持久化 checkpoint。

membership digest 绑定 graph ID/fence、graph event、projection revision/digest 和 total ready。page digest 额外绑定数据库时间截面、cursor、page size、next cursor、has-more 以及当页完整 proof。

claim 不做全图重算，而是在同一事务内：

- 读取 checkpoint，并要求 schema v4；
- 精确匹配 graph fence/event 与 projection revision/digest；
- 使用 checkpoint 的数据库时间重新查询同一页，避免 TTL 在验证中途改变分页集合；
- 重算 membership/page digest 并比较当页 proof；
- 要求待 claim 节点确实属于该页；
- 校验 admission proof 后再执行 graph/node CAS claim。

graph revision 不再单独作为 ready proof 的有效性条件。纯 lease 维护不会改变可执行事实；真正影响 ready 的 node、branch、cancel 与 reducer 事件会改变 event sequence/projection digest，旧 proof 仍会失效。graph takeover 则改变 fencing token，旧 owner 的 proof 同样不能使用。

## 5. Branch、取消与恢复边界

条件节点只有在前驱 succeeded 且对应 decision 已产生并匹配 expected outcome 后才进入查询索引。decision proof ID 或 projection row digest 被篡改时，checkpoint/claim fail closed。未选分支即使 remaining 和 unresolved 都归零，也因 `branch_rejected=true` 永不被 claim，随后仍由原 reducer 写入 skipped 事实。

graph cancel event 会推进 projection state，且 ready 查询要求 graph active、无 cancel intent，因此旧页立即失效。pending 节点继续由 reducer 转 cancelled/skipped；已 claim 节点继续走 graph/node fence 和 Runner cancel 三态。

ACTIVE claim 的 expiry 不需要后台改写投影。查询直接使用数据库时间判断是否 reclaimable；checkpoint 记录该时间截面，claim 会重新验证节点 revision、claim fence 与 expiry。节点心跳续租后，即使没有 graph event，旧页内该节点的 proof 也不再匹配。

## 6. 复杂度变化

| 操作 | 阶段 46～48 | 阶段 49 |
| --- | --- | --- |
| 新图初始化 | O(V+E) | O(V+E)，同时建立投影 |
| 普通 node event | 下一页再 O(V+E) 重算 | O(直接后继数) 增量更新 |
| branch decision | 下一页再 O(V+E) 重算 | O(同 source/key 条件边数) |
| ready page | O(V+E) 应用层 materialize | DB COUNT/索引分页 + O(当页前驱数) proof |
| claim 校验 | 再次 O(V+E) | 重查同一页 + 当页 proof/CAS |
| 旧库首次访问 | O(V+E) | 一次 O(V+E) lazy rebuild，随后增量 |

当前 cursor 仍是 offset，而非 keyset；COUNT 和大 offset 仍可能扫描索引范围，但不会把完整 graph/proof payload 搬进应用内存。

## 7. 验收结果

```text
Ruff:  All checks passed
mypy:  Success, 125 source files
pytest: 353 passed
Alembic: 0021_incremental_ready (head), no new operations
frontend vitest: 15 files, 126 passed (workspace Node 24.14.0)
frontend type-check/build: passed
```

新增覆盖包括：

- 512 根宽图建立投影后，禁用全图 loader，ready checkpoint、claim 和下一页仍全部成功；
- 512 个节点只返回 7 个当页 proof，claim 后 projection revision 单调推进且剩余 ready 数正确；
- root succeeded 只改写两个直接后继，非直接 join 的计数/revision 不变；
- branch decision 将选中行投影为 ready、未选行投影为 rejected，并保留 decision proof；
- projection 表缺失时只重建一次，后续分页不再调用全图 loader；
- state event 漂移触发安全重建，projection row digest 篡改 fail closed；
- ready v3 的分页、跨页 claim 拒绝、崩溃后 ACTIVE reclaim、分支篡改、dispatcher backpressure、集群 admission 与补偿回归继续通过；
- `head -> 0020 -> head` 往返、表/列/索引/外键和 metadata drift 检查通过。

## 8. 已知边界与下一步

1. ready 查询仍执行数据库 COUNT 与 offset pagination；超大 ready 集合可继续演进为 keyset cursor 和分段计数。
2. projection state 使用事件 hash-chain 与逐行摘要，不是可向外部不可信验证者提供 inclusion proof 的 Merkle tree；当前信任边界仍是受控数据库和应用事务。
3. lazy rebuild 会在首次访问旧图或发现 state/event/行数漂移时读取完整图；这是有界修复路径，需要增加次数、耗时和失败指标。
4. PostgreSQL 尚未执行真实大图 EXPLAIN、锁竞争、进程杀死与网络分区注入。
5. ready projection、cluster admission、graph-control 和 Outbox/DLQ 都尚无统一的受保护运维查询、retention scheduler、指标与告警。

下一阶段入口：**建设 graph-control、cluster admission、ready projection 与 Outbox/DLQ 的受保护运维面、retention 和指标审计；继续保持内容证明、所有 fence、claim-before-runner 与 prepare/commit/unknown 语义。**
