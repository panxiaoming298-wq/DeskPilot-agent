# 阶段 116B：持久多 Agent 编码循环第十三检查点

## 目标与结论

本检查点把第十二检查点的 Conversation/Workbench 编码主链固化为版本化 Python/Node 黄金任务，并首次在真实 Uvicorn OS 进程之间验证 Patch 审批和 Git 审批恢复。黄金验收只调用已有公共 Conversation/Workbench API，不调用内部 binder/runtime，不新增 API，也不引入第二套执行状态机。

该结果证明的是 LOCAL-only/Fake 模型、真实本地 Git 和真实隔离测试运行时下的持久执行与恢复语义，不是真实 cloud 模型质量结论，也不把两个隔离 fixture 冒充为大型用户仓库 cohort。

## 版本化黄金任务

- 新增严格、冻结的 `deskpilot.workspace-coding-golden-suite.v1` Schema，绑定 suite/case 版本、生态、目标、固定测试路径、初始文件、重启检查点、最大推进次数与预期候选/变更/Delivery 结果。
- `workspace_coding_v1.yaml` 包含一个 Python 跨进程完整交付任务和一个 Node 公共 API 完整交付任务。两者都要求 exact 候选路径、exact 变更路径、`delivered`、push disabled 和可对账回滚备份。
- 加载器只读包内 YAML；拒绝 symlink/非普通文件、空文件、超限文件、YAML anchor/alias、未知字段、Schema/路径漂移、重复 case/文件/候选，并对规范化 suite 计算内容摘要。
- 套件必须同时覆盖 Python 和 Node；candidate 顺序必须与预期 changed path 顺序一致。黄金资产只描述受信输入和验收期望，不授予任何执行权限。

## 公共 API 与跨进程恢复

- 验收 harness 为每个 case 物化独立 Git 仓库，然后仅使用 `POST /conversation-turns`、`workbench:advance`、Patch commit、Git commit 和 Workbench read API 推进。
- Node case 使用记录式固定 Node Test runtime，走完 Explorer → 两次 exact 对话确认 → Coordinator/Readers/Patch Planners → Patch 审批 → Test → Git 审批 → Delivery。
- Python case 启动真实 Uvicorn 子进程和共享 SQLite/receipt/workspace，在 Patch approval 准备完成后停止第一个 API 进程，由第二个进程恢复同一 confirmation digest 和节点状态；Patch 提交和真实断网 AppContainer pytest 通过后，再在 Git approval 前重启，由第三个进程完成 commit 和 Delivery。
- 恢复后的 Workbench 必须保持完全相同的 approval digest 与 `(local_key, status)` 投影；已 verified 的 Coordinator/Reader/Planner/Patch 节点不重复执行。
- 交付验收要求变更文件集、push-disabled Git receipt、服务器命名中文 commit 与每个变更文件唯一的可恢复 backup 精确匹配；除已绑定 backup 外不允许额外 Git status。

## 长执行 fencing 修复

真实跨进程黄金任务暴露了一个只有冷启动才容易命中的缺口：隔离 pytest worker bundle 的首次构建可以超过 `CapabilityExecutionRuntime.run_once()` 的旧 60 秒默认 claim 租约。固定测试已完成时，候选结果却因 fencing 过期无法落库，系统只能保留 `running/outcome unknown` 并拒绝透明重放。

TaskLoop Coordinator 现在对同步 Workbench Capability 使用与旧有 Workbench 效果一致的 600 秒最大 claim 窗口，足以覆盖当前最长 180 秒节点预算及冷启动准备。协调器继续保留原有 stale-fence 领域异常合同，Workbench/API 边界将 Capability 层拒绝转换为稳定冲突，不再泄漏为无分类 500。这没有改变底层过期恢复规则：进程丢失、租约真正过期或 outcome unknown 仍只能收敛为未知结果，不会自动重跑可能已执行的测试或副作用。

跨进程夹具还为 database、artifact、receipt、worker runtime 和 AppContainer profile journal 配置用例私有路径，避免依赖开发仓库的共享 `data`。Windows 黄金仓库使用 pytest 管理的短临时根，使用例专注于重启恢复；运行时对 OS 无法可靠验证的超长路径继续 fail closed，没有在本批次偷偷扩大 Windows 路径权限边界。

## 验证结果

- 黄金套件 3/3 通过：严格加载/篡改拒绝、Node 公共 API 完整 Delivery、Python 三 API 进程 Patch/Git 审批恢复与真实 AppContainer pytest。
- Capability 持久化专项、116B Explorer/编码循环/韧性联合回归全部通过；旧 claim 过期、candidate 恢复和 `NO_AUTOMATIC_REPLAY` 反向用例未放宽。
- 默认后端实际收集 833 项，完整单进程运行 `821 passed + 12 skipped`、失败/错误为 0；最终异常边界调整后再次通过 Capability 专项和黄金套件 3/3。
- Ruff 全仓和 strict mypy 307 个生产源码通过；`uv lock --check`、60 包 `pip check`、Alembic/SQLite 唯一 head `0065_confirmed_change_task_loop` 的 `current/check` 通过，无新 migration。
- 前端未修改，24 个测试文件 / 165 项、type-check 和 production build 仍全部通过。
- Windows Evaluation v2 compare 无违规，report digest=`6e83eace19998dab8805c1aff6d389b57387235790a8a53ec05d10ce793b6734`；Phase75 v21 compare 无违规，report digest=`805d03c4f4ab5eedb82bb877b4980fa583c7ee700a891b5286ab1bea13d95d53`，未修改 immutable baseline。
- wheel 重建通过，33/33 Prompt 资源保持完整且包含唯一 `deskpilot/evaluations/workspace_coding_v1.yaml`；`git diff --check` 通过。

## 方向判断与下一步

项目没有跑偏。第十三检查点没有继续横向增加工具或界面，而是把已有多 Agent 编码链变成可重复、可版本化、可跨进程验证的用户任务，并用真实冷启动发现和修复长执行 fencing 缺口。这比只增加 Agent 数量更接近 Codex 类持久多 Agent 的核心能力。

当前 116B 仍不宜标记全部完成。下一批应在同一黄金 harness 上增加长时间 soak、一次已知失败后的 Repair 成功续接、proof 漂移的启动前拒绝，以及运行中强制终止后 outcome unknown 不透明重放。随后再用更多、更真实但仍可抛弃的仓库扩展 cohort，而不是开放自由 Shell、依赖安装或自动 push。

115B 的真实 Candidate/Judge、代码出站、费用、真人评审和激活授权仍未提供，所以 cloud-only cohort 继续 disabled，不执行 116C 或 Production Admission/activation。
