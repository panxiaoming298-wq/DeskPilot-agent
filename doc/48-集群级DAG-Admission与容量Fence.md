# 阶段 48：集群级 DAG Admission 与容量 Fence

## 1. 本阶段目标

阶段 46 的公平 admission 只在单个 API 进程内计数。部署两个 API 后，每个进程都可能各自发满全局、每图和每 Tool 配额；阶段 47 虽已把 graph cancel 路由到 live owner，却不能阻止多实例在进入 Runner 前超卖容量。

阶段 48 将 admission 真值迁入数据库，并保持下列既有边界不变：

- admission 必须发生在 node claim 和 Runner IPC 之前；
- 获得容量不等于获得节点所有权，node claim 仍受 graph/node fence 保护；
- forward 与 compensation 共用同一组集群配额；
- graph cancel 能撤销数据库中的 pending waiter，不能留下伪 node claim；
- permit 丢失会取消受保护的 Runner await，但结果仍由 prepare/commit/receipt/unknown 协议归并；
- 多 API 使用不同容量配置时 fail closed，不能由最后一次写入者偷偷改变集群上限。

## 2. 持久化 admission 模型

Alembic `0020_cluster_dag_admission` 新增两个表。

`tool_effect_dag_admission_state` 是当前唯一的全局调度域，保存：

- CAS `revision` 和单调 `next_grant_sequence`；
- 全局、每图、默认每 Tool 配额；
- 完整配置摘要与 Tool 覆盖配置摘要；
- 最近更新时间。

`tool_effect_dag_admissions` 为每个候选节点保存一张 ticket，绑定：

- `admission_id`、批次、graph、node、Tool 和请求 owner；
- `pending/granted/released/cancelled/withdrawn/expired` 状态；
- 数据库时间 lease、心跳、过期与释放时间；
- 行 revision、permit fencing token 和全局 grant sequence。

`(batch_id, node_id)` 唯一约束防止同一批次重复登记。pending 与 granted 是唯一占用调度状态的记录；过期记录在任一调度事务中按数据库时钟收敛，不依赖原 API 进程继续存活。

## 3. 严格容量与跨实例公平

所有 API 都可以推动调度，但一次调度必须先 CAS 更新全局 state revision。只有取得该 revision 的事务可以读取当前 live grants、计算余量并提交新 grants；失败者回滚、抖动退避并重试。因此 SQLite 与 PostgreSQL 通用路径都不会由两个 API 同时基于同一旧容量快照发证。

每轮调度遵循：

1. 清理已过期的 pending/granted ticket；
2. 统计集群当前全局、每 graph 和每 Tool 的 live grants；
3. 每个 graph 只考虑最早的 pending batch；
4. graph 按其最近一次 grant sequence 排序，没有历史 grant 的 graph 优先；
5. 一轮最多从每个 graph 批次选择一个可容纳节点；
6. 若某节点受 Tool 配额阻塞，其他 Tool 或 graph 仍可继续使用剩余容量；
7. 一个批次取得一张 permit 后，同批未选 ticket 转为 withdrawn，下一轮 dispatcher 会用新的 ready proof 重新竞争。

这提供 work-conserving 的 graph round-robin，并把单进程时期的全局/每图/每 Tool 上限提升为集群共同上限。grant sequence 只增不减，过期重领不会复用旧顺序或旧 fence。

## 4. Admission proof 与 node claim 原子绑定

数据库 grant 产生 `EffectDagAdmissionProof(admission_id, owner_id, fencing_token)`。dispatcher 和补偿执行器在 node claim 前取得 permit，再把 proof 传入 `TaskService`。

node claim 事务先以精确的 admission ID、graph、node、owner、granted 状态、未过期时间和 fencing token 执行 CAS 心跳，然后才写 graph/node claim。任一字段不匹配都会以 `EFFECT_DAG_ADMISSION_PROOF_REJECTED` 回滚整个事务。因此：

- 过期或被回收的 permit 不能生成 node claim；
- API A 的 permit 不能被 API B 的 node owner 借用；
- graph/node claim fence 与 capacity fence 各自负责不同事实，且在进入 Runner 前同时成立；
- 进程内测试 admission 没有数据库 proof，仍可作为显式本地实现使用，不改变既有单元测试边界。

## 5. Permit 心跳、丢失与取消

granted permit 按 TTL 持续用精确 owner/fence 续租。`permit.run(work)` 同时等待业务执行与 permit-loss 信号：续租被拒绝、记录过期或 fence 变化时，受保护 work 会被取消并以 `EffectDagAdmissionPermitLostError` 收敛。

这层取消不会自行宣称 Tool 没有副作用。若 Runner 尚未 commit，可安全归并为 cancelled；若已经进入 committing 且没有 receipt，仍进入 unknown；若 receipt 能证明成功，仍保留 succeeded/effect。换言之，capacity lease 只决定“这个 API 是否还能继续占用执行槽”，不能覆盖 Runner 提交事实。

graph cancel 会同时唤醒本进程 waiter 并把该 graph 的数据库 pending ticket 改为 cancelled。由于 admission 在 node claim 之前，纯 backpressure 等待中的节点保持 pending、无 claim、无 Runner call。已 granted/claimed 的节点继续走阶段 45/47 的 intent-before-IPC、graph/node fence、Runner generation 和跨实例 graph-control 路由。

应用停机先停止 TaskProcessor，再关闭 admission controller；controller 会撤回 pending 批次并释放本 owner 持有的 permit。若进程硬退出，数据库 TTL 提供最终回收。

## 6. 配置一致性

容量配置按规范 JSON 计算 SHA-256 摘要，覆盖全局、每图、默认每 Tool 配额及按 Tool 覆盖值。首次调度将配置绑定到全局 state：

- 摘要相同的 API 只读复用，不反复改写 state；
- 摘要不同且存在 live pending/granted ticket 时抛出配置不一致错误；
- 只有 admission 已空闲时才允许切换到新配置。

新增环境变量：

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `DESKPILOT_EFFECT_DAG_ADMISSION_LEASE_TTL_SECONDS` | 15 | pending/granted ticket 的数据库 TTL |
| `DESKPILOT_EFFECT_DAG_ADMISSION_POLL_INTERVAL_SECONDS` | 0.05 | 等待 grant、取消与 permit 状态复核间隔 |

原有 `DESKPILOT_EFFECT_DAG_GLOBAL_CONCURRENCY`、`DESKPILOT_EFFECT_DAG_GRAPH_CONCURRENCY`、`DESKPILOT_EFFECT_DAG_TOOL_CONCURRENCY` 现在是集群配置，而非单进程计数器。

## 7. 验收结果

```text
Ruff:  All checks passed
mypy:  Success, 124 source files
pytest: 348 passed
Alembic: 0020_cluster_dag_admission (head), no new operations
frontend vitest: 15 files, 126 passed (workspace Node 24.14.0)
frontend type-check/build: passed
```

新增覆盖包括：

- 两个独立 Database/controller 共享全局容量，第二个 graph 在首个 permit 释放前保持数据库 pending 且零 node claim；
- 每 Tool 配额阻塞同类 Tool 时，独立 Tool 仍可 work-conserving 地取得容量；
- 两套 service/controller/dispatcher 在共享 SQLite 上执行两个 graph，严格保持全局活动数为 1 并按 graph 交替 grant；
- backpressure graph cancel 持久化撤销 waiter，节点保持 pending、无 claim、Runner 零调用；
- permit 过期后由其他实例回收，grant sequence/fence 单调递增，旧续租和旧释放均被拒绝；
- 过期 admission proof 在 node claim 同一事务中被拒绝；
- permit-loss 能取消被守护 work；
- live 配置不一致 fail closed，仅在队列空闲后允许切换；
- `head -> 0019 -> head` 往返、state seed、表/列/索引/外键和 metadata drift 检查通过；
- 阶段 45～47 的 Runner cancel 三态、ready proof、graph-control owner/fence 与补偿回归继续通过。

## 8. 已知边界与下一步

1. 严格容量目前由一个全局 state CAS 点串行化；语义清晰，但超高吞吐部署需要分片调度域或 PostgreSQL 专用批量路径。
2. 调度与取消唤醒仍依赖数据库轮询，延迟下界受 poll interval 与数据库负载影响。
3. admission 历史 ticket 尚无 retention/cleanup scheduler、受保护查询 API、容量指标或等待时延告警。
4. 当前已用双连接 SQLite 证明跨实例竞争，尚未在真实 PostgreSQL 上执行进程杀死、长事务和网络分区故障注入。
5. ready v3 只限制 proof 页大小；每页 membership 仍会读取完整 graph 并进行 O(V+E) 重算。

下一阶段入口：**为大图建立数据库侧增量 ready 索引/投影，避免分页时反复重算完整 membership；继续保持 admission proof、claim-before-runner、graph/node fence、graph-control cancel 与 prepare/commit/unknown 语义。**
