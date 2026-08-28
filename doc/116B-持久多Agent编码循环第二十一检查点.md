# 阶段 116B：持久多 Agent 编码循环第二十一检查点

## 目标与结论

本检查点闭合安装态并发强杀的故障域：三个独立 Python 任务共享唯一 SQLite/TaskLoop，生产 Workbench 以 `concurrency=2` 公平运行真实断网 AppContainer Ruff→pytest。当三个 Ruff 均已验证、两个 pytest 正在运行而第三个 pytest 尚未领取时，外部强杀完整 sidecar 进程树。

第二代 sidecar 恢复后，只有强杀时持有租约的两个 pytest Attempt 收敛为 `outcome_unknown`，两者均没有伪造 ResultRef、没有透明重放；未领取的第三个 pytest 只执行一次并完成 Final/Delivery。最终是 6 个 Attempt、4 个 verified ResultRef、2 个 unknown Attempt，Planner/Draft/Binding 每任务仍各一份，AppContainer profile journal 从强杀前恰好 2 项回收到终态空集合。

项目方向没有跑偏。本次证明的是 Codex 类持久多 Agent 的进程级故障隔离，而不是简单把并发数调大：进程故障只污染当时已领取的副作用，不扩散到未开始的同伴任务；唯一 TaskLoop、租约 fence、ResultRef 和 Delivery 仍是恢复真值。至此 ADR-016 定义的 116B LOCAL-only 开发完成口径已全部闭合，但这仍不等于真实模型质量、生产 cloud activation 或 Codex 产品等价完成。

## 第九层不可变契约

- 新增 `deskpilot.workspace-coding-frozen-concurrency-kill-suite.v1`，以第二十检查点 suite digest `7e83ed9e4e7a4b58f3958af5f0c3f630c7153f3b8d3c224f854720ada905ea3d` 精确绑定前八层资产；新 suite digest 为 `17371530aa12d8642fe81385fbc711d8b00696d797a78b7904a829922cde4c53`。
- 契约复用三个 24 源文件仓库的 exact ID、消息标记、项目路径与 Ruff→pytest Profile 顺序，但把本轮三个 pytest 均冻结为健康结果；并发固定为 2、外部强杀 1 次、安装进程恰好两代。
- 终态矩阵固定为 2 个 unknown Task、1 个 succeeded/Delivery Task、6 个 Attempt、4 个 verified ResultRef 和 2 个 unknown Attempt；`fault_domain=claimed_tasks_only` 与 `NO_AUTOMATIC_REPLAY` 不可漂移。
- 严格加载器会重验前驱 digest、仓库身份/顺序、工具链目录/digest、公平首波、并发上限、自动恢复、生产 Fake `unsupported` 默认与不重放边界。
- 手动 Windows workflow 纳入第九层严格资产；PowerShell wrapper 默认执行 5 项安装态测试，并提供只缩小测试选择、不改变安装/隔离流程的 `-OnlyConcurrentKill` 定向复跑开关。

## 安装态故障域证明

强杀前同时满足：三个 Ruff 节点均为 `verified/attempt=1`，两个 pytest 为 `running/attempt=1`，第三个 pytest 为 `ready/attempt=0`；AppContainer profile journal 恰好记录两个活跃 profile，证明物理命令并发峰值为 2。

强杀后 supervisor 只拉起第二代 sidecar。两个受影响任务各保留 Ruff ResultRef，pytest Attempt 在租约恢复时写入 `CAPABILITY_OUTCOME_UNKNOWN_AFTER_LEASE` 和 error digest，candidate、verification 与 ResultRef 均为空；第三个任务的 pytest 只创建一个 Attempt/ResultRef 并到达 Delivery。5 秒稳定观察不增加 Attempt、ResultRef、Planner、Draft 或 Binding，终态 profile journal 为空。

首次安装态运行只在最终峰值证明处报告 3。数据库证据证明这不是生产并发越界：旧 pytest 进程已被整树强杀，但 unknown Attempt 的 `updated_at` 是新进程“发现并固化未知结果”的时间，不是不可观测的实际结束时间；若把它当结束时刻，就会与第二代的健康 pytest 虚构重叠。最终证明算法只对具有 candidate/verification 的已知 Attempt 计算可观测时间线，unknown 不伪造结束时刻；物理并发则由强杀前 journal=2、配置上限=2 和终态 journal=0 共同证明。没有为通过测试增加阻塞未受影响任务的全局恢复屏障。

## 验收结果

- 九层 Workspace Coding 联合契约 48/48，新第九层严格资产 7/7；默认后端 117 个测试文件 / 883 项，完整单进程 `871 passed + 12 skipped`、失败/错误为 0，用时 `1:34:01`，仅保留 1 条既有 Starlette TestClient/httpx 弃用 warning。
- 同一最终 NSIS 的安装态五项 5/5 通过：supervisor 资源门 153.13 秒、单任务 unknown 不重放 178.54 秒、步骤间恢复 Delivery 146.42 秒、三任务公平/已知失败隔离 417.57 秒、并发强杀故障域 352.17 秒。
- 最终安装器/安装后 desktop/sidecar SHA-256 分别为 `7925f61391b204c6c6d4ff4f03db504795d6419a67437c7b8aebfa70b9eaa975`、`c9bbb245d237b2bb4d2c76e15e15ec9fabad8e36d8e8cf996ea9db6c788dec9b` 和 `104721d82e42fd2b7b42faf73f2b523a451cbf5882a6430193c7ed61606a7c03`；固定工具链 digest 仍为 `486ae41dac2a697a792a1ab2584fe66a768bb7ba3b9e695a16ae8ec6fb03dd4c`。
- supervisor 资源门三代峰值约 386 MiB working set、931 句柄、5 进程，未放宽第二十检查点冻结上限。
- Ruff 全仓与 strict mypy 310 个生产源码通过；Rust fmt、strict Clippy、默认 `4 passed + 5 ignored` 通过；前端 24 个文件 / 165 项、type-check 与 production build 通过。
- `uv lock --check`、60 包 `pip check`、Alembic/SQLite 唯一 head `0065_confirmed_change_task_loop` 的 current/check 通过。wheel 包含 Prompt 33/33、Workspace Coding YAML 9/9，测试故障 fixture 为 0。
- Windows Evaluation v2 无违规，report digest=`7fa595b5ab6ef854e1ae5cba9b743a1a7cc972d55d10cd6f10490c4464105bda`；Phase75 v21 无违规，report digest=`805d03c4f4ab5eedb82bb877b4980fa583c7ee700a891b5286ab1bea13d95d53`，immutable baseline 未改写。
- 本检查点没有 migration、生产 API、前端页面、生产权限、依赖安装、自由 Shell、push/PR、cloud capture 或 activation 变更。

## 边界与下一步

116B 的 LOCAL-only 开发完成，不代表 115B/116C 已完成。当前只用 Fake seed、固定 Profile 与隔离测试仓库证明运行时/工具/恢复安全；没有真实 Candidate/Judge、人类评审、代码出站授权、费用授权或 Production Admission，cloud-only cohort 必须继续 disabled。

下一个安全任务应转为 116C 的离线准备检查点：冻结至少 20 个版本化 Python/Node 真实仓库任务的 manifest、一次性仓库物化/清理规则、重复次数、成功阈值、false-success/unauthorized-effect 零容忍和只读 harness 预检。该批可以完成任务资产与离线安全验证，但不得执行真实模型 capture、生成生产质量结论或激活 cloud Agent；这些动作仍等待 115B 五项外部授权。
