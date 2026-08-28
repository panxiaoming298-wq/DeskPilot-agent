# 阶段 116B：持久多 Agent 编码循环第十六检查点

## 目标与结论

本检查点把第十五检查点的单任务 sidecar 恢复继续推进为三个持久编码任务的受控负载验收：两个 Python 仓库和一个 Node 仓库各有 24 个源码文件，通过普通 Conversation/Workbench API 形成三个独立 `WorkspaceCommandPlan`。第一代 API 在三任务首步均为 `ready` 时停止，第二代 API 只依靠生产 `WorkbenchRuntimeCoordinator` 自动恢复；浏览器侧不再发送推进 POST。

实测三个仓库都在任一仓库进入第二 Profile 前获得首步执行机会，实际命令线程峰值恰好为配置的 2，最终回到 0。`python-api` 的 Ruff 首次失败保留独立失败 ResultRef，一次有界 Repair 后通过；`python-worker` 和 `node-console` 在同一故障窗口继续前进，没有重新规划、重放已通过步骤或混用路径/Profile 证明。

当前结论仍限定为 Windows、LOCAL-only/Fake Planner、recorded Command Runtime 和可抛弃受控仓库。这是真实 API/SQLite/TaskLoop/后台调度器的并发与恢复证据，不是真实命令质量、真实模型成功率或生产 Codex 等价结论。

## 严格版本资产

- 新增 `deskpilot.workspace-coding-concurrency-suite.v1`，以 `sidecar_suite_digest` 精确绑定第十五检查点；基础黄金任务→韧性→sidecar→并发场景形成四层内容寻址链。
- 场景冻结恰好 3 个唯一仓库、72 个源码文件、Python+Node 覆盖、每仓库 2 个唯一同生态 Profile、后台并发 2、实际峰值 2 和恰好 1 个可修复故障源。
- 资产同时冻结 200 ms 命令重叠窗口、250 ms GET 观察间隔、120 s SQLite 持久收口上限、首轮公平性合同、自动 Repair 和 `NO_AUTOMATIC_REPLAY`。
- 加载器拒绝 sidecar digest 漂移、重复仓库 ID/marker/path、跨生态 Profile、并发上限或推进预算跨权、非唯一故障源和中型仓库文件数不足。
- 资产仍不提供 executable、argv、cwd、env 或新的执行权限；Profile 只能来自现有服务器 Catalog。

## 真实三任务恢复与调度纵切

1. 测试物化 `projects/python-api`、`projects/python-worker` 和 `projects/node-console`；每个仓库包含 24 个实际源码文件，Node 仓库额外有固定 `package.json`/`pnpm-lock.yaml` 结构。
2. 首个真实 Uvicorn 进程禁用后台 Workbench runtime，通过公共 API 分别让三任务的首个 Command node 到达持久 `ready`；此时 Planner 恰好各调用 1 次，Command Runtime 为 0 次。
3. 正常停止第一代 API，将 Workbench runtime 切换为并发 2 并启动第二代 API。生产启动恢复从同一 SQLite 补种三个 WorkItem；之后测试客户端只发送 Workbench GET。
4. 测试专用 runtime 在真实 `asyncio.to_thread` 命令边界记录 start/finish sequence 与实时 active 数。结果证明三仓库首步全部开始后才有第二 Profile 开始，峰值恰好为 2，从未超过 2，最终回到 0。
5. `python-api` 的 `python.ruff.v1` 首次返回已知 `failed`；TaskLoop 保留失败回执，一次 Repair 后第二 Attempt 通过，再解锁 `python.mypy.v1`。
6. 两个同伴任务均在失败任务的成功重试前已取得进展，最终两步各只执行 1 次；它们的 `repair_count` 和失败回执数都为 0。
7. 三个 TaskLoop 最终都为 `succeeded`，所有 Command node 为 `verified` 且只用成功 ResultRef 满足依赖。Planner 总计仍只调用 3 次，与三个独立任务一一对应。

## 实现与安全边界

- 产品调度器与命令执行器不需要修改：现有 WorkItem 按 `available_at/created_at/work_item_id` 排序，批次完成后重入队；Command Runtime 已在 `asyncio.to_thread` 中运行。
- 扩展的 Planner route、延迟/故障注入、并发活动账本和 runtime 控制文件全部位于 `tests/fixtures`；不进入 wheel，不改变生产权限面。
- 调用账本写入由线程锁保护，但保留第十四/十五检查点原有的字符串账本格式；旧失败、中断和 supervisor 断言无需放宽。
- 本检查点没有新增数据库状态机、API、前端页面、migration、自由 Shell、依赖安装、push/PR 或 cloud activation。

## 验证结果

- 第十四至十六检查点 Python 联合专项 17/17 通过；新并发 suite 严格/漂移/真实 API 纵切 5/5 通过。
- Rust 生产 supervisor 回归仍为 4/4，真实 sidecar 用例用时 72.10 s；`cargo fmt --check` 和严格 Clippy 通过。
- Ruff 全仓通过；strict mypy 307 个生产源码通过。
- 默认后端只读收集为 112 个测试文件 / 850 项；完整单进程运行到 100%、退出码 0，冻结为 `838 passed + 12 skipped`、失败/错误为 0，用时 1:33:23。唯一 warning 仍为既有 Starlette TestClient/httpx 弃用提示；12 个 skip 仍是未配置 PostgreSQL/RabbitMQ 专用环境的外部 cohort。
- Alembic/SQLite 唯一/current head 仍为 `0065_confirmed_change_task_loop`，`alembic check` 无新 upgrade 操作；本检查点无 schema 变更。
- `uv lock --check`、60 包 `pip check` 通过。Windows Evaluation v2 compare 无违规，report digest=`923a607e2d1612d80222c9871a5fd467228f70933e3fabef2c23f7fc0019a24c`；Phase75 v21 compare 无违规，report digest=`805d03c4f4ab5eedb82bb877b4980fa583c7ee700a891b5286ab1bea13d95d53`，baseline 未修改。
- wheel 构建通过，包含 Prompt 33/33 与基础/韧性/sidecar/并发 Workspace YAML 各唯一 1 份，测试 fixture 为 0 份；`git diff --check` 通过。

## 方向判断与下一步

项目没有跑偏。这一检查点补的是 Codex 类持久多 Agent 最关键的负载语义：多个任务同时存在时不饥饿、不越过资源上限、一个任务失败不污染其他任务，且 API 进程更换后仍从唯一 TaskLoop 真值续接。

下一批不应再增加另一个短时 recorded 场景。应将现有四层 suite 提升为独立发布 cohort：使用冻结 sidecar/NSIS 链、更长真实墙钟、进程/Job 资源观测与多次外部强杀，验证安装态运行不泄漏、不重放、不超预算。该 cohort 应从默认后端单元门禁分离，避免将数十分钟 soak 永久加入快速开发循环。

若要下真实模型质量结论，仍必须先获得 115B 的 Candidate/Judge Provider、代码出站、费用、两名真人主审/仲裁和激活人授权，再进入 116C。在此之前仍不开放自由 Shell、依赖安装、自动 push 或 cloud activation，所有 cloud-only 候选继续 disabled。
