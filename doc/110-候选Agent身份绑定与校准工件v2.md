# 阶段 110：候选 Agent 身份绑定与校准工件 v2

## 1. 本阶段结论

阶段 110 修复了 Phase 107/109 之间最后一个隐式假设：校准证据不再把 Coordinator `1.1.0` 与 Patch Planner `1.0.0` 只写死在 Python 实现里。新的 run/report/baseline v2 会显式保存两个候选 Agent 的 ID、版本、Contract digest、Prompt Package digest 与输出 Schema digest；重放时必须从当前受信代码 Registry 解析同一精确版本并逐项一致，才能继续 Judge、grade、baseline compare 或 production admission。

```text
capture --coordinator-version X --patch-version Y
                         │
                         ▼
  Registry 精确解析 X/Y + 校验 harness 输出 Schema
                         │
                         ▼
 run v2: ordered calibrated_agents + cohort/run digest
                         │
            ┌────────────┴────────────┐
            ▼                         ▼
 report v2 / baseline v2       每个 trial 的 request digest
            │                         │
            └────────────┬────────────┘
                         ▼
      Phase 109 全量回放 + exact admission identity
```

本阶段仍没有执行真实外部模型 capture、独立 Judge、真人评审或签发 production admission。校准专用解析策略只存在于 `Phase107CalibrationService` 的候选解析过程中，不进入应用 composition root，也不授予任何 Runtime route、Capability 或写权限。

## 2. v2 候选身份

`Phase107CalibratedAgentIdentity` 为每个候选保存：

- 固定角色：`dynamic_coordinator` 或 `patch_planner`；
- exact Agent ID 与 SemVer；
- Agent Contract digest；
- Prompt Package digest；
- Contract output Schema digest。

run v2 必须恰好以 Coordinator、Patch Planner 的顺序保存两项，不能遗漏、重复角色或交换顺序。该列表进入 cohort digest、run ID 与 run digest；report v2 原样继承并进入 report digest，baseline v2 也必须批准完全相同的 ordered identity。baseline compare 新增 `CALIBRATED_AGENT_DRIFT`，因此即使质量指标未回退，Agent、版本、Contract、Prompt 或 Schema 任一变化也需要重新 capture、评审和批准。

旧的独立 `coordinator_prompt_digest`、`patch_prompt_digest` 与组合 request Schema digest 继续保留，既用于旧工件兼容，也让审阅者能直接判断 Prompt/请求协议漂移；v2 identity 不是用来替代这些证明，而是补齐“这些摘要属于哪个 Agent 版本”。

## 3. 显式 capture 与零调用拒绝

capture CLI 新增两个显式参数：

```powershell
.\.venv\Scripts\python.exe -m deskpilot.phase107_gate capture `
  --provider-id <candidate-provider> `
  --build-id <immutable-build-id> `
  --coordinator-version <exact-semver> `
  --patch-version <exact-semver> `
  --output artifacts/phase107/candidate-run.json
```

默认值仍为当前 harness 兼容的 Coordinator `1.1.0` 与 Patch Planner `1.0.0`。capture 在任何 Provider 调用前完成以下检查：

1. exact Agent 版本必须已登记在当前受信 builtin Registry；
2. Coordinator Contract 的 output Schema 必须等于 `DynamicCoordinatorLoopDecision`；
3. Patch Contract 的 output Schema 必须等于 `WorkspacePatchLoopDecision`；
4. Prompt/Contract identity 必须能生成与 production request builder 同源的绑定请求。

不存在的版本或把旧 Coordinator `1.0.0` 错当动态图候选都会稳定拒绝，并保持 Provider 调用次数为零。CLI 输出会投影候选 ID、版本、Contract 与 Prompt digest，便于人工核对 capture 是否针对预期代码 cohort。

## 4. 全量回放与 Admission

`make_blind_packet()`、`judge()` 与 `grade()` 不信任 run 中自报的 identity。每次重放都会：

- 从 run v2 的两个 exact version 重新解析 Registry；
- 重新计算实际 Contract、Prompt 和 output Schema identity；
- 要求它们与 `calibrated_agents` 完全相同；
- 使用解析后的真实 Prompt 重建每个 trial 的 ModelRequest；
- 重算 request digest 与确定性判断。

Phase 109 admission 在完整 grade replay 成功后，直接从 v2 run 建立可批准 Agent 集。admission 的 Agent ID/version/Contract/Prompt 只要不在该集合，或 run identity 已与当前代码漂移，就会在 production Registry 加载前失败。Provider、build、request Schema、report、baseline、review 与有效期的原有 exact binding 继续同时成立。

capture 中的 permissive candidate policy 只帮助离线 harness 解析一个明确指定的代码版本；生产 Registry 仍使用默认空 admission 或受信 bundle。当前两个版本的 Contract 都是 LOCAL-only，因此即使固定测试构造出合成 cloud admission，它们也不会被扩权为 cloud route，更不会因一次 capture 成为 preferred cloud Agent。

## 5. v1 摘要兼容

run/report/baseline 模型同时接受 v1 与 v2：

- v1 的 `calibrated_agents` 语义必须为空，旧 JSON 可以完全没有该字段；
- 解析旧 JSON 时该字段只在内存中取空 tuple 默认值；
- 校验 v1 run/report/approval digest 时显式排除新增默认字段；
- v1 cohort/run ID 的材料保持原格式；
- v1 回放继续使用当时冻结的默认版本 `1.1.0`/`1.0.0`，并通过 trial request digest 检测代码漂移；
- Phase 109 对 v1 bundle 走受控 fallback，从当前 Registry 重建旧默认身份后再做完整 grade 与 admission 校验。

固定测试从 v1 capture 开始，走完 blind packet、独立 Judge、双真人 review、grade、v1 baseline compare 与 Phase 109 admission，证明不是只放宽 Pydantic 解析而跳过证据重放。新 capture 默认只生成 v2；v1 参数仅由测试用于摘要兼容证明，没有在 CLI 暴露降级开关。

## 6. 安全边界

- calibration identity 证明“测了哪段受信 Agent 代码”，不证明模型总体正确，也不授予执行权限；
- Contract 仍是权限上限，admission 不能扩大 location、privacy、Context、Tool、Handoff 或预算；
- Judge/human accept 仍不能替代用户 Patch confirmation 或服务器 verified edge；
- 不存在/不兼容候选在 Provider 零调用前拒绝；
- v2 identity、trial request、report、baseline 或 admission 任一漂移均 fail closed；
- 当前仓库仍不包含真实 baseline、真人身份资料、外部 response ID 原文或生产 admission bundle。

## 7. 固定验收范围

新增固定测试覆盖：

- v2 run 的 ordered Coordinator/Patch identity 与显式版本；
- candidate identity 进入 report/baseline compare，Contract 漂移产生 `CALIBRATED_AGENT_DRIFT`；
- v1 run/report/baseline 的旧摘要格式和完整 replay；
- v1 admission bundle 继续可完整重放；
- v2 run identity 漂移在 admission 前拒绝；
- 未登记版本和 Schema 不兼容版本均在 Provider 零调用前拒绝；
- CLI 精确传递两个候选版本；
- 当前 LOCAL-only Contract 即使拥有合成 evidence 也保持 cloud disabled。

本阶段没有新增 migration、API、Settings、前端协议或依赖。checkpoint 的最终统一后端收集 83 个测试文件 / 615 项，`603 passed + 12 skipped`、统一退出 0，耗时 2328.01 秒；Phase 107/109/Registry 定向 26 项通过。Ruff 全仓、严格 mypy 249 个源码通过。Phase75 v15 保持 11/11、false-success=0、unauthorized-effect=0，compare 无违规，report digest 仍为 `5c0c2fb35f3bf5fcf28f8e8b521a6592c2ed69c992eb216e7783c67249463a30`。前端 22 个测试文件 / 154 项、type-check 和 production build 通过。Alembic current 且唯一 head 为 `0050_agent_graph_test_conditions`，default/fresh SQLite upgrade/check、`integrity_check=ok`、foreign-key 零违规；`pip check`、frozen `uv` 同步、Prompt wheel 和 diff whitespace 通过。

## 8. 阶段 77～110 checkpoint

2026-08-24 在 `codex/stage-110` 对阶段 77～110 做了独立的全量 checkpoint。最初冻结的 151 个路径全部纳入审阅；门禁期间增加 fail-closed downgrade guard、并发 claim/fencing 修复、PostgreSQL 确定性计划夹具、阶段 110 汇总 CI 和当时的阶段 111～116 路线文档（现已扩展并更名为阶段 111～117 路线）后，最终范围为 159 个路径（51 个已跟踪修改、108 个新增）。阶段 80 引用的 `frontend/src/components/WorkspaceApproval.preview.html` 明确纳入；真实 `.env`、数据库、缓存、JUnit/Vitest 报告、wheel 和构建产物均排除。

迁移 `0037`～`0050` 的 downgrade 会在存在不可安全降级的数据时 fail closed。Agent 执行、Model Loop、Workspace、Patch 和 Supervisor 统一数据库锁顺序并在副作用前复验 exact active lease/fence/attempt；lease 重试耗尽会终结当前调用、父图与路由，清除并 fence 全部未完成兄弟节点，旧 worker 不能再写入。Patch confirmation 使用 CAS，PostgreSQL 计划测试通过固定数据复位与统计信息取得确定性，没有改 baseline、隐藏重试或放宽安全阈值。

完整门禁除上节结果外，还包括 Evaluation digest `269b901f906781a5cece78967a5246d428214c9499e68d1b4d5afc06cc72227f`、16 份 Phase75 baseline 的比较前后 SHA-256 完全一致、wheel 内 22/22 Agent Prompt JSON/TXT，以及专用 `deskpilot_test` 的 11 项真实 PostgreSQL 门禁（含固定容器重启）和临时 RabbitMQ 的 1 项真实 Broker 门禁。PostgreSQL 已恢复原 stopped 状态，临时 Broker 已移除，凭据未输出。汇总 CI 在托管环境对默认 615 项和精确 12 个条件 skip 做检查；本地 Workflow YAML/脚本预检、敏感信息/产物扫描和 whitespace 门禁通过。本 checkpoint 不包含真实 cloud capture、生产凭据或 push，并随本地中文提交 `完成阶段77至110全量门禁检查点` 固化。

## 9. 下一步

从 cloud 启用链的技术依赖看，仍需要新的、不可原地修改旧 Contract/Prompt 的 Agent 版本，并解决其 exact Handoff companion 版本关系。该版本必须在默认 Registry 中不可派发，只有 exact live-model/Judge-human evidence 与 Phase 109 admission 同时存在时才能启用；不能仅因 SemVer 更高而被 `resolve_preferred()` 选中。

但阶段 109/110 已把生产启用安全链推进得明显快于通用任务能力。2026-08-24 路线复核后，项目下一实现优先级调整为阶段 111“模型驱动的通用任务提案 + 服务器 Capability Offer”，随后建设通用执行/验证/修复循环和 Codex 类安全编码工具包。现有确定性 Router 保留为安全回退，模型 proposal 不授予 Capability。

Cloud 候选生命周期现在固定为阶段 115：先实现独立 Release Manifest、显式 activation channel 和三角色 Calibration v3 Schema，再在用户明确选择 Provider、数据出站范围、费用上限与评审安排后完成真实 Provider/Judge capture、真人评审和 production Admission。合成证据只能形成内部 checkpoint；没有真实 Admission 时所有候选仍为 disabled，本地稳定版本保持 preferred，且不得把阶段 115 标记为完成。阶段 116 改为 Codex 类真实仓库长循环，原 LOCAL-only Edge/记事本能力顺延为阶段 117。详细顺序见根目录 `项目进度.md` 与 `doc/111-117-通用多Agent与Codex纵切实施路线.md`。
