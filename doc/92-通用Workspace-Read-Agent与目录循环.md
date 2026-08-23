# 阶段 92：通用 Workspace Read Agent 与目录循环

阶段 92 将 `workspace_directory_list` 从直接 Capability 执行迁入阶段 89～90 建立的持久 Agent Model Loop。文件读取和目录读取现在都经过：

`request_route → server-owned read Route → durable Observation → submit_result → verified edge`

这一步让 Workspace Reader 从“只能读一个文件的演示 Agent”升级为“可承载多个固定只读 Route 的版本化 Agent”。任务仍由阶段 91 的服务端 Workbench Runtime 持续推进，关闭页面不会中断目录读取。

本阶段仍不宣称已经是 Codex/Marvis 式动态多 Agent。模型不能创建 Agent、选择任意工具、扩大路径、修改预算或跳过验证；这些边界是下一阶段引入服务器验证 `ProposeHandoff` 前必须保留的地基。

## 1. 不可变 Agent 版本

没有修改已发布的 `builtin.workspace_reader@1.0.0`：它继续只提供 `workspace.file.read.v1`，历史 Plan/Handoff 仍可按精确 Contract 和 Prompt digest 解析。

新增 `builtin.workspace_reader@1.1.0`：

| 版本 | 固定能力 | 用途 |
| --- | --- | --- |
| `1.0.0` | `workspace.file.read.v1` | 历史文件读取计划 |
| `1.1.0` | `workspace.file.read.v1`、`workspace.directory.read.v1` | 新文件/目录读取计划 |

Plan Binder 对新计划选择最高启用版本，并把精确 Agent Contract、Prompt Package、Capability 和节点预算写入不可变 Plan。Runtime 额外使用版本/Route 矩阵复核：`1.0.0` 不能执行目录 Route，Handoff capability 必须与持久 Route 一致。

## 2. 一个循环，两种服务器 Route

模型第一轮只能返回严格结构化决定：请求 Handoff 中已存在的 binding 和精确路径。Runtime 不接受模型提供新的 Route ID、Capability、路径或参数。

服务器根据持久 Route 决定实际动作：

- `workspace_file_read` 调用 `WorkspaceFileRuntime.read()`；
- `workspace_directory_list` 调用 `WorkspaceFileRuntime.list_directory()`。

目录读取只列直接子项，最多 200 项；隐藏项、reparse point、不允许或超限文件继续由既有 Workspace 边界过滤。Observation 绑定规范相对路径、排序后的 entry 元数据、截断状态和结果摘要。第二轮必须原样引用 Observation digest，才能提交候选结果并解锁 verified edge、final acceptance 和 delivery。

缺文件路径仍可产生持久 `needs_user_input`；目录 Route 必须在进入 Agent 前已有精确路径，不能借文件路径追问协议扩大输入语义。

## 3. 不可信目录数据与失败终态

文件正文和目录 entry 名称/元数据都以 `external_untrusted_workspace_data` 进入第二轮，系统 Prompt 明确它们只是数据。即使文件名写着“忽略指令”或“暴露密钥”，也不能改变 binding、路径或观察摘要。

失败现在会同时落在 Model Turn、Invocation/Node/Run 和 Turn Route：

- 第一轮改变 binding/path：`AGENT_ROUTE_BINDING_REJECTED`；
- 第二轮缺少提交或改变 Observation digest：`AGENT_LOOP_NO_PROGRESS`；
- 两轮/单 Route/Token/成本预算不足：`AGENT_TURN_BUDGET_EXHAUSTED`；
- Workspace 路径、版本或目录快照异常：沿用受限 Workspace 错误码。

这些状态不会被后台推进器当作可重试成功，也不会创建 verified AgentResult。持久 Observation 的 projection 被修改后，读取 Execution/Workbench 投影仍会 fail closed。

## 4. Workbench 与历史兼容

新目录 Plan 的 Route 节点是绑定 `workspace_reader@1.1.0` 的 Agent Node，因此手动 `workbench:advance` 和阶段 91 后台 WorkItem 都调用同一个 Workspace Agent Runtime。旧数据库中没有 `bound_agent` 的目录 Capability Node 仍走原来的直接确定性 Route，避免历史计划因部署升级失效。

前端协议没有变化：`workspace_directory` 继续显示相同 proof-carrying 结果，Model Turn 和 Invocation 已由现有 Workbench 投影显示。阶段 92 没有数据库结构变化，Alembic head 仍为 `0042_workbench_runtime_items`。

## 5. 评测基线

新增 Agent Contract/Prompt 会有意改变 Registry cohort。阶段 92 没有覆盖 v3，而是新增不可变 `multi-agent-core-v4.baseline.json`，其 `previous_baseline_digest` 精确绑定 v3。v4 仍维持零容忍阈值：11/11 required trial、false-success=0、unauthorized-effect=0、Verifier precision/recall=1.0。

## 6. 验收

专项覆盖包括：

- 新文件和目录 Plan 都绑定 `workspace_reader@1.1.0`；
- `workspace_reader@1.0.0` 仍存在且只提供历史文件能力；
- 目录完整路径经过两轮 Model Turn、一个 Observation 和 verified AgentResult；
- 无客户端 `workbench:advance` 时，服务端后台仍自动完成目录任务；
- 恶意 entry 名称只作为数据，结果证明可重验；
- 错误 binding 在目录 Route 执行前失败；
- 错误 Observation digest 进入 no-progress 失败，不能伪报交付；
- Observation projection 篡改后 Workbench GET 返回冲突；
- 文件暂停/续接、研究 Agent、后台 WorkItem、全部 migration 和 Phase75 联合回归保持通过。

最终实测：Ruff 全仓通过，严格 mypy 检查 230 个生产源码通过；Agent Registry、Plan、研究/Workspace Agent、Workbench/后台推进器、Phase75 与全部 migration 共 120 项组合后端回归通过；Phase75 v4 baseline compare 通过。前端协议未修改，但 22 个测试文件/152 项、type-check 和 production build 仍全部通过。`uv lock --check`、`pip check`、Alembic 单一 `0042` head、独立迁移/metadata check 和 `git diff --check` 通过。唯一警告为既有 Starlette TestClient 对 httpx 的弃用提示。

默认开发 SQLite 不会被测试自动升级；实际启动前仍需执行：

```powershell
alembic upgrade head
```

## 7. 下一步：从多 Route Agent 到可控多 Agent

下一阶段应新增严格的 `ProposeHandoff` 决定，而不是允许模型直接创建子进程或调用任意 Agent。建议最小闭环为：

1. Parent 只提出目标 capability、目标引用、必要 context refs 和预算切片；
2. 服务器根据 Registry 解析精确 Agent 版本，并验证 `may_delegate_to`、最大深度、最大 handoff 数、隐私、Tool scope、预算守恒和禁止循环；
3. 持久化 parent/child Invocation，Parent 进入 `waiting_children`；
4. Child 只能提交候选结果，经独立 verified edge 后作为 Observation 回到 Parent；
5. 重启、停止和 fence 同时覆盖 Parent、Child 与未消费结果；
6. Workbench 显示任务树、每个子任务的预算/状态/证据，并允许用户停止整个树。

这个闭环完成后，系统才真正从“服务端持续运行的版本化单 Agent”进入“模型可提议、服务器可约束和审计的多 Agent”。
