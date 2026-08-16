# 阶段 60：Graph-control PostgreSQL 原生批量 Claim

## 1. 本次范围

阶段 47 已建立持久化 graph-control mailbox，但 owner 领取仍是“先查候选、逐条读 graph、再逐条 CAS”。这在 SQLite 单机路径足够清晰，在多个 PostgreSQL API engine 同时轮询时却会增加往返、重复读取和 CAS 竞争，也没有可版本化的查询计划门禁。

本阶段完成以下收敛：

1. PostgreSQL 使用一条 `UPDATE ... RETURNING` 批量领取 owner 定向 control；
2. 候选以 `FOR UPDATE OF controls SKIP LOCKED` 隔离，多 engine 不等待也不重复领取；
3. claim 同时证明当前 graph owner、graph lease TTL 和 target graph fencing token；
4. ack、renew、retry 都要求 claim 尚未过数据库 TTL；
5. 过期 control 可重新路由给新 graph owner，旧 graph/node/control fence 全部 fail closed；
6. 16000-control PostgreSQL 17 JSON plan 固化索引、扫描行数和禁用退化节点；
7. SQLite 保留原逐条 CAS 兼容路径。

RabbitMQ 没有参与这条控制路径。graph-control 是需要和 graph lease/fence 同源判断的数据库控制邮箱；RabbitMQ 仍只是可选事件 transport，不是取消命令正确性的前提。

## 2. 单语句 Claim 协议

PostgreSQL claim 由两个 CTE 和一次更新组成：

```text
locked_graph_controls
  按 status + target_owner + available_at + created_at + control_id
  读取 owner 的有序前缀
  LIMIT batch_size
  FOR UPDATE OF controls SKIP LOCKED

claimable_graph_controls
  对已锁前缀逐条以 graph 主键验证：
  lease_owner_id = owner
  lease_expires_at > database current_timestamp
  fencing_token = control.target_fencing_token
  fencing_token >= 1

UPDATE controls
  pending -> processing
  revision + 1
  attempt_count + 1
  claim_fencing_token + 1
  写 claim owner/acquired/expires
  RETURNING 完整 control
```

先锁定有界有序前缀、再校验 graph proof 是刻意保留的语义：它与原实现的 `LIMIT` 后逐条验证一致，单次事务最多触碰 `batch_size` 个 mailbox 候选。若前缀中存在尚未 reroute 的陈旧 target，本批允许少返回，但不能越过 fence 误领；下一轮 `route_pending` 会按当前 graph lease 重定向。

返回行在应用层按 `(available_at, created_at, control_id)` 排序，避免 `UPDATE RETURNING` 的无序结果泄漏为调度语义。

## 3. Route 与 Claim 竞态

PostgreSQL 的 `route_pending` 现在同样对 control 行使用 `FOR UPDATE SKIP LOCKED`。因此：

- claim 已锁住的 control 不会被旧 route pass 覆写为 pending；
- route 已锁住并准备更新 target owner/fence 的 control 不会被并发 claimant 领取；
- 不同 control 仍可由不同 engine 并行处理；
- SQLite 不使用该语法，继续依赖单机事务和 revision CAS。

route 只处理非 applied，且 processing claim 必须已过数据库 TTL。重新路由会清除旧 claim owner/acquired/expiry，保留单调递增的 `claim_fencing_token`，所以旧 delivery 身份不会复活。

## 4. TTL 与 Fence

本阶段补齐了一个原有缺口：`mark_applied` 和 `retry` 过去只比较 owner 与 claim fence，没有要求 claim 仍在 TTL 内。现在 renew、ack、retry 三条写路径都同时要求：

```text
status = processing
claim_owner_id = delivery owner
claim_fencing_token = delivery fence
claim_expires_at > database current_timestamp
```

真实 PostgreSQL 门禁使用四个独立 `Database`/engine 和 12 个真实 task/graph/control：

- 两个 engine 并发领取 `limit=8`，合计恰好 12 个且 control ID 无重复；
- 首次 claim fence 全为 1；
- 强制让一个 claim 按数据库时间过期后，旧 ack、renew、retry 全拒绝；
- 同 owner 重新领取同一 control，claim fence 从 1 增为 2；
- 再次过期并释放 graph owner A，由 owner B 获得新 graph lease；
- route 后 owner A 无法领取，owner B 以新 graph fence 和 claim fence 3 接管；
- 旧 owner/旧 control claim 的 ack 被拒绝，新 claim 可以 applied，记录的新 graph fence 与当前 lease 一致。

实际取消 handler 仍会通过 `TaskService` 的 graph/node owner/fence 写门禁执行在途取消。因此 mailbox claim fence 防止旧 delivery 改写 control，graph/node fence 防止旧执行者改写 DAG；两层职责不混用。

## 5. 索引与迁移

Alembic `0025_graph_control_claims` 将既有 route 索引从：

```text
(status, target_owner_id, available_at)
```

扩展为：

```text
(status, target_owner_id, available_at, created_at, control_id)
```

这样 owner 候选的过滤与稳定顺序由同一索引覆盖。没有增加 graph owner/lease 复合索引：候选已被 `LIMIT` 限定，graph proof 使用 graph 主键逐条验证；额外 owner 索引在真实计划中没有收益，反而增加 lease 更新成本。

迁移测试精确断言 head 的五列顺序、downgrade `0024` 后的三列顺序，再升级并执行 metadata check。迁移还防御性删除阶段开发期间未发布的试验索引，避免本地早期 `0025` 形状残留。

## 6. 16000-Control JSON Plan

版本化基线：

```text
backend/tests/baselines/postgresql/
  graph-control-claim-v1-16000-controls.postgresql-17.json
```

工作负载为 16 个 owner、每 owner 1000 个 live control，共 16000 个；page+sentinel batch 为 101。PostgreSQL 17.10 录制摘要：

- root rows 101；
- scan rows 303：control route index 101、graph PK 101、control PK 101；
- rows removed 0；
- 使用 `ix_tool_effect_graph_controls_route`、`tool_effect_graphs_pkey`、`tool_effect_graph_controls_pkey`；
- 无 `Sort`、`Bitmap Heap Scan`、`Seq Scan`；
- shared hit/read 为 2451/0；
- execution time 2.806 ms（本次容器样本）。

测试严格比较 workload、参数化 query shape、PostgreSQL major、plan/node/index 结构和行数；shared block 只允许 128 页环境波动。扫描从 101 候选扩展为三次各 101 的主键验证是当前设计的明确成本上限，不允许退化为 owner 全量或全表扫描。

## 7. 验证结果

已完成：

- SQLite 空库迁移、`0025 -> 0024 -> 0025` 索引往返与 metadata check；
- PostgreSQL `deskpilot_test` 同样完成 head、downgrade/upgrade 和 Alembic check；
- PostgreSQL SQL shape 门禁；
- 四 engine 唯一领取、数据库 TTL、同 owner 重领、跨 graph owner 接管、旧 ack/renew/retry 拒绝；
- 16000-control PostgreSQL 17.10 plan compare；
- 默认后端全量 `393 passed, 11 skipped, 1 warning`（404 collected）；
- Ruff 全仓规则检查、本阶段 8 个文件 format check、mypy 139 个生产源码、`uv lock --check`；
- 开发 PostgreSQL 与测试 PostgreSQL 均为 `0025_graph_control_claims (head)`，测试库 Alembic check 无漂移。

11 个 skip 是默认未注入 URL 的 PostgreSQL/RabbitMQ 显式外部门禁。唯一 warning 仍是既有 FastAPI/Starlette TestClient 的 httpx2 迁移提示。本阶段无前端代码变更，沿用最近一次 17 文件/133 项测试、type-check 和 build 结果。

## 8. 已知边界与后续入口

- claim 选择的是有界有序前缀，不保证跳过无限陈旧 target 后填满 batch；长期陈旧项应由 route 收敛，而不是让 claim 做无界扫描。
- PostgreSQL 的单语句验证使用语句级数据库快照；handler 最终写 graph/node 时仍必须重新执行当前 fence 门禁，不能把 mailbox claim 当成副作用授权。
- route 仍是轮询唤醒。RabbitMQ 可优化通知延迟，但不得替代数据库 mailbox、TTL 或 fence。
- plan 基线绑定 PostgreSQL 17；升级 major 时应新增版本化基线，不应覆盖旧文件。

下一阶段推进受保护运行时告警通知与 audit 完整 keyset 游标/导出：保持数据库审计真值、脱敏字段和 hash-chain 证明，通知投递仍可选，不能反向成为运维操作的正确性依赖。
