# 阶段 116B：持久多 Agent 编码循环第十五检查点

## 目标与结论

本检查点把第十四检查点的手动 API 进程重建推进为真实桌面监督链证据：生产 Rust `SidecarSupervisor` 启动真实 Uvicorn 测试进程，在持久 WorkspaceCommandPlan 的安全 `ready` 边界被外部强杀后自动退避并拉起第二代进程；新进程只依靠同一 SQLite 真值和生产 `WorkbenchRuntimeCoordinator` 在后台完成剩余命令链，测试客户端不再发送推进 POST。

当前结论仍限定为 Windows、LOCAL-only/Fake/recorded runtime 与隔离测试项目。它证明真实 supervisor、进程树终止、API 重建和后台恢复的组合语义，不把短时 canary 冒充数小时/数日 soak，也不把源码虚拟环境测试进程冒充冻结 PyInstaller/NSIS 安装包。

## 严格版本资产

- 新增 `deskpilot.workspace-coding-sidecar-soak-suite.v1`，以 `resilience_suite_digest` 精确绑定第十四检查点的 `workspace_coding_resilience_v1.yaml`；前序故障矩阵变化后本场景立即 fail closed。
- 场景继续绑定相同 project、两个唯一 Command Profile、`max_advances` 和 `NO_AUTOMATIC_REPLAY` 合同，并冻结 5 秒观测窗口、250 毫秒轮询、一次预期重启、两代进程与 supervisor 三次重启预算。
- 加载器同时拒绝 digest 漂移、project/Profile/推进预算跨权、未知字段、YAML alias/anchor、symlink、非 UTF-8 和尺寸越界。
- 资产只描述验收期望，不提供 executable、argv、cwd、环境变量或命令权限。

## 真实 supervisor 强杀恢复纵切

1. Rust 单测从 exact 后端虚拟环境启动测试专用 Uvicorn fixture，实际执行生产 `SidecarSupervisor::start/supervise/spawn_sidecar/wait_for_retry/terminate_process_tree` 路径。
2. 第一代 API 经普通 `POST /conversation-turns` 和已有 Workbench action 形成两步 WorkspaceCommandPlan，并停在首个 Command Profile 的持久 `ready` 边界。
3. 测试只用公共 Workbench GET，以 250 毫秒间隔持续 5 秒观察 Task、TaskLoop execution、两个 Command node、Attempt 与失败回执状态完全稳定；Planner 只调用 1 次，Command Runtime 尚未调用。
4. 测试把下一次启动的 Workbench runtime 控制文件切换为 `true`，随后用 Windows `taskkill /T /F` 外部终止第一代 sidecar 进程树；不是调用 supervisor 的正常 shutdown。
5. supervisor 观察异常退出，发布一次 `Backoff`，再发布第二代 `Starting/Running`。第二代 Uvicorn 使用相同 SQLite、workspace、receipt 和调用账本启动。
6. 新进程的生产 `WorkbenchRuntimeCoordinator` 从持久可恢复 TaskLoop 自动入队并完成 Ruff、mypy 两步；恢复后测试只发送 GET，不调用 `workbench:advance`。
7. Task 与 TaskLoop 均成功后，再持续 5 秒观察持久命令状态不变；Planner 总调用数仍为 1，Command Runtime 调用顺序精确为 Ruff、mypy，各 1 次。
8. 最终 supervisor 正常 shutdown，状态为 `Stopped`，第二代 API 不再可达；全程没有第三代进程或 `Failed` 状态。

## 稳定真值口径

首轮测试曾把 Workbench 顶层 `projection_digest` 误作后台完成过程中的恒定值。真实运行证明，当 TaskLoop 已先到 `succeeded`、Task 终态尚在同一后台循环收口时，顶层投影会合法变化。

最终门禁改为先等待 `task.status=succeeded` 与 `task_loop.execution_status=succeeded` 同时成立，再比较 Task、Execution、Command node sequence/Profile/status、Attempt count、成功 Result 和失败回执计数组成的持久业务真值。这里没有放宽状态一致性，而是避免用仍允许合法推进的聚合投影冒充终态。

## 测试装配边界

- `SidecarLaunchSpec` 的测试 arguments/environment 字段只在 Rust `cfg(test)` 编译；生产构建中字段不存在，`resolve()` 仍只接受桌面可执行文件同目录的固定 sidecar 名称。
- Python fixture 的 Workbench runtime 控制文件只位于 `tests/fixtures`。未提供该文件时，第十四检查点原有 `Settings()` 行为保持不变。
- 测试环境继续由 supervisor 先 `env_clear()`，再填固定系统变量和生产安全开关；测试专用值只用于隔离数据库、workspace、固定 Profile 和本地 Fake Provider。
- 新增的 `serde_json` 只是 Rust dev-dependency，既有锁中已有同版本包，不进入产品运行权限面。

## 验证结果

- 严格 sidecar suite 4/4、旧第十四检查点韧性 8/8、Planner/Workbench 协调器 17/17，联合专项 29/29 通过。
- Rust 真实 supervisor 纵切单项通过；最终 Rust 全量 4/4、严格 Clippy 和 `cargo fmt --check` 通过。全量中的纵切用时 77.62 秒，包含两次真实 API/Runner 冷启动和前后各 5 秒墙钟观测。
- Ruff 全仓与 strict mypy 307 个生产源码通过。
- 默认后端只读收集为 111 个测试文件 / 845 项，完整单进程运行到 100%、退出码为 0，冻结结果为 `833 passed + 12 skipped`、失败/错误为 0；仅保留 1 条既有 Starlette TestClient/httpx 弃用警告。12 项仍精确对应未配置 PostgreSQL/RabbitMQ 专用环境时的外部 cohort 安全跳过。
- Windows Evaluation v2 compare 无违规，report digest=`6bb2f0b28a40cf0ea8c05096204c2ed1be54bb583122e12407baef45036d22b0`；Phase75 v21 compare 无违规，report digest=`805d03c4f4ab5eedb82bb877b4980fa583c7ee700a891b5286ab1bea13d95d53`，immutable baseline 未修改。
- Alembic/SQLite 唯一/current head 均为 `0065_confirmed_change_task_loop`，`alembic check` 无新增 upgrade 操作；本检查点不新增 migration，完整 migration 回归已包含在后端全量中。
- `uv lock --check`、60 包 `pip check` 与 wheel 构建通过。wheel 包含 Prompt 33/33，基础/韧性/sidecar-soak Workspace YAML 各唯一 1 份，测试故障服务器/fixture 为 0 份；最终 `git diff --check` 通过。

## 方向判断与下一步

项目没有跑偏。本检查点把“窗口关闭后仍运行”的桌面承诺与持久 TaskLoop 真值真正接在一起：GUI、sidecar 进程和 API 实例都不是正确性来源；只有持久 Plan/Node/Attempt/ResultRef 和服务器 proof 能决定恢复位置。进程在安全边界消失后可由 supervisor 替换，已确认的 Planner 与命令步骤不会重放。

下一批应转向更多可抛弃中型 Python/Node 仓库，以及最多三个并发任务下的公平性、资源上限和恢复隔离；同时保留更长墙钟 soak 与冻结 sidecar/NSIS 同链验收作为发布 cohort。仍不得开放自由 Shell、依赖安装、自动 push 或 cloud activation；115B 外部授权未闭合前不执行 116C 真实模型质量结论。
