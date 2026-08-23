# 阶段 100：批准式 Agent 补丁与固定测试闭环

## 1. 本阶段结论

阶段 100 完成了阶段 99 预定的受控写入闭环。新 `workspace_agent_patch_test@1` Route 允许本地 Model 读取一个用户显式指定、位于固定测试项目内的文本文件，提出一次精确替换；提案本身没有写权限。服务器先在隔离 staging 中生成 before/after/manifest，只有用户确认当前 `confirmation_digest` 后才原子写入原工作区并保留备份，随后立即运行服务器绑定的 Python pytest 或 Node `node:test` 文件。

```text
explicit target/project/test/objective
                 ↓
Patch Planner Turn 1: request exact file Route
                 ↓
server read → immutable Observation
                 ↓
Patch Planner Turn 2: propose one old_text/new_text
                 ↓
isolated before/after/manifest → PAUSED
                 ↓
user confirms exact confirmation_digest
                 ↓
atomic commit + backup → fixed offline test snapshot
                 ↓
WorkspacePatchTestRead: verified / test_failed / test_error
```

这不是自由代码执行，也不是模型获得工作区写权限。Model 不能选择第二个文件、测试命令、executable、argv、环境变量、依赖安装或网络权限。

## 2. 确定性 Route 与显式输入

新增命令格式：

```text
修复并测试工作区：文件："backend/src/example.py" Python项目："backend" Python测试："tests/test_example.py" 目标：修复失败测试
```

Node 版本把两处 `Python` 替换为 `Node`。Route 冻结：

- 单个目标 `path`；
- `project_path` 与单个 `test_path`；
- `python` / `node` 测试种类；
- 只作为 external-untrusted 数据的目标描述；
- 完整 parameter digest。

目标文件必须位于被测试项目内。测试文件仍经过阶段 82/83 的既有路径、类型、数量和大小限制；目标或项目越界会在 Model 获得任何执行机会前拒绝。

## 3. 无授权 Patch Planner

冻结 Registry 新增 `builtin.workspace_patch_planner@1.0.0` 和 `workspace.patch.propose.v1`：

- 只允许本地 Model Provider；
- 最多两次 Model Turn、一次服务器文件读取；
- Tool grants 为空，最大风险为 R0；
- 无 Handoff、Shell、动态代码、网络或写入能力；
- 输出严格为一次文件 Route 请求或一次单文件精确替换建议。

第一轮必须回显服务器绑定的 file Route，且不能附带测试路径。服务器完成读取后持久化 Decision、Observation 和文件版本证明。第二轮只能为同一路径提交一个 `old_text → new_text`；源文本必须非空、在当前文件中恰好出现一次，且替换不能是 no-op。

Model 的 `submit_result` 只表示“提交候选建议”，不表示补丁已批准或已执行。数据库 Decision kind 继续使用既有兼容枚举，因此本阶段没有 schema migration。

## 4. 隔离预演与一次批准

服务器复用阶段 80 的 Workspace Patch staging 与 Windows 原子替换边界，但为本 Route 把变更数严格限制为 1。预演会生成：

- 原文件与建议结果的隔离副本；
- 绑定任务、文件版本、原/新内容摘要的 manifest；
- 精确 diff 与 `confirmation_digest`；
- 用户可检查的 Workbench 审批卡。

预演完成后 Invocation、Node、Run 和 Route 分别进入 `waiting_user` / `waiting_user` / `paused` / `needs_user_action`，原文件保持不变。错误摘要、外部并发编辑或已改变的 staging proof 都会拒绝提交。

确认时服务器不只比较前端传回的 digest，还重新验证：

- Route parameter 与 preview/manifest；
- Handoff、Invocation、第二轮 Model Turn；
- Decision manifest/digest/binding；
- 第一轮 Observation 的 binding、状态、ResultRef、文件路径、内容和版本摘要；
- Model proposal 与 staging diff 的逐字段一致性。

因此复制旧按钮请求、篡改持久提案或替换 preview 都不能获得写权限。相同确认在完成后返回同一持久回执，不会重复写入。

## 5. 原子提交后的固定测试

批准只覆盖当前单文件补丁。服务器先按阶段 80 的规则全量预检、原子替换并保留安全备份，然后从已提交后的工作区建立新的有界测试快照：

- Python 使用固定 pytest-file 协议；
- Node 使用固定内置 `node:test` 协议；
- executable、argv、runtime digest、网络关闭、进程/内存/时间/输出限制仍由服务器固定；
- 不开放 npm/npx、package scripts、联网安装或自由 Shell。

`WorkspacePatchTestRead` 同时绑定确认摘要、已提交 PatchReceipt 和固定测试完整结果。只有测试 `passed` 才形成 `verified` 并解锁 final acceptance/delivery。

如果测试为 `failed` 或运行错误：

- Route/Run/Node 如实进入 failed/blocked；
- 已发生的工作区写入和备份回执继续展示，绝不伪装成“没有修改”；
- 不自动 Replan、不生成第二个补丁、不扩大 Capability；
- Repair Advice、测试输出、Memory、Summary、UI 或旧 ResultRef 都不会因此获得新写权限。

## 6. Workbench 与前端

Workbench 把初始阶段显示为“生成受约束补丁建议”，预演后显示为“确认补丁并运行固定测试”。审批卡明确标注：

- 建议本身没有写权限；
- 只提交一个精确替换；
- 确认后会运行服务器固定测试；
- 测试失败会保留真实写入事实，不会自动继续修改。

完成后沿用现有 `workspace_patch` 与 `workspace_python_test` / `workspace_node_test` 投影，因此没有新增前端 API 动作，也没有让浏览器参与证明裁决。

## 7. 持久化与数据库版本

本阶段复用既有 Turn Route、Agent Handoff/Invocation/Turn/Decision/Observation/Result、Workspace staging/PatchReceipt 和固定测试结果结构，没有增加表或列。Alembic 唯一 head 继续是 `0048_agent_test_capability_inputs`。

默认开发 SQLite 已处于 `0048`，`alembic check` 无待生成操作，`integrity_check=ok`。不需要为阶段 100 再执行升级；启动前保留 `alembic current` / `alembic check` 检查即可。

## 8. 验证结果

- Phase 100 正向闭环、错误确认摘要、持久提案篡改、测试失败保留写入事实且无 Replan 专项通过；
- 后端 pytest 全量收集 81 个文件 / 575 项，统一首轮退出 0，12 个既有平台条件 skip；
- Ruff 全仓、严格 mypy 238 个生产源码通过；
- Phase75 11/11，false-success=0、unauthorized-effect=0，链向 v11 approval digest 的 v12 baseline compare 无违规；
- 前端 22 个测试文件 / 153 项、type-check 和 production build 通过；
- Alembic 单一 `0048` head/current/check、SQLite `integrity_check=ok`、`pip check` 与 diff whitespace 通过。

## 9. 与 Codex/Marvis 的距离

系统现在拥有“动态只读/测试 DAG → 失败快照与跨代 verified evidence → 受限 Agent 补丁建议 → 用户一次批准 → 原子写入 → 固定测试 → 真实成功/失败证明”的连续工作链，已经能在持续对话中完成一类真实修复任务。

它仍不是“任意任务图、任意代码权限”的 Codex/Marvis：当前补丁闭环是单目标、单替换、单固定测试的受信 Route，尚未成为动态图中的通用批准节点；没有 live-model/Judge-human 校准、运行中条件分支、多个批准补丁代、自由 Shell、依赖安装或登录态浏览器。下一阶段应把同样的批准边界抽象为服务器裁决的图节点/Contract amendment，并让每一轮新补丁都要求新的精确授权，而不是放大当前 Route 的权限。
