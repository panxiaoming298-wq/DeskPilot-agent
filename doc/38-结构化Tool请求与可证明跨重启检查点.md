# 38. 结构化 Tool 请求与可证明跨重启检查点

## 1. 阶段结果

DeskPilot 已将 TaskProcessor 恢复所需的结构化 Tool 请求、受信计划、规范化参数与资源、Policy 事实、审批绑定和 Tool 幂等键持久化为受保护的阶段检查点。经过 API 重启后，`created`、安全的 pre-dispatch `running`、`paused`、`waiting_approval` 以及 approved-but-unconsumed 任务可从确切的下一阶段续跑，不再从 goal 文本猜测路径或重建写请求。

恢复不是“只要有一行 checkpoint 就续跑”。启动时必须同时证明 checkpoint 与任务事件序列、任务状态、Tool call 账本、Policy 请求/决策和审批记录一致。密文损坏、身份错配、事件游标落后或执行账本已越过安全边界时全部 fail closed，且不派发 Tool。

## 2. 受信任务图与持久化载荷

当前任务图仍由应用程序中的固定 9 阶段 TaskProcessor 定义，数据库不保存可执行代码、callable、import path 或模型生成的跳转条件。`next_stage` 仅能指向下列受信节点：

| `next_stage` | 下一个受信动作 | 恢复前的主要证明 |
| --- | --- | --- |
| 0 | `created -> classifying` | 任务刚创建，结构化写请求已与创建事件原子持久化 |
| 1 | 分类与计划 | 任务为 `classifying` |
| 2 | `classifying -> running` | 分类和计划已完整保存 |
| 3 | `step.started` | 任务为 `running` |
| 4 | 规范化资源并写入 `tool.requested` | 受信计划可精确选出唯一 Tool step |
| 5 | Policy/Approval | Tool call 必须为 `requested`，参数和资源事实已保存 |
| 6 | Tool 派发 | Tool call 必须仍为 `requested`，Policy 和审批绑定完整 |
| 7 | `step.completed` | Tool call 必须已为 `succeeded` |
| 8 | `task.completed` | step 已完成，任务尚未进入终态 |

`TaskCheckpointPayload` 使用 strict/frozen Pydantic v1 schema，并根据阶段强制完整性：计划阶段后必须有 classification/plan，Tool requested 阶段后必须有 arguments/resources，授权阶段后必须有 Policy 请求与决策，写 Tool 还必须有原始高熵幂等键。Tool call ID 由 task ID 确定性派生，恢复时会重算核对。

## 3. 存储与机密性边界

Alembic `0012_task_runtime_checkpoints` 新增一对一 `task_runtime_checkpoints` 表：

- 明文投影只包含 `task_id / schema_version / next_stage / event_seq / revision / protection_scheme / payload_digest / timestamps`；
- `protected_payload` 使用与 Provider runtime config 相同的 current-user Windows DPAPI protector，但 entropy/context 独立绑定 `DeskPilot/TaskCheckpoint/{task_id}/v1`；
- payload 先以 canonical JSON 编码，限制为 512 KiB，编解码临时 `bytearray` 在 `finally` 中零覆盖；
- ciphertext SHA-256 用于提前检测字节不一致，DPAPI 负责受保护载荷的机密性与完整性；
- 原始文件路径和 Tool 幂等键不进入 `tool_calls`、TaskEvent 或 Outbox，也不以明文出现在 checkpoint SQLite 文件中；
- 任务进入 succeeded/failed/cancelled 终态时删除当前 checkpoint，任务事件和 Tool 摘要账本仍保留。

初始 checkpoint 与 `task.created`/Outbox 在同一事务中写入。后续阶段事件和 checkpoint 目前是相邻事务；如果进程恰好在两个事务之间崩溃，`event_seq` 精确绑定会让旧 checkpoint 失效并安全终止，而不是猜测已执行到哪一步。

## 4. 启动恢复顺序与证明规则

API lifespan 使用固定顺序：

1. 解密并验证所有活动 checkpoint，重建仅属于当前受信图的 `_TaskRuntime`；
2. 对缺失/无效 checkpoint 的 pending 或 approved-but-unconsumed 审批执行原有 fail-closed 收敛；
3. 运行 Tool call 启动恢复，仅跳过已被可验证 pre-dispatch checkpoint 绑定的 `requested` call；
4. 启动 Runner Supervisor 和 Outbox Publisher；
5. 自动续跑 `created/classifying/running`，保持 `paused` 等待用户 Resume，保持 pending `waiting_approval` 并恢复过期计时。

每个 checkpoint 至少核对以下事实：

- checkpoint `event_seq` 等于 `tasks.last_event_seq`；
- `next_stage` 允许当前 TaskStatus；
- call ID 等于从 task ID 派生的稳定身份；
- Tool call 在阶段 5/6 必须为 `requested`，在阶段 7/8 必须为 `succeeded`；
- Policy request 的 task/call 和 decision request digest 必须一致；
- Approval 的 task/call 绑定、pending/approved 状态、`consumed_at` 与 TaskStatus 必须一致。

失败时只写入稳定、脱敏的 `TASK_CHECKPOINT_INVALID` 或 `TASK_CHECKPOINT_BINDING_INVALID`，任务进入 failed 并删除 checkpoint。解密错误、DPAPI 系统详情和受保护载荷均不进入事件。

## 5. 不重放边界

checkpoint 不会改变阶段 27 建立的边界：

- `requested` 只有在可验证 checkpoint 证明尚未派发时才可续跑；
- `start_tool_call` 一旦将账本置为 `running`，就不会通过任务 checkpoint 重放；
- API 在 Tool 运行期崩溃时，旧 checkpoint 会因事件/call 绑定错配失效，原 call 由已有启动恢复收敛为 `unknown` 并创建 Reconciliation；
- 已经 `succeeded` 且事件绑定完整的 call 可从 `step.completed` 或 `task.completed` 续跑，不会再调用 Runner。

因此“跨 API 重启恢复”只扩大了能被证明尚未越过副作用边界的范围，没有将幂等键当作自动重放授权。

## 6. 审批、暂停与终态语义

- `pause`/`resume` 和 approval resolution 事件会在同一事务内将 checkpoint 重绑到新的 `last_event_seq`；
- pending approval 跨重启保持 pending，过期时间仍使用持久化 `expires_at`；
- approved-but-unconsumed 审批在绑定完整时会在 Runner 就绪后续跑，只消费一次；
- 审批缺失、已消费、已拒绝/过期/取消或与 task/call 不匹配时不恢复；
- 终态任务不需要运行时检查点，checkpoint 会被删除。

## 7. 验收

```text
Ruff:  All checks passed
mypy:  Success, 105 source files
pytest: 282 passed
Alembic: 0012_task_runtime_checkpoints (head), no new upgrade operations
frontend vitest: 15 files, 122 passed
frontend type-check/build: passed
```

新增覆盖包括：codec 上下文绑定与缓冲区清零、保护方案/身份拒绝、从 `created` 恢复结构化 `file.move` 且不解析 goal、重启后审批只派发一次、paused Resume、pending approval 保留、approved-but-unconsumed 自动续跑、checkpoint 密文篡改与事件绑定落后时零 Runner 派发，以及 SQLite 中不存在原始 Tool 幂等键明文。

## 8. 已知边界

1. 当前是单 Tool、固定 9 阶段受信图，不是通用的持久化 DAG/LangGraph runtime。
2. 后续事件与 checkpoint 尚未在同一事务内提交；崩溃窗口会安全失败，但不能恢复每一个已完成的无副作用小步。
3. checkpoint 由 Windows current-user DPAPI 保护；当前生产安全边界仍是同一台 Windows 主机、同一用户。
4. 未实现多 API 实例的 checkpoint lease/所有权，当前仍以单 API 进程为目标。
5. 运行中 Tool 的最终效果仍必须依靠 Tool 账本、Runner commit receipt 和 Reconciliation 证据，不由任务 checkpoint 推断。

## 9. 下一步

1. 将当前单 Tool 固定图扩展为版本化、可查询的多步 Tool effect graph，明确 node/edge/attempt/effect/compensation 身份。
2. 使用事件+节点 checkpoint 同事务提交或显式 transition journal，缩小安全失败窗口。
3. 在 effect graph 上定义多步 saga 补偿顺序、不可补偿节点和 unknown 阻塞语义，仍保持每次补偿需要新授权与新回执。
