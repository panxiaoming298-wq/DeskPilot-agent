# 阶段 69：Task Contract 与 Executable Plan Compiler 最小闭环

## 1. 交付结果

阶段 69 把用户任务意图之后、Agent/能力运行之前的可信边界落成了可执行代码：严格的 `TaskContract` 与不可信 `DraftPlan` 进入纯确定性 `PlanCompiler`，输出绑定精确版本和摘要的不可变 `ExecutablePlan`。Contract 版本和 Plan generation 原子持久化，HTTP 只提供证明复核后的只读投影。

同时声明了 `Conversation/Message/TurnInterpretation/TaskAmendment` 类型，以及 `research.read.v1`、`artifact.html.v1`、`browser.verify.v1` 三个固定 Capability Pack。后三者在本阶段全部 `runtime_enabled=false`；`research_to_html` fixture 可以被完整编译、持久化和检查，但不能执行。

## 2. 关键不变量

- `TaskContract` 版本从 1 连续递增；修订必须精确引用上一版本摘要，旧版本永不覆盖。
- `DraftPlan` 使用 `extra=forbid`，只能提交 Agent/Capability selector、DAG、验收引用和预算，不能携带 approval、凭据、可信摘要、Shell 或 Python 代码。
- Compiler 精确绑定 Agent Contract、Prompt Package、Tool grant 和 Capability Pack 摘要，并校验 DAG、Handoff、风险、隐私外发、分节点预算总和及验收覆盖。
- Plan ID、node ID、node spec digest、binding snapshot digest 和 manifest digest 使用规范 JSON 确定性生成；相同输入与 generation 得到相同结果。
- `task_contract_versions` 和 `task_plan_generations` 保存不可变 manifest；`task_planning_states` 只保存活动指针。Contract 修订、Plan 激活和上一代 supersede 在一个数据库事务内完成。
- 所有读取重新执行 Pydantic、内容摘要和当前 Registry/Catalog 漂移校验；损坏或不一致返回 `409 PLANNING_PROOF_REJECTED`。
- 文本输入只形成类型化 Turn/Contract 候选，不形成执行授权。

## 3. `research_to_html` 计划 fixture

固定计划包含五个显式节点：

1. `research`：只读研究和 Claim/Citation 验收；
2. `build_html`：Task Workspace 中的 HTML Artifact 验收；
3. `browser_verify`：隔离静态页面浏览器验收；
4. `final_acceptance`：确定性安全不变量；
5. `delivery`：只依赖最终验收，不自行证明验收条件。

Contract 同时绑定公开网络外发许可、R1 风险上限、总预算、静态 HTML 输出、Workspace 配额和无登录/断网/禁用 JavaScript 的浏览器验证 profile。由于三个 Capability runtime 均未实现，整个计划明确为不可运行。

## 4. 只读 API

```text
GET /api/v1/capabilities
GET /api/v1/tasks/{task_id}/planning
GET /api/v1/tasks/{task_id}/contract
GET /api/v1/tasks/{task_id}/contracts
GET /api/v1/tasks/{task_id}/plans
GET /api/v1/tasks/{task_id}/plans/{generation}
```

成功响应均为 `Cache-Control: no-store`。阶段 69 不提供 Contract/Plan 公共写 API，避免把任意客户端 Draft 直接接成运行授权。

## 5. 数据库与门禁

- Alembic head：`0030_task_contract_plans`；支持从 `0029_evaluation_traces` 升级、降级和再次升级。
- 专项测试覆盖确定性编译、伪造字段、未知绑定、隐私/风险/预算/验收拒绝、类型化 Turn、版本链、Plan supersede、只读 API 和数据库篡改拒绝。
- `.github/workflows/phase-69-plan-compiler-gate.yml` 在 Windows 上执行专项测试、迁移往返、Ruff、mypy、锁文件与 Alembic head 检查。

## 6. 明确未实现

阶段 69 没有实现 Conversation/Message 持久化与自然语言解释器，没有持久 Agent Invocation/Handoff/Result，没有 Web Search/Page Reader、Artifact Workspace、HTML Builder 或 Browser Verifier，也没有把新 Plan 接入现有 Task Processor 执行。阶段 70 才实现持久 Invocation 和只读联网研究；阶段 71 再完成 Artifact、独立 Verification 和首个真实 `research_to_html` 纵向闭环。
