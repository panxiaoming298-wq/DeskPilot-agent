# 阶段 116B：持久多 Agent 编码循环第七检查点

## 1. 检查点结论

第七检查点完成了“受控探索 → 候选文件集 → 用户确认 → 新不可变只读计划”的持久授权内核。项目不再只能在首轮 Route 中预知全部 exact path：服务器可以先封存一个项目快照，接受一个无执行权 Explorer 候选，等待同会话用户精确确认，再原子生成一个新的 generation-1 Reader Plan。

这一步让系统更接近 Codex 类持久多 Agent 的关键点不是新增了一个 Agent 名称，而是把“模型建议哪些文件值得调查”和“系统真正允许哪些节点读取”分成两道可恢复、可对账的边界。Proposal 不是权限；只有用户确认与服务器编译 Plan 同时持久化成功，候选路径才成为后继 Reader 节点的 exact mapping。

本检查点仍不是 116B 完成态。Explorer 的真实 Model Turn/Handoff、后继 Reader TaskLoop activation，以及 Reader verified join 到 Patch 提案/再次确认的执行入口尚未接通；现有完整 Coordinator/Reader/Planner/Patch/Test/Git/Delivery 链也没有被新探索路径自动启动。

## 2. 服务器封存的探索快照

- `WorkspaceCodingRuntime.exploration_snapshot` 只在配置的 workspace/project 根下扫描，继续拒绝 symlink、junction、reparse point 和路径逃逸。
- Python 只接受 `.py/.pyi`，Node 只接受 `.js/.jsx/.mjs/.cjs/.ts/.tsx/.mts`；项目必须至少有两个同生态文件。
- 快照最多保存 256 个文件元数据、扫描最多 2,000 个候选文件和 32 MiB 内容；持久清单只含相对路径、字节数、内容摘要、版本摘要和逐文件 proof，不保存文件正文。
- 快照绑定 exact Task、用户消息及其摘要、目标摘要、项目路径、生态、固定测试路径、Catalog digest、扫描计数、截断位和创建时间。
- 每次接受 Proposal、确认或读取 Workbench 投影前都会重新构造当前快照并全等比较；任何文件、路径、目标或源消息漂移均 fail closed。

## 3. 无权限 Explorer Proposal

- 新增不可变 `builtin.workspace_coding_explorer@1.0.0` 与独立 Prompt Package，仅允许 LOCAL、R0、一次模型调用、零工具调用。
- Explorer decision 只能按规范顺序选择 2～8 个已存在于 snapshot 的 exact path，并逐项携带服务器 file proof digest 与简短理由。
- `WorkspaceCodingExplorationProposal` 绑定 exact Explorer Contract/Prompt、snapshot、Decision 及所有摘要；Proposal 本身不授予 read、Patch、test、Git、Shell、网络或 approval 权限。
- 当前 Binder 已能验证并持久化 Explorer decision，但尚未把它接到标准 Agent Model Turn/Invocation；因此本检查点只证明授权内核，不宣称真实 Explorer Agent 已可从对话自动运行。

## 4. 用户确认与原子 Reader Plan

- 唯一确认文案为 `确认候选文件集：<proposal_id>`，必须来自同一 Conversation 中一个新的用户 Task/Message，Task goal、消息正文和消息摘要必须完全一致。
- 确认后服务器编译新的 R0 Task Contract，只包含 `workspace.file.read.v1`；明确关闭 Patch、test、Git、Shell、动态代码、依赖安装和自动 push。
- Draft 恰好包含 2～8 个互不依赖的 `builtin.workspace_reader` 节点、一个依赖全部 Reader 的 Final Acceptance 和一个 Delivery 节点。
- 每个候选路径与 exact generation-1 Plan node ID/local key/node spec digest 形成不可变 mapping；路径、proof、ordinal 或 node 任一漂移都会拒绝。
- PlanCompilation 的 Contract、Draft、Executable Plan 与 file-set binding 在同一数据库事务中提交。错误确认会回滚全部 Planning 记录，不留下半个 Contract 或 Plan。
- 重启后会同时回查 snapshot、Proposal、Contract、generation-1 Plan 和 mapping；重复确认返回相同 binding，不创建 generation 2，也不重复已持久化计划。

## 5. Schema 与 Workbench

Alembic head 升级为 `0061_workspace_coding_explorations`，新增三张不可变证明表：

- `workspace_coding_exploration_snapshots`
- `workspace_coding_exploration_proposals`
- `workspace_coding_file_set_plan_bindings`

任一表存在记录时 downgrade 都会拒绝，避免丢失探索授权、用户确认或 Plan lineage。

现有 Task Workbench 新增可空 `workspace_coding_exploration` 投影。源 Task 和已确认的后继 Task 都能读取同一条脱敏证明链：阶段、项目/生态、候选路径与理由、精确确认文案、binding 和 generation-1 Plan 摘要。旧投影没有该字段时继续按原 digest 读取；新投影则把该字段纳入顶层摘要。没有新增第二套执行状态机，也没有自动动作或前端页面。

## 6. 验收结果

- 新增 3 项持久纵切测试，覆盖 snapshot/proposal/confirmation、错误确认原子回滚、项目漂移、跨表篡改、重启恢复、幂等确认和源/后继 Workbench 对称投影。
- 默认后端实际收集 818 项。首轮只有 Windows Evaluation 延迟门发生一次系统抖动失败；空载专项立即通过且未修改基线。最终第二轮单进程统一运行 `806 passed + 12 skipped`、失败/错误为 0，耗时 1:23:39，仅保留既有 Starlette/httpx 弃用 warning。
- Ruff 全仓通过；strict mypy 通过 298 个生产源码；`uv lock --check` 与 `pip check` 通过。
- SQLite 已升级到唯一/current head `0061_workspace_coding_explorations`；`alembic check` 无新操作，`integrity_check=ok`、foreign-key 零违规，完整 migration 测试通过。
- 当前环境未配置 PostgreSQL 专用 URL 且无 Docker 命令，因此 12 个 PostgreSQL/RabbitMQ 外部 marker 按默认规则 skip；本检查点不冒充执行过真库 cohort。
- Phase75 追加不可变 v20 并精确链接 v19 approval digest；11/11、false-success=0、unauthorized-effect=0，report digest=`65f2195aacb8a5cc22603b9b5a387ef0681a3d28dac20c6b352cf4a89908b043`。
- Windows Evaluation v2 最终 compare 通过，report digest=`6a4a8d5d5ea2f682968ec1ddb0a9132218c4a13ae110fdc67862fc94af5e1c9d`；没有因一次延迟抖动放宽 baseline。
- wheel 内 Prompt resource `31/31`；前端未修改，24 个测试文件 / 165 项、type-check 与 production build 通过；`git diff --check` 通过。

## 7. 方向校准

项目方向没有跑偏，原因是本检查点继续强化 Codex 类系统最稀缺的四个属性：

1. 模型 Proposal 与执行权限严格分离。
2. 用户确认绑定 exact 内容和同会话 lineage，不使用模糊“已确认”状态。
3. 多 Reader 的路径与 Plan node 内容寻址，可在进程重启后恢复和验真。
4. 文件或数据库证据漂移在执行前失败，不把内存对象或 UI 状态当作真值。

仍需避免一个错误方向：不能把当前 Binder 中由调用方提交的 decision 描述成“真实 Explorer 已运行”，也不能把只读 Reader Plan 描述成已闭合 Patch/Test/Git。自由 Shell、模型提供 argv/env、自动依赖安装、自动 push、cloud activation 和 116C 真实模型质量均未进入本检查点。

## 8. 下一执行入口

下一检查点按以下顺序接通真实执行，不先扩大工具权限：

1. 让 `builtin.workspace_coding_explorer@1.0.0` 通过标准持久 Invocation/Model Turn 消费 exact snapshot，Binder 只接受该 Turn 的 verified Decision，而不是任意调用方 decision。
2. 把 prepare/proposal/confirmation 接入 Turn Planner 与现有 Workbench 动作，使用户在同一对话看到候选并提交 exact 确认；仍不新增独立页面。
3. 用现有 TaskLoop activation 为后继 Plan 创建逐 Reader input proof，绑定 exact project/path/file proof/node mapping；进程重启不得重复已通过 Reader。
4. 只有全部 Reader ResultRef verified join 后，才允许生成精确 Patch 候选并请求新的写入确认，再复用现有 Patch/Test/Repair/Git/Delivery 链。
5. 任何 snapshot drift、旧 lease、迟到 ResultRef 或 outcome unknown 都必须终止当前授权，重新探索时创建新的不可变后继 Task/Plan，不透明重放。

这一入口完成前，116B 继续标记为进行中。
