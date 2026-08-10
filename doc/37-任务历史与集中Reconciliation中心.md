# 37. 任务历史与集中 Reconciliation 中心

## 1. 阶段结果

DeskPilot 已将分散在单任务页中的 `unknown` 证据能力扩展为独立的“历史与对账”工作台。用户现在可以查询持久化任务历史，按状态筛选 Reconciliation，集中查看 Runner 证据、提交不可改写的人工裁决，并沿原任务、新 attempt 和 compensation 血缘切换到对应任务。

该中心只编排已有的受控 API，不引入客户端真值：原 `tool_calls.status=unknown` 永不改写，裁决和后继任务都以服务端返回的持久化快照为准。

## 2. 有界任务历史 API

新增接口：

```text
GET /api/v1/tasks?status=&limit=50&offset=0
```

- `status` 可选且使用既有 `TaskStatus` 枚举；
- `limit` 限定为 `1..100`，默认 50；
- `offset` 必须大于等于 0；
- 按 `created_at desc, task_id desc` 稳定返回最新任务；
- 响应包含 `items / total / limit / offset`，并设置 `Cache-Control: no-store`；
- 列表只返回 Task 快照，不聚合或泄露事件 payload、Tool 参数与幂等键。

本阶段复用现有 `tasks` 表，无需新增 migration。

## 3. 集中工作台

前端侧栏新增独立“历史与对账”入口，工作台由三部分组成：

1. 最近任务：状态筛选、25 条分页、任务状态与创建时间；
2. Reconciliation 列表：按 pending/resolved 筛选并选择一条记录；
3. 对账详情：展示 call/Runner 稳定标识、receipt/no-receipt/query-failed 证据、不可改写裁决、后继任务动作与血缘导航。

刷新证据仍调用签名 Runner journal 查询，不派发 Tool。历史任务切换会重建事件、控制、审批和单任务 Reconciliation 上下文；如果当前任务尚未终止，其他历史任务按钮会被锁定，避免失去活动任务的控制入口。

## 4. 人工裁决提交语义

pending 记录支持以下 outcome：

- `confirmed_succeeded`；
- `confirmed_failed`；
- `confirmed_no_effect`；
- `accepted_unknown`。

用户必须填写证据摘要。第一次点击只显示不可改写警告，第二次点击才发送请求。前端不做乐观更新、不自动重放 POST；传输失败后的人工重试在 outcome 和规范化摘要不变时复用原 `Idempotency-Key`，内容变化则生成新 key。成功后只接受服务端 Reconciliation 快照；pending 筛选中已 resolved 的记录会立即退出当前列表。

## 5. 后继任务与重试边界

新 attempt 与 compensation 同样要求内联二次确认，失败后的人工重试复用对应动作的稳定幂等键。创建成功后，工作台将服务端返回的新 Task 快照加入历史第一页并切换到该任务，后续审批和执行仍由既有任务工作台负责。

本阶段同时收紧了旧 attempt 能力：只有精确匹配 `computer.disk_usage@1.0.0` Contract、能够由当前可信处理器确定性重建请求的调用才会返回 `can_create_attempt=true`。`file.move` 的原始参数尚未持久化，因此即使人工裁决为 `confirmed_no_effect`，也不能从 goal 猜测路径并重建 attempt；它仍只能在存在已验证 committed receipt 时走服务端派生的补偿路径。

## 6. 前端并发与筛选一致性

- 列表并行加载，generation 会忽略筛选切换前的延迟响应；
- 证据刷新、裁决、attempt 与 compensation 共享互斥动作锁；
- task/reconciliation 筛选变化由服务端重新查询，不在客户端伪造总数；
- 新后继任务只在匹配当前 task 筛选时更新总数和第一页；
- 血缘导航优先使用已加载 Task 快照，缺失时再执行 `GET /tasks/{id}`；
- 所有历史与对账 GET 均使用 `no-store`。

## 7. 验收

```text
Ruff:  All checks passed
mypy:  Success, 102 source files
pytest: 274 passed
Alembic: 0011_file_move_compensation (head), no new upgrade operations
frontend vitest: 15 files, 122 passed
frontend type-check/build: passed
```

新增覆盖包括：任务历史有界分页/筛选/排序/no-store、side-effecting call attempt 拒绝、集中列表并行加载、稳定幂等键、裁决 body 变化换 key、筛选后快照移除、后继任务血缘更新、缓存/远端导航、裁决与补偿二次确认，以及活动任务切换锁。

## 8. 下一步

1. 持久化结构化 Tool 请求、可信任务图与阶段检查点，使安全的 created/paused/waiting-approval 状态具有可证明的跨 API 重启恢复语义。
2. 将单步 attempt/compensation 关联扩展为可查询 Tool effect graph，为多步 saga 补偿保留审计与故障语义。
3. 为任务历史增加服务端游标分页和更细粒度的时间/工具筛选，并为 Reconciliation 证据采集设计显式后台调度策略。
