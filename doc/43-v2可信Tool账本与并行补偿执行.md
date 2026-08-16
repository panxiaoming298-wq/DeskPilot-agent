# 43. v2 可信 Tool 账本与并行补偿执行

## 1. 阶段结果

本阶段将阶段 42 的通用 v2 DAG dispatcher 接入真实的受信 `file.move` 业务链路。新的 `file_move_dag` 结构化请求只接受用户明确提供的路径、操作标识和依赖；路径会由应用层规范化并检查冲突，不从自然语言或模型输出提取写入参数。

每个正向节点现在都独立经过 `ToolCall -> effect attempt -> Policy -> Approval -> authorization grant -> Runner -> effect/receipt` 主干，终态提交同时校验 graph fence 和 node fence。同一 ready-set 的审批可以批量等待，最后一项批准后才恢复任务；整批审批及其未派发 Tool call 都受 protected checkpoint 绑定，可跨 API 重启恢复。

已持久化的 compensation plan 不再只是证明投影。`EffectDagCompensationDispatcher` 会逐 wave 消费它，为每个反向节点创建新 call/attempt/审批/回执，同 wave 有界并行，下一 wave 必须等待前一 wave 全部 `compensated`。补偿失败和结果不明分别收敛到显式图终态，不会偷偷重放。

## 2. 受信 DAG 请求边界

`FileMoveDagRequest` 的强制条件：

- 只接受 `kind=file_move_dag`。
- 包含 2～10 个操作，`operation_id` 全图唯一。
- `depends_on` 只能引用请求中已出现的操作，因此输入顺序本身就是拓扑序，不允许自依赖或重复依赖。
- API 使用可信 `file.move` normalizer 生成规范绝对路径，要求同卷、源文件存在、目标不存在，且全部操作的规范路径互不重叠。
- `TaskProcessor` 使用受信应用模板生成 v2 node/edge；模型无权生成、改写或扩展这些写路径。

示例：

```json
{
  "kind": "file_move_dag",
  "operations": [
    {"operation_id": "left", "source": "C:/in/a.txt", "destination": "C:/out/a.txt", "depends_on": []},
    {"operation_id": "right", "source": "C:/in/b.txt", "destination": "C:/out/b.txt", "depends_on": []},
    {"operation_id": "join", "source": "C:/in/c.txt", "destination": "C:/out/c.txt", "depends_on": ["left", "right"]}
  ]
}
```

## 3. 逐节点 Tool 账本与授权

`EffectDagLedgerPreparer` 只处理已由 ready-set 或 compensation plan 证明的节点。它在 dispatcher claim 之前：

1. 从 protected structured request 重建正向参数，或从 forward commit receipt 重建反向参数。
2. 重新投影真实文件资源与 source version，对补偿额外校验 forward receipt 记录的 destination-after version。
3. 为节点和 attempt kind 派生稳定、独立的 call/attempt 标识与幂等键。
4. 持久化 Tool request、effect attempt 和 Policy decision。
5. Policy 需要人工授权时创建一次性审批，并将节点放入 `waiting_approval`；未批准节点不进入 claim 集合。

`LedgerBoundEffectNodeExecutor` 只执行同时持有有效 graph lease/fence 和 node claim/fence 的节点。它根据持久化 Policy/审批事实签发 authorization grant，通过 Runner 提交，再将 Tool terminal、attempt terminal、effect、commit receipt 和 node transition 作为一个受围栏命令提交。

失败语义按 commit boundary 分开：

- 参数/资源/授权/Runner-ready 准备失败尚未跨过外部提交边界，因此记为确定 `failed`。
- Runner 调用开始后抛出无法证明结果的异常，记为 `unknown`，并由 reducer 收敛到 `blocked_unknown` 或 `blocked_compensation_unknown`。
- 不会将已跨过 Runner 边界的不确定结果伪装成可自动重试的普通失败。

## 4. 批量审批与跨重启恢复

同一 ready-set 可一次准备最多 4 个节点，每个节点仍有独立审批卡和主动过期计时器。批准其中一项时，只更新它自己的审批事实；只要同任务仍有 pending 审批，Task 就保持 `waiting_approval`。最后一项批准后，Task 才回到 `running` 并唤醒 stage 3 DAG 循环。重启恢复会为仍 pending 的整批审批重建计时器。

v2 checkpoint 保存 graph ID/schema、受信请求、计划与整批 `dag_approval_ids`，但不保存短命 dispatcher lease 或 node claim。启动恢复会：

- 校验 checkpoint/event/task/graph 绑定。
- 重建整批审批对应的 call ID，将这些已请求但未派发的 call 列入可恢复集合。
- 若全部 incomplete call 都由可验证 checkpoint 覆盖，启动 Tool recovery 不会错误失败它们，也不会留下阻塞 dispatcher 的 startup graph lease。
- 任何 checkpoint 缺失、损坏或绑定不一致仍 fail closed。

自动化在根节点审批前、join 审批前各重启一次 API，第三个进程批准 join 后原图成功，证明批量审批与节点账本不依赖单进程内存。

## 5. 真实并行补偿消费

补偿 dispatcher 使用已持久化的 content-addressed plan，不在每次恢复时根据可变图状态重新猜测顺序。对 `left + right -> join` 的已应用节点，波次是：

```text
wave 0: join
wave 1: left, right
```

每个 wave 的执行规则：

1. 从 forward receipt 生成反向 `file.move` 和精确 source version。
2. 创建全新 compensation call/attempt/Policy/审批；不改写 forward 账本。
3. 审批全部就绪后，用 plan ID + wave ordinal + graph fence 原子 claim 该 wave 的节点。
4. 对同 wave 节点有界并行，同时续租 graph/node claim。
5. 只有所有早期 wave 均为 `compensated` 时才能 claim 后续 wave；越过 barrier 的命令会被拒绝。
6. 逐 wave 运行 compensation reducer。

图级补偿终态：

| 节点证据 | 图终态 |
| --- | --- |
| 计划中所有节点均为 `compensated` | `compensated` |
| 任一节点为 `compensation_failed` | `blocked_compensation_failed` |
| 任一节点为 `compensation_unknown` | `blocked_compensation_unknown` |

原 DAG 失败但全部已应用效果成功补偿时，graph 为 `compensated`，Task 仍以明确 `DAG_COMPENSATED` 失败归档：这表示业务目标未完成，但外部已应用效果已由回执绑定的反向操作撤销。

## 6. 迁移与验收

Alembic head 推进到 `0017_parallel_compensation`。该迁移扩展 `tool_effect_graphs.status` 检查约束，增加：

- `blocked_compensation_failed`
- `blocked_compensation_unknown`

自动化已完成最新迁移往返、开发库升级到 head 和 `alembic check`。

```text
Ruff:  All checks passed
mypy:  Success, 117 source files
pytest: 314 passed
Alembic: 0017_parallel_compensation (head), round-trip passed, no new operations
frontend vitest: 15 files, 126 passed
frontend type-check/build: passed (workspace Node 24.14.0)
```

新增 6 项后端用例：逐节点账本/批量审批/并行正向回执闭环、持久化 wave 的并行反向审批/回执闭环、三进程审批恢复与过期计时器重建、批量中任一拒绝后原子取消其余未消费审批/call，以及 compensation wave barrier 在 failure/unknown 两种终态下的拒绝与 reducer 收敛。

## 7. 已知边界与下一步

1. 当前 v2 业务图只开放受信结构化 `file_move_dag`，仅支持 success edge；下一步实现条件边、显式 branch-decision 内容证明和分支恢复。
2. graph cancel intent 已阻止新 claim，但仍未向已在途 Runner call 广播 cancel IPC；需与 commit-boundary/unknown 语义一起设计。
3. 并发上限当前为单图固定 4；尚需全局公平性、每图/每 Tool 配额、大图 ready-set 分页和 backpressure。
4. 补偿失败/unknown 会安全阻断，但尚无补偿的再补偿、图级人工处置 API 或对后台证据的周期采集。
5. Outbox DLQ/requeue/cleanup 仍只有应用原语，尚需受保护的运维 API/UI、retention scheduler、指标与审计。
6. PostgreSQL 已有驱动和原生 claim SQL 方言验证，当前开发机仍无 PostgreSQL/Docker；需在真实服务上运行双 API/dispatcher、进程杀死、超时与网络分区故障矩阵。
