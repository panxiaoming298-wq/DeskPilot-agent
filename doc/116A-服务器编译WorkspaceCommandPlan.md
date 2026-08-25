# 阶段 116A：服务器编译 WorkspaceCommandPlan

## 1. 检查点结论

阶段 116A 第二个代码检查点已落地：服务器编译的 `WorkspaceCommandPlan` 已绑定到 exact Task、generation-1 ModelPlanner Draft/Step、Offer 和 TaskLoop capability node，并与 Draft/Step 在同一事务写入新的 `0056_workspace_command_plan_bindings` 表。计划表只保存不可变授权与映射证明，Node、Attempt 和 VerifiedResult 仍是唯一执行状态真值。

这个检查点已闭合固定命令链的持久执行、失败即停、一次有界 Repair 和重启恢复；它仍不代表 116B 的完整多 Agent 编码循环或 116C 真实模型质量验收已完成。

## 2. 编译与绑定边界

编译器公开输入仍只有 exact Task、计划代、工作区内 `project_path` 和 1～6 个已注册 `command_profile_id`。模型或调用者不能提供 executable、argv、cwd、环境变量、shell 字符串、网络开关、安装指令、超时或进程数。

ModelPlanner 将连续、同项目、同生态的 command Offer 编译成一条严格串行计划；非 command Route、项目变化或 Python/Node 生态变化会切分计划组。重复 Profile、超过 6 步、映射不唯一、越界路径或摘要篡改都在命令启动前 fail closed。

`WorkspaceCommandPlanBinding` 和每节点 `WorkspaceCommandPlanStepProof` 绑定：

- Task、TaskLoop、Draft 和 exact generation-1 Plan digest；
- Offer ID/key/digest、ModelPlanner Step ID/digest/ordinal；
- composite node ID/spec digest 与 fixed Command Profile ID/digest；
- 归一化项目路径、Catalog digest、command step/plan/binding digest。

Planning、Activation 和每次 command claim 都重建并比对当前路径、Catalog、Profile、Offer 和 node proof。新 command node 必须有且只有一个完全匹配的步骤证明，证明进入 BoundCapabilityInput、Context 和 ResultRef 摘要链；旧非命令输入缺少该字段时保持原 digest。

## 3. 执行、失败与恢复

- 只有 `status=passed` 的 command ResultRef 才能满足依赖并解锁后续步骤。
- 非 `passed` 输出仍生成独立、经验证、不可变的失败 ResultRef；对应 Attempt/Node 标为 `failed`，后续 Node 保持 `pending`。
- Command Profile node 预算允许已知失败后一次新 Attempt；首次 Repair 成功后只用成功 ResultRef 满足边，历史失败回执保留。TaskLoop 全局 Repair 上限仍为 2。
- `running`、lease 过期或 outcome unknown 继续使用 `NO_AUTOMATIC_REPLAY`，不把不确定命令当作已知失败重试。
- 重启后从原 Draft/Plan/Node 恢复，已通过步骤不重复执行；无 binding 的未执行 legacy command node 在激活/领取前失败关闭。

Workbench 沿用原 TaskLoop API，节点只新增可选 `command_plan_id`、步骤序号、Profile ID 和已验证失败回执数；没有新建第二套 API 或前端状态机。

## 4. 实现位置

- `backend/src/deskpilot/domain/workspace_command_plans.py`：Plan/Binding/Mapping/StepProof 冻结 Contract 与摘要自验证；
- `backend/src/deskpilot/application/workspace_command_plan_binder.py`：command Offer 分组、编译与 exact ModelPlanner node 映射；
- `backend/src/deskpilot/application/multi_step_plan_runtime.py`：Draft/Step/Binding 原子持久与重启读取；
- `backend/src/deskpilot/application/task_loop_activation_runtime.py` 与 `capability_execution_runtime.py`：激活/claim 重验、通过解锁、失败回执和恢复；
- `backend/src/deskpilot/infrastructure/migrations/versions/0056_workspace_command_plan_bindings.py`：不可变绑定表与有数据 downgrade guard。

## 5. 验收结果

- 默认后端 `779 passed + 12 skipped`，失败/错误为 0；12 项为未配置的可选 PostgreSQL/RabbitMQ 外部服务 cohort。
- WorkspaceCommandPlan 专项覆盖单/多步、混合 Route、跨项目/生态分组、事务回滚、重启恢复、失败停链、一次 Repair、路径/Catalog/Profile/proof 漂移拒绝和 downgrade guard。
- Ruff 全仓通过；严格 mypy 通过 289 个生产源码文件；`uv lock --check` 与 `pip check` 通过。
- Alembic 唯一/current head 为 `0056_workspace_command_plan_bindings`，`upgrade/current/check` 通过；SQLite `integrity_check=ok`、foreign-key 零违规。
- PostgreSQL marker 精确选中 11 项，因本机未配置专用测试库而全部安全跳过，未宣称本检查点完成了真 PostgreSQL 实测。
- 前端 24 个测试文件 / 165 项、type-check 和 production build 通过。唯一警告仍是既有 Starlette `TestClient` / httpx 弃用提示。

## 6. 下一纵切

116A 的固定命令链持久执行检查点已闭合。下一步进入 116B：在同一持久 TaskLoop 中连接持续对话、两个并行只读 Child、verified join、精确多文件 Patch、固定 Test/Repair、独立 Verify 和 Delivery 证据，形成 `Inspect → Plan → Delegate → Patch → Test → Repair → Verify → Deliver` 的 LOCAL-only/Fake 纵向闭环。

## 7. 生产边界

本检查点只使用本地代码、Fake runtime 和隔离测试仓库，不调用真实 Candidate/Judge，不生成 Production Admission，也不改变 cloud-only 2.0.0 cohort 的 disabled 状态。115B 的五项授权继续阻塞真实 capture、activation 和 116C 真实模型质量结论。
