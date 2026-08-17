# 阶段 68：Agent Contract 与 Registry 最小闭环

## 1. 完成范围

本阶段把原先只用于展示的 `PlanStep.agent` 标签与可绑定的 Agent 身份分开，新增以下启动期可信组件：

- 不可变、纯数据的 `deskpilot.agent-contract.v1`；
- 有大小上限、UTF-8、重复 JSON key、未知字段和类型强校验的 Contract/Prompt Loader；
- 只接受受信应用组合代码注册、启动后冻结的 `AgentRegistry`；
- Agent、Prompt Package、输入输出 Schema、Tool Contract、Model capability 和 Handoff 静态图的交叉校验；
- 脱敏只读 Descriptor、精确版本查询和内容寻址 Registry snapshot；
- 把不可信 Agent selector 转换为精确版本、Contract digest、Prompt digest、Tool grant 和预算的 `AgentPlanBinder`。

这不是完整 Agent Runtime。本阶段不创建 Invocation/Handoff 记录，不运行 Agent Model Loop，不持久化 Executable Plan，也不让 Agent Contract 代替 Policy、Approval、Runner 或 Verification。

## 2. 内置 Agent

Registry 固定注册三个 v1 Agent：

| Agent | 类型 | 权限边界 |
| --- | --- | --- |
| `builtin.computer_observer@1.0.0` | worker | 仅允许 `computer.disk_usage@1.0.0`，风险上限 R0 |
| `builtin.knowledge_researcher@1.0.0` | worker | 只读本地知识引用，结果必须保留 Citation |
| `builtin.task_synthesizer@1.0.0` | synthesizer | 无 Tool，只汇总已验证的上游引用 |

Supervisor 仍是控制面角色，不注册成 Agent。三个 Agent 都禁止 Memory write；默认 Fake Provider 能满足 strict structured-output 要求。若当前 Provider 集合不能满足某个 Contract，Registry 仍可启动，但该版本会以 `disabled / model_requirements_unsatisfied` 公开并拒绝绑定。

## 3. Digest 与绑定

Agent Contract、Prompt Package、Schema 和 Registry snapshot 都使用 canonical JSON SHA-256。Prompt Package digest 同时覆盖严格 manifest 与指令正文，指令文件变化不会静默复用旧绑定。

`AgentPlanBinder` 只接受 Agent ID selector、可选 Tool 精确版本和请求预算。绑定结果由受信 Registry 填入：

- `agent_id + version + contract_digest`；
- `prompt_package_digest`；
- Contract 内已经固定的 Tool grant/digest；
- 不超过 Contract 上限的预算。

Draft 中伪造 digest 或动态执行字段会因 `extra=forbid` 被拒绝。绑定后的 Contract/Prompt/Tool 任一漂移都会在使用前 fail closed；向 Registry 增加无关 Agent 不会使已有精确绑定失效。

## 4. 启动期校验

Registry 在 freeze 前完成：

1. Agent 输入输出 Schema 与受信 Pydantic 模型逐字典一致；
2. Prompt package ID、版本、renderer version 和 digest 一致；
3. Tool 名称、版本、Contract digest 与风险上限一致；
4. Model locality、structured output、strict JSON Schema、tool calling 和 context window 能力满足要求；
5. Handoff 目标存在、正反向声明一致、边数有界且静态图无环；
6. Agent ID/version 不重复，freeze 后禁止继续注册。

Prompt Loader 只读取同一受信目录中的普通 `.json/.txt` 文件，拒绝目录逃逸、符号链接、重复 key、秘密标记、非法变量和超限内容。Prompt 正文与文件路径不进入 Descriptor API。

## 5. 只读 API

```text
GET /api/v1/agents
GET /api/v1/agents?status=&kind=&capability=
GET /api/v1/agents/registry-snapshot
GET /api/v1/agents/{agent_id}/versions/{version}
```

响应统一 `Cache-Control: no-store`，只返回公开 Contract 投影和 Schema digest，不返回 Prompt 正文、Prompt 文件路径、callable、import path、凭据或动态代码。disabled/revoked 版本仍可查询其 Descriptor，但精确执行解析和 Plan binding 会拒绝它们。没有 Agent 注册、上传、启停或调用写 API。

## 6. CI 与验收

`test_agent_registry.py` 覆盖：

- 三个内置 Agent、Supervisor 排除、Registry freeze 和 Descriptor 脱敏；
- API 列表/筛选/精确版本/snapshot/404/405；
- Contract/Prompt 的 unknown field、duplicate key、秘密标记拒绝；
- I/O Schema、Prompt digest、Tool digest 和 Handoff cycle 漂移拒绝；
- Model capability 不满足时 disabled 且不可解析；
- Binder 的精确 digest、Tool allowlist、预算上限和伪造字段拒绝；
- 无关 Registry 扩展兼容与同版本 Contract 漂移拒绝。

`.github/workflows/phase-68-agent-registry-gate.yml` 在 Windows 锁定依赖后运行专项测试、Ruff、mypy，并构建 wheel 验证 Prompt Package 资源确实随包发布。

本地验收结果：专项 8 项通过；后端全量 441 collected，`429 passed, 12 skipped, 1 warning`；Ruff、mypy 176 个生产源码、Alembic check、`uv lock --check`、wheel 资源复核和阶段 67 evaluation baseline compare 全部通过。

## 7. 明确保留边界

- Contract 是声明，不是授权；真实 Tool 仍必须通过 Policy/Approval/Runner。
- Handoff 声明只是 Registry 静态允许边，不代表已经发生 Handoff。
- Binder 是阶段 69 Plan Compiler 的最小安全原语，不是完整 Compiler。
- AgentResult 尚无持久 Invocation identity、Claim/Evidence 与独立 Verification，不得作为完成证明。
- 当前 Registry 是单进程启动快照；动态第三方 Agent、签名供应链、撤销分发和滚动升级仍在阶段 75 之后。

## 8. 下一阶段入口

阶段 69 应实现 Task Contract 与完整 Draft/Bound/Executable Plan Compiler：持久化 plan identity/generation、分层校验、原子激活和重规划边界。只有 Executable Plan 固定 Agent/Prompt/Tool/Policy/预算后，才进入 Handoff/Invocation Runtime；不得直接从本阶段 Binder 跳到模型或 Tool 执行。
