# 多 Agent 运行时、记忆与验证实施路线

## 1. 文档目的

本文不是“已经完成”的功能说明，而是阶段 66 之后的实施路线。它把现有设计文档中的 Agent、Handoff、Verifier、Memory 和 Context Compression 概念，拆成可以独立开发、演示和验收的阶段。其余 Plan Compiler、Model Loop、跨层恢复、部署、可观测、评测、用户控制面和第三方供应链议题的讨论顺序见[《多 Agent 后续技术架构讨论总纲》](多Agent后续技术架构讨论总纲.md)。

多 Agent 的顶层分流、统一执行内核、Policy/Approval、两级 Verifier 和 Deliverer 边界，以[《多 Agent 系统总体架构》](多Agent系统总体架构.md)为准；Agent 身份、版本、Registry 和计划绑定细节以[《Agent Contract 与 Agent Registry 技术设计》](Agent-Contract与Agent-Registry技术设计.md)为准；Handoff、Invocation、AgentResult、并行 join 和恢复语义以[《Agent Handoff、Invocation 与 Result Runtime 技术设计》](Agent-Handoff与Invocation-Runtime技术设计.md)为准；Claim、Evidence、Verification、Repair/Replan 和最终验收以[《Claim、Evidence、Verification 与 Repair/Replan 技术设计》](Claim-Evidence与Verification-Repair技术设计.md)为准；Context、Conversation、Memory、RAG、Artifact、数据出境和压缩链以[《Context Builder、Memory Broker 与 RAG/Artifact 数据平面技术设计》](Context-Memory-RAG数据平面技术设计.md)为准。本文只负责阶段顺序和验收门。

阶段 67 的脱敏 OpenTelemetry/显式回归门禁和阶段 68 的 Agent Contract/冻结 Registry 都已经完成。当前工程断点是阶段 69 Task Contract/Executable Plan Compiler；本路线同时按 [ADR-015](ADR-015-通用任务Agent产品边界与首个纵向切片.md)重排通用任务能力，避免把真实用户价值继续推迟到记忆与压缩之后。

## 2. 当前代码事实

| 能力 | 当前状态 | 不能宣称的内容 |
| --- | --- | --- |
| `TaskProcessor` + 受信 DAG | 已完成较深的执行、安全、恢复和故障边界 | 不能称为独立子 Agent 调度器 |
| `ModelGateway` 角色路由 | 已实际调用 intent、planner 等结构化角色 | `PlanStep.agent` 目前主要是标签，不等于 Agent Invocation |
| Agent Contract/Registry | 阶段 68 已实现固定内置 Contract、严格 Prompt Loader、冻结 Registry、只读 Descriptor API 和精确 Binder | 尚没有持久 Executable Plan、Handoff/Invocation 或 Agent Model Loop |
| Agent Handoff Runtime | 未实现 | 没有持久化 invocation、最小上下文包、子 Agent 结果和 handoff 血缘 |
| Verifier | 只有规则、Schema、Tool receipt、后置状态与评测基础 | 没有独立的任务完成验证记录；不能把 Tool 成功等同目标正确 |
| Task/Event/checkpoint | 已持久化且可安全恢复 | 它们是任务真值，不是会话/长期记忆 |
| 本地知识库 | 已有 Markdown/文本只读最小闭环 | 文档检索不是用户偏好或 episodic memory |
| 联网研究 | 未实现 | 没有 Web Search/Page Reader、PageSnapshot 或 Claim 级 Citation |
| Artifact 工作区/HTML | 未实现 | 没有任务隔离工作区、受控 Patch、HTML Builder 或浏览器验收 |
| 通用多轮任务 | 只有有限自由文本分类/计划和固定前端任务类型 | 不能称为可联网、可生成产物的通用对话 Agent |
| 会话/长期记忆 | 未实现 | 没有 MemoryItem CRUD、确认、冲突、过期或遗忘流程 |
| 上下文压缩 | 未实现 | `summary` 字段不是压缩链，也没有 source coverage 证明 |

## 3. 不变的工程原则

1. Agent 是版本化角色配置和受限运行实例，不要求每个 Agent 使用独立进程或不同模型。
2. Agent 的工具权限由 Registry、Policy 和 Runner 强制，不能只写进 prompt。
3. Supervisor 不直接执行 OS 副作用；子 Agent 也不能绕过现有 Tool ledger、审批、fence 和 receipt。
4. Agent 输出是待验证声明，不是事实；事实来自事件、Artifact、Citation、Tool receipt 和可重算后置状态。
5. Verifier 不能依靠执行 Agent 的自报成功；确定性 grader 优先于 LLM judge。
6. 多数 Agent 投票不等于正确。相同模型和相同上下文会产生相关性错误。
7. 记忆是带来源、作用域、状态和期限的数据；模型输出不能直接成为已激活的长期记忆。
8. 权限记忆只能自动收紧，不能自动放宽。放宽权限必须走显式用户设置/审批。
9. 摘要和压缩记忆永远不是 Task/Event/Approval/Tool ledger 的真值来源。
10. 不保存模型私有思维链；只保存必要的结构化决定摘要、证据和可审计结果。

## 4. 阶段依赖

```mermaid
flowchart LR
    S67["67 脱敏可观测与回归门禁"] --> S68["68 Agent Contract 与 Registry"]
    S68 --> S69["69 Contract/Plan Compiler 与通用能力合同"]
    S69 --> S70["70 Invocation Runtime 与只读联网研究"]
    S70 --> S71["71 独立验证与 research_to_html"]
    S71 --> S72["72 会话与工作记忆"]
    S72 --> S73["73 长期记忆与遗忘"]
    S73 --> S74["74 上下文压缩与重建"]
    S74 --> S75["75 通用/多 Agent 对抗发布门禁"]
```

不能把长期记忆提前到 Agent identity 和验证之前，否则系统无法可靠回答“谁生成了这条记忆、依据是什么、是否已验证”，容易把模型错误永久化。

## 5. 阶段 67：脱敏可观测与显式回归基线

本阶段已经完成；以下保留其验收边界供后续能力复用。

### 实现范围

- 为 task、model call、tool、MCP、evaluation 建立相关 span/metric；
- 普通属性只记录 ID、版本、计数、状态、风险和摘要，不记录 prompt、正文、凭据或 MCP 原文；
- 提供本地导出和 trace ID 查询；
- 黄金报告增加显式 `record`/`compare` 基线流程；
- CI 默认 compare，禁止测试成功时静默重写基线。

### 验收门

- 同一 task 可以沿 trace ID 定位 planning、tool、verify/evaluation 边界；
- 敏感 canary 不出现在 span、metric、普通日志和 CI artifact；
- 基线版本/模型/套件/阈值不匹配时稳定失败；
- 只有显式 record 命令才能创建或更新基线。

## 6. 阶段 68：Agent Contract 与只读 Registry

本阶段已经完成。实现记录见[《阶段 68：Agent Contract 与 Registry 最小闭环》](68-Agent-Contract与Registry最小闭环.md)。

### 目标

先定义“什么是一个可运行 Agent”，让 Planner 不能再输出任意 `agent` 字符串。

### 核心模型

- `AgentContract`：`agent_id`、schema/version、kind、描述、prompt package digest；
- `AgentRegistration`、脱敏 `AgentDescriptor` 和独立 Registry 状态；
- 固定 `allowed_tools`、`allowed_handoffs`、数据作用域和风险上限；
- Model capability/location/privacy 要求；
- input/output Schema；
- 单 invocation 的 model/tool/token/time/retry 预算；
- Contract manifest digest；启用/弃用/停用/撤销状态由 Registry 单独管理。

### 实现范围

- 仓库内固定内置 Agent Contract：首批只做 `computer_observer`、`knowledge_researcher`、`task_synthesizer`；确定性 Supervisor 不注册为 Agent；
- 启动时严格加载、Schema 校验和内容摘要复核；
- 只读 Registry API/UI，展示版本、工具、handoff、模型与风险边界；
- Plan Validator 必须解析每个 Agent、Tool、能力和 handoff；未知/禁用/越权引用 fail closed；
- 模型 DraftPlan 只输出 Agent selector；受信 Plan Binder 生成绑定精确 Agent/Prompt/Tool digest 的 ExecutablePlan；
- 运行时 Contract/Prompt/Tool digest 漂移时拒绝旧计划，新增无关 Agent 不应使旧计划失效。

### 验收门

- 未知 Agent、未知 Tool、超预算和 Agent→Agent 非法边均被本地拒绝；
- Agent 自称拥有某工具不能扩大 Registry allowlist；
- Contract/Prompt/Tool 被修改后旧 plan 不能冒充原版本继续；
- Registry 本阶段仍不接受用户上传 prompt、Python 或命令。

### 非目标

- 不执行真实 handoff；
- 不开放动态 Agent 创建或 Agent 自我复制；
- 不把 Profile 当安全授权本身，最终授权仍由 Policy/Runner 决定。

## 7. 阶段 69：Task Contract、Plan Compiler 与通用能力合同

### 目标

把自由对话中的目标编译为可绑定、可修订、可恢复的任务真值，并先定义联网研究和 Artifact 工作区的能力合同。阶段 69 不以“Agent 已经会联网做网页”为目标，而是消除后续运行时依赖自由文本、任意 Agent/Tool 名称或隐式授权的风险。

### 核心模型

- `Conversation/Message/Turn`：对话容器、消息和一次输入的结构化解释；
- `TaskContract/TaskAmendment`：目标、范围、产物、来源、成功条件、风险意图和 active generation；
- `DraftPlan -> BoundPlan -> ExecutablePlan`：模型草案、Registry/Capability 绑定和可执行图；
- `CapabilityPack`：版本化 Research、Artifact、Browser Verify 能力合同；
- `TaskWorkspaceContract`：工作区根、文件类型、配额、保留、fence 和导出边界；
- `ResearchContract`：Search/Page Read 预算、域/时间范围、来源和引用要求；
- Contract/Plan/Agent/Prompt/Tool/Capability/Verification digest 及 generation。

### 实现范围

- Turn Interpreter 只输出 `answer_only/new_task/task_amendment/clarification/typed_command`，不能直接产生副作用；
- Task Contract 明确区分用户目标、交付格式、事实来源、时间窗、数据出境和写入意图；
- 模型只产生不可信 DraftPlan；Compiler 解析冻结 Registry、Tool/Capability allowlist、Schema、资源和风险后生成 Bound/Executable Plan；
- Plan Validator 检查 DAG、coverage、Agent/Handoff、预算、风险、Workspace、Research 和 Verification requirement；
- Plan 原子激活且 generation 不可原地改写；用户修订创建 Amendment 和新 generation；
- 定义 `research.read.v1`、`artifact.html.v1`、`browser.verify.v1`，但未接 Runtime 的能力保持 disabled；
- 新增只读 Contract/Plan/Capability/Workspace 投影，前端能展示“将查什么、写什么、如何验收”；
- `research_to_html` 可编译为固定能力边界的 Executable Plan fixture，但不能伪造 Search、Artifact 或 Verification 结果。

### 验收门

- 未知 Agent/Tool/Capability/Verification profile、越权 handoff、环、预算超限和未覆盖成功条件稳定拒绝；
- 同一句用户输入不会隐式变成审批、导出或覆盖命令；
- Contract 修订后旧 generation 不能被新 claim 激活或继续交付；
- Agent/Prompt/Tool/Capability digest 漂移使旧计划 fail closed，无关 Registry 扩展不使旧绑定失效；
- `research_to_html` 计划中 Research、Artifact、Browser Verification 和 Final Acceptance 都是显式节点；
- 阶段 69 不新增任意 Shell、动态 Python、包安装或用户目录任意写入。

### 实施拆分

- 69A：Conversation/Turn 与 Task Contract/Amendment；
- 69B：Draft/Bound/Executable Plan Compiler 和 generation；
- 69C：Capability Pack、Research/Workspace/Browser Verify Contract；
- 69D：Plan Validator、原子激活、只读投影和 `research_to_html` fixture。

## 8. 阶段 70：持久化 Invocation Runtime 与只读联网研究

### 目标

让计划里的 Agent 从标签变成真实、可恢复、可观测的运行实例，并先接通只读联网研究。联网结果仍是待验证声明，不能直接解锁 Artifact Builder 或正式成功终态。

### 核心模型

- `TaskExecutionRun/Node/Edge`：ExecutablePlan 的规范化运行投影、ready/claim/join 和 Edge requirement；
- `HandoffEnvelope`：Supervisor 派生的不可变 objective、constraints、Artifact/Evidence 引用、输出 Schema、预算和 deadline；
- `AgentInvocation`：task/node/agent/model identity、attempt、执行状态、验证状态和预算；
- `AgentModelTurn`：每次 Provider 调用的 request/response digest、usage、状态和 unknown 边界；
- `AgentResult`：待验证 claims、Artifact/Evidence 引用、限制和错误分类；
- `ResearchSession/SearchCall/SearchHit/PageSnapshot/CitationEvidence`：Provider-neutral 的搜索、页面快照和引用链；
- input/output/context digest、parent/repair lineage、event sequence、lease/fence 和 trace ID。

### 实现范围

- Supervisor 只能从已验证计划创建 invocation；数据库 TaskExecutionRun 是运行真值，Outbox/Broker 只负责通知；
- Context Builder 只装配当前步骤所需的目标、约束、引用和工具 Schema，不复制全部任务历史；
- 子 Agent 通过 Model Gateway 调用，可按 Profile 选择同一或不同 Provider；
- SearchProvider 与 ModelGateway 分离；模型原生 Web Search 只能作为可替换 Adapter，并归一化为相同领域对象；
- `web.search` 和 `web.page.read` 经 Egress/SSRF/域名/重定向/大小/MIME/预算边界派发；外部正文始终标为 `external_untrusted`；
- Research Agent 只可提出 Claim/Citation，不能写 Artifact、扩大 Capability、批准操作或写 active Memory；
- Agent 的 Tool 请求仍进入原有 Policy→Approval→Runner 路径；
- 无依赖且只读/无资源冲突的 invocation 最多并行 3 个；
- node、invocation、handoff、model turn、结果和重试持久化，所有写入绑定 claim lease/fence/revision；
- Provider 调用崩溃窗口记录 outcome unknown；只允许在预算内创建新 model attempt，不伪造 exactly-once；
- API 重启后只恢复可证明的下一状态，不重复已持久化模型响应、Tool receipt、Result 或 Artifact identity；
- 前端显示真实 Agent、attempt、handoff、预算和血缘。
- 阶段 71 完成前，多 Agent/联网研究路径默认关闭或只产生 `awaiting_verification` 结果，不能解锁 Artifact Builder/Task Synthesizer，也不能驱动正式 `task.completed/succeeded`。

### 首个演示任务

固定 `research_to_html` 的前半段：Research Agent 对一个无登录公开主题执行真实 Search/Page Read，产生 PageSnapshot、ResearchClaim 和 CitationEvidence，并停在 `awaiting_verification`。可保留 Computer Observer + 本地 Knowledge 的并行 fixture 验证调度，但它不再冒充首个通用用户任务。阶段 71 验证通过后，确定性 Supervisor 才能解锁 Artifact Builder。

### 验收门

- 至少两个真实 invocation 有不同 profile/context/tool allowlist；
- 并行步骤无依赖才并行，资源冲突自动串行；
- 子 Agent 请求未授权工具时在模型调用之后、Tool 派发之前拒绝；
- URL scheme、私网/loopback/link-local/云元数据、重定向逃逸、超限响应和非允许 MIME 被 Page Reader 拒绝；
- 网页中的 Prompt Injection 不能改变 Task/Agent Contract、Capability、Policy、审批或 Memory 状态；
- 旧 claim fence 的迟到 Model/Result 被拒绝；Model dispatch 崩溃显式进入 outcome unknown；
- API 中断恢复无重复 invocation terminal event、已持久化 Tool call/Result/Artifact；
- `awaiting_verification` 节点不能满足默认依赖或解锁 Builder/Synthesizer；
- 递归 handoff 深度、总 invocation 数和总预算都有硬上限。

### 实施拆分

- 70A：Run/Node/Edge/Handoff/Invocation/ModelTurn/Result 数据模型；
- 70B：ready、claim、lease/fence、cancel/pause 和恢复；
- 70C：单 Agent 无 Tool，结果停在 awaiting_verification；
- 70D：单 Agent 只读 Tool 循环，复用 Policy/Runner/Evidence；
- 70E：SearchProvider/Page Reader、SSRF/Egress Gate 和 Research 领域对象；
- 70F：真实只读联网研究，仍不运行 Artifact Builder/Synthesizer。

### 非目标

- 不允许 Agent 动态编写新 Agent；
- 不做无限递归“Agent 自治”；
- 不允许子 Agent 直接调用 Shell、文件系统或网络。
- 不在缺少独立 VerificationRun 时把 invocation 自报结果当作任务成功。

## 9. 阶段 71：独立验证、Artifact 工作区与首个通用纵向闭环

### 目标

把“Agent 说完成了”和“系统证明完成了”分开，并完成首个真实可用的 `research_to_html`：已验证研究证据进入受控 Artifact 工作区，HTML 在隔离浏览器中通过渲染验收后才可交付。

### 核心模型

- `CompletionClaim`：声明内容、类型、来源 invocation 和必须证据；
- `EvidenceRef`：Artifact/Citation/Tool receipt/TaskEvent/后置状态引用及 digest；
- `VerificationSpec/PolicyRegistry`：每个 Node 的可信验收要求、freshness、partial 和 recovery policy；
- `EvidenceSnapshot`：Resolver 在固定数据库时间解析出的不可变证据快照；
- `GraderContract/Registry/Observation`：版本化确定性/语义/人工 Grader，但不能直接改 Node；
- `VerificationRun/ClaimVerdict`：grader 版本、输入摘要、逐 Claim 结论和总结果；
- `FinalAcceptanceRun`：Task Contract coverage、Synthesizer 血缘、当前性和未决 effect 验收；
- `TaskWorkspace/Artifact/ArtifactRevision/PatchReceipt`：任务隔离根、不可变内容版本和受控修改回执；
- `BrowserRenderRun`：Browser profile、入口 revision、网络/console/page error、DOM、截图和可访问性证据；
- `DeliveryManifest`：实际交付版本、引用、截图、限制和导出状态；
- 业务结果：`verified`、`partial`、`rejected`、`needs_user`；基础设施失败单独为 `verification_error`。

### 验证顺序

1. Claim/Evidence Schema、identity、digest、scope、authorization；
2. Tool receipt、资源后置条件、Artifact/Citation 完整性和来源复核；
3. 确定性业务规则、版本和 freshness；
4. 仅对不可规则化的语义质量使用固定 Judge；
5. 高风险、证据冲突或不足时交给用户，不让模型猜测通过。

### 实现范围

- `step.completed` 前必须存在匹配的 `VerificationRun`；
- 阶段 70 Research Claim 通过节点验证后才允许 Supervisor 解锁 Artifact Builder；
- 不再仅写一个没有 proof identity 的 `verified: true`；
- 语义 judge 无任何副作用 Tool，并与执行 Agent 使用不同 prompt/context；
- 执行模型不能成为自己输出的唯一裁判；同模型 judge 必须标记相关性风险；
- Judge/Resolver 基础设施失败进入 `verification_error`，不能把 AgentResult 误判为 rejected；
- Verification terminal、Node status、successor ready、TaskEvent 和 Outbox 原子提交并绑定 lease/fence；
- 验证失败最多触发一次有界修复 invocation，不能无限自我反思；
- repair 后使用新 invocation/verification attempt，保留原失败证据；Replan 创建新 plan generation 并继承旧 effect/deny/unknown；
- 所有 Node verified 后仍必须通过 Final Task Acceptance，Synthesizer 新事实必须有 verified Claim 血缘。
- 为每个 Task 创建独立 Workspace；拒绝绝对路径、`..`、符号链接、junction/reparse point、类型和配额逃逸；
- HTML Builder 只能通过 `artifact.html.v1` 创建不可变 revision 和 PatchReceipt，不能联网、Shell、安装依赖或写用户目录；
- HTML v1 默认单页静态、无外部资源、无运行时网络、禁用 JavaScript；具体 profile 参数后续确认；
- Browser Verifier 每次使用无登录的新 Context，默认阻断外网、Service Worker 和下载，采集截图/DOM/console/page/network 证据；
- 工作区内 R1 写可使用 Task Contract 绑定的范围授权；导出或覆盖用户路径必须是独立 Command/Approval；
- `research_to_html` 只有在 Contract coverage、Claim/Citation、Artifact revision 和 Browser evidence 全部通过后才可形成 DeliveryManifest。

### 验收门

- Agent 自报成功但文件/引用/receipt 不存在时任务不能成功；
- 部分有效结果返回 `partial` 和缺失项，不用流畅总结掩盖失败；
- Verifier 被注入“忽略规则”文本时不能改变 Policy 或确定性 grader；
- 修复预算用尽后稳定停止并保留可解释失败分类。
- 页面尝试访问 CDN/远程图片/fetch 时被浏览器阻断并使对应 profile 验收失败；
- Builder 尝试路径逃逸、读取其他 Task 或覆盖用户文件时在 Patch 派发前拒绝；
- API 在 PageSnapshot、Artifact patch、revision 激活和 Browser render 崩溃窗重启后可对账，且不重复终态；
- 用户能实际打开交付 HTML、查看来源/截图/限制并选择是否导出。

### 实施拆分

- 71A：Claim、Evidence、VerificationSpec、Grader Contract/Registry；
- 71B：Evidence Resolver、不可变 Snapshot 和 Research Claim 验证；
- 71C：Task Workspace、Artifact revision、PatchReceipt 和恢复；
- 71D：HTML Builder 与受限 `artifact.html.v1`；
- 71E：隔离 Browser Verifier 和 BrowserRenderRun；
- 71F：partial/reobserve/一次 repair/Replan Gate；
- 71G：Final Acceptance、statement lineage、DeliveryManifest 和 `research_to_html` UI。

## 10. 阶段 72：会话记忆与任务工作记忆

### 目标

先实现短期、可界定的记忆，不立即做自动长期个性化。

### 核心模型

- `Conversation`、`Message` 和大内容 `content_ref`；
- `WorkingMemoryItem`：task/session scope、kind、source、status、TTL；
- `ContextRequest/ContextItem/ContextManifest`：本次 invocation 的允许 source、候选、包含/排除项、版本、token、出境决定和 digest；
- 内容分类、Provider 出境决策和删除状态。

### 实现范围

- 会话消息、当前目标、明确约束、未决问题和选定 Artifact 引用持久化；
- Context Builder 按 Agent/Profile/步骤选择最小上下文；
- Agent 不能直接查询 Memory/RAG/Artifact Store，所有读取范围按 Contract/Handoff/Task/ACL/privacy 求交；
- 用户可以查看当前会话被系统保留和实际送入模型的项目；
- 删除会话记忆后不能再进入新 invocation；
- Task/Event/Approval/Tool ledger 与工作记忆分表、分语义；删除记忆不能伪造删除审计真值；
- 不保存 CoT；长正文转受控 Artifact/reference。

### 验收门

- 新步骤只收到相关上下文，不收到无关会话正文或其他任务数据；
- LOCAL_ONLY/云端 Provider 的 ContextManifest 可验证数据出境决策；
- TTL、用户删除和任务终止后的保留策略可重复测试；
- prompt injection 文本只能作为不可信 message/artifact，不能写成系统约束。

### 非目标

- 本阶段不做跨会话自动偏好；
- 不做 embedding 记忆检索；
- 不从模型推断用户事实并永久保存。

## 11. 阶段 73：长期记忆、确认、冲突与遗忘

### 目标

增加跨会话记忆，同时防止记忆污染和权限升级。

### 记忆类型

| kind | 写入策略 | 默认状态 |
| --- | --- | --- |
| `preference` | 用户明确表达或确认提案 | active |
| `restrictive_permission` | 用户明确设置；系统只能自动建议收紧 | active/pending |
| `fact` | 用户确认后激活 | pending |
| `episode` | 从已验证任务结果提案 | pending，短 TTL |
| `skill_template` | 用户显式保存已验证模板 | versioned |

### 核心约束

- `MemoryItem` 必须有 source IDs、创建主体、scope、confidence、status、TTL 和 digest；
- 模型/Agent 只能创建 `MemoryProposal`，不能直接创建 active 长期记忆；
- 权限放宽永远不能由 Agent、总结器或冲突合并器自动完成；
- 优先级固定为：当前用户指令 > 当前任务约束 > 显式用户设置 > 已确认长期记忆 > 模型提案；
- 冲突项不自动覆盖，进入可查看的 conflict 状态；
- 提供 list/create/confirm/reject/edit/delete/export API 和前端；
- 敏感 value 使用本地保护，明文不进入普通日志、trace 或向量索引；
- Memory 与 RAG 保持独立真值和派生索引；向量索引不能成为 source of truth；
- 删除 active memory 后派生索引同步失效，并留下不含原文的 tombstone/audit。

### 验收门

- 恶意网页、MCP 输出或子 Agent 文本不能直接写 active memory；
- “以后不要访问财务目录”可形成限制记忆，“以后无需审批”不能自动放宽 Policy；
- 记忆冲突、过期、删除后不会继续被 Context Builder 召回；
- 用户能看到“为什么记住、何时使用、提供给哪个 Agent/Provider”。

## 12. 阶段 74：可证明的上下文压缩与重建

### 目标

在上下文预算受限时压缩历史，同时确保关键约束不会因摘要漂移而丢失。

### 核心模型

- `CompactionSnapshot`：conversation/task、source message range/IDs/digests、版本和 parent；
- 结构化摘要字段：goal、active constraints、confirmed decisions、open questions、Artifact/Evidence refs、active memory refs；
- `ContextManifest`：原始片段、摘要、工具 Schema 和实际 token 预算；
- coverage、conflict 和 stale 状态。

### 实现范围

- 按 token/消息/Artifact 预算触发，不按模糊“感觉太长”触发；
- 先确定性提取 ID、约束、决定和 evidence，再由模型压缩非权威叙述；
- 摘要绑定完整 source ID 集和 digest，源消息修改/删除后 snapshot 标记 stale；
- 多轮压缩形成 parent chain，禁止无来源地反复摘要摘要；
- 当前明确指令、权限限制、未决审批和 unknown Tool 状态禁止仅靠模型摘要保存；
- Provider Egress Gate 和 ContextManifest 必须在压缩后重新校验 classification、scope 和 digest；
- 支持从 source refs 重建上下文，并提供压缩前后差异/预览；
- 不保存私有思维链。

### 验收门

- 在长会话 fixture 中，目标、否定约束、路径、数字、未决问题和 evidence refs 100% 保留；
- 压缩后 Agent 不能获得已删除或越作用域的记忆；
- 摘要 hallucination 不能成为 Task/Policy/Memory active truth；
- source digest 漂移时旧 snapshot 不被静默使用；
- 相同输入/版本产生稳定 manifest，语义摘要允许变化但必须重新验证 coverage。

## 13. 阶段 75：通用任务与多 Agent 对抗评测发布门禁

### 目标

用真实通用任务和多 Agent 任务证明阶段 68～74，而不是只展示多个角色名称、安全底座或流畅最终文本。

### 黄金/对抗案例

- 两个独立只读 Agent 并行，Supervisor 正确 join；
- 一个分支失败，其他分支形成 `partial`；
- 子 Agent 输出结构合法但事实错误，Verifier 拒绝；
- 相同模型的两个 Agent 产生相关性错误，确定性 evidence grader 拒绝多数意见；
- Agent 越权 Tool/handoff/递归深度被拒绝；
- API 重启后 invocation/handoff/verification 不重复；
- 恶意文档、网页、MCP 试图写长期记忆被隔离为不可信提案；
- 记忆冲突、遗忘和 TTL 后不再召回；
- 长上下文压缩保持关键约束，删去 source 后旧摘要失效。
- `research_to_html` 覆盖真实搜索、页面读取、引用、Task Workspace、浏览器断网验收和交付；
- 搜索/页面 Prompt Injection、SSRF、重定向、恶意 HTML、路径逃逸和远程资源加载均被阻断；
- Contract Amendment 使旧 plan/research/artifact generation 不能继续交付。

### 指标

- handoff Schema 通过率；
- claim evidence coverage；
- deterministic verifier precision/recall；
- semantic judge 与人工 rubric 一致率；
- memory recall precision、错误激活率和遗忘生效率；
- compaction constraint retention 和 unsupported-claim rate；
- 每任务 invocation/model/tool 数、Token/费用、p50/p95；
- unauthorized side effect、approval bypass 和 memory-policy relaxation 必须为 0。

### 门禁

- 新增独立版本化 multi-agent suite，不能用阶段 66 的单编排器案例冒充；
- CI 使用阶段 67 的显式 baseline compare，不静默 record；
- Judge 配置、Agent Contract、prompt package、模型和记忆/压缩版本分组统计；
- 不能以“多个 Agent 一致”替代 evidence grader；
- 低置信度、证据不足或 judge 分歧必须进入 `partial`/`needs_user`，不强行给成功。

## 14. 明确延后

完成阶段 75 前不做以下功能：

- Agent 自主创建/修改 Agent；
- 开放式无限递归 handoff；
- 第三方 Agent Contract 或上传 prompt package；
- 用共享聊天文本代替结构化 handoff；
- 让 Agent 直接写 active 长期记忆；
- 让摘要覆盖原始 Task/Event/Policy 真值；
- 仅靠自我反思、投票或 LLM-as-judge 宣称“保证准确”；
- 多 Agent 并发执行未经证明可交换的写副作用。

## 15. 完成定义

只有满足以下条件，项目才可以对外称为“已实现多 Agent 系统”：

1. 至少两个不同 Agent Contract 被实际实例化并产生独立 Invocation；
2. Handoff 输入、输出、预算、证据和父子血缘持久化且可恢复；
3. 工具权限在 prompt 外强制执行；
4. 至少一个真实任务发生并行 Agent 执行和确定性 join；
5. 完成状态由独立 VerificationRun 支持，不能只来自 Agent 自报；
6. 记忆有来源、作用域、确认、冲突、TTL 和删除机制；
7. 压缩上下文不承载授权或任务真值；
8. 多 Agent、记忆污染和压缩漂移通过版本化黄金/对抗门禁。
9. 至少一个自由对话任务完成真实联网研究、Claim 级引用、Task Workspace 产物和隔离浏览器验收；
10. 用户能查看并修订 Task Contract、研究来源、Artifact revision、验证问题和交付限制。

## 16. 实施前需要确认的设计选择

以下是后续阶段仍需固化的关键选择。产品目标与首个纵向切片已经由 [ADR-015](ADR-015-通用任务Agent产品边界与首个纵向切片.md)接受；具体参数不因方向接受而自动定案。

| 决策 | 当前建议 | 原因与代价 |
| --- | --- | --- |
| 编排框架 | 延续现有领域 Runtime/受信 DAG，不引入 LangGraph 核心 Runtime；图 UI 走只读投影 | 当前事务、fence、checkpoint 和恢复已很深；双编排真值的迁移风险高。Agent Invocation reducer 与图投影需自行实现，边界见 [ADR-014](ADR-014-图可视化与LangGraph采用边界.md) |
| 首批 Agent | 只做 `computer_observer`、`knowledge_researcher`、`task_synthesizer`；Supervisor 保持确定性控制组件 | 足以证明真实并行、join 和证据约束；把 Supervisor 注册成模型 Agent 会混淆控制权与执行权限 |
| 阶段 70 上线方式 | feature flag 默认关闭，或仅显示 `awaiting_verification` | Handoff/联网研究完成不代表结果正确，必须等阶段 71 才能接正式成功终态 |
| 首个通用切片 | `research_to_html`，先受控研究和静态 HTML，不开放任意 Shell | 能同时检验对话、联网、证据、写入和浏览器验收，又不把首版风险扩成通用代码执行 |
| Search 架构 | Provider-neutral SearchProvider；模型原生 Search 仅作 Adapter | 避免 Provider 隐藏引用成为领域真值；代价是要维护归一化合同 |
| HTML v1 | 单页静态、无外部资源、默认禁用 JavaScript | 先让渲染和网络边界可证明；交互网站另立 profile |
| Verifier 模型 | 规则优先；语义 judge 使用独立 prompt/context，允许同模型但记录相关性风险 | 强制不同云模型会增加费用和隐私风险；同模型又不能宣称真正独立 |
| 长期记忆激活 | 用户明确写入可 active；Agent 派生内容只能 pending proposal | 降低错误永久化和记忆投毒；代价是首次使用会多一次确认 |
| 权限记忆 | 自动化只允许收紧，放宽必须显式设置 | 安全优先，避免摘要或 Agent 输出绕过审批 |
| 会话原文保留 | 本地可配置，默认 90 天；敏感大内容使用受控 Artifact/ref | 支持追溯和重建，但必须给用户删除与导出能力 |
| 压缩 Provider | 默认本地；云端压缩遵循原数据 privacy/classification | 压缩输入通常包含高密度上下文，泄露影响比单段检索更大 |
| 并行上限 | 默认最多 3 个只读/无冲突 invocation | 足够演示且便于预算与取消；更高并发暂时没有价值证据 |
| 动态/第三方 Agent | 阶段 75 前关闭 | 先证明固定内置 Agent 的权限、记忆和验证边界，再扩大供应链面 |

其中最重要的取舍是：不要为了“看起来更 Agentic”引入自主递归、自动写记忆或多数投票。这些功能会明显扩大错误面，却不会自动提高任务正确率。
