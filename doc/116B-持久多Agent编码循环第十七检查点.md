# 阶段 116B：持久多 Agent 编码循环第十七检查点

## 目标与结论

本检查点把第十六检查点的四层 Workspace Coding suite 提升为第五层、显式 opt-in 的 Windows 发布 cohort。门禁从当前源码构建 PyInstaller one-file sidecar 和 Tauri NSIS，静默安装到校验过的系统临时目录，再由生产 `SidecarLaunchSpec::resolve` 只选择安装目录中的固定同级 `deskpilot-backend-sidecar.exe`。

安装态 sidecar 连续运行三代，每代健康后观察 30 秒；第一、第二代各被外部 `taskkill /T /F` 强杀一次，supervisor 恰好退避/恢复两次并进入第三代。三代完整进程树的 working set 峰值约为 386/367/367 MiB，private bytes 约为 333/313/314 MiB，句柄峰值为 931/925/925，进程数始终为 5，均低于冻结上限。正常关闭后逐一确认所有采样过的父子 PID 已消失，最终状态为 `Stopped`、无 `Failed`，随后静默卸载并清理临时目录。

这个 cohort 是真实冻结/安装产物的 supervisor、资源和外部强杀门禁，但它明确是 health-only canary。生产 Fake Provider 对 Turn Planner 固定返回 `unsupported`，本检查点不会在冻结进程中伪造第十六检查点的三仓库任务，也不把健康探针冒充为编码执行或 `NO_AUTOMATIC_REPLAY` 任务证明。

## 五层不可变发布契约

- 新增 `deskpilot.workspace-coding-frozen-release-soak-suite.v1`，以 `concurrency_suite_digest` 精确绑定第十六检查点；基础黄金任务→韧性→sidecar→三仓库并发→冻结发布 cohort 形成五层内容寻址链。
- 资产冻结 exact installer/desktop/sidecar 文件名、每代 30 秒、1 秒采样、2 次外部强杀、3 代进程、supervisor restart budget 3，以及整树 512 MiB working set、2048 句柄和 6 进程上限。
- 资产强制 `health_only_canary=true`、`replays_command_tasks=false`和 `no_automatic_replay=true`；任何尝试把它声称为任务重放的修改都在严格加载期被拒绝。
- 加载器拒绝 concurrency digest、scenario ID、世代/强杀矩阵、资源上限和 health-only 边界漂移；资产不授予 executable、argv、cwd、env 或新 Capability。

## 独立执行与 CI

- 新增 `frontend/scripts/run-frozen-release-soak.ps1`；默认先运行 `pnpm desktop:build`，然后只接受 exact `DeskPilot_0.1.0_x64-setup.exe`。
- 脚本校验 NSIS 静默安装后的三个固定文件，输出 installer/desktop/sidecar SHA-256，仅在当前子进程设置 opt-in 环境变量，最终恢复原环境。
- Rust ignored test 仅能经该 wrapper 显式运行；日常 `cargo test --all-targets` 保持快速，只将它列为 ignored。
- 新增仅 `workflow_dispatch` 的 `phase-116b-frozen-release-soak.yml`；它使用 frozen uv/pnpm 依赖，先跑严格资产、Ruff、mypy、Rust fmt/Clippy，再构建、安装、soak、卸载。该门禁不进入默认 PR 单元测试。

## 故障发现与修正

1. 首次安装态运行指向未创建的 Conversation workspace root；冻结 sidecar 在 lifespan 启动阶段 fail closed，supervisor 按预算退避。门禁现在先创建 exact artifact/workspace 根，不放宽生产路径校验。
2. 第二次运行证明真实进程树为 5，原预设的 4 进程上限没有计入 PyInstaller 引导进程与 runner 子进程。修正不是只放宽数量：采样已升级为整树聚合 working set/private bytes/句柄，进程数上限调为 6，实测稳定为 5。
3. 测试模式下 supervisor 将 sidecar stdout/stderr 定向到隔离 app-data 日志，一旦未健康或进入 `Failed` 会连同状态序列报错；生产模式仍不暴露子进程输出。

## 验证结果

- 新严格资产测试 5/5 通过；Ruff 全仓通过，strict mypy 309 个生产源码通过。
- 默认 Rust 门禁为 `4 passed + 1 ignored`，第十五检查点的真实 Uvicorn supervisor 恢复回归仍通过；`cargo fmt --check` 和严格 Clippy 通过。
- 显式 frozen release soak 为 1/1 通过：三代、两次强杀、两次 Backoff、零 Failed，所有观测过的进程 PID 在 shutdown 后消失。
- 最终 installer/desktop/sidecar SHA-256 分别为 `39902f28b3e1711fac13adfc0ea3f7e6dccf8163bd42e8d60c95405516fa4b01`、`98b04edc4039bf7b222685ab80fb88ba54eb27b02366c4387eda30b0c563c93d` 和 `230c70877091c0db0ea3b6678b5fc263c97f4100a9a6f1075fd38c4effd46948`；第五层 suite digest 为 `a6c68304b42e51df539ddd1ae1775f10fe4d6b4f0674ced084bdbb24a48b794b`。
- 本检查点没有 migration、API、前端页面、cloud capture 或 baseline 变更。第十六检查点的默认后端 850 项全量结果仍是最近全量基线；本批仅增加严格资产/发布编排与 Rust opt-in 门禁，不将长时 soak 塞入默认后端测试。

## 方向判断与下一步

项目没有跑偏。本检查点补上了 Codex 类持久 Agent 不能回避的桌面发布边界：测试不再只从源码 Python 或 Cargo fixture 启动，而是穿过真实冻结、NSIS 安装、同级发现、多代恢复、整树资源与卸载边界。

下一批应专门闭合“安装态任务语义”，不再重复 health-only soak：先让冻结 sidecar 完整携带服务器固定的 Python Command Profile toolchain，再将一个已持久到 `ready` 边界的可抛弃任务交给安装态生产 Workbench/TaskLoop，验证真实 AppContainer Profile、ResultRef、强杀恢复与不重放。预置任务可由测试 harness 在冻结进程启动前写入同一 SQLite；不应为此改变生产 Fake Provider 的 `unsupported` 安全默认。

真实模型质量结论仍必须等待 115B 的 Candidate/Judge Provider、代码出站、费用、两名真人主审/仲裁和激活人授权，然后才能进入 116C。自由 Shell、依赖安装、自动 push 和 cloud activation 仍不开放。
