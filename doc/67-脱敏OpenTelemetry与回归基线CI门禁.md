# 阶段 67：脱敏 OpenTelemetry 与回归基线 CI 门禁

## 1. 完成范围

本阶段建立诊断遥测的最小闭环，但不改变既有真值边界：Task/Event、Receipt、Evidence、Audit 仍是运行正确性真值，Evaluation Run/Trace 仍是评测证明；OpenTelemetry 可丢失、可过期，也不能参与恢复、授权、幂等或报告摘要。

实现内容：

- 引入固定大版本范围的 OpenTelemetry API/SDK，并由 `TelemetryFacade` 统一创建 span 和低基数 metric；
- 任务接收、模型派发、Runner Tool 执行、MCP 调用、评测 Run/Case 接入短生命周期 span；
- `deskpilot.telemetry-schema.v1` 属性注册表默认拒绝未知字段，导出层再次过滤；
- 本地 exporter 使用有界内存队列，只保存规范化安全投影；支持真实 OTel trace ID 或现有 `trc_<32hex>` TaskCorrelationId 查询；
- `GET /api/v1/telemetry/traces` 和 `GET /api/v1/telemetry/metrics` 均要求本地会话并返回 `Cache-Control: no-store`；
- 新增 `deskpilot.evaluation-baseline.v1`、显式 `record`/只读 `compare` CLI 和 Windows CI 门禁。

## 2. 脱敏边界

普通 telemetry 永不接收 Prompt、模型输出、Tool/MCP 参数或结果、文件名/路径、URL、RAG/Memory 正文、凭据、Header、SQL 参数、原始异常正文或 stack。Facade 关闭 SDK 的自动异常记录，只写稳定错误码；直接绕过 Facade 写入 SDK 的未知属性仍会在 exporter 边界被删除。

Correlation ID 只进入本地投影；metric dimension 固定为 `category/outcome`，不含 task、request、run、case、call 等实例 ID。未知 enum 归一为 `other`，避免无界 time series。

Exporter 的 `export/flush/shutdown` 异常被隔离，不能改变领域调用结果。当前不启用远程 OTLP，也没有 blanket Python logging/HTTP/SQL 自动采集。

## 3. 查询

```text
GET /api/v1/telemetry/traces?trace_id=<32hex>&limit=100
GET /api/v1/telemetry/traces?task_correlation_id=trc_<32hex>&limit=100
GET /api/v1/telemetry/metrics
```

两个 trace 查询键必须且只能提供一个。当前本地 store 有界且随 API 进程结束清空，它是诊断缓存，不是持久证据或恢复输入。

## 4. 版本化基线与门禁

基线位于 `backend/tests/baselines/evaluations/`。v1 同时绑定 suite ID、version、digest，并检查：

- run success rate 不低于 100%；
-最新 run safety rate 不低于 100%；
- Windows run p95 不超过 30 秒；
- Windows case p95 不超过 10 秒。

CI 只执行：

```powershell
.\.venv\Scripts\python.exe -m deskpilot.evaluation_gate compare
```

`record` 必须显式设置 `DESKPILOT_EVALUATION_BASELINE_MODE=record`，在 `CI=true` 时硬拒绝，且不能覆盖已有文件。更新基线必须创建新的版本化路径并审阅 diff：

```powershell
$env:DESKPILOT_EVALUATION_BASELINE_MODE = "record"
.\.venv\Scripts\python.exe -m deskpilot.evaluation_gate record `
  --output tests/baselines/evaluations/golden-resilience-v3.windows-v1.json `
  --baseline-id golden-resilience-v3.windows-v1 `
  --max-run-p95-ms 30000 `
  --max-case-p95-ms 10000
```

报告 digest 不包含 OTel trace/span ID、采样或 exporter 状态；CI 也会用 `git diff --exit-code` 确认 compare 没有重写 baseline。

## 5. 自动化验收

- 泄漏金丝雀覆盖 Prompt、Authorization、URL、异常正文与直接 SDK 属性；
- exporter 抛错不向业务传播；
- TaskCorrelationId/OTel trace ID 查询、`no-store` 与低基数 metric 已覆盖；
- 评测一次 Run 产生 1 个 run span 与 20 个 case child span，Evaluation Trace digest 不进入 OTel；
- 基线严格 Schema、不可覆盖、成功/安全/两类 p95 漂移均有门禁测试；
- CI 使用只读权限、锁定依赖、测试/静态检查、compare 和 baseline diff guard。

## 6. 后续边界

阶段 67 没有实现远程 OTLP、持久化 telemetry store、tail sampling、多 Agent Handoff/link、Verification/Memory/RAG/Research/Artifact 专属 span 或前端 trace UI。它们应随阶段 70～74 在相同注册表和真值边界上增量扩展。阶段 68 Agent Contract/Registry 已在后续完成，当前工程阶段进入 69 Task Contract/Plan Compiler。
