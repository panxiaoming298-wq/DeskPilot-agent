# 阶段 116B：持久多 Agent 编码循环第九检查点

## 1. 检查点结论

第九检查点已将用户确认的 generation-1 只读 Reader Plan 接入现有 `TaskLoopExecution → AgentExecutionRun → AgentInvocation → VerifiedResultRef` 主干。已确认计划不再停留在“可持久但不执行”的授权层；恢复扫描能发现它，通用 Reducer 能激活它，两个 exact Reader 能并行产生独立验证回执，进程重启后不会重复读取已产生候选回执的文件。

这是同一套执行状态机的第二种受信来源，不是第二套 Reader FSM。原 ModelPlanner 来源继续绑定 TaskLoop/Draft/Step/Offer；确认文件集来源则绑定 snapshot/proposal/confirmation/file-set binding，不伪造 ModelPlanner Offer 或 Draft。旧 ModelPlanner 的 ID、摘要、manifest 序列化和历史读取保持不变。

本检查点仍不标记整个 116B 完成。Reader verified join 当前只能完成只读 Final/Delivery，不会继承为 Patch 权限。“读取后生成变更提案、请求新的用户确认、再编译后继写计划”是下一个真实纵切。

## 2. 单一 TaskLoop 的双来源证明

- `TaskLoopExecution` 和原 `model_planner_node_bindings` 表增加 `model_planner | confirmed_file_set` 互斥来源形状；Node、Attempt、Invocation、ResultRef 和终态仍是唯一执行真值。
- 新增不可变 `WorkspaceCodingReaderNodeProof`，绑定 exact snapshot/catalog/project/ecosystem、Proposal、确认消息、file-set binding、候选序号、项目内相对路径、工作区相对路径、文件 proof、Plan/node、Reader Agent 和 Capability。
- Reader 绑定的输入只包含经证明的 `path/project_path/source_file_proof_digest/workspace_reader_node_proof_digest`；项目内 `src/a.py` 在执行边界被精确解析为工作区中的 `project/src/a.py`。
- proof digest 进入 node binding、Attempt input/context、Agent 结果验证与 ResultRef 摘要链。缺失、重复、路径不一致或任一摘要漂移都 fail closed。
- 为保持历史兼容，默认 `model_planner` 和空 Reader 字段不进入旧 manifest/ID/digest material，现有调用方从 `model_dump()` 重算旧摘要也得到原结果。

## 3. 激活、恢复与失败即停

- 确认文件集在激活前两次重验当前 snapshot/Proposal/binding/Agent/adapter/Plan，并在同一数据库事务中写入 ExecutionRun、TaskLoopExecution、每节点绑定和 activation event。
- `recoverable_task_ids` 同时扫描原 ModelPlanner TaskLoop 和已确认文件集；通用 `activate_plan` 指令根据来源选择正确激活器，后台工作者无需知道新的平行协议。
- 每次 `get/advance/claim` 前都会重新扫描项目快照并重建 exact node bindings。文件内容、mtime/version、Catalog、项目路径、Plan/node、Agent/Prompt 或数据库 proof 任一变化，都在新 Attempt 创建前拒绝。
- 两个 Reader 使用现有 batch=2 调度，每个候选和 verified ResultRef 独立持久。重启只从已持久的 awaiting-verification/verified 边界继续，不重复已读文件。
- 只有所有 Reader 都 verified 才能约减 Final Acceptance 和只读 Delivery。该 Plan 的 Contract 仍是 LOCAL/R0/`workspace.file.read.v1`，不含 Patch、Test、Git、Shell 或网络权限。

## 4. 数据库与迁移

Alembic head 升级为 `0063_confirmed_reader_task_loop`：

- `task_loop_executions` 增加互斥来源类型和 file-set binding FK/digest，原 loop/draft FK 仅对 ModelPlanner 来源非空。
- `model_planner_node_bindings` 作为现有 TaskLoop runnable-node authority 表继续使用；对确认 Reader 来源，ModelPlanner Draft/Step/Offer/Recipe 列必须全空，file-set binding 和 Reader proof 必须完整。
- 迁移对旧行使用 `model_planner` 安全默认，不做虚构回填。空库可在 `0062 ↔ 0063` 往返；存在 confirmed execution/node proof 时 downgrade 拒绝，避免丢失执行授权。

## 5. 验收证据

- 新增端到端用例覆盖：已确认 Plan 被恢复扫描发现、通用 Reducer 激活、两 Reader 并行读取 exact 工作区路径、候选回执后重启、两个 verified ResultRef join、控制节点收敛与终态成功。
- 用例同时验证：重启不增加第二次文件读取，Attempt input/context 包含 exact Reader proof，没有伪 Draft/Step，文件漂移在 claim 前拒绝，数据库 node proof 篡改在恢复时拒绝。
- 原 TaskLoop activation/Agent/Capability/MultiStepPlan 与 116B Coding Loop/Resilience 定向回归通过，旧 ModelPlanner 序列化摘要回归已覆盖。
- 默认后端实际收集 821 项，最终单进程统一运行 `809 passed + 12 skipped`、失败/错误为 0；探索专项 5 项与 migration 专项 48 项通过。Ruff 全仓、strict mypy 301 个生产源码、`uv lock --check`、`pip check`、wheel Prompt 31/31 与 `git diff --check` 通过。
- SQLite/Alembic 已由工作区原 `0061` 实际升级到唯一/current head `0063_confirmed_reader_task_loop`；`current/check`、空库 `0062 ↔ 0063` 往返、有确认 Reader execution/node proof 时的 downgrade guard 均通过。
- Windows Evaluation v2 compare 通过，report digest=`7b3764de88a1b1c980e4a057aef365f698f09149c5405b8a5aa5634d1c7dd253`；Phase75 v20 compare 通过，report digest=`65f2195aacb8a5cc22603b9b5a387ef0681a3d28dac20c6b352cf4a89908b043`，baseline 未改。wheel 包含 31/31 Prompt 资源；前端 24 文件 / 165 项、type-check/build 通过。
- 本机仍未配置 PostgreSQL/RabbitMQ 专用外部 cohort，不宣称真库、真消息队列、真模型质量或生产激活通过。

## 6. 方向校准与下一步

项目方向没有跑偏，并比第八检查点更接近 Codex 类持久多 Agent：一个来自真实模型 Turn 的 Explorer 提案，经用户确认后转换为可恢复的多 Reader 执行；进程、租约和内存对象都不是真值，并且模型输出仍不等于写权限。

下一检查点应继续纵向闭环：

1. 在 Reader ResultRef 全部 verified 后，启动一个持久、无写权限的 Change Proposal Model Turn；它只能基于 exact Reader 证据和原用户目标生成有界变更提案。
2. 将提案通过统一 Workbench/Turn 动作展示，要求新的 exact 用户确认；文件集确认不得继承为 Patch 确认。
3. 把已确认变更编译为后继写计划，复用现有 Patch/Test/一次 Repair/Git/Delivery 证明链；不修改已封存的 generation-1 只读 Plan。
4. 将 snapshot prepare、Explorer run、Proposal/confirmation、Reader activation 收敛到统一对话/Workbench 动作，不新增独立前端页面或第二套 API。
5. 自由 Shell、模型提供 argv/env、依赖安装、自动 push、cloud activation 和 116C 真实模型质量结论继续不进入下一 LOCAL-only 检查点。

在“Reader 证据 → 无权变更提案 → 新确认 → 后继写计划”闭合前，116B 继续标记为进行中。
