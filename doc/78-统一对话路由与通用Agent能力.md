# 阶段 78：统一对话路由与通用 Agent 能力

阶段 77 已把首页改成对话入口，并能在同一 Conversation 中建立不可变 replacement Task、自动推进 `research_to_html` 和持续展示证据。但服务器仍把每条用户消息解释成同一种研究任务：界面像 Agent，能力路由还不是 Agent。

阶段 78 在不开放任意代码执行、任意文件写入或客户端自定义计划的前提下，引入统一 Turn Router。用户只需自然语言描述目标；服务器把消息解释为候选意图，再由受信 Route Catalog 和参数绑定器决定是否能够形成 Task Contract。第一版支持研究交付、本地知识查询和内置只读 MCP 文本统计三条真实执行路线，并允许它们在同一 Conversation 中连续出现。

本阶段完成后可以称为“多能力通用对话 Agent 原型”，不能称为 Codex 等价物。项目文件编辑、终端、登录态浏览器、任意网站操作和第三方插件仍需后续逐个增加 Capability Pack、审批与验证器。

## 0. 实现结论（2026-08-18）

阶段 78 已实现，不再是界面原型或待办规格：

- 新增受信 `TurnRouter`、固定 Route manifest、确定性参数绑定和 `0037_turn_routes` 持久化迁移；
- `research_to_html@1`、`knowledge_lookup@1`、`mcp_text_metrics@1` 共用同一 Conversation Turn API、Task、Contract、Plan 和 Execution Run 真值；
- 澄清与 unsupported 只生成路由决策和 Assistant message，不生成可执行 Run；
- 本地知识和 MCP 直接复用阶段 63/64 的证明与审计边界，没有新建第二套执行器；
- MCP 仍默认关闭，只能由用户在对话流中显式启用；
- 前端右侧证据区会按 Route 切换 Research、Knowledge 或 MCP proof，不支持的指令不显示假进度。

## 1. 一个入口、一条运行真值

现有 Conversation Turn API 继续作为唯一用户入口，不增加“请先选择任务类型”的必填下拉框：

```text
POST /api/v1/conversation-turns
POST /api/v1/tasks/{task_id}/conversation-turns
```

服务器内部处理顺序固定为：

```text
User Message
  -> untrusted Turn Interpretation candidate
  -> trusted Route Catalog match
  -> deterministic parameter binding and policy preflight
  -> immutable Task Contract + Executable Plan
  -> Agent / Knowledge / MCP runtime
  -> verification, evidence and delivery
  -> Assistant Message
```

模型、规则或关键词只能产生候选解释，不能直接选择 Tool、拼装权限或激活 Plan。Route Catalog 由应用组合根静态注册；每条 Route 精确绑定版本化 Task template、Capability Pack、风险上限、参数 Schema、执行节点和验收条件。客户端不能提交 `route_id`、Agent ID、Tool 名称、风险等级、审批结论或可信摘要来绕过服务器决策。

每个成功路由的 Turn 都建立新的不可变 Task/Contract/Run，并保留 `conversation_id` 关联。第一版继续维持“一个 Conversation 同时最多一个活动 Run”：新指令到达时，若旧 Run 尚未结束，服务器先 cancel、提升 fencing token，再建立新 Task，迟到结果不能进入新任务。

## 2. Turn Route 投影

新增服务器拥有的 `deskpilot.turn-route.v1` 投影，至少包含：

- `task_id`、`conversation_id`、`user_message_id`；
- `decision`：`routed`、`needs_clarification` 或 `unsupported`；
- `route_id`、`route_version` 与 Route manifest digest；
- 候选解释摘要、已绑定参数摘要和稳定 `reason_code`；
- 候选解释摘要、参数摘要、结果摘要、revision 和时间戳；Contract/Plan 血缘继续由同一 Workbench 投影中的类型化对象提供；
- 需要用户动作时的服务器 Action Availability，不把按钮状态留给前端猜测。

候选解释正文不成为授权证明。读取投影时重新校验 Route manifest、绑定参数、Task Contract 和 Plan 血缘；不一致返回稳定的 proof rejection，不降级到默认研究路线。

三类决策的行为边界：

- `routed`：参数完整且 Route/Policy 可用，原子建立 Task、Contract、Plan、Run 和 Assistant 受理消息；
- `needs_clarification`：只写入 Assistant 澄清问题，不建立可执行 Run，也不预授权任何 Tool；
- `unsupported`：明确说明当前缺少哪类 Capability，不把未知请求强行包装为 `research_to_html`，也不静默执行相近但不同的任务。

## 3. 第一版 Route Catalog

### 3.1 `research_to_html@1`

复用阶段 70～77 已验证的研究、Claim/Citation、Artifact Workspace、PatchReceipt、隔离 Browser Verifier、DeliveryManifest 和精确导出链。路由只负责从用户消息绑定研究目标与现有联网策略，不能放宽来源、浏览器或导出权限。

完成条件仍是五条 verified edge 全部成立；生成 HTML、模型自报完成或页面可打开都不能单独构成成功。精确用户路径导出继续使用 prepare/confirm 两步协议，不进入自动推进。

### 3.2 `knowledge_lookup@1`

把阶段 63 的本地知识库接入 Conversation Task，而不是在独立导航页中孤立调用。计划最少包含：

1. 对当前已导入的 `.md`/`.txt` 来源执行确定性检索；
2. 复核 source version、Artifact manifest、Chunk proof 与 retrieval proof；
3. 形成带来源路径、行号 locator 和证据摘要的 Assistant 回答；
4. 将来源过期、无命中和 proof rejection 如实投影为限制或失败。

知识片段始终是 `external_untrusted` 数据，不能修改 Route、Contract、Policy、审批或 active Memory。若后续使用模型把片段组织成自然语言，回答中的事实必须绑定检索 Citation；无法绑定的内容标记为未核验，不得伪装成 verified fact。

### 3.3 `mcp_text_metrics@1`

把阶段 64 唯一的内置 `deskpilot.readonly-text` / `deskpilot.text.metrics` 接入 Conversation Task，用来验证 Agent 能够通过统一路由调用另一种 Runtime，而不是扩大 MCP 权限。

该 Route 只接受 1～4096 字符文本，固定绑定已注册 Server、Tool Schema、bundle digest、R0 风险下限和 3 秒会话 deadline。Server 必须已经由用户显式启用；Router 不得代替用户启用。若 Server disabled，Task 停在服务器声明的 `needs_user_action`，前端展示启用说明，未授权前不能创建 MCP 子进程。

MCP annotation、text content 和 structured content 都不能产生授权。Host 仍需复核 bundle、`tools/list` Schema、本地输入/输出模型、大小上限与 hash-chain audit；只有校验后的结构化统计结果能够进入 Assistant message。

## 4. 路由与澄清规则

Router 首版采用“模型候选 + 确定性绑定”或等价的严格结构化解释器，但不建立自由规划循环。候选输出只允许：

- 选择 Route Catalog 中已有的精确版本；
- 提取该 Route Schema 允许的参数；
- 表达缺失字段和候选置信状态；
- 建议 `routed`、`needs_clarification` 或 `unsupported`。

服务器必须拒绝未知字段、未知 Route、越界文本、额外路径、Tool 名称、代码、权限声明和候选输出中的审批结论。以下情况必须澄清或拒绝，不能猜测：

- 同一句话同时要求多个第一版不支持组合的任务；
- 用户只说“处理一下”“继续”等，且当前 Task 上下文不足以绑定目标；
- 请求要求覆盖文件、执行 Shell、安装包、使用登录态或调用未注册 MCP；
- 本地知识查询与联网研究边界不清，且选择不同来源会实质改变结果；
- 模型 Provider 不可用、候选 JSON 不合法或 Route manifest 漂移。

普通 follow-up 可以引用同一 Conversation 的已交付结果，但必须建立新的 Task Contract；Conversation transcript、Summary、Memory 或上一 Task 的模型文本不能被当作新任务授权。需要继承的目标、Artifact 或 Citation 必须用类型化 ID 和摘要重新绑定。

## 5. 自动执行、审批与停止

Router 只决定“可以形成哪种合同”，不改变风险规则：

- R0 只读节点在 Contract 和 Policy 允许时可由服务器自动推进；
- 网络访问仍经 Egress Policy，真实 Provider 默认关闭时不得把 recorded 结果冒充实时结果；
- MCP enable、R1+ 副作用、用户路径写入和覆盖始终使用既有显式动作/审批；
- `stop` 常驻可用，并提升 Run/Invocation fencing；
- `unknown`、partial、awaiting verification 和 needs user action 必须原样展示，不能折叠成“失败后重试”或“已完成”。

阶段 78 不允许 Router 生成任意 DAG，不允许模型把自然语言路径直接变成写权限，也不把 UI 中的“自动模式”视为无限授权。

## 6. 对话界面改动

阶段 77 的对话主界面保持不变，只增加能力感知，不再新建一个路由控制台：

- Assistant 受理消息显示服务器确认的任务类型和关键约束；
- 当前 Run 显示 Route、Contract 和正在执行的能力节点；
- 右侧证据区随 Route 切换为 Research/Citation、Knowledge Citation 或 MCP proof；
- 澄清直接出现在对话流，用户回复后重新尝试绑定；
- unsupported 请求明确展示当前可用能力，不出现假进度条；
- 前端不根据文字内容、按钮点击或请求返回顺序自行判定 Route 和完成状态。

## 7. 实施顺序

阶段 78 按以下顺序编码，避免先做新的视觉层或第二套执行器：

1. 定义 Route Contract、静态 Route Catalog、严格候选 Schema 和确定性参数绑定器；
2. 持久化 Turn Route 决策，并把 Conversation Turn 创建改为“先路由、后建 Task”；
3. 将现有 `research_to_html` 改为 Catalog 中的一条 Route，保持阶段 76/77 API 兼容；
4. 接入 `knowledge_lookup` 的检索证明、回答消息与 workbench 投影；
5. 接入 `mcp_text_metrics` 的 enable gate、调用审计与 Assistant 结果；
6. 让同一 Conversation 连续执行不同 Route，并补齐停止、replacement、幂等和迟到结果 fencing；
7. 最后调整前端 Route/证据投影，运行完整后端、前端和真实浏览器验收。

除非现有表无法表达不可变 Route 决策，否则不新增第二套 Task、Run 或事件状态机；优先复用阶段 63/64/69～77 的 Repository、证明与 Runtime。

## 8. 验收门

阶段 78 只有同时满足以下条件才算完成：

1. 用户不选择任务类型，仅用自然语言即可分别触发三条受信 Route；
2. 同一 Conversation 依次完成知识查询、文本统计和研究交付，产生三个独立不可变 Task；
3. 模糊请求进入澄清，未知请求进入 unsupported，二者均不创建可执行 Run；
4. 未知 Route、额外候选字段、伪造风险/审批、Prompt Injection 和 Route manifest 漂移全部 fail closed；
5. Knowledge source 变化、Chunk/manifest/proof 篡改不能产生回答；
6. MCP disabled、bundle/Schema/audit 篡改不能启动或冒充成功调用；
7. replacement Task 取消旧 Run 并提升 fencing，迟到 Research/Knowledge/MCP 结果不能污染新 Task；
8. 旧阶段 76/77 的研究、停止、精确导出、同会话历史和浏览器证明全部保持兼容；
9. 前端能够如实展示 routed、clarification、unsupported、needs user action、partial、unknown 和 verified；
10. 后端全量 pytest、Ruff、mypy、Alembic check、锁文件检查，以及前端 test、type-check、build 和 320～桌面真实浏览器验收全部通过。

实际验收已覆盖同一 Conversation 内三条 Route 产生三个独立 Task、澄清/unsupported 零 Run、MCP 显式 enable gate、路由参数篡改拒绝和 `0037` 迁移往返。真实浏览器完成 MCP 授权后执行与模糊 follow-up 追问；320/375/414/768 宽度无页面水平溢出、无按钮文字换行，控制台无 warning/error。

## 9. 明确不在本阶段

- 任意 Shell、PowerShell、Python、包安装或代码仓库修改；
- 从自然语言直接提取并执行文件移动、覆盖、删除或目录级操作；
- 登录态浏览器、通用 Computer Use 或任意网站自动化；
- 用户提供的 MCP 命令、第三方 Server、插件市场或远程 MCP；
- 多个并行活动 Run、后台自治循环或无限预算；
- PDF/DOCX/图片等新 Artifact builder；
- 用普通聊天回答、Memory、Summary、MCP/网页内容替代 Contract、Policy、receipt 或 Verification。

后续阶段优先增加受控 Workspace 文件读取/编辑与审批式文件效果，再增加浏览器操作和代码执行。每项能力都进入同一个 Route Catalog、Task Contract、Policy、Effect Ledger 和 Verification 体系，不为“像 Codex”另建一套不受控执行通道。
