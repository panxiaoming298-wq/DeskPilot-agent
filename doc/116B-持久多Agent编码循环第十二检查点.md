# 阶段 116B：持久多 Agent 编码循环第十二检查点

## 目标与结论

本检查点把第十一检查点已经闭合的内部三 Task 编码证明链接到现有 Conversation/Workbench 公共入口。用户现在可以在 `POST /api/v1/conversation-turns` 中同时提交自然语言目标和结构化 `workspace_coding` 项目范围，随后只通过同一 Workbench 的推进动作、两次 exact 对话确认以及既有 Patch/Git 审批接口完成任务。

这不是新的编码状态机，也没有新增独立 Workspace Coding API 或前端页面。Explorer、confirmed Reader 和 confirmed write 仍复用既有 Contract/Plan/Run/TaskLoop/Attempt/Invocation/VerifiedResult；结构化入口只负责把用户明确选择的 project、ecosystem 和固定 test path 绑定到第一条不可变 Task/Message。

## Conversation 与 Workbench 纵切

- `CreateConversationTurn` 新增可选 `workspace_coding`：`project_path`、`ecosystem=python|node`、`test_path`。字段缺失时旧 Conversation Route、摘要和行为保持不变。
- Workspace Coding Turn 不经过普通自然语言 Route 猜测。服务端先在配置的 conversation workspace 根内封存 Catalog，再暴露只读 `explore_workspace` Workbench action；模型不能从文本创造项目路径、测试路径、命令、环境变量或权限。
- Explorer snapshot 在浏览器未继续或进程重建时会被启动恢复扫描重新发现。只有尚无 Invocation 的 snapshot/Explorer Run 可自动派发；一旦存在未完成 Invocation 或 Model Turn，投影进入 `explorer_blocked`，继续执行 `NO_AUTOMATIC_REPLAY`。
- Explorer Proposal 的 `confirmation_text` 可直接作为同一 conversation 的下一条用户消息。服务端创建新 Task/Message，并原子绑定 confirmed Reader Plan；confirmation-shaped 文本只要不是逐字匹配就返回冲突，不会退化为普通 Route。
- verified Reader TaskLoop 完成后仍由现有 `propose_workspace_change` action 运行零工具 Change Proposer。第二次 exact confirmation 创建第三个 Task 和 confirmed write Plan；错误 proposal ID 同样 fail closed。
- confirmed Reader/confirmed write 在 Run 尚未激活时也使用现有 TaskLoop execution Workbench 投影，显示节点、ready/pending 数量和可恢复动作；普通 ModelPlanner TaskLoop 的历史 pre-execution 投影保持兼容。

## 执行与恢复缺口修复

- Change Proposer Workbench 只在 confirmed Reader execution 真正 `succeeded` 后出现；“Plan 已确认但 Reader 尚未激活”不再被误报为 proof drift。
- Patch verified 之后，Change Proposal Workbench 与 Activation/claim 使用同一 receipt 规则：从不可变 Reader ResultRef 重建 exact post-patch 完整文件，只允许 Proposal 指定内容变化；额外路径、额外文本变化或 receipt 缺失仍失败即停。
- LOCAL Fake Patch Planner 从服务器请求 metadata 读取 exact confirmed change，用它生成候选；运行时仍逐字段复核 path/old_text/new_text，因此 metadata 或模型输出不能扩权。旧非 confirmed 请求在字段缺失时保持原请求摘要和固定 Fake 行为。
- Patch 和 Git 审批仍是两个独立用户动作，固定 Test 仍由服务器 Profile 执行，Git hooks、签名和 push 继续关闭。

## 隔离用户验收

- Python 隔离 Git 仓库通过公共 API 完成：创建 Workspace Coding Turn → Explorer → 错误/正确文件集确认 → Reader TaskLoop → Change Proposal → 错误/正确变更确认 → Coordinator/Readers/Patch Planners → Patch 审批 → 固定 Python Test → Git 审批/commit → Delivery。
- Node 隔离仓库通过同一公共 API 完成三轮对话并形成 fresh-confirmed write TaskLoop，证明入口、Catalog、Explorer、Reader 和 Change Proposal 不依赖 Python 特例。
- 无浏览器调度的 snapshot 由新建的 Workbench coordinator 扫描恢复，生成同一 Proposal，并停在 exact 用户确认；没有重复 Explorer Model Turn。
- 公共投影返回 Workspace Coding Exploration/Change 的类型化摘要；没有返回 Catalog 内容、ResultRef payload、Plan/Offer 正文或写 authority manifest。

## 最终门禁

- 默认后端实际收集 830 项，代码冻结后的单进程统一运行结果为 `818 passed + 12 skipped`，失败/错误为 0，用时 5192.54 秒；仅保留 1 条既有 Starlette TestClient/httpx 弃用警告。12 个 skip 来自未配置的 PostgreSQL/RabbitMQ 外部 cohort。
- Ruff 全仓、strict mypy 305 个生产源码、`uv lock --check`、60 个 Python 包 `pip check`、Alembic `current/check` 与 `git diff --check` 全部通过；SQLite 当前且唯一 head 仍为 `0065_confirmed_change_task_loop`，没有 schema 漂移或新迁移。
- Windows Evaluation v2 compare 通过，report digest=`a78dbb03240d1483f80ca5ecc1419f9a81a27ec6f1ac2a6181b1aad0ed47fc91`；Phase75 v21 compare 通过，report digest=`805d03c4f4ab5eedb82bb877b4980fa583c7ee700a891b5286ab1bea13d95d53`。23 份不可变 baseline 的 SHA-256 前后一致。
- wheel 构建成功并包含 33/33 Agent Prompt resource；前端 24 个测试文件 / 165 项、type-check 与 production build 全部通过。
- 本检查点没有运行未配置的 PostgreSQL/RabbitMQ 真环境，也没有真实模型网络调用、费用、真人评审或生产激活；默认 skip 与 LOCAL Fake/recorded 验收不被描述为对应生产质量证据。

## 方向判断与下一步

项目方向没有跑偏，而且比第十一检查点更接近 Codex 类持久多 Agent：此前“内部 runtime 能跑”的三任务链现在已经成为用户可发起、可观察、可确认、可恢复并能完成实际本地 Git 交付的 Conversation/Workbench 纵切。核心仍是一个可证明的持久执行主干调度职责 Agent，而不是多个模型自由聊天或旁路 Shell。

本检查点仍不等于 Codex，也不宣称 116C 或生产完成。当前验收使用 LOCAL Fake/recorded runtime 和隔离仓库；真实模型在真实仓库上的成功率、长上下文质量、成本和长时间稳定性仍无证据。下一批应把本检查点的 API 流程提取为版本化 Python/Node 黄金任务，增加跨真实 API 进程的审批中断/恢复和长时间 soak，并继续修复可用性与恢复问题。没有 115B 五项授权时不得执行 cloud live capture、Production Admission 或 activation。

自由 Shell、依赖安装、create/rename、自动 push/PR 和 cloud activation 均未开放；未来若增加，必须作为新的显式 Capability 和审批范围，不能借 Workspace Coding 对话入口扩权。
