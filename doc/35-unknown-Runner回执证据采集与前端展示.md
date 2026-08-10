# 35. `unknown` Runner 回执证据采集与前端展示

## 1. 阶段结果

DeskPilot 已把阶段 33 提供的签名 `tool.commit_receipt.get/result` 查询接入 `unknown` Reconciliation。任务页收到 `tool.unknown` 后，会定位对应对账记录并自动查询当前 Runner 的持久化 commit journal；用户也可以在证据卡中手动刷新。

本阶段严格保留既有不变量：自动证据只追加到对账证据层，绝不把原 `tool_calls.status=unknown` 改写为 succeeded/failed，也不自动提交人工 outcome 或创建新 attempt。

## 2. 证据模型与迁移

Alembic `0010_reconciliation_receipt_evidence` 新增 `tool_reconciliation_evidence`：

- `commit_receipt`：签名 Runner 查询返回与原调用精确绑定的 committed receipt；
- `no_receipt`：当前 Runner journal 没有该 call 的 committed receipt；
- `query_failed`：Runner 不可用、查询超时、协议失败或 receipt 绑定无效。

每条证据保存 reconciliation、查询 Runner ID、稳定错误码或 commit receipt 外键、观察时间和内容摘要。`(reconciliation_id, evidence_digest)` 唯一约束让相同观察天然幂等；重复刷新仍会执行只读 Runner 查询，但不会生成重复数据库行，响应返回 `replayed=true`。

发现 committed receipt 时，控制面会先复核 call/tool/version/authorization/idempotency-key digest，再写入已有 `tool_commit_receipts` 类型化投影，并由证据行引用。投影只包含授权摘要、prepare 摘要和 source/destination 版本，不保存原始路径或幂等键。

## 3. API

新增受认证接口：

```text
POST /api/v1/reconciliations/{reconciliation_id}:refresh-evidence
```

响应包含：

```json
{
  "reconciliation": {
    "status": "pending",
    "outcome": null,
    "receipt_evidence": []
  },
  "evidence": {
    "kind": "no_receipt",
    "queried_runner_id": "runner_...",
    "commit_receipt": null,
    "error_code": null,
    "observed_at": "2026-08-10T00:00:00Z"
  },
  "replayed": false
}
```

该 POST 只触发签名只读查询和内容寻址证据追加，不派发 Tool、不调用 prepare/commit，也不接受调用参数。相同结果由数据库唯一摘要归并，因此不要求客户端提供 `Idempotency-Key`。接口仍要求本地 Bearer 会话、可信写来源并返回 `Cache-Control: no-store`。

列表和详情响应的 `ReconciliationRead.receipt_evidence` 按最新观察优先返回完整证据历史。

## 4. 证据解释

三种自动观察不是对称结论：

- committed receipt 是已经越过提交边界的正向证据，可支持人工裁决 `confirmed_succeeded`；
- no receipt 只表示当前 journal 没有 committed 记录，不能证明外部副作用没有发生；
- query failed 不能证明成功、失败或无副作用，只允许显示稳定错误码后重试查询。

因此 no receipt/query failed 都不会开启 `can_create_attempt`。只有人工作出不可改写的 `confirmed_no_effect` 裁决后，既有 API 才允许创建全新 attempt。

## 5. 查询与安全边界

`TaskProcessor.refresh_reconciliation_evidence` 获取当前冻结 Runner lease，并直接通过该 lease 的客户端执行签名查询，避免查询结果与另一代 Runner ID 混淆。Runner 返回 receipt 后，控制面再次验证：

- `call_id`；
- Tool name/version；
- `authorization_id`；
- idempotency key digest。

绑定不匹配不会持久化伪造 receipt，而是记录 `RUNNER_COMMIT_RECEIPT_BINDING_INVALID` 查询失败证据。其他异常只映射到满足稳定格式的错误码；异常正文、本地路径、IPC 细节和 Runner stderr 不进入 API 或证据表。

## 6. 前端任务页

新增 `useTaskReconciliation` 与 `ReconciliationEvidenceCard`：

1. 监听当前任务最新 `tool.unknown` 事件并读取该 task/call 的 Reconciliation；
2. 自动调用 refresh-evidence 一次；
3. 展示最新证据、历史数量、Runner ID、观察时间和稳定错误码；
4. committed receipt 额外展示 receipt ID、提交/落盘时间与 source/destination 前后版本；
5. 提供手动“重新查询 Runner 日志”。

页面始终提示“原始调用保持 unknown”和“最终结果仍需人工裁决”。no receipt 卡片明确说明不能据此安全重试；query failed 不展示服务端异常正文。

当前实现是任务工作台内的对账卡，不是完整任务历史/集中对账中心。没有打开任务页时不会由后台周期轮询 Runner。

## 7. 验收

```text
Ruff:  All checks passed
mypy:  Success, 101 source files
pytest: 269 passed
Alembic: 0010_reconciliation_receipt_evidence (head)
frontend vitest: 13 files, 108 passed
frontend type-check/build: passed
```

新增覆盖包括：

- 空库/旧库升级到新 head，并验证 evidence 表的 check/unique/index；
- no receipt 重复刷新执行两次签名查询但只留下一个内容寻址证据；
- Runner 查询失败只持久化稳定错误码，不泄漏异常正文；
- 同一 unknown 后续发现有效 receipt 时追加正向证据并投影 receipt；
- receipt 发现后 reconciliation 仍为 pending、原 Tool 调用仍为 unknown、不能自动创建新 attempt；
- 前端 unknown 事件只触发一次自动采集，任务切换后忽略旧响应；
- 证据卡完整区分 committed/no-receipt/query-failed，并在刷新时禁用重复操作；
- API 使用编码路径、no-store 查询和无请求体 refresh POST。

## 8. 下一步

1. 实现基于 committed receipt 的显式反向 `file.move` compensation：重新验证当前 destination 版本、source absent，创建全新 call/幂等键并要求新的一次性审批。
2. 为任务历史和集中 Reconciliation 中心补齐列表、人工裁决、证据筛选与后继 attempt 导航。
3. 持久化结构化工具请求、任务图与阶段检查点，使安全任务状态具备可证明的跨 API 重启恢复语义。
