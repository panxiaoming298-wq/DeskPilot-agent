# 阶段 116A：服务器编译 WorkspaceCommandPlan

## 1. 检查点结论

阶段 116A 的第一个代码检查点已落地：原有“一个 Offer 绑定一个固定 Command Profile”的安全边界保留，并新增不可变、内容寻址的 `WorkspaceCommandPlanRequest`、`WorkspaceCommandPlanStep` 与 `WorkspaceCommandPlan`，由服务器编译器把多个已注册 Profile 选择编译成严格串行、失败即停的命令链。

这个检查点只证明命令计划的输入面、项目目标解析、Profile 绑定和内容证明成立。它还没有把多步计划接入持久 TaskLoop，也不代表阶段 116A/116B 或真实仓库长循环已经完成。

## 2. 模型可提供与不可提供的内容

编译器公开输入只有：

- `task_id`；
- `plan_generation`；
- 工作区内的结构化 `project_path`；
- 1～6 个 `command_profile_id`。

模型或调用者不能提供 executable、argv、cwd、环境变量、shell 字符串、网络开关、临时目录、超时或进程数。项目目标先通过既有 `WorkspaceFileRuntime` 解析，绝对路径、`..`、隐藏路径、symlink、junction、reparse point、非目录或工作区外目标继续 fail closed。

## 3. 服务器编译结果

服务器为每个选择解析完整 `CommandProfile`，并把 Profile digest、ecosystem、固定断网、临时快照、dependency mode、timeout 和进程上限绑定进 Step。计划同时绑定：

- exact Task 与 `plan_generation`；
- 归一化项目路径和请求摘要；
- 全量服务器 Profile Catalog 摘要；
- 连续 Step sequence、前一步依赖和每步摘要；
- 总 timeout budget；
- `stop_on_failure=true`、`network_access=false`、`temporary_snapshot_per_step=true`；
- 内容寻址的 plan id 与 plan digest。

当前版本刻意只生成单链：第一步无依赖，之后每步只依赖紧邻前一步。重复 Profile、Python/Node 混用、空计划、越界项目目标或任何摘要篡改都会在命令启动前被拒绝。

## 4. 实现位置与自动验证

- `backend/src/deskpilot/domain/workspace_command_plans.py`：请求、Step 和 Plan 的冻结 Contract、identity 与 digest 自验证；
- `backend/src/deskpilot/application/workspace_command_plan_compiler.py`：项目根解析、Catalog 绑定、ecosystem 校验和确定性步骤链编译；
- `backend/tests/test_workspace_command_plan_compiler.py`：确定性、依赖链、路径拒绝、重复/混用拒绝、篡改拒绝及无进程字段输入面。

最终门禁结果：

- 默认后端 `774 passed + 12 skipped`，失败/错误为 0；
- 新 Plan、Release/Admission 默认关闭与 Agent Registry 联合专项 `26/26`；
- Command Profile、Command Runtime、Workspace path/coding 与 TaskLoop reducer 联合回归通过；
- Ruff 全仓通过；严格 mypy 287 个生产源码通过；
- 唯一警告仍是既有 Starlette `TestClient` / httpx 弃用提示。

## 5. 下一纵切

下一步不是开放自由 Shell，而是把 `WorkspaceCommandPlan` 绑定到现有 TaskLoop 的 exact generation 和持久节点：

1. 保存计划请求、plan digest 与每个 Step 的执行/验证状态；
2. 每个 Step 继续调用现有断网临时快照 `WorkspaceCommandRuntime`，不新建任意命令入口；
3. 失败后停止未启动 Step，并把经过验证的失败结果交给有界 Repair；
4. API/sidecar 重启只恢复确定未启动的 Step，`running` 或 outcome unknown 不透明重放；
5. 最终 Delivery 聚合 exact plan、每步 snapshot/toolchain/result digest、失败/修复历史和剩余风险。

该持久命令链闭合后，再把两个并行只读 Child 的 verified join、Patch 和 Repair 接成 `Inspect → Plan → Delegate → Patch → Test → Repair → Verify → Deliver` 用户纵切。

## 6. 生产边界

本检查点只使用本地代码、既有固定 Profile 和离线自动化，不调用真实 Candidate/Judge，不生成 Production Admission，也不改变 cloud-only 2.0.0 cohort 的 disabled 状态。115B 的五项授权继续阻塞真实 capture、activation 和 116C 真实模型质量结论。
