# 阶段 108：每 Turn Agent 模型路由裁决

## 1. 本阶段结论

阶段 108 把 Agent Registry 的“启动时存在至少一个可用模型”提升为每个 Agent Model Turn 的实际派发授权。Model Gateway 仍负责从健康度、角色、隐私、能力和费用候选中选择 Provider；但它的选择不再自动等于 Agent 有权使用该 Provider。每次派发都必须重新通过精确 Agent/Contract/Prompt、请求语义、Provider snapshot 和节点预算裁决。

```text
Handoff 中的 exact AgentRef
          │
          ▼
Registry resolve_exact + 冻结 Prompt 渲染
          │
          ▼
节点预算检查 ── Gateway 选择候选 Provider
                         │
                         ▼
             Agent Contract Route admission
                         │
                         ▼
               持久 prepared Model Turn
                         │
                         ▼
             Context / Memory / Compaction
                         │
                         ▼
     同一 Provider snapshot + Prompt + Contract + 预算复验
                         │
                         ▼
                    Provider call
```

因此，Registry freeze 是可用性证明，逐 Turn admission 才是运行时授权；二者不再混用。

## 2. 精确 Agent 与 Prompt 绑定

`bind_agent_model_request()` 是统一的纯请求绑定器。它从服务器冻结的 `AgentRegistration` 写入：

- Agent ID 与版本；
- Agent Contract digest；
- Prompt Package digest；
- Prompt Package 的完整 instruction，作为首条 system message。

Runtime 不再信任各业务调用点手写的 system prompt。Handoff 中的 `BoundAgentRef` 必须通过 `resolve_exact()` 同时匹配 Agent ID、版本、Contract digest 和 Prompt digest，且该版本仍为 enabled；随后统一绑定器才渲染冻结 instruction。Phase 107 calibration capture 也使用同一个绑定器，因此候选 request digest 会随 Contract 或 Prompt 漂移而失效。

`validate_model_route()` 会复验 metadata 中的四个身份字段，并要求首条消息仍是与冻结 Prompt Package 逐字相等的 system instruction。Context、Memory 或 Compaction 若替换该指令、删除 digest 或伪装另一个 Agent 版本，均在 Provider 调用前 fail closed。

## 3. 请求语义不得弱化 Contract

每个实际 `ModelRequest` 必须满足目标 Agent 的 `AgentModelPolicy`：

- `role` 与 Contract 精确一致；
- `privacy_mode` 位于允许集合；
- structured output、strict JSON Schema、streaming、tool calling、parallel tool calls、vision 等必需能力不能从请求中降级；
- 请求的最小 context tokens 不得低于 Contract；
- strict 输出 Schema 必须与 Agent Contract 的输出 Schema 完全相等。

允许请求声明比 Contract 更强的能力要求，因为这只会缩小候选 Provider；不允许弱化 Contract 后再依赖 Gateway 偶然选择一个较强模型。

## 4. Provider 选择不是授权

Gateway 选择出的 `ModelProviderDescriptor` 还必须逐 Turn 满足 Agent Contract：

- Provider location 位于 `allowed_locations`；
- Provider 的 structured/strict/tool/parallel/vision/streaming 能力满足 Contract；
- Provider 最大 context window 达到 Contract 下限。

这堵住了一个原有缺口：LOCAL-only 的 Coordinator 或 Patch Planner 即使 Registry freeze 时存在本地模型，也不能因为 Gateway 默认 Provider、角色路由或运行态排序变化而被派发到 cloud Provider。Provider hint 同样只是候选指示，不能绕过 Contract。

阶段 108 没有为任何现有 Agent 扩大 location 或 privacy。Coordinator、Workspace Reader/Tester 与 Patch Planner 的既有本地边界保持不变。

## 5. Context 前后双重裁决

第一次裁决发生在写入 `prepared` Model Turn 之前，非法 cloud/privacy/role/schema/capability/budget 路由不会创建派发状态，也不会调用 Provider。

Context Memory Runtime 可能合法插入长期记忆或 compaction message，因此系统在 Context 完成后执行第二次裁决：

- Gateway 必须仍选择与第一次完全相等的 Provider descriptor snapshot；
- 首条冻结 Prompt、Agent/Contract/Prompt identity、privacy、role、Schema 和能力要求必须仍合法；
- 请求预算必须仍位于 Handoff 节点分配内。

第二次失败会把已经 prepared 的 Turn/attempt 持久标记为 `failed`，稳定错误码为 `AGENT_MODEL_ROUTE_REJECTED`；不会进入 `dispatching`，也不会触发 Provider。这样既保留审计，又不把 Context 扩展误当成新的路由授权。

## 6. 节点预算是派发硬边界

派发前会把请求与 `HandoffEnvelope.budget_allocation` 对照：

- `max_output_tokens` 不得超过节点输出额度；
- timeout 不得超过节点 wall-time；
- `max_attempts` 必须显式存在，且不得超过 `retries + 1`；
- `max_task_cost_micros` 必须显式存在，且不得超过节点费用额度。

业务 Runtime 在响应后仍累计真实 input/output/cost；阶段 108 增加的是 Provider 调用前的声明上限，避免调用点通过增大输出、重试、超时或费用上限绕过已绑定 Plan。

## 7. 与 Phase 107 的边界

Phase 107 提供真实 candidate/Judge/human cohort 的证据设施；Phase 108 提供生产派发时的 exact route authority。两者职责不同：

- 校准报告不能代替 Agent Contract；
- Registry 或 Gateway 中出现一个 cloud Provider 不能代替通过的校准报告；
- Judge/human accept 不会修改当前 Agent 的 allowed location/privacy；
- 当前没有真实批准 baseline，因此本阶段不会开放 cloud Agent 版本。

后续若要启用真实 Provider，应新增不可变 Agent Contract/Prompt 版本，并把通过且未过期的 Phase 107 cohort、精确 Provider snapshot、Prompt/Schema/build digest 绑定成启动与派发 admission；不能在原版本上原地放宽 location，也不能只按 Provider ID 或模型别名放行。

## 8. 固定验收

专项测试覆盖：

- 合法 LOCAL Patch Planner request/Provider 的正向 admission；
- cloud Provider 对 LOCAL-only Agent 的拒绝；
- privacy、role、Prompt instruction、Contract capability 和 strict Schema 漂移拒绝；
- 节点 output/retry/timeout/cost 声明越界时 Provider 零调用；
- Context 扩展后偷换 privacy 时保留失败审计、零 dispatching、Provider 零调用；
- 既有 Workspace Reader、Coordinator、动态 Patch、可组合 Patch 与研究路径继续通过；
- Phase 107 capture/盲审/Judge-human 固定门禁继续通过。

本阶段只修改应用层 Runtime、Registry、共享请求绑定器和固定测试；没有新增数据库字段、migration、API 或前端交互。Alembic head 继续为 `0050_agent_graph_test_conditions`。

最终统一后端收集 82 个测试文件 / 606 项，`594 passed + 12 skipped`、首轮退出 0；Ruff 全仓和严格 mypy 244 个生产源码通过。Phase75 v15 保持 11/11、false-success=0、unauthorized-effect=0，compare 无违规，report digest 为 `5c0c2fb35f3bf5fcf28f8e8b521a6592c2ed69c992eb216e7783c67249463a30`。前端 22 个测试文件 / 154 项、type-check 和 production build 通过。Alembic 当前且唯一 head 为 `0050_agent_graph_test_conditions`，无待生成迁移；SQLite `integrity_check=ok`，`pip check`、`uv lock --check` 与 diff whitespace 通过。本阶段没有 migration，也没有调用真实外部模型、Judge 或真人评审。
