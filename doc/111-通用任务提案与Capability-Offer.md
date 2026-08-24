# 阶段 111：通用任务提案与 Capability Offer

## 1. 当前结论与状态

阶段 111 在阶段 77～110 checkpoint 之上增加“无权限的模型提案层”：15 条已存在的确定性 Route 仍是优先快速路径；只有确定性规则无法路由时，Workbench 才允许 LOCAL-only `builtin.turn_planner@1.0.0` 对服务器预先生成的 opaque Capability Offer 做一次解释。模型不能创建 Capability、Agent、命令、路径、Provider 或授权事实，最终 Route、Task Contract 和 Executable Plan 仍由服务器的 trusted recipe 生成并逐项复核。

```text
持久化用户 Turn
       │
       ├─ 15 条确定性 Route 命中 ──> 旧 v1 manifest / 行为不变，模型零调用
       │
       └─ 无法路由
              │
              ▼
    服务器预编译 Capability Offers
              │
              ▼
     独立 TurnPlannerRuntime 单次派发
              │
      ┌───────┼───────────┐
      ▼       ▼           ▼
   单步骤   多步骤       追问/不支持/失败
      │       │           │
      ▼       ▼           ▼
服务器绑定  保存 deferred  保存证明并保持
可信 Plan   证明，交 112   安全回退/稳定终止
```

截至 2026-08-24，本阶段代码、迁移、前端投影和 CI 已完成，并取得默认后端、Phase75、前端、SQLite/PostgreSQL/RabbitMQ、wheel 与静态门禁的完整退出 0。阶段提交仍保持 LOCAL-only，不执行真实 Provider/Judge capture，也不扩大任何 Capability 或审批权限。

## 2. 确定性兼容与 Route Recipe

`RouteRecipeCatalog` 将 Route 的服务器配方版本化：

- 现有 15 条确定性 Route 命中后完全跳过 Planner，继续使用阶段 110 已接受的 v1 route manifest digest；
- Planner 只能选择服务器提供的 v2 recipe，不能提交自定义 `route_id`、Capability 集合或执行图；
- recipe 负责参数 Schema、Task Contract 输入、exact Agent/Prompt、Capability 和计划模板之间的映射；
- 历史 v1 manifest 继续按原材料校验，修正新映射时新增 recipe 版本，不覆盖旧摘要；
- Planner 失败时保留原确定性 `unsupported` 候选及其 digest，不通过模型结果改写历史 Route 证明。

规则优先不是性能提示，而是兼容和权限边界：已能确定路由的 Turn 不得因为 Provider 状态或模型输出而改变旧行为。

## 3. 服务器 Capability Offer

每个 Offer 都由服务器创建并持久化，模型输入只看到 opaque `offer_key`、意图标签和有界参数说明。受信 Offer 自身至少绑定：

- exact Task Contract 与输入摘要；
- 实际执行 Agent 的 ordered identity、Contract/Prompt digest 和 location/privacy 限制；
- 服务器预编译的 generation 1 `ExecutablePlan` 及其 plan/binding digest；
- Capability binding、Provider configuration snapshot、Policy snapshot 与预算；
- trusted Route recipe、参数 Schema、用户消息 ID/digest 和 Offer digest。

Offer 的 `expected_plan` 在模型调用前由纯内存 compiler 生成。单步骤提案激活计划后，持久 Plan 必须与该 expected plan 完全相同；Registry、Prompt、Contract、Provider、recipe 或计划 binding 任一漂移都拒绝绑定。Planner Agent 本身只负责解释 Turn，不得被误当成后续执行 Agent。

模型参数必须逐字对应已持久化用户消息中的连续文本。服务器会重新执行 recipe 参数绑定；模型生成或改写的路径、URL、命令、凭据、环境变量和权限声明均不能进入计划。

## 4. Planner 决策与稳定终止

Turn Planner 的输出 Schema 只接受三类结果：

1. `propose_steps`：引用 1～8 个不同 Offer，并为每个参数给出用户消息中的原文片段；
2. `needs_input`：保存明确缺失输入，但不激活 Route/Plan；
3. `unsupported`：保存服务器裁决后的不支持证明。

阶段 111 只激活恰好一个步骤。多步骤结果保存为 `MULTI_STEP_PLAN_DEFERRED`，等待阶段 112 的版本化多步骤 Planner，既不截断成第一个步骤，也不静默执行。unknown Offer、重复 Offer、Schema 错误、参数不匹配、Provider 不可用、timeout、取消、绑定漂移或 outcome unknown 都形成显式失败证明；同一持久 Run 不自动重放 Provider 调用。

准备阶段使用受信 Provider 配置快照冻结候选身份，实际 dispatch 仍按实时健康、熔断和预算裁决。这样临时 circuit-open 不会让 Task 在 Run 创建前留下无法解释的半状态；若 dispatch 时 Provider 已不可用，则终结为 `PLANNER_PROVIDER_UNAVAILABLE`，保持零自动重试。

## 5. `0051` 持久证明与恢复

Alembic `0051_turn_planning_offers` 新增四类不可混用的真值：

- `turn_planning_offers`：服务器 Offer 与 exact binding；
- `turn_planner_runs`：请求、Provider/Planner identity、claim/lease、终态和失败证明；
- `turn_planner_adjudications`：服务器对模型 decision 的裁决；
- `turn_plan_bindings`：单步骤 Offer 到受信 Plan/Route 的绑定。

`turn_routes` 同时增加可空 Planner provenance。Run 使用完整稳定请求身份计算 `reservation_digest` 和内容寻址 `run_id`；Route 在准备事务中保存 Run/reservation 锚点，终态成功时再保存 adjudication、binding 与完整 provenance。这样删除或丢失 Run/Adjudication/Binding 不能把“曾经规划过”伪装成“从未规划”，读取与推进都必须 fail closed。

claim、lease、revision 和 compare-and-swap 防止两个 Workbench worker 同时派发同一 Run。读取接口不修复数据库；唯一允许的成功单步骤 crash gap 由显式协调/推进路径恢复绑定，GET 不产生写入。迁移 downgrade 在存在阶段 111 数据或 provenance 时拒绝丢失式降级。

全量门禁还暴露并修复了一个阶段 110 锁序加强后遗留的并行 Run 状态竞态：一个兄弟提交结果后，若同 Run 仍存在 `ready`、`claimed` 或 `running` 节点，Run 保持 `active`；最后一个可执行兄弟到达验证边界后才转为 `awaiting_verification`。提交仍要求 exact Run/Node/Invocation lineage、当前 attempt、owner/fence、有效 lease 和 `running` 节点，所有终止态继续拒绝。Phase75 并行 harness 同时改为等待所有 submit 分支 settle 后再按固定顺序抛首错，并在 `finally` 释放 SQLite engine，避免原始异常被 Windows 文件占用错误覆盖。

## 6. Workbench API 与最小公开投影

现有写 API 保持兼容，新增无请求体入口：

```text
POST /api/v1/tasks/{task_id}/workbench:interpret-turn
```

Workbench 增加：

- `interpreting` stage；
- `interpret_turn` action；
- 可空 `turn_planning` 摘要投影。

公开投影只应提供状态、Offer 数量、opaque proof digest、裁决结果、稳定 reason/failure code 和必要时间信息。它不得返回原始 Offer manifest、用户参数值、模型 response/proposal manifest、claim owner、Provider 私有配置、Planner/执行 Agent 内部 binding 或完整 expected plan。内部 Runtime 证明与公开 Workbench DTO 必须分离；前端只用脱敏摘要展示“正在解释、需输入、已绑定、已 deferred 或失败”。

## 7. 安全边界

- `builtin.turn_planner@1.0.0` 是 LOCAL-only；阶段 111 不新增 cloud Agent activation，也不执行真实 Provider/Judge capture。
- 模型选择 Offer 不等于获得 Capability；Contract、Policy、Approval、预算、Workspace、Runner 和 verified edge 继续分别生效。
- Planner 不依赖既有 Plan/Invocation，也不调用 Agent Model Loop 的递归入口。
- 任意写能力继续复用现有 staging、manifest、精确确认和回执；本阶段不开放任意 Shell、动态 executable/argv、联网安装、删除/覆盖或登录态浏览器。
- 模型输出、UI 点击、Judge 结果、Memory、Summary、MCP 文本和外部页面内容都不能作为授权或正确性证明。
- 错误路径不得通过删除失败证明、放宽 baseline、隐藏重试或回退到更大权限继续执行。

## 8. 验收与 CI

新增 `.github/workflows/phase-111-turn-planner-gate.yml`，以阶段 110 checkpoint 为防回退底线，并增加阶段 111 专项：

- frozen `uv`、`pip check`、Ruff 与严格 mypy；
- 阶段 111 当前树的唯一 head `0051_turn_planning_offers`，upgrade/current/check、SQLite integrity/foreign-key；长期 CI 要求始终只有一个当前 head、0051 保持在其祖先链，并让 SQLite version 与动态 head 完全相同；
- Evaluation 与 Phase75 compare，golden 加 Phase75 v1～v16 共 17 份不可变 baseline 比较前后 SHA-256 不变；
- wheel 构建并要求 `turn_planner.json/.txt` 与全部 Prompt 资源均已打包；当前源码目标集合为 24 个 JSON/TXT 资源；
- Route Recipe、Planner persistence/runtime、Workbench 纵切专项测试；
- 默认后端全量及精确 11 项 PostgreSQL + 1 项 RabbitMQ 条件 skip 身份；
- 前端 Vitest、type-check 与 production build；
- Workflow/diff whitespace 检查。

最终结果：后端 87 个测试文件 / 653 项，单次完整运行 `641 passed + 12 skipped`、退出 0，耗时 2540.26 秒；12 个 skip 的身份精确等于 PostgreSQL 11 项与 RabbitMQ 1 项。阶段 111 专项 35/35、Phase75 文件 6/6、Ruff 全仓和严格 mypy 253 个源码通过。Alembic 当前且唯一 head 为 `0051_turn_planning_offers`，default/fresh SQLite upgrade/current/check、integrity 与 foreign-key 均通过。wheel 内 Prompt 为 24/24；前端精确 22 文件 / 155 项、type-check 和 production build 通过。

Phase75 保持 11/11、false-success=0、unauthorized-effect=0、precision/recall=1.0。经人工确认后只追加不可变 v16，未修改 v1～v15；v16 绑定 v15 approval digest，report digest=`ea488c2b74c94845e6718c86f8dc5cfc73bc590eade001dfaea3a0a548d2f82c`，plan digest=`a730d07312a95a44ccf8430296c749f3599cb6ef9b53c08cb9c63b97054dd649`，cohort digest=`88553a0bd9927b8b6bd706a5477f614b45491748cbfad6fc37bd4904195c67b0`。真实 PostgreSQL 11/11（专用 `deskpilot_test`、含固定容器重启）和临时 RabbitMQ 1/1 通过；PostgreSQL 恢复原 `exited` 状态，临时 Broker 零残留，凭据未输出。

## 9. 阶段 112 续接边界

阶段 112 从阶段 111 已通过的中文本地提交建立独立分支。它会消费阶段 111 已保存的 1～8 步 Offer proposal，新增版本化 `model_planner` DraftPlan 和通用 `Observe → Plan → Execute → Verify → Repair` 循环；不得通过修改阶段 111 的历史 adjudication 或把 deferred proposal 直接视为已授权计划来实现。
