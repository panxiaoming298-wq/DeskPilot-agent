# 阶段 116B：持久多 Agent 编码循环第十九检查点

## 目标与结论

本检查点闭合第十八检查点留下的“安装态完整成功与步骤边界恢复”：同一个持久 `WorkspaceCommandPlan` 在第一代安装 sidecar 中以真实 Windows AppContainer 完成 Ruff，在 Ruff ResultRef 已持久、pytest 仍为 `ready/attempt_count=0` 的安全边界外部强杀进程树。第二代启动后先证明 Workbench 与 SQLite 边界证据完全不变，再通过公共 `workbench:advance` 每次只执行一个服务器授权步骤，依次完成 pytest、Final 与 Delivery。

最终数据库仍只有一个 Turn Planner run、一个 ModelPlanner Draft 和一个 WorkspaceCommandPlanBinding；Ruff/pytest 各只有一个 `attempt=1/status=verified` 的 Attempt 和一个 passed ResultRef。这证明已通过步骤不重放、未领取步骤不伪造 unknown，且进程重启不会重新规划。

项目方向没有跑偏：本次推进的是 Codex 类持久 Agent 必需的“持久计划—真实受控执行—结果证据—跨进程续接—最终交付”主干，而不是扩张新的旁路编排器或自由 Shell。

## 第七层不可变契约

- 新增 `deskpilot.workspace-coding-frozen-command-recovery-suite.v1`，以第十八检查点 suite digest `ed993889e9cd1c025cd3b19c8eb1de82a992fed95aab03790ed2f172b780ae89` 精确绑定既有六层资产；新 suite digest 为 `d096cdcdf68e92d16f0a55a5ea6e4fef8fa7e838af0de980f2b6acddde2c9df1`。
- 契约冻结 Ruff→pytest 顺序、工具链 digest、两代进程、一次外部强杀/恢复、首步后重启、重启前第二步零 Attempt、重启后三次单步推进、两个 ResultRef 和 Final/Delivery 终态。
- 加载器除了拒绝前驱 digest 漂移，还会跨层重验 Profile 顺序、工具链目录/digest、AppContainer 隔离、生产 Fake `unsupported` 默认和 `NO_AUTOMATIC_REPLAY`。
- 新场景不修改生产 Runtime、schema、API 或前端页面；它只将既有公共接口、TaskLoop 和安装产物组合成一条更强的验收纵切。

## 安全边界与恢复证据

1. 测试专用 fixture 只负责通过普通 Conversation/Workbench API 产生并持久 ready 计划，运行时不执行任何命令；Planner 调用账本恰好一条。
2. 第一代安装 sidecar 关闭后台自动推进，调用公共 `workbench:advance` 一次。该 API 的既有契约是每次只推进一个 safe step，因此返回时可精确观测 Ruff `verified/attempt=1` 与 pytest `ready/attempt=0`。
3. 测试外部 `taskkill /T /F` 终止第一代完整进程树，生产 `SidecarSupervisor` 恰好拉起第二代。恢复后 GET 投影与重启前 durable state 全等，数据库 results/attempts/counts 证明也全等。
4. 第二代恰好三次单步推进：真实 AppContainer pytest、控制节点归约、Delivery 终态。额外 replay POST 和后续 5 秒观察都不再创建 Attempt/ResultRef。
5. 两个结果均重验 `windows_appcontainer`、`network_access=false`、临时快照、修改丢弃、工具链 digest 和 ResultRef/result digest 链。

## 验收结果

- 七层 Workspace Coding 契约联合专项 35/35，新第七层严格资产 5/5；默认 Rust 为 `4 passed + 3 ignored`，三个 ignored 安装态测试由显式 wrapper 各 1/1 通过。新成功恢复场景用时约 146 秒。
- 本次复用的安装器/desktop/sidecar SHA-256 分别为 `eb322fcab911911f8c66140d0d07bdfe5d54908177eadceedc4c2dc3f58d6194`、`3b064d34f8958f54168955e1c5067ffbfcddb49fd4b599b6a3c7ab09331d4079` 和 `1ef05c216bab3b13e4d239a508624dae93f01bb06ecd3b7c50517c5da63a61cb`；固定工具链 digest 仍为 `486ae41dac2a697a792a1ab2584fe66a768bb7ba3b9e695a16ae8ec6fb03dd4c`。
- Ruff 全仓、strict mypy 310 个生产源码、frozen lock、60 包 `pip check`、Rust fmt/check/Clippy 与前端 24 文件 / 165 项、type-check/build 通过。
- 默认后端收集 115 个测试文件 / 868 项，统一全量运行 `856 passed + 12 skipped`、失败/错误为 0，用时 1:19:13；wheel 中 Prompt 33/33 和 Workspace Coding YAML 7/7 唯一资源通过。
- 本检查点没有 migration、生产代码路径、API、前端页面、Evaluation/Phase75 baseline、PostgreSQL/RabbitMQ 外部 cohort 或 cloud activation 变更。

## 边界与下一步

本次仍是 LOCAL-only/Fake seed 与隔离测试仓库，不声称真实模型质量、真实用户仓库长循环、生产激活或 Codex 等价完成。自由 Shell、依赖安装、Node 安装态工具链、自动 push/PR 和登录态浏览器仍不在权限面内。

下一纵切应将第十六检查点的三任务公平并发提升到安装态：三个独立项目共享同一 SQLite 与生产 Workbench concurrency=2，跨 sidecar 重启保持公平首步、任务故障隔离、每个 Profile 不重放并全部到达 Delivery。在没有 115B 五项外部授权时，cloud-only cohort 必须继续 disabled。
