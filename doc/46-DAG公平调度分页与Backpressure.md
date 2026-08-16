# 阶段 46：DAG 公平调度、分页与 Backpressure

## 1. 本阶段目标

阶段 45 已能取消当前 graph/node fence 下的在途 Runner call，但不同任务仍各自以固定并发运行：先启动的宽图可能占满 Runner 容量，ready-set checkpoint 也会把整批可运行节点写入一个 JSON proof。

阶段 46 在不改变 prepare/commit/unknown、graph lease 和 node claim fence 语义的前提下，完成以下收敛：

- 一个 API 进程内的所有 forward/compensation DAG 共享全局容量；
- 同时执行全局、每图、每 Tool 三层并发限制；
- 等待容量的工作不提前创建数据库 node claim；
- ready-set 使用有界、内容寻址的 v3 页证明；
- 取消可唤醒尚未 claim 的 backpressure waiter；
- 调度基础设施支持最多 1000 节点的受信图，并以 128 节点宽图执行压力验证。

## 2. Claim 前的公平 Admission

新增进程级 `EffectDagAdmissionController`。应用组合根只创建一个实例，并注入 `TaskProcessor`、`EffectDagDispatcher` 和 `EffectDagCompensationDispatcher`。

每个候选节点以 `graph_id + node_id + tool_name` 请求许可。控制器同时检查：

1. `active_total < global_limit`；
2. `active_by_graph[graph_id] < per_graph_limit`；
3. `active_by_tool[tool_name] < tool_limit`。

等待队列按 graph 分组，并使用 graph ring 轮询。一次 drain 每次只为当前图发放一个候选，再把该图旋转到队尾；同一时刻已经等待的图因此按轮次分享新释放的容量。若只有一个图等待，算法仍保持 work-conserving，可填满其合法配额。

许可严格在 `claim_effect_dag_nodes` 或 `claim_effect_dag_compensation_nodes` 之前取得。许可不足时，节点仍保持 `pending/succeeded`，没有 claim owner、claim TTL 或虚假的在途所有权。proof 在等待期间若因取消或其他 fenced mutation 变旧，原有 ready proof 校验拒绝 claim，许可随后归还并重新 reduce。

许可覆盖 Runner 执行与终态 transition，完成、fence 拒绝或协程异常都会在 `finally` 中释放。直接取消等待协程时，waiter 会从 graph queue 移除，不遗留容量或队列泄漏。

## 3. Backpressure 与取消

backpressure 是阻塞 admission，不是先 claim 后排队。这样不会出现大量 ACTIVE node 占着数据库 lease、却只是在进程内等待 Runner 槽位的情况。

`EffectDagDispatcher.request_cancel` 的顺序仍保持阶段 45 的安全边界：

1. 移除该 graph 尚未获得许可的 admission waiter，使调度协程立即醒来；
2. 使用当前 graph owner/fence 持久化 `cancel_requested_at`；
3. 只向当前实际 claim 的 `node_id + claim_fencing_token` 广播 Runner cancel；
4. reducer 将未 claim 的节点直接收敛为 cancelled/skipped。

若取消发生在“许可已发放、claim 尚未提交”的窄窗口，数据库 cancel intent 会推进 graph revision，旧 ready page proof 无法 claim。若 Runner 已开始，则仍由阶段 45 的 generation-bound cancel 和 prepare/commit/unknown 终态决定事实。

## 4. Ready-set v3 页证明

`checkpoint_effect_dag_ready_set` 新增 `page_size` 与 `cursor`，单页范围为 1～1000。proof 分为两层：

- membership digest：绑定 graph ID/revision/event seq 和该 revision 下完整 ready membership；
- page digest：绑定 membership digest、cursor、page size、next cursor、total、has-more 与页内 predecessor/branch proofs。

checkpoint JSON 使用 `deskpilot.effect-ready-set.v3`，只持久化当前页节点。claim 时会重算完整 membership 和指定页，并同时校验：

- checkpoint graph revision/event 未变化；
- membership digest 一致；
- cursor、页大小、total、next cursor、has-more 一致；
- 被 claim 的每个节点都属于该 proof 页。

因此不能用第一页 proof claim 第二页节点，也不能在 graph revision 推进后继续使用旧页。旧 v2 checkpoint 行无需迁移：它们是 revision-bound 的临时证明，新调度会重新生成 v3 checkpoint。

当前实现把单次持久化 proof 和 dispatch batch 限制为有界页，但 ready membership 的重算仍需读取该图全部 node/edge，复杂度为 O(V+E)；它不是数据库流式图遍历。

## 5. 图规模与配置

图验证边界调整为：

| 边界 | 上限 |
| --- | ---: |
| 单图节点 | 1000 |
| 单节点前驱 | 128 |
| 全图逻辑依赖 | 10000 |
| 单 ready 页 | 1000 |

依赖总数限制避免在大图中构造无界 edge/proof 膨胀。公开 `file_move_dag` 请求仍保持原来的受信、小批量业务输入限制；提升的是内部 DAG 调度基础设施边界，不开放无界文件操作。

新增启动配置：

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `DESKPILOT_EFFECT_DAG_GLOBAL_CONCURRENCY` | 8 | 单 API 进程内全部 DAG Runner 工作上限 |
| `DESKPILOT_EFFECT_DAG_GRAPH_CONCURRENCY` | 4 | 单 graph 上限 |
| `DESKPILOT_EFFECT_DAG_TOOL_CONCURRENCY` | 4 | 每个 Tool name 的独立上限 |
| `DESKPILOT_EFFECT_DAG_READY_PAGE_SIZE` | 64 | durable ready proof 页大小 |

graph/tool 上限不得超过 global 上限。forward 与 receipt-bound compensation 共用同一控制器，因此补偿不会绕过全局或 Tool 配额；补偿 wave barrier 和终态规则不变。

## 6. 验收结果

```text
Ruff:  All checks passed
mypy:  Success, 119 source files
pytest: 333 passed
Alembic: 0018_branch_decision_proofs (head), no new operations
frontend vitest: 15 files, 126 passed (workspace Node 24.14.0)
frontend type-check/build: passed
```

新增覆盖包括：

- 两个已等待 graph 在容量释放后各获得一个槽位；
- 全局单槽下两个三根图交替执行，不发生图饥饿；
- 每 Tool 配额独立计数，同图不同 Tool 可并行、同 Tool 不超限；
- 等待 admission 的协程被直接取消后，queue 与容量无泄漏；
- backpressure 期间 node 保持 pending 且无 claim owner，graph cancel 可立即唤醒并收敛；
- 三个 ready 页共享 membership digest，但 page digest 各不相同，跨页 claim 被拒绝；
- 128 个并行根节点在 17 节点 checkpoint 页、8 个执行槽下完成，所有 durable checkpoint 均未超过页上限；
- 阶段 45 的 cancel reason、Runner generation、unknown/committed truth、graph/node fence 与补偿回归继续通过。

本阶段没有数据库结构变更，Alembic head 保持 `0018_branch_decision_proofs`。

## 7. 已知边界与下一步

1. 当前“全局”是单 API 进程全局；多个 API 实例仍各有自己的容量计数。集群级公平需要数据库/外部 broker 支持的分布式 admission，而不能共享内存 semaphore。
2. ready 页限制持久化 payload 和单轮候选数，但 membership 重算仍读取完整图；更大规模需要数据库侧增量 ready 索引或流式拓扑投影。
3. 跨 API 实例 cancel 仍不能主动唤醒远端 live graph owner。下一阶段应增加持久化 graph control message 或外部 broker 路由，并使用 graph owner/fence 去重和拒绝旧控制消息。
4. Outbox DLQ/requeue/cleanup 仍缺受保护运维 API/UI、retention scheduler 与指标告警。

下一阶段入口：**跨 API 实例的持久化 graph cancel 控制消息与远端 owner 路由；保持 intent-before-IPC、Runner generation、prepare/commit/unknown 和 graph/node fence 语义。**
