# 阶段 50：受保护运行时运维面与 Retention 审计

## 1. 本阶段目标

阶段 47～49 已分别建立 graph-control mailbox、集群 admission 和增量 ready 投影，阶段 42 的 Outbox/Inbox/DLQ 也已有可靠投递原语，但这些状态只能通过数据库或测试代码观察。阶段 50 为四个域增加统一、受本地会话保护且默认脱敏的运维闭环，同时保持：

- graph owner、control delivery、admission、node claim 与 Outbox 的所有 fence 不被运维查询绕过；
- admission 继续严格发生在 node claim 之前，运维面不能直接 grant ticket 或创建 ACTIVE node；
- ready v4 proof、branch decision proof 和 graph event/projection hash-chain 仍是调度真值；
- DLQ requeue 只重新开放同一 durable message，不修改 TaskEvent 或生成替代 payload；
- prepare/commit/receipt/unknown 和“running Tool 不自动重放”语义不变。

## 2. 统一受保护查询面

新增以下 API，全部位于既有本地 Bearer session、Origin 与 Fetch Metadata 边界之后，所有成功响应均为 `Cache-Control: no-store`：

```text
GET  /api/v1/operations/effect-runtime?sample_limit=
GET  /api/v1/operations/effect-runtime/audit?after_sequence=&limit=
POST /api/v1/operations/effect-runtime:sample
POST /api/v1/operations/effect-runtime:run-retention
POST /api/v1/operations/outbox/{message_id}:requeue
```

统一 snapshot 同时给出 graph-control、cluster admission、ready projection 和 Outbox/Inbox 的数据库时间截面、聚合计数、告警以及有界样本。样本只包含身份、状态、revision、fence、时间和内容摘要：

- graph-control 不返回用户 reason；
- Outbox 不返回 payload、`last_error` 或 DLQ reason，只返回 payload/error digest；
- ready projection 不复制 Tool 参数、授权、receipt 或路径；
- 所有列表受 `sample_limit <= 200` 约束。

snapshot 自身生成规范 JSON `snapshot_digest`。查询不 claim、不 ack、不续租、不修复投影，也不触发 Runner、Provider 或外部网络。

## 3. 指标与告警

运维 snapshot 包括：

- control 各状态、可投递数、未路由数、过期 delivery claim 和最老可操作时间；
- admission 各状态、live pending/granted、过期 lease、scheduler revision、next grant sequence 与容量配置摘要；
- ready graph/node/实际 ready 数、live graph 缺失投影、event 漂移、行数漂移、累计成功 rebuild 次数和最近 rebuild 耗时；
- Outbox ready/scheduled/in-flight/published/DLQ、Inbox receipt 数和最老 pending/DLQ 时间。

当前内建告警为 control claim 过期或停滞、admission lease 过期、ready projection 需要修复、DLQ 非空和 Outbox 投递停滞。阈值由 `DESKPILOT_OPERATIONS_STALLED_AFTER_SECONDS` 控制。告警只是安全投影，不自动接管 graph、不释放 permit、不 requeue DLQ。

后台 scheduler 按配置周期持久化指标快照，并按独立周期运行 retention。多实例在 PostgreSQL 上通过 audit state 行锁串行追加审计；SQLite 开发模式由单进程 mutation lock 串行化。

## 4. 内容链运维审计

Alembic `0022_effect_runtime_ops` 新增：

- `effect_runtime_operations_state`：保存单调 sequence、revision、最后事件摘要与最近 retention 时间；
- `effect_runtime_operations_audit`：保存 metrics sample、retention 和 DLQ requeue 的 request/result digest、脱敏 details、前序摘要和事件摘要。

每条事件摘要绑定 sequence、action、actor、request/result digest、前序摘要、脱敏 details 和数据库时间。读取分页会验证：

1. 页首与 `after_sequence` 精确连续；
2. 每条事件内容摘要可重算；
3. 页内 previous digest 连续；
4. 读到链尾时与 state head 精确一致。

内容、序号、前序摘要或 head 被篡改时返回 `409 EFFECT_RUNTIME_OPERATIONS_AUDIT_REJECTED`。运维写请求的 `Idempotency-Key` 只存摘要；相同 key/相同请求返回原审计结果，不同请求稳定冲突。

## 5. Retention 安全矩阵

每次 retention 受 batch size 限制，并在删除前对本批记录的身份、revision/fence 与既有 proof digest 生成内容清单摘要。删除与 append-only 审计事件在同一数据库事务提交；审计提交失败则本批删除回滚。

| 域 | 自动清理条件 | 明确保留 |
| --- | --- | --- |
| graph-control | graph 已进入安全终态，control 为 applied/superseded 且超过 cutoff | active/compensating/blocked graph 的控制消息 |
| admission | graph 已进入安全终态，ticket 为 released/cancelled/withdrawn/expired 且超过 cutoff | pending/granted permit 和非终态图历史 |
| ready | 安全终态图的旧 checkpoint、派生 node/state；state 只在 node 已清空后删除 | active/compensating/blocked graph 投影和 proof |
| Outbox | 已 published 且超过 cutoff | 所有 pending/in-flight/DLQ 消息与 TaskEvent 真值 |
| Inbox | 超过 cutoff 的消费幂等 receipt | cutoff 内 receipt |

安全终态限定为 succeeded、compensated、failed、cancelled。所有 `blocked_unknown`/补偿阻断图均不清理，避免破坏后续人工 reconciliation。DLQ 永不由自动 retention 删除。

## 6. DLQ 显式重新入队

DLQ requeue 必须携带本地可信写来源和 16～128 位 `Idempotency-Key`。同一事务内它：

1. 锁定精确 message，要求未 published 且当前确实在 DLQ；
2. 归零 attempt，清除错误原文、delivery claim 和 dead-letter 状态；
3. 使用数据库时间重新设置 `available_at`；
4. 单调提升 `claim_fencing_token`，使旧 publisher 不能提交迟到 ack/fail；
5. 追加绑定旧 DLQ 内容摘要的新运维审计事件。

事务提交后只唤醒正常 OutboxPublisher；仍由原有 per-task 顺序、claim TTL、delivery ID 和 fence 执行投递。运维 API 不直接调用 broker。

## 7. 验收结果

```text
Ruff:  All checks passed
mypy:  Success, 129 source files
pytest: 358 passed
Alembic: 0022_effect_runtime_ops (head), no new operations
frontend vitest: 15 files, 126 passed
frontend type-check/build: passed
```

新增覆盖包括：

- 未认证读取返回 401，缺可信写来源返回 403，snapshot 不泄露 task goal、Outbox payload 或错误原文；
- metrics snapshot digest、审计 sequence/previous digest 连续，篡改 details 后读取 fail closed；
- ready rebuild 次数/耗时可见，event 漂移产生 repair-required 告警；
- DLQ requeue 幂等、清除敏感错误、提升 fence、唤醒 publisher；相同 key 不同消息冲突；
- retention 清理终态 graph-control/admission/ready 和已发布 Outbox/旧 Inbox，同时保留 DLQ、TaskEvent 与所有非终态/unknown 边界；
- `head -> 0021 -> head` 往返、两张运维表、ready rebuild 列和 metadata drift 检查通过；
- graph-control、cluster admission、ready v4、Outbox fencing、Runner 与 prepare/commit/unknown 全量回归继续通过。

## 8. 已知边界与下一步

1. ready 查询仍使用 COUNT + offset；下一阶段可升级 keyset cursor，并在真实 PostgreSQL 上执行 EXPLAIN/锁竞争验证。
2. 当前 rebuild 指标持久化成功次数与最近耗时；失败以缺失/event/行数漂移和 repair-required 告警体现，尚无直方图或外部 Prometheus 导出。
3. 运维面提供 API 与审计，尚无前端运维页、告警通知通道或审计导出签名。
4. retention 是有界多批收敛；大库需结合真实 PostgreSQL vacuum、索引膨胀和长事务测试调优批大小。
5. 尚未执行双实例进程杀死、数据库网络分区、broker 响应丢失和 DLQ 人工处置演练。

下一阶段入口：**在真实 PostgreSQL/外部 broker 环境执行大图 EXPLAIN、双实例锁竞争、进程杀死与网络分区注入，并将 ready offset page 演进为 keyset；继续保持内容证明、所有 fence、claim-before-runner 与 prepare/commit/unknown 语义。**
