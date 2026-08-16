# 阶段 58：Ready membership count 投影与漂移门禁

## 1. 本次范围

阶段 57 后的 ready v5 页查询虽然已经使用 ordinal keyset，但每次 checkpoint 仍执行两类全局计数：一类计算当前 ready membership，另一类在 `_ensure_effect_dag_ready_projection()` 中比较 graph node 与 projection row 总数。宽图稳态成本因此仍随 graph size 增长。

本阶段将“当前可领取”本身投影化，目标是：

1. 稳态 checkpoint 不执行全局 `COUNT`；
2. claim、node transition、依赖释放和 branch decision 在同一事务内维护精确计数；
3. claim TTL 仍以数据库时间为准，过期后可重新进入 ready membership；
4. count/revision/content proof 或可见 row 漂移时 fail closed；
5. PostgreSQL 17 保留版本化 JSON plan 与双 engine 竞争门禁。

## 2. 数据模型与升级 fence

Alembic `0023_ready_membership` 为 ready state 增加：

- `membership_version`：升级哨兵；旧投影为 0，首次访问强制全量重建为 1；
- `projected_node_count`：该 graph 的投影行数；
- `ready_node_count`：当前 materialized membership 数量。

ready node 增加 `membership_ready`，并新增 `(graph_id, membership_ready, ordinal)` keyset 索引；effect node 增加 `(graph_id, claim_expires_at)` 索引，用于数据库 TTL 接管。数据库约束保证两个计数非负且 `ready <= projected`。

迁移会为已有数据做一次兼容回填，但不伪造新的内容摘要。旧 state 保持 `membership_version=0`，生产路径必须重建并生成 v2 node/rebuild digest；旧 v5 checkpoint 在 v6 reader 和 claim gate 中直接失效，不能跨版本继续领取。

## 3. 事务内增量维护

membership 定义与原 ready 查询一致：

```text
branch 未拒绝
AND remaining_predecessors = 0
AND unresolved_branches = 0
AND node status IN (pending, active)
AND (无 claim owner OR 无 expiry OR expiry <= database_time)
```

状态变化在原 graph event 事务中更新：

- claim：当前 node `true -> false`，计数减一；
- succeeded：当前 node保持 false，直接 successor 依赖计数减少，满足条件者 `false -> true`；
- failed/cancelled/skipped 等终态：当前 node 保持或转为 false；
- branch decision：选中目标解除 unresolved，未选目标设 rejected，并按目标 node 当前状态计算计数差；
- rebuild：一次计算所有 row membership 和两个总数。

每次 state chain 摘要绑定 previous digest、projection revision、graph event、`projected_node_count`、`ready_node_count`、mutation 和 changed row digests。node digest 同时绑定依赖计数、branch 状态、membership 与 row revision。

## 4. 数据库 TTL 接管

持久化布尔值不会自行随时间变化。新 checkpoint 因此先锁定 ready state，并按数据库时间查询“依赖已满足、membership=false、claim 已过期”的 node。命中行在同一事务内：

1. 校验旧 row proof；
2. 将 membership 改为 true 并提升 row revision；
3. 增加 `ready_node_count`；
4. 生成 `effect-ready-projection-expiry.v1` 链摘要。

cursor 页沿用前一页绑定的 database time；若 cursor 校验后仍发生 expiry mutation，旧 cursor 立即拒绝，避免跨 projection revision 拼页。两个实例同时接管时，PostgreSQL state row lock 使计数只增加一次。

## 5. 稳态查询与漂移门禁

v6 页查询只读取：

```text
graph_id = ? AND membership_ready IS TRUE AND ordinal > ?
ORDER BY ordinal
LIMIT page_size + 1
```

`total_ready` 直接来自 state，不再执行 ready `COUNT`；`_ensure` 也不再扫描 projection/node 两张表。自动化 SQL capture 明确断言完整稳态 checkpoint 没有 `COUNT(`。

fail-closed 证据包括：

- state 版本或计数边界非法立即拒绝；
- 首页面返回行数与持久化 total/sentinel 不一致立即拒绝；
- 每个返回 row 的 membership-bound digest 不一致立即拒绝；
- 返回 node 的真实状态、claim/database time、前驱与 branch proof 不再满足 membership 时立即拒绝；
- 运维快照显式扫描 graph node、projection row、materialized membership 与数据库时间 membership，任一计数漂移产生 `row_count_drift_graphs` 和 repair-required alert。

稳态不做全图校验是刻意的成本边界；未落在当前有界页内的任意数据库外部篡改由显式 operations 漂移审计发现，而不是重新把每次 scheduler page 退化为 O(graph size)。

## 6. PostgreSQL 17 实库结果

新基线：

```text
backend/tests/baselines/postgresql/
  ready-v6-membership-1000-nodes.postgresql-17.json
```

PostgreSQL 17.10 记录结果：

- graph nodes：1000；
- after ordinal：898；
- page + sentinel：101 行；
- scan rows：202；
- rows removed：0；
- shared blocks：310；
- execution time：0.314 ms（本次容器样本）；
- 使用 ready `(graph_id, ordinal)` unique index 与 node primary key。

同一真库门禁随后由两个独立 engine 并发领取同一 proof/node，恰好一个成功；提交后 state 为 `projected_node_count=1000, ready_node_count=999`，目标 row `membership_ready=false`。record 后立即以 compare 模式复验通过。

真库升级还实际发现 PostgreSQL 不接受 `boolean = integer`。迁移 SQL 已统一使用 `IS FALSE / IS TRUE` 和 boolean literal，并重新通过 SQLite 往返与 PostgreSQL head 升级。

## 7. 验证结果

聚焦门禁覆盖：

- SQLite head/重复升级与 `head -> 0022 -> head` 往返；
- 512 根宽图禁用 full graph loader 后分页与 claim 计数；
- claim expiry 的数据库时间接管；
- state count 和 membership row 两类漂移拒绝；
- 稳态 SQL 零 `COUNT(`；
- successor/branch 增量维护；
- operations count drift alert；
- PostgreSQL 1000-node v6 plan、双 engine 单获胜与 count=999。

默认未注入外部 URL 的后端全量为 `389 passed, 8 skipped, 1 warning`（397 collected）；8 个 skip 是显式选入的 7 个 PostgreSQL 外部门禁和 1 个 RabbitMQ 外部门禁。Ruff 全仓、mypy 136 个生产源码、`uv lock --check`、开发库 `upgrade/current/check` 全通过；唯一 warning 仍是既有 Starlette/httpx 弃用提示。Alembic head 为 `0023_ready_membership`。

## 8. 后续入口

下一阶段优先推进 cluster admission 分片与 PostgreSQL 原生批量领取：把单一 global admission state 的写热点拆分为可证明的 shard，补跨 shard 公平性、容量 fence、故障接管和 JSON plan 门禁。graph-control mailbox 的 PostgreSQL `SKIP LOCKED` 批量 claim 可在同一阶段或紧随其后完成。
