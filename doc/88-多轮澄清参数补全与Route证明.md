# 阶段 88：多轮澄清参数补全与 Route 证明

阶段 88 让统一对话入口具备最小的“问清楚再继续”能力：当一句普通话语已经确定能力、但缺少该 Route 的一个必需参数时，系统先返回 `needs_clarification`，不创建可执行计划；用户下一句只提供缺失值即可继续。补全仍由受信任的确定性规则完成，不让模型、Memory、摘要、网页或 Assistant 消息自由拼接参数。

这一步更接近 Codex 式持续任务体验，但还不是通用自主 Agent Loop。当前系统会在一个已知参数槽上追问一次并生成新的不可变 replacement Task；它不会自行拆分任意目标、在未知 Tool 间自由循环、动态扩大权限或把历史对话当作授权。

## 1. 已支持的补全槽

| 第一轮 | 第二轮 | 结果 |
| --- | --- | --- |
| `帮我看看文件` | `README.md` | `workspace_file_read(path=README.md)` |
| `在 backend 里运行测试` | `tests/test_api.py` | 单个显式 pytest 文件 |
| `查一下知识库` | `安全边界` | `knowledge_lookup(query=安全边界)` |
| `统计字符数` | `DeskPilot` | `mcp_text_metrics(text=DeskPilot)` |
| `帮我做一份 PDF 报告` | `量子计算` | 原 `research_to_html(goal=量子计算)` 交付链 |

如果第二轮本身已经完整匹配一个 Route，它会作为新的完整请求处理，不会被上一轮强行解释。文件补全仍要求像文件路径，测试补全仍受单个 `tests/test_*.py`、`*_test.py`、`*.spec.js` 或 `*.test.js` 白名单约束；空值、批量测试、自由 Shell 和不支持的写入不会因此获得执行能力。

## 2. 持久化真值与 fail-closed 校验

`0039_turn_route_resolutions` 为 `turn_routes` 增加：

- `resolved_from_task_id`：精确绑定提出澄清的源 Task/Route；
- `resolution_rule`：记录有限白名单中的补全规则；
- `resolution_digest`：绑定源消息/候选/参数摘要与目标消息/候选/参数摘要。

三项字段必须同时为空或同时存在，且只能出现在 routed Turn 上。每次读取、执行或领取 Route 时，服务器都会回读源 Route 和用户消息，检查同一 Conversation、源决策确为 `needs_clarification`、时间顺序、候选与参数摘要以及补全证明。缺源、跨会话、循环引用、字段不完整或任何摘要漂移都会以冲突拒绝，不会继续执行。

前端 Route Receipt 只展示该服务器证明的短摘要；它不是授权入口。原 R1 工作区预览/确认、MCP 显式启用、verified-edge、Artifact render evidence 和精确不覆盖导出边界均未改变。

## 3. 下一段 Codex 式能力边界

阶段 88 已具备“持续对话 + 澄清后续跑”的最小纵向闭环。要进一步接近 Codex，下一阶段应实现受限、持久化的目标驱动 Model Loop：模型只能从冻结 Tool/Route binding 中提出结构化下一动作，服务器逐步校验、执行、观察和 replan，并以预算、no-progress、审批、未知效果和最大步数收敛。模型输出不能直接成为路径、argv、权限或完成证明；高风险写入仍需原有预览和用户确认。

## 4. 验收

Workspace Runtime 与 Workbench 组合 45 项、migration 26 项通过；新增用例覆盖五种补全槽、文件读取实际交付、完整请求优先和 resolution digest 篡改拒绝。Ruff 全仓、mypy 223 个生产源码、Python 依赖一致性和 `git diff --check` 通过。前端 22 个测试文件/152 项、Vue type-check 和 production build 通过。

默认开发 SQLite 没有被本阶段命令升级；独立临时数据库已验证 `0039 → 0038 → 0039` 往返和 Alembic metadata check。
