# 阶段 116B：持久多 Agent 编码循环第十八检查点

## 目标与结论

本检查点把第十七检查点的 health-only 冻结发布 cohort 提升为第六层“安装态任务语义”门禁。当前源码构建的 PyInstaller sidecar/Tauri NSIS 现在携带服务器固定、内容寻址的 Python Command Profile 工具链；安装后的生产 `SidecarLaunchSpec` 只从固定同级资源目录注入该工具链，不接受模型或调用方提供 executable、argv、cwd、env 或依赖。

测试 harness 先在同一 SQLite 中经公共 Conversation/Workbench API 创建一个停在 `ready` 边界的 Ruff→pytest `WorkspaceCommandPlan`，随后关闭 seed fixture，改由真正安装后的 sidecar、生产 Workbench coordinator、唯一 TaskLoop 和真实 AppContainer 执行。Ruff 通过后产生一个不可变 ResultRef，第二步 pytest 运行时外部强杀完整 sidecar 进程树；第二代 sidecar 在 5 秒租约过期后将该 Attempt 收敛为 `CAPABILITY_OUTCOME_UNKNOWN_AFTER_LEASE`，后续 10 秒稳定观察期间不重放未知命令，也不重复已通过 Ruff。

这证明了安装产物中的固定 Python 执行基座、ResultRef、短租约心跳、外部强杀和 `NO_AUTOMATIC_REPLAY` 边界。它仍不是完整的真实模型 Codex 等价：本次任务由 LOCAL-only/Fake harness 预置，第二步被故意中断，没有宣称安装态完整 Delivery、真实模型质量、依赖安装、自由 Shell、push/PR 或 cloud activation。

## 第六层不可变契约

- 新增 `deskpilot.workspace-coding-frozen-command-task-suite.v1`，以第十七检查点 frozen release suite digest `a6c68304b42e51df539ddd1ae1775f10fe4d6b4f0674ced084bdbb24a48b794b` 精确绑定既有五层资产；第六层 suite digest 为 `ed993889e9cd1c025cd3b19c8eb1de82a992fed95aab03790ed2f172b780ae89`。
- 场景冻结 `python.ruff.v1`→`python.pytest.v1` 顺序、工具链 digest、两代进程、一次外部强杀、一次 supervisor restart、第二步中断、首步一个 ResultRef、5 秒租约及 outcome-unknown 不重放。
- 严格加载器拒绝 release digest、Profile 顺序、进程代际、生产 Fake 安全默认和 replay 边界漂移。生产 Fake Provider 继续保持 `unsupported`；测试不会把 seed fixture 的 recorded command runtime 带进安装态执行。
- 安装态数据库最终必须只有一个 Turn Planner run、一个 ModelPlanner Draft 和一个 WorkspaceCommandPlanBinding；这防止外部强杀被错误实现为重新规划或创建第二套执行状态。

## 固定 Python 工具链与安装接线

- 新增 `python_command_runtime_resource` 构建入口，复用现有 `COMMAND_RUNTIME_DISTRIBUTIONS` 和 `prepare_worker_runtime`，将 Python/pytest/Ruff/mypy 闭包及 Ruff 可执行文件写入内容寻址目录。当前工具链 digest 为 `486ae41dac2a697a792a1ab2584fe66a768bb7ba3b9e695a16ae8ec6fb03dd4c`。
- `build-sidecar.mjs` 在冻结 sidecar 前构建并校验 exact digest 资源，将唯一目录复制到生成的 `src-tauri/rt`；Tauri 将其映射为安装根下固定 `python-command-runtime/`。生成目录被忽略，不进入源码提交。
- Rust resolver 只在安装根存在该固定资源目录时设置 `DESKPILOT_BUNDLED_PYTHON_COMMAND_RUNTIME_ROOT`；后端严格要求资源根恰有一个 64 位十六进制 digest 目录，并逐文件复核 manifest、文件集合、大小和 SHA-256。
- 安装资源首次使用时复制到受保护 runtime root、投射专用 AppContainer capability RX ACL 并再次完整校验；进程内缓存只复用同一个已验证 bundle，不改变每次命令的项目快照、Profile proof 或 ResultRef 摘要。

## 短租约与 Windows 长路径修正

- TaskLoop capability claim TTL 现在是受配置约束的 5～600 秒整数，生产默认 30 秒。候选执行和审批预览期间，运行时按约三分之一 TTL 在同一事务中续租 exact Attempt/node owner、fencing token、revision 和 expiry；丢失任一 fence 即拒绝持久化结果。
- 新增 5 秒租约、超过一个完整 TTL 的阻塞候选回归，确认 revision/expiry 持续推进且 executor 只调用一次。过期 fence、running/outcome unknown 的既有恢复规则不变。
- 安装态首次失败暴露了 4462 个工具链文件在 Windows legacy `MAX_PATH` 下的漏枚举；manifest 加载、文件集合遍历、复制与 SHA-256 复核现统一使用扩展长度路径。复制和哈希按独立文件有界并行，但没有减少任何完整性检查。
- 第二次失败继续暴露 AppContainer 启动前的 runtime mirror 仍使用普通 `pathlib.stat`；沙箱镜像也改为扩展长度 `os.walk/stat/link/copy` 并继续拒绝 symlink/reparse point。对真实失败现场手工复测时，AppContainer Ruff 在约 6 秒内通过；集成测试固定至少一个超过 260 字符的深层 runtime 文件。

## 安装态实测结果

- 最终候选安装器/desktop/sidecar SHA-256 分别为 `eb322fcab911911f8c66140d0d07bdfe5d54908177eadceedc4c2dc3f58d6194`、`3b064d34f8958f54168955e1c5067ffbfcddb49fd4b599b6a3c7ab09331d4079` 和 `1ef05c216bab3b13e4d239a508624dae93f01bb06ecd3b7c50517c5da63a61cb`。
- supervisor 基线 1/1 通过：三代完整进程树均为 5 个进程；working set 峰值为 405,983,232 / 394,039,296 / 393,838,592 bytes，private bytes 为 349,048,832 / 337,477,632 / 337,215,488 bytes，句柄为 931 / 926 / 926。
- 安装态命令任务 1/1 通过：Ruff `passed` ResultRef 的工具链 digest、AppContainer、断网、临时快照和丢弃修改证明全等；pytest Attempt 被外部强杀，第二代只做租约恢复并保持后继 pending，稳定观察期间无透明重放。

## 验证结果

- 六层 Workspace Coding 联合专项 30/30；新增第六层严格资产 5/5。AppContainer、capability runtime、runner settings 和完整 migration 定向回归通过；Alembic 唯一/current head 仍为 `0065_confirmed_change_task_loop`，`check` 无新 upgrade 操作。
- Ruff 全仓、strict mypy 308 个生产源码通过；frozen lock、60 包 `pip check`、wheel Prompt 33/33、六个 Workspace Coding YAML 6/6 与 `git diff --check` 通过。
- 前端 24 个测试文件 / 165 项、type-check/build 通过。Rust `fmt/check/Clippy` 通过；默认库测试 `4 passed + 2 ignored`，两个 ignored 安装态测试已由显式 wrapper 各 1/1 通过。
- 默认后端只读收集 114 个测试文件 / 863 项，统一全量运行到 100%、退出码 0，结果为 `851 passed + 12 skipped`，失败/错误为 0；仅保留既有 Starlette TestClient/httpx 弃用警告。
- 本检查点没有 migration、API、前端页面、Evaluation/Phase75 baseline、PostgreSQL/RabbitMQ 外部 cohort 或 cloud activation 变更。

## 方向判断与下一步

项目没有跑偏，而且比第十七检查点更接近 Codex 类持久 Agent：能力不再只存在于源码测试或健康探针，而是穿过真实打包、安装、固定工具链、AppContainer、ResultRef、进程强杀、租约恢复和禁止重放边界；同时继续复用唯一 TaskLoop，没有增加旁路状态机。

下一批应闭合“安装态完整成功与步骤边界恢复”：新增独立场景，让 Ruff→pytest 都在安装态真实通过，并在首个 verified ResultRef 后、第二步 claim 前通过测试控制点重启 sidecar，最终到达 Final/Delivery；必须证明 Planner/Draft/Binding 仍各一份、首步不重放、第二步只执行一次。完成该单任务闭环后，才适合把第十六检查点的三任务公平并发提升到安装态。不要先扩 Node 工具链、自由 Shell、依赖安装或 push。

真实模型质量仍由 115B/116C 阻塞：没有 Candidate/Judge Provider、代码出站、费用、真人主审/仲裁和激活授权时，cloud-only cohort 必须保持 disabled。
