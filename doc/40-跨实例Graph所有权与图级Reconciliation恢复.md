# 40. 跨实例 Graph 所有权、统一事务命令与图级 Reconciliation 恢复

## 1. 阶段结果

DeskPilot 已为 `deskpilot.tool-effect-graph.v1` 增加数据库持久化 lease、单调 fencing token 和 graph/node revision CAS。每个执行中的图只能由一个 API 实例持有；续租、接管、释放和所有节点 mutation 都核对 owner、未过期 lease 与 fencing token。旧实例即使在暂停后恢复，也不能用旧 token 写入 graph/node projection。

Runner 边界的控制面写入已收敛为统一事务命令：Tool ledger、Tool/Effect event、Outbox、effect attempt、node/graph transition journal 和受保护 TaskCheckpoint 在一个数据库事务内提交。checkpoint 注入失败测试证明 request 与 terminal 路径会完整回滚，不留下半提交的 Tool 或 graph 真值。

Tool 返回 `unknown` 后，任务不再立即伪装成普通失败，而是进入非终态 `waiting_reconciliation`。人工 outcome 持久化后，用户必须再次显式选择 `continue` 或 `terminate`；原 `tool_calls.status=unknown` 永远不改写，也不会重放原 call。

## 2. Lease、CAS 与 fencing

Alembic `0014_graph_lease_recovery` 为 `tool_effect_graphs` 增加：

| 字段 | 语义 |
| --- | --- |
| `lease_owner_id` | 当前 API 实例身份 |
| `lease_acquired_at` | 当前 owner 首次取得 lease 的时间 |
| `lease_heartbeat_at` | 最近一次续租时间 |
| `lease_expires_at` | lease 失效边界，并建立查询索引 |
| `fencing_token` | 每次新 owner 接管时单调递增，释放后也不回退 |

取得新 lease 时使用 graph `revision` CAS；同一 live owner 只能续期，不产生新 fence。接管仅允许 owner 为空或 lease 已过期，并同时递增 `fencing_token` 与 `revision`。释放只清空 owner/timestamp，保留 fencing token。

所有正常 effect mutation 先执行单条 graph CAS，再执行 node revision CAS，条件同时包含：

- `lease_owner_id` 与当前实例一致；
- `fencing_token` 与 runtime checkpoint 一致；
- `lease_expires_at > now`；
- graph/node revision 与本事务读取值一致。

任一 CAS 失败会使整个事务回滚。lease worker 按 TTL 的三分之一续租；续租失败后 runtime 请求停止，旧 owner 不再追加失败 transition。默认 TTL 为 15 秒，可通过 `DESKPILOT_GRAPH_LEASE_TTL_SECONDS` 配置。

## 3. 跨实例启动恢复

启动恢复不再只依赖进程内 runtime 表：

1. 有效 checkpoint 在恢复 runtime 前先取得 graph lease；另一实例仍持有 live lease 时，将 task 记为 contended，本实例不失败、不恢复、不结算其 Tool call。
2. 无效 checkpoint 在 fail-closed 前也先 claim graph；不能取得 lease 时不删除另一实例的 checkpoint。
3. 无 checkpoint 的 pending approval 或 incomplete Tool call 同样先 claim；不能取得 lease时直接跳过。
4. 启动时把遗留 `running` call 收敛为 `unknown` 时，graph/node 修复还要经过当前 fence CAS。
5. 已进入 `waiting_reconciliation` 的任务不会自动恢复或自动终止，必须走显式图恢复协议。

自动化用两个独立 TaskService 实例证明：owner 持有 live lease 时，contender 不能把 owner 的 `requested` call 结算为失败，也不能改变 graph fence 或节点状态。

## 4. 统一 Tool/Effect/Checkpoint 事务命令

TaskProcessor 的 Runner 边界使用三类事务命令：

### 4.1 Request

`request_effect_tool_call` 同事务创建 ToolCall、Tool 幂等占用回执、`tool.requested`、EffectAttempt、`effect.attempt.requested`、node transition 和 protected checkpoint。

### 4.2 Start

`start_tool_call` 在完整 effect binding 模式下，同事务消费持久化 Policy/Approval 授权、把 ToolCall 置为 `running`、追加 `tool.started` 与 `effect.attempt.started`、更新 attempt/node/graph，并写入 dispatch 前 checkpoint。

### 4.3 Finish

`finish_effect_tool_call` 同事务提交 Tool terminal ledger、commit receipt 或 Reconciliation、Tool terminal event、effect terminal event、attempt/effect lineage、node/graph transition，以及下一阶段 checkpoint。`unknown` 还会在同一事务把 task 改为 `waiting_reconciliation`、graph 改为 `blocked_unknown`。

这里的原子性是控制面数据库原子性。外部 Runner 对文件系统的 commit 不可能与 SQLite 组成同一个物理 ACID 事务；该边界仍由 Runner 的 prepare/commit 协议、签名 commit receipt、资源版本和不可改写 Tool ledger 证明。

## 5. 图级 Reconciliation 协议

`tool_reconciliations` 新增：

- `graph_recovery_status = not_applicable | pending | applied`；
- `graph_recovery_action = continue | terminate`；
- `graph_recovery_event_id`，外键绑定实际 TaskEvent；
- `graph_recovered_at`。

迁移会把已有、且 call 已绑定 `blocked_unknown` effect graph 的 Reconciliation 回填为 `pending`。

新增接口：

```text
POST /api/v1/reconciliations/{reconciliation_id}:recover-graph
Idempotency-Key: <16..128 chars>

{"action":"continue"}
{"action":"terminate"}
```

命令先 claim graph lease，再验证 reconciliation、call、attempt、node、graph、task 和 protected checkpoint 的完整绑定；恢复 event、graph/node/attempt、task、checkpoint、recovery 状态和幂等回执同事务提交。

## 6. Outcome 与动作矩阵

| Outcome | `continue` | `terminate` |
| --- | --- | --- |
| `confirmed_succeeded` | 可继续；可补偿写节点还必须存在绑定 call 的 commit receipt | 可终止 |
| `confirmed_no_effect` | 可把当前 attempt 收敛为 failed；若有已应用前置节点则进入逆序补偿，否则图失败终结 | 可终止 |
| `confirmed_failed` | 拒绝；“调用失败”本身不能证明没有副作用 | 可终止 |
| `accepted_unknown` | 拒绝；不允许从未知事实推断执行结果 | 可终止 |

`confirmed_succeeded + continue` 会创建 receipt-bound effect（只读 `compensation_strategy=none` 可无 receipt），把 checkpoint 绑定到 `reconciled_call_id/outcome` 并从 Tool 后处理阶段继续。Processor 允许该 checkpoint 消费原 `unknown` call，但不会生成伪造的 `tool.completed`。

`confirmed_no_effect + continue` 只在裁决明确证明无效果时成立。后续 forward 节点失败会从前一个已应用节点进入 compensation；首节点或 compensation attempt 则失败终结。

`terminate` 把 graph 置为 `failed`、task 置为 `failed`，但保留 unknown node、unknown attempt 与原 Tool ledger，明确表达“调度已终止，外部结果仍未知”。

同一 Reconciliation 只允许一个 graph recovery action；相同 Idempotency-Key 和相同 fingerprint 可跨重启 replay，同键异请求返回冲突。

## 7. 前端控制面

前端新增 `waiting_reconciliation` 状态、历史筛选与状态样式。集中 Reconciliation 中心在 outcome resolved 且 graph recovery pending 时显示二次确认的“按裁决恢复原图”和“终止原图”操作。

只有 `confirmed_succeeded` 与 `confirmed_no_effect` 启用继续按钮；终止按钮对所有 resolved outcome 开放。UI 明确提示 graph 恢复不会改写或重放原 unknown call。

## 8. 验收

```text
Ruff:  All checks passed
mypy:  Success, 108 source files
pytest: 294 passed
Alembic: 0014_graph_lease_recovery (head), no new upgrade operations
frontend vitest: 15 files, 126 passed
frontend type-check/build: passed (workspace Node 24.14.0)
```

新增覆盖包括：lease 竞争/接管/fence 单调递增、旧 owner 写拒绝、启动恢复不跨 live lease、request 与 terminal 统一事务回滚、unknown 进入等待对账、accepted unknown 显式终止、confirmed success 继续图且不改写 Tool ledger、前端恢复命令幂等重试与二次确认。

## 9. 已知边界与下一步

1. 当前 lease 使用同一数据库中的 CAS，但到期时间由 API 进程 UTC 时钟计算；多主机时钟漂移仍需数据库时间或数据库原生原子 claim 消除。
2. SQLite 适合当前单机多实例验证，不宣称提供跨主机网络分区下的共识或线性一致分布式锁。
3. graph mutation 已有 owner/fence；Outbox Publisher、Reconciliation 首次幂等唯一键竞争和其他后台 worker 仍是单实例目标。
4. 统一事务覆盖 Runner 边界的 Tool/effect/checkpoint 真值；外部副作用仍只能通过 receipt 协议证明，不能加入本地数据库事务。
5. 图仍是受信应用生成的有序 DAG 切片，不支持 ready-set、条件分支、并行节点或任意图调度。
6. 下一阶段应引入数据库时间的 lease/claim 与多实例 Outbox fencing，统一并发幂等冲突归一化，并为通用 DAG ready-set/并行节点定义带版本的调度与恢复证明。
