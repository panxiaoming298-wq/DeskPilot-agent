# 阶段 98：服务器绑定固定测试 Agent 任务图

## 1. 本阶段结论

阶段 98 把阶段 82/83 已经真实验收的 Python pytest 与 Node `node:test` 沙箱接入动态 Agent 图。模型可以决定是否使用服务器公布的测试能力、节点依赖和完整 DAG，但不能提交 executable、argv、环境变量、依赖安装命令、网络权限或原项目写权限。

```text
user Route: directory + Python project/test + Node project/test
                 ↓
server offers named capability input slots
                 ↓
model proposes capability + input_source + dependencies
                 ↓
Supervisor seals exact project/test paths and Route digest
                 ↓
Workspace Tester Agent
                 ↓
bounded snapshot → fixed runtime/argv → offline AppContainer
                 ↓
typed test ResultRef + verified join → directory output
```

这不是自由 Shell，也不是模型生成命令。测试进程仍只运行一个用户明确提供且通过路径规则的测试文件。

## 2. 组合 Route 与明确输入

新增确定性组合语法：

```text
分析并测试工作区：. Python项目：backend Python测试：tests/test_sample.py Node项目：frontend Node测试：tests/sample.test.js
```

它继续使用 `workspace_directory_analyze@1`，但 Route 参数额外冻结：

- `python_project_path` / `python_test_path`；
- `node_project_path` / `node_test_path`；
- 原有目录 `path`。

只有对应的一对项目/测试参数同时存在，Supervisor 才公布 `route_python_test_spec` 或 `route_node_test_spec`。普通目录 Route、旧的目录+文件分析 Route 不会因此得到测试输入。

Task Contract 新增 `workspace.python.test.v1`、`workspace.node.test.v1`，风险仍为 R0，并加入 `server_bound_fixed_test_inputs_v1` 与 `fixed_executable_and_argv_v1` 约束。四个动态 Child 加上原有三节点 Plan 恰好消耗冻结的 Model/Tool/Token/Wall/Handoff/Plan-node 预算上限，模型不能扩容。

## 3. CapabilityInput v2 与 v4 图

测试输入使用 `deskpilot.agent-task-graph-capability-input.v2`，绑定：

- 命名 source key；
- Route 中精确的 project path 与 test path；
- 组合参数 source ref；
- `python_test` / `node_test` kind；
- 完整 Route parameter digest；
- CapabilityInput digest。

读文件/目录继续使用兼容的 CapabilityInput v1。新图升级为 `deskpilot.agent-task-graph.v4`，允许同一不可变 manifest 混合 v1 读取输入和 v2 测试输入；历史 v1/v2/v3 图继续按原摘要规则读取。

模型 proposal 的 `input_source` 仍是严格枚举。把 Python Capability 配到 Node source，或改写项目、测试路径、Route digest、节点 input manifest/digest，都会在快照或测试进程创建前拒绝。

## 4. 独立 Workspace Tester Agent

冻结 Registry 新增 `builtin.workspace_tester@1.0.0`：

- 只提供 `workspace.python.test.v1` 与 `workspace.node.test.v1`；
- 只能接收 `workspace_coordinator@1.1.0` 的受控 Handoff；
- 没有 Tool grant，最大两次 Model Turn 与一次固定 Route；
- 只允许本地 Model Provider；
- Prompt 明确把测试输出和上游 payload 视为不可信数据。

Reader 与 Tester 使用不同 Agent Contract。旧目录/文件节点仍绑定 `workspace_reader@1.2.0`，因此没有因为测试能力加入而扩大 Reader 权限。

Tester 的第一轮模型只能回显服务器绑定的 Route binding、project path 和 test path；服务器随后调用既有 `prepare_python_test` / `prepare_node_test` 创建有界快照，并将快照交给既有固定 Runtime。第二轮模型只能绑定 observation digest 提交结果。

## 5. 固定执行边界

Python 节点继续使用阶段 82 的固定 pytest harness：

- 单个 `tests/test_*.py` 或 `*_test.py`；
- 内容寻址 Python Runtime 与明确 distribution 白名单；
- 固定 pytest 参数，清空项目 addopts，禁止插件自动加载；
- 断网 Windows AppContainer、单进程、512 MiB、60 秒、32 KiB 输出。

Node 节点继续使用阶段 83 的固定 profile：

- 单个 `*.spec.js` 或 `*.test.js`；
- 内容寻址固定 `node.exe`；
- argv 固定为 `--preserve-symlinks --preserve-symlinks-main <test>`；
- 不包含 `node_modules`，禁止 npm/npx/package scripts/loader；
- 同样的断网 AppContainer、进程/内存/时间/输出边界。

测试断言失败会产生 `status=failed` 的有效测试证据，不会被伪装成测试通过；隔离、快照或 Runtime 故障才使 Agent Route 失败。

## 6. 类型化结果与 Workbench

`AgentTaskGraphResultRef.result_kind` 新增 `python_test` 与 `node_test`。服务器读取 ResultRef 时会重新验证：

- Child Invocation、Agent/Capability、Result 与 graph lineage；
- `WorkspaceAgentResultRecord` kind、完整结果 schema 与 result digest；
- Agent output 中的 project/test/status/result digest；
- persisted ResultRef manifest/digest。

Workbench 的动态图节点新增 `test_result` 证明投影，展示 status、通过/失败计数、snapshot/runtime digest、断网与单进程事实。修改测试 Runtime digest 后，整个 Workbench 读取返回 409，不展示伪造证据。

全量高负载回归还捕获到后台 Coordinator 提交期间的跨版本投影窗口。Workbench 现在会在证明冲突时有界地重新读取整份投影；只有某一次完整读取的全部证明同时成立才返回 200，稳定篡改在达到上限后仍返回 409。正常读取不增加重试。

## 7. 端到端验收图

专项 Provider 动态生成：

```text
directory_scan ─┐
python_test ────┼─→ directory_join [OUTPUT]
node_test ──────┘
```

三个根节点可并行执行；输出目录节点必须依赖全部根节点。最终 Parent 只能消费服务器验证后的四个 ResultRef，Route 输出仍是目录类型，测试节点不能替换 Task output contract。

验收同时证明：

- Python/Node 节点精确绑定两个不同项目与测试文件；
- 两个 Tester Invocation 均保留 snapshot/runtime/隔离证据；
- Python 能力选择 Node input source 时图在执行前失败且不创建 Child graph；
- 测试结果 manifest 篡改后 Workbench fail closed；
- 旧任意 DAG、异构目录/文件图、暂停/停止和 Phase 97 Replan 回归通过。

## 8. 迁移与默认开发数据库

`0048_agent_test_capability_inputs` 只扩展 `workspace_agent_results.result_kind` 检查约束，允许 `python_test` / `node_test`；没有保存 executable 或 argv 的新列。

默认 SQLite 已从 `0047_agent_replans` 升级到 `0048_agent_test_capability_inputs (head)`。升级前备份：

```text
backend/data/deskpilot.pre-0048-20260822-215621.db.bak
SHA-256 7D6A78D941A5709E6CFAEA9ED20078CE70951F2800C27F7DF1FFC2F06FF9A09C
```

`0048 → 0047 → 0048` 往返、空库 migrate、Alembic metadata check 与 SQLite `integrity_check=ok` 已通过。

## 9. 与 Codex/Marvis 的距离

系统现在能在单次动态图中组合读取、真实代码测试、并行 join、失败后新 Plan generation Replan，并让后台 Coordinator 在页面关闭后继续工作。这已经具备“模型拆任务、服务器授予能力、专业 Agent 执行、证据驱动继续”的核心形态。

仍然刻意不支持：

- 模型生成或修改 executable/argv；
- npm/npx、联网安装、第三方 Node 测试框架或自由 Shell；
- 测试进程写回原项目；
- 运行中改图、条件分支或节点级补丁批准；
- 把 failed test 自动升级为文件修改授权；
- 跨 generation 自动导入旧结果而不重新验证 source-ref。

下一阶段优先在 Replan failure snapshot 上增加无授权的结构化 Parent repair advice，以及只读、重新验证的跨 generation ResultRef source-ref import。之后再设计“补丁提案 → 用户批准 → 固定测试 → 验证”的 Contract amendment，而不是从测试失败直接获得写权限。

## 10. 验证结果

- Ruff 全仓与严格 mypy 238 个生产源码通过；
- Phase 98 固定测试图、越权 input source、测试结果篡改、旧图兼容和 Workbench 整文件回归通过；
- `0048 → 0047 → 0048` migration、空库迁移、Alembic 单一 head/check 与默认库升级通过；
- Phase75 11/11，false-success=0，unauthorized-effect=0；链向 v9 approval digest 的 v10 baseline compare 无违规；
- 前端 22 个测试文件 / 152 项、Vue type-check 与 production build 通过；
- 后端 pytest 全量收集 81 个测试文件 / 570 项并退出 0（12 个既有平台条件 skip）；
- `pip check` 与 diff whitespace 通过。
