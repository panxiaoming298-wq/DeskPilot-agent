# 阶段 96：服务器绑定 Capability 输入与异构 Agent 任务图

## 1. 本阶段结论

阶段 95 已经让动态任务图具备类型化 `ResultRef` 数据流，但每个 Child 仍读取同一条目录 Route path。阶段 96 把“模型选择节点”与“服务器授予该节点的真实输入”彻底分开：模型只能从服务器 offer 中选择一个命名输入槽，Supervisor 再把 Route 中已有的精确参数封成不可变 `AgentTaskGraphCapabilityInput`。

```text
user-authored Route parameters
  ├─ path      → route_directory_path
  └─ file_path → route_explicit_file_path
                  ↓
server capability offer: capability + budget + allowed input sources
                  ↓
model proposes capability + input_source + dependencies
                  ↓
Supervisor resolves exact path, kind, Route digest and source_ref
                  ↓
v3 graph manifest + node record + Handoff digest
                  ↓
Workspace Reader re-verifies binding before any filesystem read
```

模型没有提交原始路径的字段，也不能把目录参数改造成文件参数。它现在可以动态组合异构 DAG，但权限仍来自用户输入、Route Contract、Capability Catalog、Registry 和服务器封图，而不是来自模型文本。

## 2. 新 Route 与明确授权参数

新增只读 Route `workspace_directory_analyze@1`，当前确定性语法为：

```text
分析工作区目录：<directory> 文件：<explicit file>
```

Router 只提取两个用户明确提供的参数：

- `path`：目录能力可用的目录路径；
- `file_path`：文件能力可用的精确文件路径。

Task Contract 同时授权 `workspace.directory.read.v1` 和 `workspace.file.read.v1`，风险上限仍是 R0，禁止 Shell、动态代码、网络和写入。普通 `workspace_directory_list` 只提供目录输入槽，不会因为系统支持文件能力就获得文件路径。

## 3. Capability-specific input offer

动态 Coordinator 的服务器 offer 现在为每个 Capability 附带 `input_sources`：

- `workspace.directory.read.v1` 只能选择 `route_directory_path`；
- `workspace.file.read.v1` 只在 analyze Route 存在显式 `file_path` 时提供 `route_explicit_file_path`。

严格 `AgentTaskGraphNodeProposal` 必须提交一个枚举 `input_source`。Supervisor 同时验证 Capability 是否在 Task Contract、Agent 是否可接收、预算是否守恒、ContextRef 是否授权，以及 input source 是否属于该 Capability 的当前 offer。错配在创建 Child 或读取文件之前直接以 `AGENT_TASK_GRAPH_REJECTED` 失败。

## 4. 不可变 CapabilityInput 证明

服务器生成的 `AgentTaskGraphCapabilityInput` 绑定：

- source key 与 `turn-route://.../parameters/<name>` source ref；
- `file` / `directory` read kind；
- Route 中的精确相对路径；
- 完整 Route parameter digest；
- CapabilityInput 自身 digest。

该对象写入 v3 graph manifest，并分别持久化为节点 `input_manifest` 与 `input_digest`。它也进入 Child `HandoffEnvelope.capability_input` 和 Handoff digest。旧 v1/v2 graph 与旧 Handoff 仍可读取；新建 v3 graph 要求每个节点都存在 CapabilityInput。

Workbench 每次投影会比较 manifest、节点 input manifest、input digest、runtime node、edge 和 Invocation 血缘。直接改写 `input_digest` 会返回 `409 TASK_WORKBENCH_CONFLICT`，不会展示伪造结果。

## 5. 异构目录—文件—目录输出 DAG

验收 Provider 运行时生成三节点图：

```text
directory_scan
  workspace.directory.read.v1
  input = route_directory_path(.)
          ↓ ResultRef(directory)
file_reader
  workspace.file.read.v1
  input = route_explicit_file_path(alpha.txt)
          ↓ ResultRef(file)
directory_join [OUTPUT]
  workspace.directory.read.v1
  input = route_directory_path(.)
          ↓ ResultRef(directory)
Parent / Route output
```

三个 Worker 均是相同的冻结 `workspace_reader@1.2.0`，但 Supervisor 按节点 CapabilityInput 选择文件或目录执行 profile。每个依赖节点还收到直接上游的已验证 ResultRef 与外部不可信 payload。输出节点仍必须是目录能力，保证现有 Directory Workbench 和 Route output contract 不被模型改变。

## 6. 执行前的双重重验

Scheduler 为动态节点创建 Handoff 前，从 graph/node 持久真值重验 CapabilityInput；Workspace Reader 启动后再次验证：

- Route parameter digest 仍等于 Route parameters 的规范摘要；
- source key 对应的 Route 参数仍等于封存路径；
- read kind 与 Handoff Capability 一致；
- Handoff、graph node、runtime node 和 CapabilityRef 未漂移。

通过后才调用既有受控 `read()` 或 `list_directory()`。因此模型可以决定“使用哪个已授权输入槽”，但不能生成 `../secret`、替换显式文件或把上游不可信目录条目升级为新授权。

## 7. 持久化、Workbench 与并发投影

`0046_agent_task_graph_capability_inputs` 为 `agent_task_graph_nodes` 新增：

- `input_manifest`；
- `input_digest`。

前端任务图展示每个节点的 input source、精确相对路径和摘要，`workspace_directory_analyze` 与目录结果共用现有可验证 Workbench 投影。

后台自动推进测试还暴露了一个读取时序窗口：Workbench 可能先加载旧的 Decision/Invocation/Node 映射，随后看到后台刚提交的新 graph。证明校验现在只在映射缺项时按主键补读同一条持久记录，再执行完全相同的摘要和血缘检查；它没有放宽任何证明条件，只避免跨查询可见性造成的瞬时误报。

## 8. 与 Codex/Marvis 的距离

当前已经具备一个受控的 Codex/Marvis 式核心：模型能根据任务动态生成多节点异构 DAG，节点具有持久状态、类型化输入、类型化上游结果、并发/依赖调度、停止、重启续接和确定性输出，服务端仍掌握所有权限。

但“任意动态任务图”当前指在冻结 Task Contract 与 Route offer 内任意组合，不代表任意系统权限：

- 当前异构输入只有显式目录和显式文件；固定 Python/Node 测试尚未成为动态图节点；
- 动态图最多 4 个 Child，协议结构上限为 8；
- 尚无运行中改图、条件分支、节点级重试/批准或新 generation Repair/Replan；
- 目录条目不会自动变成新文件授权，避免 prompt injection 或路径发现绕过用户边界；
- 仍无自由 Shell、联网安装、任意 argv、目录创建、删除或覆盖。

下一阶段优先实现失败后创建新 Plan generation 的受控 Repair/Replan：旧 graph 保持不可变，Parent 只能提交结构化失败证据和建议目标，服务器重新编译 Contract/Plan、重新 offer Capability/Input，并以明确 lineage 创建 replacement Run。随后再把固定测试 Route 作为新的 CapabilityInput 类型接入动态图。

## 9. 迁移与默认开发数据库

已实际执行：

```powershell
cd backend
.\.venv\Scripts\alembic.exe upgrade head
```

默认 SQLite 从 `0045_agent_task_graph_result_refs` 升级到 `0046_agent_task_graph_capability_inputs (head)`。升级前备份为 `backend/data/deskpilot.pre-0046-20260822-184958.db.bak`，SHA-256 为 `39DD1A59E1BC85B485250F2D9D03D3142411A368B93B3B72C4D20E55C136AE62`。升级后 `alembic check` 无漂移，SQLite `integrity_check=ok`。

迁移脚本文件名刻意保持为较短的 `0046_capability_inputs.py`；Revision ID 不变。较长文件名会令内容寻址 AppContainer worker bundle 的嵌套路径达到 Windows 260 字符边界。缩短后真实 bundle 隔离、RX capability、篡改拒绝和事件循环测试全部通过。

## 10. 验证结果

- Ruff 全仓通过；严格 mypy 通过 235 个生产源码；
- pytest 全量收集 564 项并统一退出 0，包含 12 个既有平台条件 skip；阶段 96 异构 DAG、错误输入槽、CapabilityInput 篡改、v1/v2 兼容和后台自动推进专项通过；
- `0046 → 0045 → 0046` migration 往返、metadata check、默认库升级与 SQLite integrity check 通过；
- AppContainer worker runtime 4 项真实 Windows 隔离专项通过；
- Phase75 11/11，false-success=0，unauthorized-effect=0；新增链向 v7 approval digest 的不可变 v8 baseline，compare 无违规；
- 前端 22 文件/152 项、Vue type-check 和 production build 通过；
- `pip check` 与 `git diff --check` 通过，依赖/锁文件未改变；当前 shell 没有可用的全局 `uv` 命令，未虚报 `uv lock --check`；
- 一轮高负载全库运行曾触发既有跨实例 admission 完全交替时序断言，其两图成功/全局并发 1 安全不变量仍通过；该单项连续复跑 5/5、随后最终全库运行退出 0，未修改其业务语义。
