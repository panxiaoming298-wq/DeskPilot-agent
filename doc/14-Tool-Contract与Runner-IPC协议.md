# 14. Tool Contract 与 Runner IPC 协议

## 1. 目标与当前实现状态

本协议把“模型建议调用工具”与“本机真正执行动作”隔开。控制面只能请求一个已注册、版本完全匹配的工具；Runner 只接受经过认证、未过期、未重放且参数通过 Schema 校验的调用。

协议层已经实现：

- 数据化、可摘要、不可携带 Python import path 或 callable 的 `ToolContract`。
- 只由受信任组合代码写入的精确名称/版本 `ToolRegistry`。
- HMAC-SHA256 签名信封、Runner 启动随机数、时间窗和内存防重放。
- 单帧 NDJSON codec、1 MiB 帧上限、严格 UTF-8 和重复 JSON key 拒绝。
- Runner 调用授权器及协议单元测试。

独立 Runner 子进程、控制面 `RunnerClient` 和首个真实 R0 工具 `computer.disk_usage@1.0.0` 也已接通。进程实现、生命周期与集成边界见[独立 Runner 与首个 R0 工具实现](15-独立Runner与首个R0工具实现.md)。

Runner 自动换代、退避/熔断、持久化调用账本和 `unknown` 收敛也已完成，详见 [Runner 故障恢复与 unknown 调用持久化](27-Runner故障恢复与unknown调用持久化.md)。

Policy/Approval 执行前授权现已接入真实调用：控制面持久化策略决定与一次性审批，Runner 要求精确匹配的 `ToolAuthorizationGrant`。详见 [Policy / Approval 执行前授权主干](28-Policy-Approval执行前授权主干.md)。

代码位置：

```text
backend/src/deskpilot/
├── core/canonical_json.py
├── domain/tool_contracts.py
├── application/tool_registry.py
├── application/runner_client.py
└── runner/
    ├── authorization.py
    ├── executor.py
    ├── ipc_codec.py
    ├── ipc_protocol.py
    ├── server.py
    └── service.py
```

## 2. 信任边界

```mermaid
flowchart LR
    MODEL["模型 / Agent"] -->|"只生成候选调用"| CONTROL["FastAPI 控制面"]
    CONTROL --> POLICY["Policy / Approval"]
    POLICY --> CONTRACT["Tool Registry + Contract"]
    CONTRACT --> SIGN["签名 + TTL + startup_nonce"]
    SIGN -->|"单帧 NDJSON"| RUNNER["独立 Tool Runner"]
    RUNNER --> VERIFY["验签 / 防重放 / 精确版本 / Schema"]
    VERIFY -->|"仅授权后的结构化参数"| IMPLEMENTATION["内置工具实现"]
    IMPLEMENTATION --> OS["Windows / 文件 / 应用 / 网络"]
```

安全不变量：

1. 模型输出永远不是执行授权。
2. Runner 不接受任意工具名、任意模块路径、任意命令行或动态 import。
3. 工具实现只能由 Runner 的受信任组合根显式注册。
4. Contract 的风险、Schema、超时、幂等性和能力声明都进入摘要；调用绑定该摘要。
5. HMAC 只能证明消息来自持有会话密钥的一方，不等于 OS 沙箱。进程隔离、低权限账户、目录能力和审批仍须单独实现。

## 3. Tool Contract

`ToolContract` 是控制面和 Runner 共用的数据协议，不包含可执行对象。

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `name` | 稳定工具名 | 小写命名空间，例如 `computer.disk_usage` |
| `version` | 工具协议版本 | 严格三段 SemVer，例如 `1.0.0` |
| `description` | 面向模型和开发者的用途 | 非空，最多 500 字符 |
| `risk_level` | 风险等级 | `R0`～`R4` |
| `side_effects` | 可观察副作用 | 只读工具为空 |
| `reversible` | 是否可逆 | 不代表允许跳过审批 |
| `input_schema` | 输入 JSON Schema | 由 Pydantic 输入模型生成 |
| `output_schema` | 输出 JSON Schema | 由 Pydantic 输出模型生成 |
| `execution.timeout_seconds` | 单次超时 | 1～3600 秒 |
| `execution.idempotency` | 幂等策略 | `idempotent`、`key_required`、`non_idempotent` |
| `execution.max_output_bytes` | 工具输出上限 | 1 KiB～16 MiB |
| `execution.resource_locks` | 资源锁模板 | 下一阶段执行器消费 |
| `security.capabilities` | 最小能力集合 | 例如 `filesystem.metadata.read` |
| `security.network_access` | 是否需要网络 | 默认 `false` |
| `security.supports_dry_run` | 是否支持预演 | 默认 `false` |

Contract 使用排序 key、无多余空白、UTF-8 的规范 JSON 计算 SHA-256 摘要。任何字段变化都会得到新摘要；不兼容的 Schema 或语义变化还必须提升工具版本。

`ToolRegistry.register()` 同时接收 Contract、输入模型和输出模型，并验证模型生成的 Schema 与 Contract 完全一致。解析时必须同时匹配名称和版本，不进行静默版本回退。

## 4. IPC 消息模型

所有消息封装在 `SignedIpcEnvelope` 中：

```json
{
  "protocol_version": "deskpilot.runner.v1",
  "key_id": "control-plane-key-1",
  "algorithm": "HMAC-SHA256",
  "payload": {
    "message_type": "tool.call",
    "call_id": "call-0001",
    "task_id": "task-0001",
    "step_id": "step-0001",
    "tool_name": "computer.disk_usage",
    "tool_version": "1.0.0",
    "contract_digest": "<64 位十六进制摘要>",
    "arguments": {"path": "C:\\"},
    "actor": "local-user",
    "idempotency_key": null,
    "expected_resource_versions": {},
    "issued_at": "2026-08-09T12:00:00Z",
    "expires_at": "2026-08-09T12:00:30Z",
    "nonce": "<单次命令随机数>",
    "startup_nonce": "<本次 Runner 启动随机数>"
  },
  "signature": "<Base64URL HMAC-SHA256>"
}
```

支持的 v1 消息：

| `message_type` | 方向 | 作用 |
| --- | --- | --- |
| `runner.hello` | Runner → 控制面 | 回报 Runner 身份、启动随机数和支持协议 |
| `runner.heartbeat` | Runner → 控制面 | 心跳和活动调用列表 |
| `tool.call` | 控制面 → Runner | 发起一次精确版本的工具调用 |
| `tool.cancel` | 控制面 → Runner | 请求取消指定调用 |
| `tool.progress` | Runner → 控制面 | 单调序号的进度消息 |
| `tool.result` | Runner → 控制面 | 成功、失败、取消或未知结果 |

`tool.result.status=unknown` 用于进程中断后无法证明副作用是否发生的情形。控制面不得把 `unknown` 当作可安全自动重试的失败。

## 5. 签名、会话与防重放

签名输入是以下结构的规范 JSON，不包含 `signature` 自身：

```text
protocol_version + key_id + algorithm + 完整 payload
```

HMAC 会间接或直接绑定 `task_id`、`step_id`、`call_id`、工具名称和版本、Contract 摘要、规范化参数、资源版本、幂等键、时间窗、单次 nonce 与 Runner 启动随机数。比较签名使用恒定时间比较。

v1 规则：

- 会话密钥至少 32 字节，每次 Runner 启动重新生成。
- `startup_nonce` 每次 Runner 启动重新生成，旧进程签名的消息不能用于新进程。
- 命令默认最长存活 60 秒；允许最多 5 秒的“签发时间在未来”时钟偏差。
- `expires_at <= now` 时立即拒绝，不延长过期时间。
- 每个命令 nonce 在一个 Runner 生命周期内只能消费一次；校验成功后即消费，即使后续工具白名单校验失败也不能重放。
- 响应同样签名并绑定 `runner_id` 和 `startup_nonce`；控制面还必须按 `call_id` 关联活动调用。

控制面现在会生成密钥与启动随机数，通过子进程 stdin 的首个 bootstrap 帧交给它创建的 Runner；密钥不进入命令行、环境变量、日志或普通配置文件。bootstrap 后所有帧必须签名。生产桌面版还需进一步限制子进程令牌、句柄继承、当前用户 ACL 和进程权限。

## 6. NDJSON 传输约束

MVP 选择父子进程标准输入/输出上的 NDJSON：一行只允许一个完整信封，以 `\n` 结束。标准错误仅用于脱敏诊断日志，不能混入协议帧。

codec 当前强制：

- 默认单帧最大 1 MiB，编码和解码两侧都检查。
- 严格 UTF-8。
- 一次 `decode` 恰好一个、且必须换行结尾的帧。
- 拒绝重复 JSON key，避免解析歧义。
- 拒绝额外字段和未知 `message_type`。
- 解析成强类型 Pydantic 消息后再做语义规范化验签。

NDJSON 只解决分帧，不解决身份认证；身份认证来自每帧签名和启动会话绑定。

## 7. Runner 授权顺序

Runner 在调用任何工具实现前按以下顺序拒绝失败：

1. 帧大小、单帧、UTF-8、重复 key 和信封 Schema。
2. `protocol_version`、`key_id` 和 HMAC 签名。
3. `startup_nonce`、签发时间、过期时间和最大 TTL。
4. 原子消费命令 nonce，阻止重放。
5. 在 Registry 中精确查找 `tool_name@tool_version`。
6. 比较调用携带的 `contract_digest`。
7. 以 Tool Registry 中受信任的 resource projector 从已验证参数本地重算规范资源，再校验 `ToolAuthorizationGrant` 与 task/step/call、actor/origin、工具/Contract、策略、参数/资源、capability、网络/外发、副作用、交互/批量、风险和可选审批完全一致且未过期。
8. 对 `key_required` 工具检查幂等键。
9. 用注册的 Pydantic 输入模型校验参数。
10. 通过 Runner 内静态 handler 映射执行，校验输出 Schema 和 Contract 输出大小上限。

Policy 审批、参数到规范资源的语义绑定和资源范围校验已经完成，资源版本摘要也进入授权证明；dispatch 前实际资源版本验证、资源锁与 OS 级 capability 强制仍属于后续安全阶段。当前真实工具仅开放 R0 磁盘容量元数据读取。

授权结果是 `AuthorizedToolCall`，其中参数已经转换成注册的 Pydantic 模型。工具实现不读取原始 JSON，也不自行动态选择实现。

## 8. 稳定错误码

| 错误码 | 含义 |
| --- | --- |
| `IPC_FRAME_INVALID` | 帧格式、UTF-8 或信封 Schema 无效 |
| `IPC_FRAME_TOO_LARGE` | 帧超过限制 |
| `IPC_DUPLICATE_JSON_KEY` | JSON 出现重复字段 |
| `IPC_KEY_UNKNOWN` | 未知签名密钥标识 |
| `IPC_SIGNATURE_INVALID` | 签名不匹配 |
| `IPC_STARTUP_NONCE_MISMATCH` | 消息属于另一 Runner 会话 |
| `IPC_MESSAGE_ISSUED_IN_FUTURE` | 签发时间超出允许偏差 |
| `IPC_MESSAGE_EXPIRED` | 消息已过期 |
| `IPC_MESSAGE_TTL_EXCEEDED` | 消息存活期超过上限 |
| `IPC_REPLAY_DETECTED` | nonce 已消费 |
| `IPC_UNEXPECTED_MESSAGE` | 当前处理器收到不支持的消息类型 |
| `TOOL_NOT_REGISTERED` | 工具名称或版本不在白名单 |
| `TOOL_CONTRACT_MISMATCH` | 调用摘要与本地 Contract 不一致 |
| `TOOL_IDEMPOTENCY_KEY_REQUIRED` | Contract 要求但调用未提供幂等键 |
| `TOOL_SCHEMA_VALIDATION_FAILED` | 输入或输出不符合注册 Schema |

错误响应进入任务事件前必须脱敏；签名、密钥、完整敏感参数和 Python 堆栈不得返回前端。

## 9. 测试与验收

当前自动化测试覆盖：

- Contract 摘要稳定性和元数据变更检测。
- Registry 重复注册、未知版本、模型/Contract 不一致和输入输出校验。
- 合法签名与 NDJSON 往返。
- 参数篡改、未知 key、旧 startup nonce、过期、未来签发、超长 TTL 和重放拒绝。
- 未注册工具、摘要不一致、缺少幂等键、参数 Schema 失败。
- 缺少换行、多帧、重复 key 和超大帧拒绝。
- Runner hello 与工具结果的响应签名和会话绑定。

现已增加真实父子进程握手、心跳、异常启动/退出、自动换代、退避/熔断、执行超时、协作取消、输出上限，以及 `computer.disk_usage` 在临时路径上的 Windows 集成测试。测试还覆盖 generation lease、不重放、持久化 `unknown`、启动恢复、事务回滚，以及非重放安全工具执行中 timeout/cancel 的不确定结果。更强的恶意 Runner、超大 stderr 和高并发压力测试仍待后续安全阶段补充。

## 10. 后续强化顺序

1. 用 Windows Job Object、受限令牌、明确目录 capability 和每次调用进程隔离加强 OS 级边界。
2. 实现资源锁和真实工具可消费的资源版本检查。
3. 为有副作用工具增加持久化幂等回执，以及 `unknown` 的人工 reconciliation 和显式新 attempt。
4. 为协议增加高并发、恶意帧、stderr 洪泛和强制进程终止测试。

在这些能力完成前，继续禁止通用 Shell、任意 PowerShell、动态 Python 执行和高风险系统工具。
