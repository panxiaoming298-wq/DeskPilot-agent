# 阶段 116B：持久多 Agent 编码循环第二十检查点

## 目标与结论

本检查点把第十六检查点的 recorded 三任务并发提升到真实安装态：三个独立 Python 项目共享唯一 SQLite/TaskLoop，第一代安装 sidecar 在禁用后台调度时只读验证三个 ready Ruff→pytest 计划，经同步关闭和第二代重启后，生产 `WorkbenchRuntimeCoordinator` 以 `concurrency=2` 无浏览器 POST 自动调度。

最终两个健康任务均到达 Final/Delivery，一个确定性 pytest 失败任务仅重试一次后终止，且不阻塞同伴。数据库恢复后是 3 个 Ruff passed、2 个 pytest passed 和 2 个 pytest known-failed Attempt/ResultRef；没有 outcome unknown，没有 Profile 不透明重放，也没有重新规划。

项目方向没有跑偏。本次补的是 Codex 类持久多 Agent 的生产主干能力：跨任务公平调度、真实隔离命令并发、任务级故障隔离、已知失败证据与持久交付，没有新增旁路状态机、自由 Shell 或 cloud 模型权限。

## 第八层不可变契约

- 新增 `deskpilot.workspace-coding-frozen-concurrency-suite.v1`，以第十九检查点 suite digest `d096cdcdf68e92d16f0a55a5ea6e4fef8fa7e838af0de980f2b6acddde2c9df1` 精确绑定前七层资产；新 suite digest 为 `7e83ed9e4e7a4b58f3958af5f0c3f630c7153f3b8d3c224f854720ada905ea3d`。
- 契约冻结三个 24 源文件 Python 项目、每项目唯一 Ruff→pytest 计划、并发 2、两成功/一失败、7 个 Attempt 对 7 个 ResultRef、5 passed/2 failed、一次有界重试、两代安装进程和 `NO_AUTOMATIC_REPLAY`。
- 加载器不仅拒绝前驱 digest 漂移，还会跨层重验 recovery scenario、Profile 顺序、固定工具链目录/digest、生产 Fake `unsupported` 默认与不重放边界。
- 手动 Windows workflow 和 PowerShell wrapper 已纳入第四个 ignored 安装态测试；普通 Rust gate 仍不自动构建/安装 NSIS。

## 真实安装态暴露并修复的两个缺口

### AppContainer profile 并发清理

首次真实并发中，新 launcher 的 `validate()` 会对同一 profile journal 每次执行“启动遗留清理”，因而并发 B 可删除并发 A 正在使用的 AppContainer profile。TaskLoop 正确地把这些执行异常收敛为 `CAPABILITY_OUTCOME_UNKNOWN` 且不重放，但安装态并发因此不可用。

修复后，同一 journal 路径的多个实例共享进程锁，遗留 profile 只在当前 sidecar 进程生命期内 reap 一次。新 sidecar 进程的进程内标记重置，所以强杀后仍会清理旧进程遗留；同一进程的并发 launcher 则不会互删活跃 profile。

### Workbench 跨任务公平波次

隔离冲突修复后，真实调度又暴露一个 2 毫秒级的公平缺口：第二波同时 claim 了“第三任务 Ruff”和“已服务任务 pytest”，异步执行使 pytest Attempt 比第三个 Ruff 早落库 2 毫秒。本检查点没有放宽契约，而是让通用 Workbench queue 优先服务 TaskLoop Node Attempt 持久进度最少的任务，每次只 claim 同一服务轮次。

首个实现曾错用 Workbench Runtime 自身的 `attempt_count`；它计数的是规划、激活和执行在内的所有自动推进，不同生态/路线在 command ready 前就会有不同深度。第一轮后端全量因此稳定捕获了峰值退化为 1。最终修复改为相关子查询统计既有 `task_loop_node_attempts`；无新字段、无新表，跨重启仍可从唯一真值恢复公平轮次。

因此首波仍以并发 2 执行，第三任务随后独占一个 catch-up 波次，其 Ruff verified 后才允许任何 pytest 开始。TaskLoop 状态、Attempt/ResultRef 和执行权限均未改变，也没有增加第二套调度状态机。

## 验收结果

- 八层 Workspace Coding 契约联合专项 41/41，新第八层严格资产 6/6；AppContainer journal、Workbench fair-wave 和第十六检查点并发回归均通过。
- 最终候选安装器/desktop/sidecar SHA-256 分别为 `d0932655805431c75bd50c8daebb7afb46d2c627531d70da336a5fcc9a4a6f69`、`2abd3d7729c96d0afde97023f9327b76119d0322a58c5a7793929202086d6d6b` 和 `102f0a7612c1e5384308ec9a156b4124a49d9858f7074cc79205911701caf1e2`；固定 Python 工具链 digest 仍为 `486ae41dac2a697a792a1ab2584fe66a768bb7ba3b9e695a16ae8ec6fb03dd4c`。
- 同一最终 artifact 的 supervisor 强杀资源、运行中 unknown 不重放、跨步成功恢复和三任务并发四项安装态门禁 4/4 通过；新用例用时 417.46 秒。资源门禁峰值约 387 MiB、932 句柄、5 进程，仍在原冻结上限内。资源采样器另以有界重试排除已退出子进程的 PID 窗口；根进程或持续存活且不可读的子进程仍 fail closed，资源上限未放宽。
- 默认后端收集 116 个测试文件 / 876 项，最终单进程全量 `864 passed + 12 skipped`、失败/错误为 0，用时 1:30:47。首轮全量的唯一失败稳定定位了 Workbench 计数误用，原断言精确复跑、17 项组合回归和最终全量均转绿。
- Rust 默认 `4 passed + 4 ignored`，fmt/check/strict Clippy 通过；前端 24 文件 / 165 项、type-check/build 通过。Ruff 全仓、strict mypy 308 个生产源码、frozen lock、60 包 `pip check`、Alembic/SQLite `0065` current/check 通过。
- wheel 包含 Prompt 33/33 与 Workspace Coding YAML 8/8，不包含测试故障 fixture。Windows Evaluation v2 和 Phase75 v21 无违规，23 份 immutable baseline 未改写。
- 本检查点没有 migration、API、前端页面、baseline 或 cloud activation 变更。

## 边界与下一步

本次仍是 LOCAL-only/Fake seed 与隔离测试仓库，不声称真实模型质量、真实用户仓库长循环、生产激活或 Codex 等价完成。自由 Shell、依赖安装、Node 安装态工具链、自动 push/PR 和登录态浏览器仍不在权限面内。

在没有 115B 五项外部授权时，下一个安全的 116B 纵切应是“安装态并发强杀故障域”：在峰值并发 2 时强杀 sidecar，两个已运行任务必须收敛为 unknown 且不重放，未领取的第三任务在重启后仍能执行并 Delivery。这会闭合“进程故障只污染已领取任务，不扩散到未开始同伴”，仍不需要 cloud 模型授权。
