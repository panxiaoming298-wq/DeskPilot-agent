# 阶段 90：Workspace Reader Agent 与持久输入续接

阶段 90 把阶段 89 的“决定 → Route → Observation → 再决定”骨架推广到第二个真实能力：`workspace_file_read`。新 Plan 绑定 `builtin.workspace_reader@1.0.0`；路径完整时执行两轮严格 Model Turn，路径缺失时由 Agent 产生 `needs_user_input`，服务器持久输入请求并暂停运行，用户下一条回答再创建新的不可变 Task 继续。

这使 DeskPilot 更接近 Codex 式持续对话 Agent，但仍是可审计、有限状态的实现：每次运行最多两个 Model Turn、一次 R0 文件读取，不开放自由 Tool 名称、任意根路径、Shell、动态 argv 或模型自行写入。

## 1. 完整路径的两轮闭环

1. Turn Router rules v3 从普通表达中确定性绑定相对路径；旧 rules v1/v2 摘要继续可读。
2. `Workspace Reader` Turn 1 只能请求 Handoff 授予的 `workspace.file.read.v1` binding，且返回路径必须与服务器参数逐字一致。
3. 服务器通过既有 `WorkspaceFileRuntime` 复核根目录、路径、链接、文件类型、UTF-8 和大小边界，再持久只含路径、字节数和版本/内容摘要的 Observation。
4. 文件正文只作为不受信数据进入 Turn 2，不能变成指令；Turn 2 只能提交与 Observation digest 完全一致的候选结果。
5. 服务器重验结果后才将 Invocation 标为 verified、解锁 final acceptance/delivery，并把原 `WorkspaceFileRead` 证明投影给 Workbench。

历史已经持久化的 capability-node Plan 仍可沿用原直接执行路径；只有新 Plan 选择 Workspace Reader Agent。

## 2. `NeedsUserInput` 不是聊天文本

当用户只说“帮我看看文件”时，系统仍建立 `workspace_file_read` Route，但路径为空。Workspace Reader 的第一轮只能返回固定 Schema 的 `needs_user_input`：

- 问题代码固定为 `WORKSPACE_FILE_PATH_REQUIRED`；
- 阻塞字段固定为 `path`；
- 回答 Schema 固定为 `workspace_relative_file_path.v1`；
- Invocation/Node 进入 `waiting_user`，Run 进入 `paused`，Turn Route 进入 `waiting_user_input`。

`0041_agent_input_requests` 持久化问题、阻塞字段、回答 Schema、Decision 归属和 request digest。用户回答 `README.md` 后，系统不会修改旧 Task，而是创建同 Conversation 的新 Task，以 `agent_workspace_file_path` resolution proof 绑定源/目标消息和参数摘要；输入请求随后标记 `resolved`，旧 Run 被 fencing 取消，新 Run 从完整参数重新执行。

停止或改发无关完整指令会取消未决请求并使旧执行凭证失效；停止后的 Workbench 不会继续伪装成等待回答。读取 Workbench 时会重算 Decision、Observation 和 InputRequest 摘要；任一证明漂移均 fail closed。

## 3. 可复用边界

新增 `AgentModelLoopRuntime` 统一处理 Provider 选择、prepared/dispatching、严格结构化输出、Decision 持久化、usage 结算、Observation 和 outcome-unknown 边界。Workspace Reader 使用该骨架；阶段 89 的研究 Loop 暂不机械重构，以保持已经验收的历史路径稳定。

模型仍不能直接执行能力：应用层只接受一个严格决策，关闭并行 Tool 语义，并在每次 Route 前检查精确 binding 和参数。写能力、审批、receipt 与 unknown-effect 规则没有改变。

## 4. 验收

- 完整路径证明 `request_route → Observation → submit_result` 两轮决策，并保留原 Workspace 文件版本证明。
- 缺路径证明 `needs_user_input → paused → 新 Task resolution → 两轮完成`；输入请求摘要和 Route resolution 摘要被篡改时均返回 409。
- `0041 → 0040 → 0041` 在独立临时 SQLite 完成往返；旧 0040 决策、Invocation、Node 和 Turn Route 状态约束恢复。
- 阶段 75 对抗报告仍为 11/11、false-success=0、unauthorized-effect=0；Registry/Plan cohort 变化以链向 v2 approval digest 的不可变 v3 baseline 记录，compare 通过。
- Ruff 与严格 mypy 通过；前端 22 个测试文件/152 项、Vue type-check 和 production build 通过。前端会显示“等待用户输入”和持久问题，但不会展示隐藏推理。

默认开发 SQLite 没有被本阶段验收命令升级。

## 5. 下一步

下一阶段适合把相同协议推广到目录读取或固定测试 Route，并把通用循环调度从 Workbench 单步入口提取为可恢复的后台推进器。高风险写入仍应先保持“确定性预览 + 用户确认 + receipt”，不要直接并入自主 Tool Loop。
