# 阶段 89：受限、持久的 Agent Model Loop 最小闭环

阶段 89 把 `research_to_html` 的研究前半段升级为真实的两轮 Model Loop：模型先请求一个服务器冻结的只读研究 Route，服务器校验 binding 和参数后执行受控 Search/Page Reader，把脱敏 Observation 再交给模型；第二轮只能提交候选 Claim，之后仍由独立 Citation Verification 决定是否解锁 Artifact 链。

这已经具备 Codex 式“决定→执行→观察→继续”的核心形状，但是一个刻意缩小的版本：只有研究 Route，最多两个 Model Turn 和一次 Route 执行，没有自由 Shell、动态 argv、任意文件路径、无限循环或模型自行扩权。

## 1. 两轮协议

1. `web_researcher@1.1.0` 的 Turn 1 只能输出 `request_route`，并且 `route_binding_id` 和 `query` 必须与 Handoff 中的 `research.read.v1` 冻结绑定完全一致。
2. 服务器在执行前重算 binding；错误 binding 以 `AGENT_ROUTE_BINDING_REJECTED` 拒绝，不会发出 Search 请求。
3. Search/Page Reader 仍使用现有 SSRF、公网地址、域名和结果数量边界。Observation 只持久 search/page ID 及 digest，普通运行记录不复制网页原文。
4. Turn 2 只能输出 `submit_result`；再次请求 Route 视为 `AGENT_LOOP_NO_PROGRESS`。
5. 模型结果仍是 `candidate`，不能直接标记任务成功。Claim/Citation 经独立验证后，原 verified-edge 才可解锁 HTML/Markdown/PDF 构建与交付。

`web_researcher@1.0.0` 保留供历史精确 Plan 收尾；Registry 对新 Plan 选择语义版本更高的 `1.1.0`。

## 2. 持久化与恢复语义

`0040_durable_agent_model_loop` 新增三类真值：

- `model_dispatch_attempts`：保存 `prepared → dispatching → succeeded/failed/outcome_unknown` 的模型调用边界；
- `agent_decisions`：保存结构化 `request_route` / `submit_result` 和内容摘要；
- `agent_observations`：保存 Route 来源、binding、结果引用和脱敏投影摘要。

Provider 调用之前必须先持久 `prepared`，外部调用开始前改为 `dispatching`。如果租约过期时仍在派发，记为 `outcome_unknown`，不猜测 Provider 是否收到请求。每次读取 Execution Run/Workbench 都会重算 Decision 和 Observation digest；存储篡改以 `AGENT_RUNTIME_PROOF_REJECTED` fail closed。

Workbench 在研究节点中展示两个 Model Turn、决策类型和短证明摘要，但不展示隐式思维过程或网页原文。

## 3. 预算与收敛边界

- 固定最大 2 个 Model Turn、1 次只读 Route；
- 总输入/输出 Token 和费用不得超过 Plan/Handoff 预算；
- 结构错误、binding 越界、无进展和预算超限都是稳定错误；
- 模型没有新增 Tool/Route 权限，它只能选择 Handoff 已授予的精确 binding；
- 这一阶段不处理高风险写入 Loop，现有预览、审批、receipt 和 unknown-effect 边界不变。

## 4. 验收

- 研究 Runtime 共 12 项通过，覆盖两轮成功路径、错误 binding 在 Search 前拒绝、SSRF/DNS/redirect 边界和 Observation 篡改拒绝。
- Registry/Plan/Research/Context/Artifact/Workbench/Phase75/Migration 跨阶段回归全部通过；`0040 → 0039 → 0040` 在独立临时库完成往返。
- 阶段 75 对抗报告仍为 11/11、false-success=0、unauthorized-effect=0。新的 Registry/Prompt cohort 通过不可变 `v2` baseline 链式记录，旧 `v1` 原文保留，`compare` 通过。
- Ruff 全仓、mypy 224 个生产源码、`uv lock --check`、`pip check` 通过。前端 22 个测试文件/152 项、type-check 和 production build 通过。

默认开发 SQLite 未被本阶段验收命令升级；迁移验证使用独立临时数据库。

## 5. 下一步

下一阶段应把同一持久 Loop 骨架推广到第二个既有只读 Workspace Route，并增加 `NeedsUserInput` 的暂停/继续决策，从“研究专用两轮”演进为可复用的受限循环。在这些边界稳定前，不引入任意 Tool Loop、自由 Shell 或模型自主高风险写入。
