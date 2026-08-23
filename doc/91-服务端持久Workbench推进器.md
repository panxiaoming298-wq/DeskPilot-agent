# 阶段 91：服务端持久 Workbench 推进器

阶段 91 把安全步骤的推进责任从前端页面移回服务端。创建研究任务、Workspace 只读任务或持久输入的 replacement Task 后，服务端会写入一个有界的 `workbench_runtime_items` 意图；API 进程后台领取意图并重复调用既有的单步 reducer，直到任务交付、等待用户、需要明确授权、被停止或安全失败。

这一阶段解决的是“持续工作”的生命周期问题：浏览器不再是执行驱动器，关闭 Workbench 不会让服务端队列消失。它不等于已经完成 Codex/Marvis 式动态多 Agent：当前 Plan 仍由受信模板编译，Handoff 仍是预定义节点，不允许模型自由派生子 Agent。

## 1. 持久 WorkItem 与数据库真值

`0042_workbench_runtime_items` 为每个 Task 保存最多一个 `advance` WorkItem，主要状态为：

- `pending`：可被任意健康 API 实例领取；
- `processing`：已绑定 owner、TTL 和单调增长 fencing token；
- `applied`：当前 Task 已无服务端可自动执行的动作；
- `cancelled`：用户停止或 replacement Task 已使该意图失效；
- `dead_letter`：连续失败达到上限，禁止无界重试。

WorkItem 持久 attempt、连续失败数、下次可用时间、最后错误、projection digest 和租约字段。实例崩溃后不需要从内存推测任务：新实例在旧租约过期后以新 fence 接管同一条记录。

## 2. 推进仍经过原安全 reducer

后台协调器不直接改 Execution Node、Route 或 Artifact，只会选择服务端白名单中的动作：

`run_research` → `verify_claims` → `build_artifact` → `verify_browser` → `finalize_delivery`，或单个受信 `execute_route`。

每次只调用一次 `TaskWorkbenchService.advance()`，然后重读服务器投影决定是否重新入队。用户路径写入、Workspace 精确替换/补丁/新建/重命名确认、MCP 启用和导出均不在自动白名单中。一次 reducer 后投影摘要未变且仍可自动执行时，记为 no-progress 失败而不忙循环。

协调器最多同时领取配置的有界任务数，长步骤期间定期续约。停止或新 Turn 会先使 WorkItem fence 失效，原 Agent Execution 的 node/invocation fence 仍作为第二层边界。

## 3. 暂停、续接与前端

Workspace Reader 返回 `needs_user_input` 后，Run 进入 `paused`，当前 WorkItem 进入 `applied`，因此不会持续轮询模型或重复生成问题。用户回答会创建新的不可变 Task，完成阶段 90 的 input-resolution proof 后再为新 Task 入队；旧 Task 仍保留完整审计链。

前端的六次 `workbench:advance` 循环已改为只读 GET 观察。即使节点正在 claimed/running、短时没有 enabled action，只要 Run 仍处于活动的服务端推进阶段，页面就继续观察投影。页面卸载只停止观察，不取消后台任务。手动 `workbench:advance` API 仍保留供诊断和向后兼容。

## 4. 一致性修复

引入真正的后台并发读取后，SQLite 可能在多条 SELECT 之间组合出“提交前 Model Turn + 提交后 Decision”的瞬时视图，从而误报 proof drift。`AgentExecutionRuntime` 现在使用同一条 outer join 读取 Turn 与 Decision，使原子更新对于读者也是原子的；摘要重算和 fail-closed 语义没有放宽。

## 5. 运行配置与验收

新配置默认启用 `DESKPILOT_WORKBENCH_RUNTIME_ENABLED=true`，并提供 poll interval、claim TTL、并发上限、连续失败上限和指数退避边界。生产或开发库启动前必须执行：

```powershell
alembic upgrade head
```

阶段验收覆盖：

- 不调用 `workbench:advance` 时，研究任务仍自动经过五个 WorkItem attempt 交付；
- 缺路径任务只暂停一次，用户补充后 replacement Task 在后台完成；
- 过期 owner 可被新 owner/fence 接管，旧 owner 无法 settle；
- 用户停止后已领取 WorkItem 无法 settle；
- `0042 → 0041 → 0042` 在独立临时 SQLite 往返。

实测门禁：Ruff 全仓通过，严格 mypy 检查 230 个生产源码通过；Migration/Workbench/研究 Agent/WorkItem 组合回归 67 项通过，最终新增的启动补种与过期/取消 fence 2 项专项再次通过。前端 22 个测试文件/152 项、Vue type-check 和 production build 通过；`uv lock --check`、`pip check`、单一 `0042` Alembic head 和 `git diff --check` 通过。唯一警告为既有 Starlette TestClient 对 httpx 的弃用提示。

默认开发 SQLite 不会被验收命令自动升级。

## 6. 下一步

下一阶段不应再扩写一套特例轮询。建议把 `workspace_directory_list` 或一个固定测试 Route 迁入通用 Agent Model Loop，补齐最大 Turn/Tool/Token/时间预算和 CannotComplete/no-progress 终态。其后再增加服务器验证的 `ProposeHandoff`、parent/child invocation 和用户可见的多任务控制面；那才是从“可持续单 Agent”到“可控多 Agent”的关键跨越。
