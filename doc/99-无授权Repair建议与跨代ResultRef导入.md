# 阶段 99：无授权 Repair 建议与跨代 ResultRef 导入

## 1. 本阶段结论

阶段 99 让失败后的新 Plan generation 不再盲目重复上一代的全部工作。服务器会从失败快照与旧图中生成结构化 Repair Advice，并把仍能完整重验的成功节点结果作为具名 source-ref 提供给新一代 Coordinator。模型只能选择 offer 中的 source key；Advice 本身永远不授予 Capability，新图仍由 Supervisor 独立裁决。

```text
generation 1 dynamic graph
  directory_scan ── verified ResultRef ──┐
  file_reader ───── failed              │
                                        ↓
immutable failure snapshot + server recomputation
                                        ↓
Repair Advice { strategy, objective, grants: [], reusable_sources }
                                        ↓
generation 2 proposal selects exact source_key
                                        ↓
Supervisor seals v5 graph + exact imported ResultRef
                                        ↓
runtime revalidates old proof before every downstream use
```

这不是跨代共享可变状态，也不是把上一代的成功文字直接复制进提示词。导入项仍是外部不可信 payload，只有与持久证明完全匹配的 ResultRef 才能进入下游。

## 2. Replan v2 与 Repair Advice

`AgentReplanRead` 新增兼容的 `deskpilot.agent-replan.v2`。旧 v1 记录继续按旧字段和旧摘要规则读取；新 v2 必须绑定一份 `AgentReplanRepairAdvice`：

- 精确 failure snapshot digest 与稳定错误码；
- 服务器选择的 repair strategy 和有界 objective；
- `granted_capability_ids`，其长度强制为 0；
- 最多 7 个可复用 `AgentReplanResultSource`；
- Advice 自身摘要。

当前策略仅表达如何重组工作，不改变授权：

- `rebuild_graph_from_current_offer`；
- `reuse_verified_evidence_and_rebind_route`；
- `simplify_graph_and_consume_verified_evidence`。

创建 Replan 和读取 Replan 时都会重新计算 Advice。修改策略、失败引用、source-ref、source digest 或伪造非空 grant 都会使谱系读取失败。

## 3. 可复用 ResultRef 的筛选

服务器只从失败 source Run 的持久动态图收集已经成功验证的 Child。每个候选必须重新通过：

- source Plan/Run/generation 与失败图血缘；
- graph manifest、node manifest 与节点状态；
- Child Invocation、Agent Contract、Capability 和结果记录；
- `WorkspaceAgentResultRecord` 及目录/文件/测试种类的完整结果 schema；
- Workspace proof、Agent result digest 与 ResultRef digest。

source key 由上述不可变身份确定性派生，格式为 `replan_result_<32 hex>`。模型不能提交 ResultRef 对象，也不能自行构造 key。任何 proof 缺失或不一致都会让 Replan 创建或后续读取 fail closed。

## 4. v5 动态图导入绑定

`AgentTaskGraphNodeProposal` 新增最多 7 个唯一 `import_sources`。Supervisor 只接受当前 Repair Advice 已提供的 key，并在 `deskpilot.agent-task-graph.v5` 的绑定节点内同时封存：

- 选择的 source key；
- 对应的完整 `AgentTaskGraphResultRef`；
- 新图节点、CapabilityInput、依赖和预算；
- v5 graph/node digest。

旧 v1～v4 图继续按历史摘要兼容读取，且不能凭空出现 imported refs。导入不是 DAG dependency edge，也不会替代 Route 的 CapabilityInput；因此一个旧目录结果不能变成文件路径、测试说明或新的 Tool 权限。

## 5. 使用时重验，而非创建时信任

新代 Child 领取和执行时，`verified_upstream_result_refs` 会把同代依赖 ResultRef 与跨代 imported ResultRef 合并。每次解析都会重验当前 Replan、Advice、source/target Plan 与 Run、旧失败图、旧节点和完整结果证明。

同代依赖只能来自当前 running/verified graph；跨代导入只能来自 Advice 指定的旧 failed graph。二者不能通过修改 graph id、node id、generation 或 digest 相互冒充。Workspace Agent 收到的提示上下文明确标注这些 payload 仍为 external-untrusted evidence。

## 6. 运行时效果

专项 Replan 场景第一代运行：

```text
directory_scan ── succeeds
file_reader ───── fails with allowed model-protocol error
directory_join ── cannot run
```

第二代不再创建重复的 `directory_scan`，而是生成：

```text
import old directory_scan ResultRef
               ↓
file_reader → directory_join [OUTPUT]
```

这证明任务图可以基于失败上下文动态缩减和重组，同时保持新 Plan/Run/graph 独立不可变。选择未提供的全零 source key 会在 Child graph 持久化前被拒绝；修改旧 ResultRef 后，Workbench 和运行时都会返回证明冲突，而不是展示或消费伪造结果。

## 7. Workbench 与前端投影

Workbench 的 Replan 投影新增策略、导入数量和 `GRANTS 0`；动态图节点显示选中的 source key 及 ResultRef/source 摘要。前端只展示服务器已经重验的投影，不参与 Advice 生成、source 选择授权或证明判断。

## 8. 持久化与数据库版本

本阶段没有增加表或列。Repair Advice 存在既有 `agent_replans` 的不可变 manifest 中，source key 与完整 ResultRef 存在既有动态图 manifest 中，因此 Alembic head 继续是 `0048_agent_test_capability_inputs`。

默认开发 SQLite 已处于 `0048`，无需再次升级或创建备份。启动前仍可运行：

```powershell
.\.venv\Scripts\alembic.exe current
.\.venv\Scripts\alembic.exe check
```

## 9. 安全边界

Repair Advice 和跨代导入没有扩大以下能力：

- 不授予文件写入、目录创建、删除、覆盖或移动权限；
- 不接受模型提供的 executable、argv、环境变量或安装命令；
- 不允许 npm/npx、联网安装或自由 Shell；
- 不把失败测试自动转换为修改授权；
- 不允许 UI、Memory、Summary、Context、MCP 或旧 payload 充当授权；
- 不改写 source generation 的 Plan、Run、graph、Invocation、Turn 或 Result。

## 10. 与 Codex/Marvis 的距离

系统现在已经能“运行时生成任务图 → 专业 Agent 执行 → 失败后生成新代 → 复用已验证证据 → 继续动态组图”，并且服务端始终掌握 Capability、预算和证明裁决。这比固定工作流更接近持续对话式 Codex/Marvis Agent。

下一阶段的关键缺口是受控写入闭环：模型先提出结构化补丁，用户显式批准后在隔离工作区应用，再运行服务器固定测试并生成 verified result。Repair Advice 或测试失败本身都不能越过批准边界。

## 11. 验证结果

- 跨代 ResultRef 正向复用、未提供 source key 拒绝、旧证明篡改和 v1 Replan/v1～v4 graph 兼容专项通过；
- Ruff 与严格 mypy（238 个生产源码）通过；
- Phase75 11/11，false-success=0，unauthorized-effect=0，链向 v10 的 v11 baseline compare 通过；
- 前端 22 个测试文件 / 152 项、type-check 与 production build 通过；
- Alembic 单一 `0048` head/check、SQLite `integrity_check=ok`、`pip check` 与 diff whitespace 通过；
- 后端 pytest 全量收集 81 个测试文件 / 571 项并退出 0（12 个既有平台条件 skip）。

首轮全量仅有既有 Runner cooperative-cancel 用例在子进程 hello 前发生一次 10 秒启动超时；该单项隔离连续复跑 5/5 通过，降低并发负载后的第二轮全量统一退出 0。本阶段未通过放宽 Runner 超时来掩盖抖动。
