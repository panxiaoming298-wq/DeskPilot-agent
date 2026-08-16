# 阶段 59：Admission 分片与 PostgreSQL 原生调度

## 1. 本次范围

阶段 48 的集群 admission 已能证明全局、每 graph、每 Tool 容量与 permit fence，但每次 grant 都更新单一 `tool_effect_dag_admission_state(scope_id='global')`。多 API 即使处理不同 graph，仍会竞争同一 CAS 行。

本阶段把 PostgreSQL 稳态 grant 改为持久化分片调度，目标是：

1. 不再以 global state 写 CAS 串行化每次 grant；
2. 同 graph 始终进入同一 shard，保持 graph 配额与队首顺序；
3. 不同 shard 可由不同连接通过 `SKIP LOCKED` 并行接管；
4. 全局和每 Tool 配额在跨 shard 并发下仍不得超发；
5. grant、TTL 接管和旧 owner/fence 拒绝使用数据库时间与数据库隔离证明；
6. 候选查询有界，并由 PostgreSQL 17 JSON plan 防止退化为全 shard 扫描或排序。

SQLite 保留原 singleton CAS 路径，作为单机开发兼容实现；本阶段的并行分片语义仅在 PostgreSQL 路径启用。

## 2. 数据模型与迁移

Alembic `0024_admission_shards` 新增 16 行固定调度域：

```text
tool_effect_dag_admission_shards
  shard_id              0..15 primary key
  revision              单调提升
  last_grant_sequence   最近一次调度 turn 的全局序列
  updated_at             数据库时间
```

admission ticket 新增 `scheduling_shard`，取值为：

```text
SHA-256(graph_id) 前 8 字节的大端整数 mod 16
```

同 graph 的所有 batch 因此固定落在同一 shard。迁移不会把旧 ticket 留在默认 shard 0，而是用相同算法逐条回填；SQLite 往返测试实际插入旧 ticket，验证 `0023 -> 0024 -> 0023 -> 0024` 后 shard 仍正确。

PostgreSQL 另创建 `tool_effect_dag_admission_grant_seq`。sequence 用于跨 shard 单调排序，不承诺无间隙：事务回滚或“本 turn 因 Tool 容量不可授予”都会合法消耗序号。安全证明只依赖单调性，不依赖连续性。

## 3. PostgreSQL 调度协议

每次调度 transaction 的顺序为：

1. 第一条语句设置 `SERIALIZABLE`；
2. 读取数据库 `current_timestamp` 与配置摘要；
3. 从仍有 live pending 的 shard 中，按 `last_grant_sequence NULLS FIRST, shard_id` 选择最久未服务者；
4. 使用 `FOR UPDATE OF ... SKIP LOCKED LIMIT 1` 独占该 shard；
5. 回收全局过期 granted permit，并收敛该 shard 的过期 pending ticket；
6. 读取仍有效的 active permit。结果规模天然不超过受信配置的 `global_limit <= 1024`；
7. 从已锁 shard 读取最多 2048 条稳定候选；
8. 结合 graph 历史 grant sequence、每 graph active 数与每 Tool active 数，选择当前可授予 graph；
9. 每个 shard turn 最多 grant 一张 ticket，同 batch 其他候选转为 withdrawn；
10. 写 admission fence、数据库 lease 时间和 shard revision 后提交。

“每 turn 一张”是公平性边界，而不是遗漏的批量循环：它避免一个热点 shard 在一次事务内吃满全部全局容量。多个 API 可以同时锁定不同 shard；等待者轮询会继续填充空余容量。

global state 仍保存配置摘要，并供 SQLite 兼容路径使用，但 PostgreSQL grant 不再提升其 revision。真实并发测试明确断言多轮 grant 后该 revision 保持配置完成时的值。

## 4. 跨 shard 容量证明

只锁 shard 行本身不能保护全局或每 Tool 容量。PostgreSQL 路径因此依赖 `SERIALIZABLE` 的 SSI predicate conflict：

- 每个 grant transaction 读取 live granted 集合；
- transaction 随后把一张 pending ticket 写成 granted；
- 两个不同 shard 若基于同一旧容量快照同时尝试占用最后名额，会形成读写依赖；
- PostgreSQL 令其中一个以 SQLSTATE `40001` 回滚；生产重试器重新读取最新容量后再决定。

`40P01` deadlock 同样有界重试。sequence 的间隙不影响重新决策。每 graph 的 ticket 又固定在同一 locked shard，因此 graph 内选择不会跨 shard 并发；全局和 Tool 限额则由 SSI 覆盖。

真实 PostgreSQL 17.10 门禁使用四个独立 `Database`/engine、四个不同 shard、全局容量 2、每 graph 容量 1、`tool_a/tool_b` 各容量 1。第一轮恰好 grant 两张且两个 Tool 各一张；释放后第二轮服务其余两个 shard，四张原 ticket 均获得一次服务，没有超发或饥饿。

## 5. TTL 与 fence

permit 的 renew、release 和调度回收都比较数据库时间。release 现在也要求 `expires_at > current_timestamp`，过期 owner 即使持有旧 owner ID 与 fencing token，也不能把已失效 permit 伪装成正常释放。

真库门禁将一个 granted permit 直接置为数据库时间已过期，并为同 graph/node 注册 replacement：

- replacement 在原 shard 被接管并获得更大的 grant sequence；
- 旧 permit release 返回 false；
- 旧 permit renew 抛出 fence rejection；
- 另一个 live permit 仍占用容量，replacement grant 后 active_total 精确为 2。

这证明 TTL 接管不是应用进程时钟或内存状态推断。

## 6. 有界候选与 JSON plan

候选索引为：

```text
(scheduling_shard, status, expires_at, created_at, batch_id, admission_id)
```

查询使用同序 key，并以 `LIMIT 2048` 约束生产候选窗口，无 `OFFSET`。JSON plan 门禁使用更严格的 page+sentinel 101 条窗口，构造 16 个 shard、每 shard 1000 张 pending ticket，共 16000 张：

```text
backend/tests/baselines/postgresql/
  admission-shard-v1-16000-tickets.postgresql-17.json
```

PostgreSQL 17.10 录制结果：

- 返回 101 行；
- scan rows 101；
- rows removed 0；
- 使用 `ix_tool_effect_dag_admissions_shard_route`；
- plan 只有 `Limit + Index Scan`，无 Sort；
- execution time 0.136 ms（本次容器样本）。

测试库反复插入并级联删除 16000 行时，index page 回收会造成 buffer hit 波动，因此该专项把 shared-block slack 设为 128 页；query shape、node shape、索引名、返回行与 scan rows 仍严格比较，不能用 buffer 容差掩盖全 shard 扫描。

## 7. 验证结果

已完成：

- SQLite 空库、重复升级和带旧 ticket 的 `0023 <-> 0024` 往返；
- PostgreSQL 真实 `0023 -> 0024`、head 与 metadata check；
- shard lock/candidate 的 PostgreSQL 方言形状门禁；
- 四 engine 跨 shard 容量、公平、TTL 与 stale fence 门禁；
- 16000-ticket plan record 后 compare；
- 完整真库专项 record 1 次、compare 1 次，并额外连续 compare 5 次；
- 默认后端全量 `391 passed, 9 skipped, 1 warning`（400 collected）；
- Ruff 全仓、mypy 138 个生产源码、`uv lock --check`；
- 开发 SQLite 与测试 PostgreSQL 均为 `0024_admission_shards (head)`，Alembic check 无漂移。

9 个 skip 是默认未注入 URL 的 8 个 PostgreSQL 外部门禁、1 个 RabbitMQ 外部门禁。唯一 warning 仍是既有 Starlette/httpx 弃用提示。本阶段无前端代码变更，沿用最近一次 17 文件/133 项测试、type-check 与 build 结果。

## 8. 已知边界与后续入口

- 生产候选窗口固定为 2048；它给单次事务确定成本上限，但极端情况下同 shard 前缀若长期堆积大量不可授予 batch，后排 graph 的等待时间会增长。后续若实际规模触达该边界，应增加 graph-head queue 投影或 shard 内 keyset cursor，而不是取消 LIMIT。
- PostgreSQL 的全局/Tool 容量安全依赖 `SERIALIZABLE` 与 `40001` 重试；不得把该事务降回 READ COMMITTED。
- shard 数固定为 16 并写入约束与 hash 算法；改变数量需要新迁移和显式 rebalance，不能只改常量。
- 调度仍由等待者轮询唤醒，没有引入 RabbitMQ 作为正确性依赖。

下一阶段推进 graph-control mailbox 的 PostgreSQL 原生批量 claim：以 `FOR UPDATE SKIP LOCKED` 取代跨 owner 的轮询竞争，绑定 target owner/graph fence、claim TTL/fence 与稳定 keyset，并补多 engine 接管和 JSON plan 漂移门禁。
