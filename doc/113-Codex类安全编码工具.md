# 阶段 113：Codex 类安全编码工具

## 1. 结果

阶段 113 已完成 Codex 类安全编码工具的首版闭环：项目根限定的递归搜索、批量读取、Git 只读检查，以及六个服务器注册的 Python/Node Command Profile 已接入阶段 112 的通用 Task Loop。模型只能选择 opaque Offer 和固定 Profile，不能提供 executable、argv、cwd、环境变量、依赖安装或任意 Shell。命令只在断网、可丢弃的项目快照中运行；原项目不会承接测试、类型检查或构建产生的修改。

阶段 113A 的项目搜索/批读/Git 只读能力已以中文里程碑提交 `2ea7d3e 完成阶段113A安全只读编码工具检查点` 收口。113B/113C 在同一阶段分支继续闭合 Command Profile、通用循环、迁移、CI 和全量验收；不自动 push。

## 2. 项目级只读能力

`WorkspaceCodingRuntime` 新增三类 planner-only Route/Capability：

- `workspace_project_search / workspace.project.search.v1`：递归扫描项目内受限 UTF-8 源文件，稳定排序，最多 2,000 个文件、32 MiB 和 200 个匹配；
- `workspace_project_batch_read / workspace.project.read_many.v1`：在同一项目根下批量读取最多 32 个显式文件；
- `workspace_git_inspect / workspace.git.inspect.v1`：只允许 `status`、`diff`、`log` 三个服务器固定操作，执行时间和输出均有界。

所有路径都先经工作区 project-root 解析，再逐级拒绝 symlink、junction 与 reparse point。扫描不跟随链接，也不接受外部 `.git` 目录或 worktree pointer file。Git 调用固定关闭 hooks、pager、optional locks、external diff、textconv 和用户交互，不支持 commit、push、reset、checkout、clean 或调用者提供额外参数。

这些结果均为内容寻址领域对象，保存项目路径、扫描边界、截断状态、文件/匹配摘要、Git operation 与输出摘要。它们通过通用 Capability Executor 生成 verified ResultRef，下游节点只能消费闭合的 ResultRef，不能把模型文本或 UI 文本当成文件/Git 证据。

## 3. 六个服务器 Command Profile

`CommandProfileCatalog` 只注册以下不可变 Profile：

| Profile ID | 固定动作 | 依赖模式 |
| --- | --- | --- |
| `python.pytest.v1` | 对快照运行固定 pytest harness | bundled |
| `python.ruff.v1` | `ruff check` | bundled |
| `python.mypy.v1` | 严格 mypy harness | bundled |
| `node.pnpm_test.v1` | frozen/offline install 后运行 `pnpm test` | offline_frozen |
| `node.pnpm_typecheck.v1` | frozen/offline install 后运行 `pnpm type-check` | offline_frozen |
| `node.pnpm_build.v1` | frozen/offline install 后运行 `pnpm build` | offline_frozen |

Profile 固定 ecosystem、operation、timeout、最大输出、最大进程数、断网、临时快照和依赖模式，并以 `profile_digest` 内容寻址。调用输入只有 `project_path + command_profile_id`；其中 Profile ID 来自服务器预编译 Offer 的 `fixed_parameters`，不允许作为模型参数绑定。六个 Offer 的 intent description 显式带固定 Profile ID，模型可以区分用途，但仍看不到 executable、argv、cwd、环境变量或运行时路径。

Python Profile 使用受保护、内容寻址的 Python runtime bundle；Ruff executable 也属于精确 bundle manifest。Node Profile 使用单独的内容寻址 Node/pnpm bundle、冻结 lockfile 和现有离线 pnpm store。Node compatibility preload 只对已经逐级 `lstat` 且不含 link/reparse 的可信 snapshot/runtime 根保留逻辑路径，其他 realpath 请求继续走原生失败路径；它自身也进入 bundle 摘要，不能静默改写。

## 4. 快照、隔离与回执

命令执行前，服务器从允许的项目根构建最多 2,000 个文件、32 MiB 的 `WorkspaceCommandSnapshot`。快照逐文件绑定 relative path、byte count、content/version digest；完整正文不进入 snapshot digest，摘要和元数据进入内容证明。Node Profile 额外要求 `package.json`、`pnpm-lock.yaml` 和对应固定 script 存在。

执行发生在 Windows AppContainer 中：

- 网络隔离为必需条件，无法取得 AppContainer 时 Profile 不可用；
- 原工作区不映射给子进程，只映射临时项目快照、受保护 runtime，以及 Node Profile 所需的只读离线 store；
- Python/Node 工具链、工作目录和输出上限全部由服务器固定；
- 快照中的构建产物、缓存或源码变更在结束后删除，不回写原项目；
- timeout/cancel 由隔离进程取消路径生成独立 cancellation receipt；
- 普通完成回执绑定 Profile、snapshot、toolchain、exit code、duration、输出摘要/截断、termination reason 与隔离声明。

`WorkspaceCommandExecutor` 会再次验证 exact Profile digest、AppContainer、断网、临时快照和丢弃修改声明。命令失败仍能形成经过验证的失败 ResultRef，随后由阶段 112 reducer 进入有界 Repair；这不表示失败结果正确，也不授予写权限。Command Profile 的 recovery policy 为 `NO_AUTOMATIC_REPLAY`，未知副作用或取消不能透明重放。

## 5. planner-only 单步骤接入 Task Loop

阶段 112 的历史规则保持不变：15 条 legacy Route 的单步骤 Offer 继续走阶段 111 的稳定直接执行路径，2～8 步 Offer 继续走 deferred Task Loop。阶段 113 新增的四类 planner-only Route 没有旧执行器，因此新增一条窄桥：

1. Turn Planner 仍产生原有 `single_step` Adjudication 和 exact bound Plan，不改写阶段 111 Schema；
2. Workbench 识别 exact selected Offer 是否属于 planner-only Route；
3. planner-only 单步不启动传统 `ExecutionRun`，只开放 `plan_task_loop`，并禁用 `start_execution`；
4. Task Loop 重新验证同一 persisted message、Offer、recipe、Binding 和参数证据，且拒绝把任何 legacy 单步迁入该入口；
5. `ModelPlannerComposer` 仅对 planner-only Route 接受 1-step 组合，仍为它生成 namespaced business node、final acceptance 和 delivery；
6. 之后完全复用阶段 112 的 activation、Capability Executor、verified ResultRef、Repair、no-progress、预算与稳定终止。

Alembic `0055_planner_only_single_task_loop` 只把 `model_planner_drafts.step_count` 从 `2..8` 扩为 `1..8`，不修改历史 `0052`。存在 step_count=1 证明时降级守卫拒绝退回 `0054`；空数据 round-trip 会恢复旧约束。端到端测试证明 planner-only 单步会生成 1-step Task Loop、不会创建传统 Execution Run，而 legacy 知识检索单步行为不变。

## 6. CI 与验收

新增 `.github/workflows/phase-113-coding-tools-gate.yml`，锁定 Windows、Python/uv、迁移 head、focused tests、baseline、wheel、默认后端总数、前端总数与 whitespace。阶段 113 最终本地结果为：

- frozen `uv` lock/sync、`pip check`、全仓 Ruff 通过；严格 mypy 验证 282 个生产源码通过；
- 默认后端总计 772 项，`760 passed + 12 skipped`，失败/错误为 0，完整单次运行耗时 3,938.04 秒；12 个 skip 精确对应 PostgreSQL 11 项和 RabbitMQ 1 项外部门禁；
- Alembic 唯一/current head 为 `0055_planner_only_single_task_loop`；upgrade/current/check、SQLite `integrity_check=ok` 和 foreign-key 零违规通过；0055 约束升降级 round-trip 通过；
- Evaluation compare 通过，report digest=`a71fa3e8f79938b44c31a067184302ad68fb485e698a4c58590dc76208d1ca25`；Phase75 v16 仍为 11/11、false-success=0、unauthorized-effect=0，report digest=`ea488c2b74c94845e6718c86f8dc5cfc73bc590eade001dfaea3a0a548d2f82c`；17 份 immutable baseline SHA-256 前后不变；
- wheel 构建通过，Agent Prompt JSON/TXT 24/24 完整打包；
- 前端 frozen install、22 个测试文件 / 157 项、type-check 与 production build 通过；
- 专用 `deskpilot_test` 的真实 PostgreSQL 11/11 通过，包含固定容器重启；测试后恢复 PostgreSQL 原 `exited` 状态；
- RabbitMQ 首轮外部门禁因 readiness 探针未在限定窗口判定 ready 而停止阶段收口；一次性容器已由 `finally` 移除。诊断确认镜像约 8 秒完成 listener 启动且三种 `rabbitmq-diagnostics ping` 调用均返回 0；改用明确 `Server startup complete` readiness 后，完整 RabbitMQ 1/1 通过，临时随机凭据未落盘，容器零残留；
- Workflow YAML、敏感信息、变更范围和 `git diff --check` 在提交前再次检查。

没有覆盖或新增 Phase75 baseline，没有通过关闭 AppContainer、联网安装、放宽路径/命令规则或隐藏重试取得成功。

## 7. 仍然不是什么

阶段 113 是安全编码工具首版，不是自由终端：

- 不支持 arbitrary Shell、PowerShell、cmd、模型提供 executable/argv/cwd/env；
- 不支持 Git commit/push/reset/checkout/clean 或模型修改 hooks/config；
- 不支持联网安装、模型新增依赖、复用用户登录态/凭据；
- 不支持删除项目目录、任意文件覆盖或绕过 exact Patch/Approval；
- 不承诺多任务并行、窗口关闭后台运行、浏览器登录态或桌面应用操作。

## 8. 下一阶段

阶段 114 将把前端单一 task 投影升级为按 Task ID 管理的至少三个并行任务，并增加托盘与受监督本地后端 sidecar。所有 approve/pause/resume/cancel 必须绑定 exact Task/revision；关闭主窗口只隐藏到托盘，明确退出后依靠持久状态在下次启动恢复。阶段 114 不实现 Windows Service，也不承诺机器重启后的无人值守继续。
