# 阶段 65：版本化黄金任务与 Trace Replay

## 1. 目标与边界

本阶段建立评测可观测的首个纵向闭环：加载仓库内固定 YAML 黄金套件，在隔离临时资源中执行真实只读场景，持久化内容寻址结果 manifest 和 trace chain，并通过重新执行比较语义摘要完成 replay。

首版不接受用户上传 YAML、Python、Shell、URL、模型 prompt 或任意场景名称；套件只能引用组合根内三个固定 handler，运行不读取生产知识库或生产 MCP enable 状态。

## 2. 严格黄金套件

`golden_readonly_v1.yaml` 使用 `deskpilot.golden-suite.v1`，绑定 suite ID、整数版本、唯一 case ID、固定 scenario、safety 标记、输入和期望子集。Loader 限制 64 KiB、严格 UTF-8，拒绝 YAML anchor/alias，并由 Pydantic 禁止未知字段和未知 scenario。

三个首批案例：

- `mcp.text-metrics`：真实启动内置 MCP stdio Server 并校验 structured content；
- `security.mcp-bundle-tamper`：注册后改写临时 Server bundle，必须稳定返回 `MCP_SERVER_BUNDLE_REJECTED`；
- `knowledge.source-stale`：在临时 SQLite/文件中导入、命中、修改来源，再证明旧 Citation 为零且 stale source 为一。

每个案例只比较显式 expect 字段，但 output digest 覆盖完整结构化输出。预期不匹配稳定分类为 `EXPECTATION_MISMATCH`；未分类异常归一为稳定错误码，不把异常正文写入结果。

## 3. Record 与 Replay

Alembic `0029_evaluation_traces` 新增 `evaluation_runs` 与 `evaluation_trace_events`：

- Run 保存 suite identity/digest、通过/失败/安全计数、总耗时、replay 关系、语义 result manifest 与摘要；
- 每个 Case 形成一条 trace，绑定输入摘要、完整输出摘要、稳定错误码、耗时、前序摘要和 event digest；
- 读取时复核 manifest 摘要、suite/replay identity、全部计数与比例来源、trace 数量/顺序/前序链以及 trace 到 manifest 的投影；任一不一致返回 `EVALUATION_PROOF_REJECTED`。

Replay 不是回读旧结果。系统重新加载当前打包套件；suite digest 不同则拒绝冒充同版本，digest 相同才重新运行三个真实场景，并比较 case ID、scenario、status、output digest、error code 和 safety 标记。耗时不要求相等，因此性能抖动不会被误判为语义漂移。

## 4. API 与前端

新增受保护、禁止缓存的 API：

- `GET /api/v1/evaluations/runs`；
- `GET /api/v1/evaluations/runs/{run_id}`；
- `POST /api/v1/evaluations/golden:run`；
- `POST /api/v1/evaluations/runs/{run_id}:replay`。

前端新增“评测与 Trace”入口，展示成功率、安全通过率、总耗时、replay 是否一致、逐案例状态/耗时/output digest 和持久化历史。所有指标来自服务端已验证 Run，不由浏览器重算 trace 真值。

## 5. 验证与下一步

自动化覆盖全绿首次运行、语义一致 replay、历史恢复、trace 篡改拒绝、稳定 expectation failure、suite version/digest 漂移拒绝、`0029 -> 0028 -> 0029` migration 往返和前端指标/trace 展示。

最终门禁：Ruff 全仓通过；mypy 156 个生产源码通过；pytest 426 collected、414 passed、12 skipped、1 条既有第三方 warning；Alembic `0029` head/check 和 `0029 -> 0028 -> 0029` 往返通过；`uv lock --check` 通过；前端 20 文件/139 项测试、type-check 和 production build 通过。

下一阶段扩展评测可观测：把黄金任务从 3 个扩到路线图要求的 20 个，增加版本化报告导出、跨运行趋势、p50/p95、失败分类聚合和 429/Runner 崩溃/WebSocket/MCP 协议异常故障注入；仍不开放任意评测脚本。
