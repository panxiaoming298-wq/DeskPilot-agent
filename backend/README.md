# DeskPilot Backend

FastAPI 控制面最小骨架。当前 TaskProcessor 的磁盘容量任务通过 Model Gateway 的离线 Fake Provider 获得结构化分类和计划；显式单文件移动使用受信任应用计划模板和本地用户提供的结构化路径。两者都经过确定性 Policy/Approval，再由独立签名 Runner 执行。Runner 已支持自动换代、退避/熔断、持久化调用账本、`unknown` 人工对账/签名回执证据与受限显式新 attempt、Windows 每调用 Job/Low Integrity、句柄核验 resource broker、内容寻址 AppContainer worker bundle、专用 capability ACL 和孤儿 profile reaper，以及 R1 `file.move@1.0.0` prepare/commit/receipt 受控写闭环；受保护结构化请求/阶段 checkpoint、任务历史 API、Provider catalog、安全凭据、角色路由与模型韧性预算也已完成。默认首次 seed 仍只使用离线 Fake Provider。

## 启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn deskpilot.main:app --reload
```

默认 API：`http://127.0.0.1:8000`，OpenAPI：`/docs`。

## 本地 API 安全

- API 每次启动生成新的高熵 session token；也可通过 `DESKPILOT_SESSION_TOKEN` 注入至少 32 字符的令牌用于自动化启动。
- 受信任前端从 `GET /api/v1/session` 建立会话，响应带 `Cache-Control: no-store`。
- REST 使用 `Authorization: Bearer <token>`；写请求还要通过精确 Origin 或可信同源 Fetch Metadata 校验。
- WebSocket 使用 `deskpilot.v1` 与 `deskpilot.auth.<token>` 子协议，不把凭据放入 URL。
- API 错误统一返回 `application/problem+json`，包含 `type/title/status/detail/instance/code`。

当前浏览器开发模式会自动完成上述握手。发布为桌面应用时，应由桌面壳通过进程间安全通道交付 token，并启用严格 CSP。

## 任务控制状态机

当前 Model Gateway + Runner 处理器支持以下命令：

```text
POST /api/v1/tasks/{task_id}:pause
POST /api/v1/tasks/{task_id}:resume
POST /api/v1/tasks/{task_id}:cancel
```

请求体可省略，也可以传 `{"reason":"..."}`。暂停只允许从 `running` 进入，在已提交事件之间的安全点生效；恢复从当前进程或受保护且可验证的跨 API 重启 checkpoint 继续，不重复已经持久化的 Tool 事件。取消可用于所有非终态任务，并以 `task.cancelled` 结束事件流。

重复暂停已暂停任务、重复取消已取消任务是幂等操作，不会增加事件序号。非法转换返回 `409 TASK_TRANSITION_NOT_ALLOWED`；没有能与事件、Tool 账本、Policy 和审批同时证明一致的 checkpoint 时，恢复返回 `409 TASK_RUNTIME_UNAVAILABLE`。

任务历史使用 `GET /api/v1/tasks?status=&limit=&offset=` 查询。`limit` 限定为 1～100，结果按创建时间稳定倒序并返回 `items/total/limit/offset`，响应禁止缓存且不包含事件 payload 或 Tool 参数。

应用启动时会自动执行 Alembic upgrade。也可以手动检查或执行迁移：

```powershell
.\.venv\Scripts\alembic.exe current
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe check
```

第一版 migration 可初始化空库，也能接管首个开发版本通过 `create_all` 创建的数据库，原有任务数据不会被重建。当前 head 为 `0012_task_runtime_checkpoints`；在不保存原始参数或密钥的 `tool_calls` 恢复账本之外，现已包括人工对账、内容寻址 Runner 回执证据、Reconciliation API 幂等回执、`key_required` Tool 幂等键占用回执、受控提交证据投影、回执绑定的单补偿血缘和 current-user DPAPI 受保护任务 checkpoint。

## 事件可靠投递

任务状态、`task_events` 和 `outbox_messages` 在同一事务提交。后台 `OutboxPublisher` 提交后唤醒，同时保留轮询兜底；发送失败按指数退避重试，API 重启后会继续处理未发布消息。

投递语义是 **at-least-once**：进程若在发送成功、写入 `published_at` 之前退出，消息可能再次发送。WebSocket 端以任务内单调 `seq` 去重，事件补拉仍以数据库 `task_events` 为真值。

## Tool Contract 与 Runner IPC

- `domain/tool_contracts.py` 定义版本、风险、输入输出 Schema、幂等性、超时、输出上限和最小能力。
- `application/tool_registry.py` 只允许受信任组合代码注册工具，并要求每个工具提供从已验证参数生成规范资源的可信 projector。
- `runner/ipc_protocol.py` 提供 HMAC-SHA256 信封、启动 nonce、60 秒最大 TTL 和防重放。
- `runner/ipc_codec.py` 提供 1 MiB 上限的严格单帧 NDJSON。
- `runner/authorization.py` 在执行前完成验签、会话、版本、Contract 摘要、幂等键和输入 Schema 校验；Runner 还会从真实参数本地重算资源，并核对完整动态策略事实、一次性审批与有效期。

- `application/runner_client.py` 管理一代独立子进程、bootstrap、heartbeat、调用关联和 first-wins 故障通知。
- `application/runner_supervisor.py` 管理 Runner 代际、指数退避、open/half-open 熔断、稳定窗口和冻结 lease。
- `runner/server.py` 与 `runner/service.py` 提供独立 stdio Runner broker、并发上限、超时和取消；handler 不再在常驻 Runner 内执行。
- `runner/isolated_executor.py`、`runner/worker.py` 与 `runner/worker_protocol.py` 为每次授权调用创建一个一次性 worker，并在父子两侧复核版本、Contract、Schema、call ID、brokered capability coverage 和输出上限。
- `runner/resource_broker.py` 与 `runner/windows_resources.py` 在父 Runner 中打开授权路径句柄、核对最终路径并生成不可变卷容量 facts；worker 不再根据原始 path 打开资源。
- `runner/controlled_commit.py` 与 `runner/commit_receipts.py` 在父 Runner 维护 commit boundary 和独立 SQLite journal，启动时从外部资源版本恢复未完成提交。
- `runner/worker_runtime.py`、`runner/windows_acl.py` 与 `runner/profile_journal.py` 发布并校验最小 Python bundle、投影专用 capability RX DACL，并在 Runner 换代时回收孤儿 AppContainer profile。
- `runner/windows_sandbox.py` 使用 `CreateRestrictedToken`、Low Integrity、挂起创建和 Job Object 提供 Windows 进程边界；强制禁网时改用仅含 worker runtime capability、无网络 capability 的 AppContainer。
- `tools/computer.py` 实现 R0 `computer.disk_usage@1.0.0`，只消费父 Runner 提供的容量元数据。
- `tools/files.py` 实现 R1 `file.move@1.0.0`；worker 只生成 prepare，父 Runner 复核审批/资源版本后执行同卷、no-overwrite 单文件移动并持久化 receipt。

完整协议见 [`doc/14-Tool-Contract与Runner-IPC协议.md`](../doc/14-Tool-Contract与Runner-IPC协议.md)，进程实现见 [`doc/15-独立Runner与首个R0工具实现.md`](../doc/15-独立Runner与首个R0工具实现.md)，自动恢复和调用账本见 [`doc/27-Runner故障恢复与unknown调用持久化.md`](../doc/27-Runner故障恢复与unknown调用持久化.md)，执行前授权见 [`doc/28-Policy-Approval执行前授权主干.md`](../doc/28-Policy-Approval执行前授权主干.md)，Windows 每调用隔离见 [`doc/29-Windows-Runner进程隔离与低完整性实现.md`](../doc/29-Windows-Runner进程隔离与低完整性实现.md)，人工对账和新 attempt 见 [`doc/30-unknown人工对账与显式新attempt实现.md`](../doc/30-unknown人工对账与显式新attempt实现.md)，capability broker/commit/禁网边界见 [`doc/31-Contract能力Broker受控提交与Windows禁网边界.md`](../doc/31-Contract能力Broker受控提交与Windows禁网边界.md)，专用 runtime 与 profile 回收见 [`doc/32-AppContainer专用Worker运行时与Profile回收.md`](../doc/32-AppContainer专用Worker运行时与Profile回收.md)，首个受控写闭环见 [`doc/33-file.move受控提交与持久化回执.md`](../doc/33-file.move受控提交与持久化回执.md)，显式任务与审批入口见 [`doc/34-file.move显式任务入口与一次性审批.md`](../doc/34-file.move显式任务入口与一次性审批.md)，unknown 回执证据见 [`doc/35-unknown-Runner回执证据采集与前端展示.md`](../doc/35-unknown-Runner回执证据采集与前端展示.md)，回执驱动补偿见 [`doc/36-file.move回执驱动显式补偿闭环.md`](../doc/36-file.move回执驱动显式补偿闭环.md)，集中历史与对账见 [`doc/37-任务历史与集中Reconciliation中心.md`](../doc/37-任务历史与集中Reconciliation中心.md)。

受保护 checkpoint 的完整存储、证明与不重放边界见 [`doc/38-结构化Tool请求与可证明跨重启检查点.md`](../doc/38-结构化Tool请求与可证明跨重启检查点.md)。

Runner 由 API lifespan 自动启动和关闭，不应单独手工运行。初次启动失败时 API 以 degraded 状态继续启动，Supervisor 在后台恢复；旧代在途调用绝不自动发往新代。相关配置见 `.env.example`。

调用在越过可能派发的边界前写入 `tool_calls`。Runner 丢失或结果不可证明时，账本、`tool.unknown`、`task.failed` 和 Outbox 在同一事务提交。API 重启时，只有被有效 pre-dispatch checkpoint 绑定的 `requested` call 才可续跑；其他 `requested` 收敛为确定失败，遗留 `running` 始终收敛为 `unknown`，且重复恢复不新增事件。

## unknown 人工对账

```text
GET  /api/v1/reconciliations?status=&task_id=
GET  /api/v1/reconciliations/{reconciliation_id}
POST /api/v1/reconciliations/{reconciliation_id}:refresh-evidence
POST /api/v1/reconciliations/{reconciliation_id}:resolve
POST /api/v1/reconciliations/{reconciliation_id}:create-attempt
POST /api/v1/reconciliations/{reconciliation_id}:create-compensation
```

`unknown` 会原子创建 pending reconciliation。人工可裁决为 `confirmed_succeeded / confirmed_failed / confirmed_no_effect / accepted_unknown`；裁决不改写原 Tool 账本，且不可再修改。只有 `confirmed_no_effect` 且当前处理器能从持久化事实确定性重建请求时才能创建全新任务；目前该能力只开放给精确匹配 Contract 的 `computer.disk_usage@1.0.0`。`file.move` 不会从 goal 猜测未持久化路径，只能在 committed receipt 存在时走服务端派生的补偿路径。

resolve/create-attempt 两个裁决写 API 要求 16～128 位 `Idempotency-Key`，只持久化摘要与请求 fingerprint。同键同请求即使跨 API 重启也只返回原裁决/原任务；同键异请求返回 `409 IDEMPOTENCY_KEY_REUSED`。refresh-evidence 只执行签名 Runner 只读查询，并按 reconciliation + 证据内容摘要去重；receipt/no-receipt/query-failed 均不会改写原 Tool 账本或自动裁决。Contract 声明 `key_required` 时还会在 `tool.requested` 事务中占用 tool/version/key digest，禁止跨任务重复使用，但不因此自动重放。

## Windows 每调用隔离

常驻 Runner 只做授权复核和进程调度。每个通过授权的调用使用一次性 worker；Windows worker 通过受限主令牌与 Low Integrity（RID 4096）启动，并在首线程恢复前进入独立 Job Object。默认 Job 启用 kill-on-close、256 MiB 进程内存上限和 `ActiveProcessLimit=1`，因此 timeout/cancel 可以强制回收进程，工具也不能派生子进程。

worker 只继承私有 stdin/stdout/stderr，环境按白名单重建，不接收 Runner HMAC 密钥、Provider credential 或任意 `DESKPILOT_*` 秘密。控制面默认要求签名 hello 回报 `windows_restricted + per_call_process_isolation=true`，否则该 Runner 代际启动失败并进入原有 Supervisor 恢复流程。

`filesystem.metadata.read` 已由父 Runner 映射为路径句柄核验与不可变容量 facts；`file.move` 使用精确 source/destination capability、source 外部版本和 destination absent 事实。worker 收到的 resource operations 必须与 Contract capabilities 完全相等。声明 side effects 的 Contract 必须使用 `commit_protocol=brokered`；未注册 commit provider 的工具仍会 fail closed 并返回 `TOOL_CONTROLLED_COMMIT_UNAVAILABLE`。

```dotenv
DESKPILOT_RUNNER_REQUIRE_WINDOWS_SANDBOX=true
DESKPILOT_RUNNER_REQUIRE_NETWORK_ISOLATION=true
DESKPILOT_RUNNER_WORKER_RUNTIME_ROOT=./data/worker-runtime
DESKPILOT_RUNNER_APPCONTAINER_PROFILE_JOURNAL_PATH=./data/runner/appcontainer-profiles.json
DESKPILOT_RUNNER_COMMIT_RECEIPT_DATABASE_PATH=./data/runner/commit-receipts.db
DESKPILOT_RUNNER_WORKER_MEMORY_LIMIT_BYTES=268435456
DESKPILOT_RUNNER_WORKER_ACTIVE_PROCESS_LIMIT=1
```

开启网络强制后，控制面先发布约 57 MiB 的内容寻址 CPython worker bundle；protected DACL 只给稳定的 `DeskPilot.workerRuntime.v1` capability 读取/执行权限。每次调用再使用唯一 AppContainer profile，token 只加入该 runtime capability，不加入任何网络 capability，并在 Runner hello 报告 `windows_appcontainer / appcontainer`。真实 Python Tool、bundle 写拒绝和 loopback 禁网均已通过；异常退出留下的 profile 由下一 Runner 代际按 durable journal 回收。任何 bundle、ACL、journal 或预启动错误都会使该代际失败，不降级到 Low Integrity。

## Policy / Approval

- `BuiltinPolicyEngine` 只消费受信任结构化事实，输出 `allow / deny / require_approval`；R4 永久拒绝，当前非交互、批量和数据外发请求也会 fail closed。
- `0007_policy_approvals` 持久化精确预览、不可改写的用户 `decision` 与当前授权 `status`。批准后尚未派发的授权在有效 checkpoint 证明下可跨 API 重启续跑；取消、过期或绑定不可证明时显式失效并保留原批准审计。
- 审批 API 要求 Bearer、可信写来源、精确 `preview_hash` 与 `scope="once"`；所有审批读写成功响应均带 `Cache-Control: no-store`。
- 同一审批决定可幂等重放；运行时恢复竞态会再次尝试恢复检查点，但绝不透明重放工具调用。

当前 Runner 真实工具包括无副作用 R0 磁盘容量读取和 R1 `file.move`。文件移动已开放结构化单文件任务入口；路径只接受本地用户显式字段，使用固定应用计划，并始终经过一次性审批。启用 `DESKPILOT_POLICY_REQUIRE_APPROVAL_FOR_R0=true` 仍可单独验证 R0 的完整审批链。

## Model Gateway

- `domain/model_contracts.py` 定义 Provider descriptor、能力、隐私、请求、响应、usage、health 和统一流式事件。
- `domain/planning.py` 定义严格的任务分类与计划 Schema。
- `domain/model_routing.py` 定义角色 allowlist、价格、重试/费用预算、EWMA/熔断策略和安全运行投影。
- `application/model_gateway.py` 提供能力/隐私/角色/费用联合路由、全链 timeout、Retry-After fallback、EWMA 与 closed/open/half-open 熔断。
- `model_providers/fake.py` 提供无需网络和 API Key 的确定性本地 Provider。
- `model_providers/openai_compatible_chat.py` 提供 `/chat/completions`、strict JSON Schema、SSE、usage、health 和脱敏 HTTP 错误归一化。
- `domain/provider_config.py` 定义不含密钥的 Fake/兼容 Provider 配置、credential reference 与 endpoint 安全策略。
- `infrastructure/environment_credentials.py` 只解析 `DESKPILOT_CREDENTIAL_*` 环境变量并返回 `SecretStr`。
- `infrastructure/windows_credentials.py` 通过 Win32 Generic Credential 保存 DeskPilot 专用 target，支持读取、写入、幂等删除和临时缓冲区清零。
- `infrastructure/credential_resolvers.py` 按 reference backend 显式分发，禁止跨凭据存储 fallback。
- `model_providers/factory.py` 从静态 allowlist 同时构造和注册 Fake、本地 Ollama-compatible 与可选云端 Provider。
- `application/processor.py` 分别调用 `intent` 和 `planner` 角色，并持久化 `model.started/model.usage/model.failed`。

默认 `local_preferred` 不会在未批准时静默使用云 Provider。模型给出的工具名只是候选，仍需通过应用 allowlist 和 Runner 授权。角色调度策略由 `DESKPILOT_MODEL_GATEWAY_POLICY` 单行 JSON 加载；默认最多 2 次尝试、累计等待 2 秒且不设置费用上限。Gateway 主干见 [`doc/16-Model-Gateway与Fake-Provider实现.md`](../doc/16-Model-Gateway与Fake-Provider实现.md)，HTTP adapter 见 [`doc/17-OpenAI-Compatible-Chat-Provider实现.md`](../doc/17-OpenAI-Compatible-Chat-Provider实现.md)，配置与凭据边界见 [`doc/18-Provider配置与凭据引用实现.md`](../doc/18-Provider配置与凭据引用实现.md)，本阶段调度实现见 [`doc/25-角色级Provider路由与韧性预算实现.md`](../doc/25-角色级Provider路由与韧性预算实现.md)。

## Provider 只读管理 API

```text
GET /api/v1/model-providers
GET /api/v1/model-providers/routing
GET /api/v1/model-providers/{provider_id}/health
```

三个接口都要求本地 Bearer session。Catalog 只读取无密钥 descriptor、enabled/default 和有效缓存；routing 只投影角色策略、预算、EWMA、费用、重试和熔断运行态；两者都不触发探测。单 Provider health 才按需访问 adapter，并使用默认 15 秒 TTL、同 Provider single-flight、全局 4 并发和 5 秒统一超时。公共结果不包含 endpoint、credential reference、任务 ID 或上游错误 `detail`。

公开 catalog 通过 `0003_provider_catalog` 持久化。`0004` 增加 DPAPI 密文运行配置，`0005` 增加管理幂等回执。首次启动以 Settings seed；之后数据库成为 Provider 真值，endpoint、credential identifier 和密钥均不以明文进入 SQLite。实现细节见 [`doc/20-Provider-Catalog持久化与启动导入实现.md`](../doc/20-Provider-Catalog持久化与启动导入实现.md)、[`doc/22-Provider运行配置保护与审计模型实现.md`](../doc/22-Provider运行配置保护与审计模型实现.md)和[`doc/23-Provider管理服务与写API实现.md`](../doc/23-Provider管理服务与写API实现.md)。

## Windows Credential Manager

```powershell
.\.venv\Scripts\python.exe -m deskpilot.credential_cli store CLOUD_CHAT
.\.venv\Scripts\python.exe -m deskpilot.credential_cli status CLOUD_CHAT
.\.venv\Scripts\python.exe -m deskpilot.credential_cli delete CLOUD_CHAT --yes
```

store 使用两次隐藏输入，不接受命令行 secret；status 不显示密钥；delete 需要显式 `--yes`。Provider 配置使用 `{"backend":"windows_credential_manager","identifier":"CLOUD_CHAT"}` 引用，内部 target 固定在 `DeskPilot/ModelProvider/` namespace。详细边界见 [`doc/21-Windows-Credential-Manager实现.md`](../doc/21-Windows-Credential-Manager实现.md)。

## Provider 运行配置保护与审计

`0004_provider_runtime_config` 新增 DPAPI 密文运行配置和仅追加审计表。完整 `ProviderConfig` 在进入 SQLite 前使用当前 Windows 用户范围的 DPAPI 保护；API Key 仍只保存在 environment 或 Credential Manager。仓储支持单 Provider revision、条件更新、相同内容幂等和脱敏审计，删除 Provider 时默认保留凭据。

数据库运行配置已经接入 adapter 启动真值。完整管理 API、并发、幂等、审计和动态 Gateway 细节见 [`doc/23-Provider管理服务与写API实现.md`](../doc/23-Provider管理服务与写API实现.md)。

## Provider 管理 API

```text
GET    /api/v1/model-providers/audit
POST   /api/v1/model-providers
PUT    /api/v1/model-providers/{provider_id}
POST   /api/v1/model-providers/{provider_id}:enable
POST   /api/v1/model-providers/{provider_id}:disable
POST   /api/v1/model-providers/{provider_id}:make-default
DELETE /api/v1/model-providers/{provider_id}
```

GET Catalog 返回形如 `"provider-catalog-v3"` 的 ETag。所有写请求必须携带该值作为 `If-Match`，并提供 16～128 位高熵 `Idempotency-Key`。成功响应返回新 ETag；过期版本返回 412，相同 key 的成功重试返回原结果且 `replayed=true`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

当前全量结果为 `282 passed`；Ruff 和 105 个源码文件的 mypy 检查均通过。Alembic 当前为 `0012_task_runtime_checkpoints (head)` 且 `alembic check` 无漂移；前端为 15 个文件、122 项测试通过，type-check/build 通过。
