# 阶段 107：Live Model 与 Judge-Human 校准门禁

## 1. 本阶段结论

阶段 107 为阶段 106 的动态图 Coordinator 与 Patch Planner 增加了一个默认不联网、显式启用、可内容寻址审计的生产前校准闭环。它冻结候选模型请求、Provider/模型快照、Prompt、输出 Schema、构建版本、盲审包、独立 Judge 结果和真人评审，再由确定性门禁统一计算质量与安全指标。

本阶段完成的是校准设施和离线固定测试，不是一次真实外部模型认证。当前仓库没有自动调用任何云端或本地 live model，也没有伪造真人评审或签发 live baseline；在取得真实候选 Provider、不同 Judge Provider/模型和独立评审人结果前，不能声称生产模型已经通过校准。

```text
冻结 suite + build + Provider/model + production request
                         │
                         ▼
                 candidate run（8 samples）
                    │              │
           deterministic guard     └── blinded packet
                    │                         │
                    │               independent Judge
                    │                         │
                    └──────── two human primaries
                                      │ disagreement only
                                      ▼
                              independent arbiter
                                      │
                                      ▼
                         immutable report / baseline compare
```

Judge 和人类评审都只评价候选提案，不能创建 Capability、确认 Patch、写 Workspace 或替代服务器条件边。确定性拒绝始终保留；人类或 Judge 的“accept”不能把非法提案提升为可执行计划。

## 2. 精确复用生产 ModelRequest

`agent_model_requests.py` 提取了生产 Runtime 原有的两个纯请求构造器：

- `build_dynamic_coordinator_model_request`；
- `build_patch_planner_model_request`。

`WorkspaceAgentRuntime` 和 Phase 107 capture 共用这些构造器，因此 Coordinator/Patch Planner 的 system/user message、strict JSON Schema、角色、预算、metadata 和 `max_attempts=1` 保持同源。校准直接调用显式选定的 Provider adapter，避免路由切换、fallback 或隐藏 retry 污染 cohort；每个 repeat 都是独立样本，而不是一次失败后的隐式重试。

候选 run 同时绑定：

- suite/harness/build ID；
- 完整脱敏 `ModelProviderDescriptor` 及其 digest；
- Coordinator/Patch Prompt digest；
- 两类输出 Schema digest；
- started time、case/ordinal/sample 关系与逐 trial request/response/output digest。

自阶段 110 起，新 capture 默认生成 run v2，并额外显式绑定 Coordinator/Patch Planner 的 exact Agent ID、版本、Contract、Prompt 与 output Schema identity；旧 v1 工件仍按原摘要材料回放。详见 [`110-候选Agent身份绑定与校准工件v2.md`](110-候选Agent身份绑定与校准工件v2.md)。

`run_id`、`sample_id`、cohort digest 和 run digest 都能从保存材料重算。原生 Provider response ID 只保存 SHA-256，不把外部标识原文写入 artifact。

## 3. 冻结样本集

`phase107-live-agent-calibration-suite.v1.json` 固定 4 个 case，每个重复 2 次，总计且最多 8 次候选模型调用：

1. 单 Patch 槽位的 Coordinator 图提案；
2. 两个新鲜 `patch_slot_n` 的组合 Coordinator 图提案；
3. Python 单文件、单精确替换建议；
4. Node 单文件、单精确替换建议。

确定性检查会验证图 Capability 集、Patch binding 完整且不重复、Patch 链的服务器条件边、非 Patch 节点不得夹带 binding，以及 Patch 的 exact binding、Observation、path、old/new text。模型输出解析失败、身份错配或 Provider 异常使用稳定错误码记录；非法输出可以进入审计 artifact，但不能获得通过状态。

## 4. 盲审与独立 Judge

review packet 只包含随机化 sample 顺序、任务类型、受控输入投影和候选结构化输出，不包含候选 Provider ID、模型名、case ID 或隐藏 expected answer。packet 会重新绑定 candidate run/suite digest。

Judge 使用另一份 Provider/model descriptor 快照和独立的 strict Schema。Judge 请求只接收 blind sample、rubric version 与服务器固定 Judge prompt，不接收候选 Provider、case ID、expected output 或真人结论。与候选完全相同的 Provider/model snapshot 会被拒绝。

Judge 只计算辅助一致性信号：

- Judge-human agreement；
- Judge needs-review；
- Judge false accept。

Judge 不能替代真人评审；Judge run 缺失、错误或与人类不一致时，报告最多进入 `needs_review`，Judge false accept 则直接失败。

## 5. 真人评审与仲裁

每个 sample 必须有恰好两名不同 `reviewer_ref` 的 primary reviewer。两人 verdict 一致时禁止加入不必要的 arbiter；不一致时必须由第三名、且与两位 primary 均不同的 arbiter 给出最终 verdict。缺评、重复评审者、未来时间、过期评审和超过 90 天有效期都会 fail closed。

冻结 rubric 包含：

- task correctness；
- minimal change；
- safety boundary respected；
- evidence sufficient；
- accept/reject/needs_review 与受控 reason codes。

自由文本评论不进入普通日志；如评审系统保留受控评论，只在判断 artifact 中携带 `controlled_comment_digest`。

## 6. 报告与不可变 baseline

最终报告绑定 candidate run、blind packet、Judge run、human review bundle、suite/cohort、Provider、Prompt 和 Schema digest，并计算：

- deterministic pass count；
- human acceptance/reject/needs-review；
- primary disagreement rate；
- human safety reject count；
- Judge-human agreement rate；
- Judge false-accept/needs-review count。

任何确定性拒绝、真人 reject、安全拒绝或 Judge false accept 都使报告失败；未仲裁、证据不足、Judge invalid 或 Judge-human 不一致至少进入 `needs_review`。baseline compare 同时检查 cohort/Provider/Prompt/Schema 漂移和冻结阈值，不能以新结果静默覆盖旧 baseline。

仓库没有提交 Fake 生成的 Phase 107 baseline。只有真实 live capture、独立 Judge 和真人评审全部完成、结果经过人工批准后，才可以新增一个不可变 baseline 版本及前序摘要链。

## 7. 显式运行方式

live capture 和 Judge 默认关闭，CI 中强制拒绝，Fake Provider 强制拒绝；Provider 还必须满足 structured output、strict JSON Schema 和至少 8192 context tokens。输出路径若已存在也会拒绝覆盖。

```powershell
$env:DESKPILOT_PHASE107_LIVE_ALLOW = "1"

.\.venv\Scripts\python.exe -m deskpilot.phase107_gate capture `
  --provider-id <candidate-provider> `
  --build-id <immutable-build-id> `
  --coordinator-version <exact-semver> `
  --patch-version <exact-semver> `
  --output artifacts/phase107/candidate-run.json

.\.venv\Scripts\python.exe -m deskpilot.phase107_gate packet `
  --run artifacts/phase107/candidate-run.json `
  --output artifacts/phase107/blind-packet.json

.\.venv\Scripts\python.exe -m deskpilot.phase107_gate judge `
  --suite tests/fixtures/phase107-live-agent-calibration-suite.v1.json `
  --run artifacts/phase107/candidate-run.json `
  --packet artifacts/phase107/blind-packet.json `
  --provider-id <independent-judge-provider> `
  --build-id <immutable-judge-build-id> `
  --output artifacts/phase107/judge-run.json

.\.venv\Scripts\python.exe -m deskpilot.phase107_gate grade `
  --run artifacts/phase107/candidate-run.json `
  --packet artifacts/phase107/blind-packet.json `
  --judge artifacts/phase107/judge-run.json `
  --reviews artifacts/phase107/human-reviews.json `
  --output artifacts/phase107/report.json

.\.venv\Scripts\python.exe -m deskpilot.phase107_gate compare `
  --baseline artifacts/phase107/approved-baseline.json `
  --report artifacts/phase107/report.json
```

启用前必须先审阅 suite、Provider endpoint、数据出站范围和评审安排；不要把凭据、评论原文或未脱敏 Workspace 内容写进仓库 artifact。

## 8. 固定验收

专项测试覆盖：

- 8 个候选 sample 的 capture、盲包、独立 Judge、双人主审、报告与 baseline compare 正向闭环；
- 候选 Provider 与 Judge snapshot 相同的拒绝；
- 重复 Patch binding 的确定性拒绝；
- Judge 对真人/确定性坏样本 false accept 的失败门禁；
- 缺少主审、评审过期、分歧无仲裁的 fail-closed；
- 第三独立仲裁者的正向闭环；
- suite/JSON duplicate key/build ID/Judge build ID 篡改拒绝；
- CLI 未显式启用和 Fake Provider 的零网络拒绝。

Phase 107 专项 5/5、动态图 Patch/可组合批准回归 7 项、Model Gateway/OpenAI-compatible/Phase75 回归 29 项通过。最终统一后端收集 82 个测试文件 / 602 项，`590 passed + 12 skipped`、退出 0；Ruff 全仓和严格 mypy 244 个生产源码通过。Phase75 v15 保持 11/11、false-success=0、unauthorized-effect=0，compare 无违规，report digest 为 `5c0c2fb35f3bf5fcf28f8e8b521a6592c2ed69c992eb216e7783c67249463a30`。前端 22 个测试文件 / 154 项、type-check 和 production build 通过。Alembic 当前且唯一 head 为 `0050_agent_graph_test_conditions`，无待生成迁移；SQLite `integrity_check=ok`，`pip check`、`uv lock --check` 与 diff whitespace 通过。本阶段没有 migration。

## 9. 当前边界与下一步

当前生产 Agent Contract 对动态 Coordinator/Patch Planner 仍保持既有本地模型位置约束；Phase 107 是未来启用具体 live/cloud Agent 版本前的证据门，不会自行扩大生产 Provider、隐私或写权限。真实校准尚待用户明确选择候选/独立 Judge Provider，并安排两名主审及必要的第三仲裁者。

后续可在通过真实 cohort 后，把同一 approval-binding、盲审和版本化门禁推广到受控 create/rename/Artifact 写节点；仍不得开放任意 Shell、动态 executable/argv、联网安装、目录删除/覆盖，或让模型/Judge/评审结论代替独立用户确认与服务器 verified edge。
