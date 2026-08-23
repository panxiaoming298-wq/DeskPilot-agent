# 阶段 93：服务器裁决 Agent Handoff 与父子续接

## 1. 本阶段结论

DeskPilot 已完成第一个真正由模型提议、服务器裁决并持久续接的父子 Agent 闭环。新的工作区目录计划不再只运行一个可访问多 Route 的 Agent，而是预编译四个节点：

```text
Workspace Coordinator (Parent)
  └─ Workspace Reader 1.1.0 (optional Child slot)
       verified result → Handoff Observation → original Parent Invocation
Parent verified → final acceptance → delivery
```

模型输出的 `propose_handoff` 仍然只是无权限提议。它不能创建任意 Agent、修改计划、扩大路径、选择版本、扩大 Tool scope 或增加预算。服务端只允许它选择当前 Executable Plan 中唯一的预编译子槽位。

## 2. 严格决定与服务器裁决

`CoordinatorLoopDecision` 只有两个分支：

- Turn 1：`propose_handoff`，回显服务器提供的 handoff binding、目标 capability、objective ref、context refs 与预算切片；
- Turn 2：`submit_result`，只能引用已经验证的 child Observation digest。

接受提议前，Runtime 会重新验证：

1. Parent/Child 都是冻结 Registry 中的精确 Contract/Prompt digest；
2. `may_delegate_to` 与 `may_receive_from` 双向匹配；
3. 子节点是当前 Plan 的预编译 optional slot，且尚未被激活；
4. 最大 outgoing handoff、最大深度、禁止循环和父节点 handoff 预算成立；
5. Task privacy mode 属于 Child 允许范围；
6. Child capability 精确为 `workspace.directory.read.v1`；
7. Child 没有额外 Tool grant，预算切片不超过 Plan/Contract 上限；
8. 模型没有改变路径、上下文引用、目标或任何绑定字段。

任一项不成立时，决定以 `AGENT_HANDOFF_PROPOSAL_REJECTED` 失败，Child Invocation 不会被创建。

## 3. 持久父子状态机

`0043_agent_delegations` 新增：

- `task_execution_nodes.handoff_parent_node_id`：Plan 中预编译的父子关系；
- `agent_invocations.parent_invocation_id`：实际 Invocation 血缘；
- `agent_delegations`：决定、binding、父/子节点、父/子 Invocation、深度、预算、Child Result 与 Observation 的持久真值；
- Node/Invocation `waiting_children`；
- `propose_handoff` 决定类型和 `handoff` Observation 来源。

状态转换如下：

```text
Parent running
  → propose_handoff accepted
  → Parent Invocation/Node waiting_children
  → Child slot ready
  → Child Invocation(parent_invocation_id=Parent)
  → Child candidate result
  → deterministic verification
  → Child verified
  → Handoff Observation persisted on Parent decision
  → original Parent Node ready
  → reclaim same Parent Invocation, same attempt, new node fence
  → Parent submit_result
  → Parent verified
```

父节点续接不会创建第二个 Parent Invocation，也不会增加 attempt。恢复时读取原 Handoff manifest；新的 claim fence 会拒绝停止或 lease 接管前的旧 Worker。

## 4. verified-result 边界

Workspace Reader 仍按阶段 92 的两轮循环执行受控目录 Route。不同点是：作为 Child 时，它不能直接完成 Run 或解锁最终交付。

Child reducer 先保存候选 `AgentOutputResult`，执行确定性验证，再写入绑定 Parent `propose_handoff` Decision 的 `source_kind=handoff` Observation。只有这个 verified Observation 才把 Parent 从 `waiting_children` 唤醒。Parent 的最终结果必须同时核对：

- Child Invocation 的 `parent_invocation_id`；
- Child `result_id` 与 verification status；
- delegation 的 decision/binding/node 血缘；
- Observation digest 与 verified projection；
- Route result digest。

未验证、被篡改或来自其他 Invocation 的结果都不能解锁 Parent。

## 5. 停止、重启与读取验真

- 停止 Run 会 fence 全部未完成节点、取消等待中的 Parent 和未开始 Child，并把活动 delegation 标记为 cancelled；
- Child 失败或 lease 过期且重试耗尽时，失败会向等待 Parent 收敛；
- 应用在 Parent 提议后退出，重启可先领取 Child，再以原 Parent Invocation/attempt 续跑；
- Workbench 每次读取都会重算 proposal digest，并交叉核对 Parent/Child node、Invocation、Decision、预算、verified Result 和 Observation；存储篡改返回冲突，不展示伪造任务树。

## 6. Workbench 控制面

目录 Route 保持用户与历史 API 兼容。新的工作台会展示 Agent Task Tree：

- Parent/Child Agent ID；
- 节点状态；
- Model/Tool/Handoff 预算；
- delegation 深度和状态；
- verified Observation/Result 证据摘要。

前端仍只观察服务器投影，不负责创建 Child、推动正确性状态或解释模型“思考”。一次兼容的 `workbench:advance` 可以在有界三阶段内完成父提议、Child 和 Parent 续接；每个阶段都已经独立事务持久化，崩溃后可从边界继续。

## 7. 兼容与发布证明

- 历史目录 Plan manifest 仍绑定 `builtin.workspace_reader@1.1.0` 并按旧单 Agent 路径收尾；
- 新目录 Plan 使用 `workspace_directory_list.v2` 与 `builtin.workspace_coordinator@1.0.0`；
- `builtin.workspace_reader@1.1.0` 只新增 Coordinator 的 receive edge，旧 `1.0.0` 保持不变；
- Registry/Prompt cohort 的有意变化写入不可变 Phase75 v5 baseline，v5 的 `previous_baseline_digest` 精确绑定 v4 approval digest；零容忍阈值不变。

验证结果：

- Ruff 全部生产源码与测试通过，严格 mypy 231 个生产源码通过；
- Registry/Plan/Model Loop/Workspace/后台推进/Workbench/Phase75 组合 95 项通过；
- migration 完整 30 项通过，包含 `0043 → 0042 → 0043` 往返与 metadata check；
- Phase75 11/11，false-success=0，unauthorized-effect=0，v5 baseline compare 通过；
- 前端 22 文件/152 项、type-check 与 production build 通过；
- `uv lock --check`、`pip check` 和 diff whitespace 通过。

默认开发 SQLite 不会自动升级。实际启动前执行：

```powershell
cd backend
.\.venv\Scripts\python.exe -m alembic upgrade head
```

## 8. 诚实边界与下一阶段

本阶段已经是真实的受约束多 Agent，但还不是允许模型动态生成任意任务图的 Codex/Marvis：

- 只开放一个预编译、只读、深度 1 的 Child slot；
- 还没有多个并行 Child、部分成功 join 或用户单独停止某一分支；
- Context refs 与预算切片目前要求精确回显，尚未开放服务端允许范围内的缩减选择；
- 目录 Route 的 Parent 只做验证后汇总，不进行复杂 replan；
- 测试、补丁、Artifact 和 Research 还没有迁入同一 Handoff 协议。

下一阶段优先把 delegation 运行时提取为通用 Supervisor 服务，并增加两个并行只读 Child 的 verified join、分支级停止/重试和 Workbench 树操作。之后再考虑受控 Replan/new generation；不要用执行中原地改图或自由 Shell 模拟动态 Agent。
