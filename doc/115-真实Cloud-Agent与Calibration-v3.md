# 阶段 115：真实 Cloud Agent 与 Calibration v3

## 1. 当前结论

阶段 115 已完成 115A 代码与 115B 的不可绕过授权/工件准备，但尚未完成真实 Provider capture、独立真人评审和生产激活，因此当前只能形成内部 checkpoint，不能标记为阶段完成，也不能宣称 cloud Agent 已达到生产质量。

本 checkpoint 没有访问模型网络、没有消费费用、没有写入凭据，也没有用 Fake/recorded 证据生成生产 Admission。依据 [ADR-016](ADR-016-115B生产门与116开发纵切解耦.md)，115B 继续阻塞 production cloud activation 和 116C 的真实模型质量结论，但不再阻塞 116A/116B 的 LOCAL-only 运行时开发。

## 2. Release Manifest 与 activation channel

新增 `AgentReleaseManifest`、`AgentReleaseEvent`、`AgentActivationChannel` 和 `AgentReleaseBundle`。一个发布清单固定绑定：

- `builtin.turn_planner@2.0.0`；
- `builtin.workspace_coordinator@2.0.0`；
- `builtin.workspace_patch_planner@2.0.0`；
- 闭合 Handoff 所需的 `workspace_reader@2.0.0` 与 `workspace_tester@2.0.0` companion。

注册、激活、停用、到期、替换和回滚都追加内容寻址事件，并校验 sequence、前序摘要、时间单调性、注册顺序、当前 active source、replacement/rollback target 和最长 90 天有效期。离线命令 `python -m deskpilot.phase115_release_gate` 生成或推进不可变 JSON 工件；输出路径已存在时拒绝覆盖。

运行时仍默认关闭。只有 `agent_release_allow=true + agent_release_bundle_path` 成对出现、工件严格加载、非 CI、active release 未过期时，候选才通过 Release 层。单独出现更高 SemVer、单独提供 Release 或单独提供 Admission 都不会提升 preferred；任一三角色/companion 不满足时整个 release group 以 `release_cohort_unsatisfied` 禁用。

## 3. 不可变 Cloud 候选与任务隐私交集

新增三个 cloud-only 2.0.0 候选和两个 LOCAL companion。旧 1.x Contract/Prompt 不原地修改，既有 Run 继续按 exact identity 收尾。

Plan 绑定不再只取全局最高版本，而是在 active Registry 中与 Task Contract 的 Provider location 和 privacy mode 取交集。LOCAL-only Task 继续绑定稳定本地版本；只有 cloud Task Contract 与用户为当前 Task 选择的允许出站模式同时成立时，才可能绑定 cloud 版本。Turn Planner 的新任务使用 preferred identity，已持久 Run 的恢复仍按原 exact Agent/Contract/Prompt/Provider 重放检查。

Workspace Runtime 已识别 release cohort 的 Coordinator、Reader、Tester 和 Patch Planner 精确版本，但没有扩大工具、路径、Patch 审批或固定测试权限。

## 4. Calibration v3

Calibration v3 使用 `deskpilot.phase115-calibration-run.v3`，并保持 v1/v2 摘要兼容。v3 的 ordered cohort 固定为：

1. Turn Planner；
2. Dynamic Coordinator；
3. Patch Planner。

新 v2 suite 包含真实 Turn Planner request、动态任务图和精确 Patch request，共 4 个版本化 case、每 case 重复 2 次。Turn Planner 样本覆盖 opaque Offer 选择和越权破坏请求拒绝；盲审 packet 的 `task_kind` 增加 `turn_planning`。Judge 仍只能给出盲审意见，最终 grade 强制每个样本两名独立 primary reviewer，并在分歧时要求第三 arbiter。

`phase107_gate capture --artifact-schema-version v3` 默认选择 v3 suite 和三个 2.0.0 identity，要求 `DESKPILOT_PHASE115_LIVE_ALLOW=1`、显式 Provider ID、非 Fake cloud descriptor、strict structured output 和至少 8192 context。CI 无条件拒绝 live capture。

## 5. 三角色 Production Admission 准备

`build_phase115_admission_bundle` 和 `python -m deskpilot.phase115_admission_gate` 会先完整重放 suite/run/packet/Judge/reviews/report，再创建 v3 baseline 和三个 exact Admission。工件强制：

- candidate 为非 Fake cloud Provider/model；
- Judge Provider/model 与 candidate 独立；
- report 为 passed，acceptance 与 Judge-human agreement 均为 100%；
- false accept、安全拒绝和 primary disagreement 均为 0；
- Admission 覆盖 exact 三角色，少一个或多一个都拒绝；
- approved time 不早于 report，validity 不超过真人 review 且最长 90 天；
- Admission 输出本身 `activates_runtime=false`。

生产启用必须同时提供 Release bundle 和 Admission evidence bundle，并分别打开两个显式启动开关。凭据仍只通过既有 Credential Resolver 获取，不进入工件、日志或提交。

## 6. 自动化边界

新增 Phase 115 candidate CI，验证 Release/Calibration/Admission/Registry、旧工件回放、默认后端、immutable evaluation、wheel Prompt、前端回归，并显式证明 CI 不能 capture 或 activate 生产证据。

该 CI 只证明候选生命周期和合成测试正确，不能替代真实 Provider、费用授权或真人评审。真实 capture 工件不得提交原始敏感样本或凭据；生产 baseline/Admission 是否进入受控发布存储，需要在取得用户授权后单独决定。

115A 最终内部门禁已完成：默认后端收集 783 项，最终单进程运行 `771 passed + 12 skipped`、失败/错误为 0；Ruff 与严格 mypy（287 个生产源码）、前端 24 文件 / 165 项、type-check/build、frozen lock、`pip check`、wheel 和 29 个 Prompt resource 均通过。Phase75 追加不可变 v17 后保持 11/11、false-success=0、unauthorized-effect=0，旧 v16 不覆盖；只读过滤五个默认 disabled 的 2.0.0 identity 后，cohort/plan digest 精确回到 v16，证明漂移只来自候选注册表扩展。Windows Evaluation 另追加 v2 延迟基线，旧 v1 保留；阶段 114 HEAD 与当前代码在同机均约为 30.5 秒，故只把 run/case p95 窗口调整为 35/12 秒，success/safety 仍固定 100%。

## 7. 进入真实 115B 前仍需用户明确提供

1. candidate Provider ID 与 model，以及独立 Judge Provider ID 与 model；
2. 本批 calibration 允许的数据分类和出站范围；
3. 总费用上限与单次请求预算；
4. 两名真人主审及必要仲裁人的安排；
5. Admission/Release 有效期和生产 activation actor。

在这些授权缺失时，正确生产状态仍是所有 cloud 候选 disabled、本地稳定版本 preferred，并停在 115A checkpoint；开发状态则转入 `codex/stage-116-dev`，仅推进 116A/116B，不执行 live capture、Production Admission、cloud activation 或 116C 真实模型质量签发。

## 8. 2026-08-29 离线兼容与个人预发布补充

新增 `openai_compatible_responses` adapter，以保守公共子集覆盖 OpenAI、DeepSeek 和阿里云百炼的 `/responses` 请求、strict structured output、语义 SSE 与统一错误模型。三家只完成 MockTransport 离线合约，配置模板均保持 disabled；没有放置凭据、健康探测或真实 capture。协议选择、当前模型/端点和出站费用边界见 [ADR-017](ADR-017-Responses多Provider兼容与个人预发布门.md)。

Calibration v3 另增加严格的单人 `personal_preview` 路径：同一 operator 必须复核全部样本，Judge-human agreement 与 acceptance 为 100%，只允许 `public_synthetic` 数据，最长 14 天且 `activates_runtime=false`。它解决个人开发者不便组织双人评审的问题，但不满足本文件第 5 节 Production Admission，也不改变第 7 节的生产授权清单。

生产规则仍是两名独立真人 primary reviewer；第三名 arbiter 只在两人分歧时需要，而不是每次固定三人。Production builder 会显式拒绝个人预发布 review/report，旧 v1 生产工件的规范化序列化与 digest 保持不变。
