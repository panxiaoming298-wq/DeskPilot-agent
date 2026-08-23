# 阶段 109：真实校准证据与 Provider Admission

## 1. 本阶段结论

阶段 109 在 Phase 107 校准设施和 Phase 108 每 Turn model-route authority 之间增加默认关闭的生产 admission。一个 `passed` report 或 baseline 摘要不能单独开启 cloud Provider；启动时必须加载同一个不可变 bundle，并完整重放 suite、candidate run、blind packet、独立 Judge run、真人 review、report、baseline 和逐 Agent admission。

```text
默认：无 bundle + allow=false
              │
              ▼
      cloud admissions = empty

显式配置的受信 bundle
              │
              ▼
 strict JSON / duplicate key / size / symlink / CI guard
              │
              ▼
 Phase107 grade 全量重放 + baseline compare
              │
              ▼
 exact Agent/Contract/Prompt + Provider/build/Schema + expiry
              │
              ▼
 Registry freeze availability AND per-Turn route admission
```

本阶段没有真实模型认证结果，也没有提交任何 Fake/recorded 生产 admission。固定测试只在 pytest 临时目录构造合成 evidence，生产默认配置继续得到空 admission Registry。

## 2. 完整证据 bundle

`AgentModelAdmissionBundle` 必须同时携带：

- Phase 107 冻结 suite；
- candidate calibration run；
- blind review packet；
- 独立 Judge run；
- 两名主审及必要仲裁者的 human review bundle；
- calibration report；
- 人工批准 baseline；
- 一至十六条 exact Agent model admission。

Loader 不只验证各对象自带 digest。它会以 report 的 `evaluated_at` 重新执行 `Phase107CalibrationService.grade()`，要求重算 report digest 完全相同，再执行 baseline compare。run、packet、Judge、人类评审、确定性 guard、sample coverage 或 threshold 任一漂移都会使启动失败。

## 3. 每条 admission 的精确绑定

`ApprovedAgentModelAdmission` 绑定：

- Agent ID、版本、Contract digest 与 Prompt Package digest；
- 完整 `ModelProviderDescriptor` 与 snapshot digest；
- candidate build ID、request Schema digest；
- run/report/baseline approval/review bundle digest；
- 批准人、批准时间和失效时间；
- 内容寻址 admission ID 与 admission digest。

Provider 必须是 cloud 且 protocol 不能是 Fake。admission 最长 90 天，不能晚于 human review bundle 的 `valid_until`；批准时间不能早于 report，也不能位于启动检查的未来。Provider 改名、换 model、protocol/capability/location 漂移，或 Agent Contract/Prompt/Build/Schema 任一变化，都需要新的真实 evidence 和 admission。

## 4. 启动与 CI 默认关闭

生产加载同时要求：

```text
DESKPILOT_MODEL_ADMISSION_ALLOW=true
DESKPILOT_MODEL_ADMISSION_BUNDLE_PATH=<reviewed immutable JSON>
```

只设置一个值会触发 Settings 校验错误。CI 环境即使同时设置也会拒绝激活；缺失文件、符号链接、空文件、超过 32 MiB、非法 UTF-8、duplicate JSON key、未知字段或摘要错误均 fail closed。bundle 是受信启动配置，不提供上传 API，也不会从 Conversation、模型输出、MCP、Memory 或网页内容中读取。

普通默认启动不读任何 admission 文件，`admission_count=0`，也不会联网。

## 5. Registry freeze 与逐 Turn 双门

Agent Registry freeze 现在要求某个 Provider 同时满足：

1. Agent Contract 的 location/context/capability；
2. 对 cloud Provider 存在 exact approved admission。

缺任一条件的 Agent 版本会以 `model_requirements_unsatisfied` disabled，不能成为 preferred Agent。即使某版本在启动时通过，Phase 108 的 `validate_model_route()` 仍会在每个 Turn 对实际 Provider snapshot 再验证 admission；过期或替换后的 Provider 不会继续派发。

admission 不能扩大 Agent Contract。现有 Coordinator、Reader、Tester 与 Patch Planner 都是 LOCAL-only；固定测试证明，即使给当前 Patch Planner 构造完整通过的合成 calibration bundle，它在仅有 cloud Provider 时仍保持 disabled。证据批准和权限批准必须同时成立。

## 6. 当前刻意关闭的边界

Phase 107 的默认 request identity 精确对应 `workspace_coordinator@1.1.0` 与 `workspace_patch_planner@1.0.0`，而这两个 Contract 都不允许 cloud。因此 Phase 109 完成的是可验证的 admission 设施，不会让现有版本突然联网。

阶段 110 已把 run/report/baseline 升级为显式候选 Agent identity v2，并让本阶段 admission 直接消费 exact Agent ID/version/Contract/Prompt；旧 v1 仍完整回放。当前仍未注册新的 cloud-only 候选 Contract/Prompt 生命周期。未来启用 cloud 前必须新增默认不可派发的版本并闭合 exact Handoff companion；真实 capture、独立 Judge、两名主审/必要仲裁、批准 baseline 与 admission 全部完成后才能激活。不能在旧版本上原地放宽 location，也不能拿当前合成测试 bundle 当生产证据。详见 [`110-候选Agent身份绑定与校准工件v2.md`](110-候选Agent身份绑定与校准工件v2.md)。

## 7. 固定验收

专项测试覆盖：

- 完整合成 evidence 的 grade replay 与 baseline compare；
- exact Agent/Contract/Prompt/Provider/build/Schema/expiry 正向匹配；
- Provider model、Prompt、build、baseline source 和时间漂移拒绝；
- Fake cloud admission 模型层拒绝；
- allow/path 双开关、CI、duplicate JSON key 和严格文件加载拒绝；
- cloud Contract 同时需要独立 admission 才能在 Registry freeze 中 enabled；
- admission 不能覆盖现有 LOCAL-only Patch Planner Contract；
- 既有本地 Workspace/Research Agent 路径继续通过。

本阶段新增两个生产源码和一个测试文件，修改 Registry/Settings/composition root；没有数据库字段、migration、API 或前端改动。Alembic head 继续为 `0050_agent_graph_test_conditions`。

最终统一后端收集 83 个测试文件 / 610 项，`598 passed + 12 skipped`、首轮退出 0；Ruff 全仓和严格 mypy 246 个生产源码通过。Phase75 v15 保持 11/11、false-success=0、unauthorized-effect=0，compare 无违规，report digest 为 `5c0c2fb35f3bf5fcf28f8e8b521a6592c2ed69c992eb216e7783c67249463a30`。前端 22 个测试文件 / 154 项、type-check 和 production build 通过。Alembic 当前且唯一 head 为 `0050_agent_graph_test_conditions`，无待生成迁移；SQLite `integrity_check=ok`，`pip check`、`uv lock --check` 与 diff whitespace 通过。本阶段没有 migration、真实模型/Judge 调用、真人评审或生产 admission artifact。
