# 阶段 101：动态任务图 Patch/Approval 节点与验证续接

## 1. 本阶段结论

阶段 101 把阶段 100 的批准式单补丁闭环接入服务器裁决的动态 Agent DAG。新 `workspace_dynamic_patch_test@1` Route 允许 Coordinator 在服务器公布的能力区域内生成包含目录上下文、Patch/Approval 和最终输出的任务图；Patch Planner 仍只能提出一次无授权精确替换。服务器把隔离预演的 manifest 和确认摘要持久绑定到当前 graph/node，暂停整个执行链等待用户确认，确认后才原子写入并运行固定测试。

```text
explicit directory/target/project/test/objective
                         ↓
Coordinator proposes graph inside sealed offer
                         ↓
directory_context ──typed ResultRef──→ patch_approval
                                            ↓
                           isolated preview, no write
                                            ↓
                         graph/node WAITING_USER
                                            ↓
                 user confirms this node's fresh digest
                                            ↓
                 atomic commit + fixed offline test
                                            ↓
                       patch_test ResultRef (verified)
                                            ↓
                   final output node / Parent join
```

图可以决定节点依赖和 verified 数据流，但不能决定写入授权。Model 输出、Repair Advice、Memory、Summary、旧 ResultRef、测试失败或前端按钮都不能替代当前节点的服务器证明和用户确认。

## 2. 确定性入口与 Task Contract

新增命令格式：

```text
多 Agent 修复并测试工作区：目录："." 文件："backend/src/example.py" Python项目："backend" Python测试："tests/test_example.py" 目标：修复失败测试
```

Node 版本把两处 `Python` 替换为 `Node`。Route v1 冻结：

- 目录读取根、单个补丁目标、固定测试项目和固定测试文件；
- `python` 或 `node` 测试种类；
- 仅作为 external-untrusted 数据的目标描述；
- 完整 Route parameter digest。

`workspace_dynamic_patch_test_contract` 把最大风险固定为 R1，并只允许目录读取、文件读取、Patch 提议、Patch bundle 以及所选测试种类。Acceptance 要求最终输出同时依赖节点审批 proof、PatchReceipt、固定测试结果和所有上游类型化 ResultRef。Contract 还显式冻结：提议不授予权限、每个 Patch 节点需要新确认，以及写入后不自动 Replan。

## 3. Graph v6 与 CapabilityInput v3

Supervisor 将动态图升级为兼容的 `deskpilot.agent-task-graph.v6`，并为 Patch 节点生成 `deskpilot.agent-task-graph-capability-input.v3`。v3 输入以 `route_patch_test_spec` 命名，绑定：

- 当前 task/plan/run/graph/node；
- `target_path`、`project_path`、`test_path` 和 `test_kind`；
- 用户目标和 Route parameter digest；
- 精确 Agent/Capability/预算与 input digest。

Coordinator 只能引用服务器 offer 中的 source key，不能自行提交路径、executable、argv、环境变量或命令。旧 graph v1～v5 与 input v1/v2 保持摘要兼容；v3 的新增字段不会被倒灌进旧版本摘要。

Patch 节点可消费上游 verified ResultRef 作为不可信上下文，但这些数据只帮助生成建议，不形成写权限。目标文件仍必须位于固定测试项目内，且测试继续复用阶段 82/83 的静态快照、断网和固定入口约束。

## 4. 节点级暂停与逐次批准

Patch Planner 生成单个 `old_text → new_text` 建议后，服务器复用阶段 100 的隔离 staging，并把 `WorkspacePatchPreview` manifest 与 `confirmation_digest` 写入当前 `agent_task_graph_nodes`。此时：

- Patch Invocation 和 graph node 进入 `waiting_user`；
- Agent Run 进入 `paused`；
- Turn Route 进入 `needs_user_action`；
- 工作区原文件保持不变；
- 下游节点没有 ready 资格。

确认时 Runtime 重新验证 Route、Plan、Run、graph v6、CapabilityInput v3、Handoff/Invocation/Turn/Decision/Observation、目标文件版本、staging manifest 和 node approval digest。批准只覆盖当前 graph/node 的当前单文件变更；另一个节点、另一个 generation 或另一个 manifest 必须生成新的确认摘要。

持久审批字段被篡改、目标发生外部并发编辑、摘要错误或旧确认重放都会在写入前 fail closed。相同确认在该节点已完成后只返回持久化结果，不会重复写入。

## 5. `patch_test` ResultRef 与图续接

确认成功后，服务器原子替换目标并保留备份，然后立即运行 Route 绑定的固定 pytest 或 `node:test`。组合 `WorkspacePatchTestRead` 被持久化为 `workspace_agent_results.result_kind = patch_test`，同时绑定：

- 当前节点的 confirmation digest；
- PatchReceipt 和工作区版本证明；
- Python 或 Node 固定测试完整结果；
- Agent result、workspace result 和 ResultRef digest。

只有状态为 `verified` 的 `patch_test` ResultRef 才能由 Supervisor 记录并使下游 join 变为 ready。成功确认不会提前把整个 Route 标为完成；后台 Coordinator 或显式“继续执行任务图”会继续领取剩余节点，直到最终输出和 Parent join 都通过。

测试失败或运行错误时，graph/node/Run 如实失败并保留已经发生的 PatchReceipt 和备份事实；系统不自动生成第二个补丁、不复用本次批准到新节点，也不把失败快照或 Repair Advice 转成 Capability。

## 6. Workbench 与前端

Workbench 的动态图节点现在投影：

- Patch target 和 v3 输入；
- 当前 approval digest 及 `waiting user` / `consumed` 状态；
- PatchReceipt、组合 `patch_test` 结果和固定测试结果；
- verified ResultRef 与下游 ready 状态。

前端在 Patch 节点暂停时展示“只批准这一补丁”的确认卡。确认完成后不再重复显示批准动作，而是显示继续 DAG 的安全动作。浏览器只提交当前 digest；所有授权和 proof 裁决仍在后端完成。

## 7. 持久化与数据库版本

新增 migration `0049_agent_graph_patch_approvals`：

- 为 `agent_task_graph_nodes` 增加 nullable `approval_manifest` 和 `approval_digest`；
- 扩展 `workspace_agent_results` 的结果种类约束，加入 `patch_test`；
- 提供 `0049 → 0048` 对称 downgrade。

默认 SQLite 在升级前备份到 `backend/data/backups/deskpilot.pre-0049-agent-graph-patch-approvals.db`；源库与备份 SHA-256 均为 `b4aa00bfbe4e66bb7509a89fea3995f98a0ed8efced9f00c367238aa397fce5f`。当前且唯一 Alembic head 为 `0049_agent_graph_patch_approvals`，`alembic check` 无待生成操作，SQLite `integrity_check=ok`。

## 8. 验证结果

- 动态图正向闭环通过：目录上下文 → Patch 节点暂停 → 用户确认 → 原子提交/固定测试 → `patch_test` ResultRef → 下游 verified join；
- 节点 approval manifest 篡改在写入前返回 409，原文件保持不变；重复确认幂等，确认完成后不会重新出现批准动作；
- 阶段 100 的直接单补丁 Route 兼容回归通过；
- 后端 pytest 全量收集 81 个文件 / 579 项，统一首轮退出 0，12 个既有平台条件 skip；
- Ruff 全仓、严格 mypy 239 个生产源码通过；
- Phase75 11/11，false-success=0、unauthorized-effect=0，链向 v12 approval digest 的 v13 baseline compare 无违规；
- 前端 22 个测试文件 / 154 项、type-check 和 production build 通过；
- `0049 → 0048 → 0049` migration 专项、空库迁移、Alembic 单一 head/current/check、SQLite 完整性、`pip check` 与 diff whitespace 通过。

## 9. 与 Codex/Marvis 的距离

系统现在已经具备“持续对话 Route → 动态异构 DAG → 类型化上下文 → 图内 Patch/Approval 暂停 → 用户逐节点授权 → 原子写入 → 固定测试 → verified 数据流续接”的真实纵向切片。批准边界不再只是顶层直连 Route，而成为可被 Supervisor 封装、Scheduler 暂停/恢复和下游 ResultRef 消费的图节点。

当前仍不是任意自主编码系统：一次 Patch 节点仍严格限于一个显式文件、一次精确替换和一个固定测试；动态图没有服务器裁决的运行中条件分支或同一任务内多个新补丁 generation；没有自由 Shell、依赖安装、目录创建/删除/覆盖、登录态浏览器或 live-model/Judge-human 校准。下一阶段应优先加入“测试结果驱动但仍由服务器裁决”的条件边或节点级新 Plan generation，并证明每个新 Patch 节点都必须取得新的精确确认。
