# 36. `file.move` 回执驱动显式补偿闭环

## 1. 阶段结果

DeskPilot 已为第一个可逆写 Tool 完成 receipt-driven 显式补偿：当 `file.move@1.0.0` 的原调用保持 `unknown`，但签名 Runner journal 返回与该 call 精确绑定的 committed receipt 时，用户可以创建一个全新的反向 `file.move` 任务。

补偿不改写原 Tool 账本，不复用原 call、幂等键或审批，也不把“可逆”解释成自动撤销。反向任务仍经过完整的 prepare / policy / 一次性 approval / commit / receipt 闭环。

## 2. 服务端派生，客户端不提供路径

新增接口：

```text
POST /api/v1/reconciliations/{reconciliation_id}:create-compensation
Idempotency-Key: <16..128 ASCII chars>
```

请求没有 body。客户端不能传入 source、destination、receipt ID 或版本；对外 `TaskCreate.tool_request` 仍只接受普通 `file_move`。内部 `FileMoveCompensationRequest` 只由控制面从以下持久化事实派生：

- 原 call 为精确的 `file.move@1.0.0`，Contract digest 匹配且状态为 `unknown`；
- Reconciliation 已保存 `commit_receipt` 正向证据；
- receipt 的 call/tool/version/authorization/idempotency digest 与原调用一致；
- receipt 的 approval ID/preview hash 与已批准且已消费的原一次性审批一致；
- 原审批的 capability、resource role 和 expected versions 与 receipt before/after 版本一致。

反向 source 固定为原 destination，反向 destination 固定为原 source，预期 source 版本固定为 receipt 中已提交的 destination 版本。任一绑定不成立均 fail closed。

## 3. 版本检查与竞态防护

创建前，`TaskProcessor` 使用现有 `file.move` 规范化器确认：

- 反向 source 仍是普通文件；
- 反向 destination 仍不存在；
- 两者仍在同一卷；
- 反向 source 的当前外部版本精确等于 committed receipt 版本。

预检与任务处理之间仍可能发生竞态，因此阶段 4 在创建 Tool call 和审批前再投影一次资源。不匹配时任务以 `COMPENSATION_RESOURCE_VERSION_MISMATCH` 失败，不会生成 Tool call。审批后 Runner 仍会在 prepare/commit 边界复核版本与 destination absent。

## 4. 补偿血缘与幂等

Alembic `0011_file_move_compensation` 为 `tool_reconciliations` 增加：

- `compensation_task_id`；
- `compensation_receipt_id`；
- `compensation_created_at`。

`compensation_task_id` 具有唯一约束。每条 committed receipt Reconciliation 最多派生一个直接补偿任务；如果已经创建 `confirmed_no_effect` 的新 attempt，也不再允许建立竞争的补偿血缘。

创建请求由 `tool_reconciliation.create_compensation` 幂等回执持久化。同 key 返回同一任务且 `replayed=true`；不同 key 不能创建第二个补偿。新任务的 `task.created.compensation_of` 只保存 reconciliation/task/call/receipt ID，不保存路径。

## 5. 全新授权与提交

补偿任务使用受信任的 compensation 计划模板和 `local_user` actor，但仍会生成：

- 新 task ID 和 call ID；
- 新高熵 Tool 幂等键及不同 digest；
- 新 Policy decision 与 approval ID；
- 反向 source/destination 的精确审批资源；
- 新的 prepare、受控 commit 和 durable receipt。

审批卡标题为“撤销先前的单文件移动”，明确说明这是新的反向提交、不改写原账本，且版本变化或原路径被占用时不会执行。

## 6. 前端交互

Runner 证据卡在已发现 committed receipt 且服务端允许补偿时显示“创建反向任务”：

1. 第一次点击只展开内联确认；
2. 第二次确认才发送无 body 补偿请求；
3. 传输失败不自动重放，人工重试复用原幂等键；
4. 创建成功后工作台切换到新任务，由现有审批卡展示精确反向路径与版本。

补偿创建本身不执行文件移动，用户未批准新审批前，外部资源保持不变。

## 7. 验收

```text
Ruff:  All checks passed
mypy:  Success, 102 source files
pytest: 272 passed
Alembic: 0011_file_move_compensation (head)
frontend vitest: 13 files, 111 passed
frontend type-check/build: passed
```

新增覆盖包括：无 receipt 拒绝、签名 receipt 绑定、反向源版本变化拒绝、创建前不执行、同 key 回放、不同 key 单血缘约束、全新审批/call/幂等 digest、批准后实际恢复文件，以及前端 API header、稳定幂等键、二次确认和任务切换。

## 8. 下一步

1. 实现任务历史与集中 Reconciliation 中心，接入人工裁决、证据筛选、新 attempt 和 compensation 导航。
2. 持久化结构化 Tool 请求、任务图与阶段检查点，使已创建但未运行的安全任务可跨 API 重启恢复。
3. 将直接补偿血缘扩展为可查询的 Tool effect graph，为后续多步 saga 补偿保留审计语义。
