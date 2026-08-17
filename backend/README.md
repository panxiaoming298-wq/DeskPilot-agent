# DeskPilot Backend

FastAPI 控制面最小骨架。当前 TaskProcessor 的磁盘容量任务通过 Model Gateway 的离线 Fake Provider 获得结构化分类和计划；显式单文件移动、线性 saga 与受信 `file_move_dag` 使用应用计划模板和本地用户提供的结构化路径。写路径不从模型文本提取，每个 v2 DAG 节点都独立经过 Tool ledger、Policy/Approval、graph+node fence、签名 Runner 和 commit receipt，并支持受持久化 plan 约束的逐 wave 并行补偿。DAG 采用进程级公平 admission、有界 ready proof 页和持久化跨 API graph control mailbox；任一 API 收到取消后都能将命令路由给 live graph owner，由其对当前 node fence 的在途 Runner call 发出 generation-bound cancel IPC。Runner 已支持自动换代、退避/熔断、持久化调用账本、`unknown` 人工对账/签名回执证据与受限显式新 attempt、Windows 每调用 Job/Low Integrity、句柄核验 resource broker、内容寻址 AppContainer worker bundle、专用 capability ACL 和孤儿 profile reaper，以及 R1 `file.move@1.0.0` prepare/commit/receipt 受控写闭环。任务历史 API、Provider catalog、安全凭据、角色路由与模型韧性预算也已完成。默认首次 seed 仍只使用离线 Fake Provider。

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

请求体可省略，也可以传 `{"reason":"..."}`。暂停只允许从 `running` 进入，在已提交事件之间的安全点生效；恢复从当前进程或受保护且可验证的跨 API 重启 checkpoint 继续，不重复已经持久化的 Tool 事件。取消可用于所有非终态任务，并以 `task.cancelled` 结束事件流。v2 DAG 即使由另一个 API 实例持有，也会先持久化内容寻址 control message，再按当前 graph owner/fence 路由；若在配置的等待时间内尚未收到 fenced ack，API 返回 `503 EFFECT_GRAPH_CONTROL_PENDING`，命令仍保留在数据库中继续投递。

重复暂停已暂停任务、重复取消已取消任务是幂等操作，不会增加事件序号。非法转换返回 `409 TASK_TRANSITION_NOT_ALLOWED`；没有能与事件、Tool 账本、Policy、审批和 effect graph 当前节点同时证明一致的 checkpoint 时，恢复返回 `409 TASK_RUNTIME_UNAVAILABLE`。

任务历史使用 `GET /api/v1/tasks?status=&limit=&offset=` 查询。`limit` 限定为 1～100，结果按创建时间稳定倒序并返回 `items/total/limit/offset`，响应禁止缓存且不包含事件 payload 或 Tool 参数。多步执行的脱敏证明投影使用 `GET /api/v1/tasks/{task_id}/effect-graph` 查询。

应用启动时会自动执行 Alembic upgrade。也可以手动检查或执行迁移：

```powershell
.\.venv\Scripts\alembic.exe current
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe check
```

第一版 migration 可初始化空库，也能接管早期数据库而不重建原有任务。当前 head 为 `0030_task_contract_plans`；在既有运行时、知识库、MCP 与 Evaluation Run/Trace 之外，新增不可变 Task Contract 版本、Executable Plan generation 和活动规划指针。

## Task Contract 与 Executable Plan 只读 API

```text
GET /api/v1/capabilities
GET /api/v1/tasks/{task_id}/planning
GET /api/v1/tasks/{task_id}/contract
GET /api/v1/tasks/{task_id}/contracts
GET /api/v1/tasks/{task_id}/plans
GET /api/v1/tasks/{task_id}/plans/{generation}
```

Plan Compiler 只绑定冻结 Registry/Catalog 中的精确版本和摘要；持久化读取会复核 manifest、摘要和绑定漂移。阶段 69 的 `1.0.0` 声明保持不变；阶段 70 以 `research.read.v1@1.1.0` 显式开关启用研究，阶段 71 以 `artifact.html.v1@1.1.0` 和 `browser.verify.v1@1.1.0` 启用受控本地交付能力。

## 事件可靠投递

任务状态、`task_events` 和 `outbox_messages` 在同一事务提交。后台 `OutboxPublisher` 为每次尝试生成 delivery ID，按指数退避重试，达到 `DESKPILOT_OUTBOX_MAX_ATTEMPTS` 后进入 DLQ；`InboxConsumer` 以 consumer + logical message 去重，并提供显式 requeue/retention cleanup 原语。

投递语义是 **at-least-once**：进程若在发送成功、写入 `published_at` 之前退出，消息可能再次发送。WebSocket 端以任务内单调 `seq` 去重，事件补拉仍以数据库 `task_events` 为真值。

## Effect runtime 运维 API

```text
GET  /api/v1/operations/effect-runtime
GET  /api/v1/operations/effect-runtime/audit
GET  /api/v1/operations/effect-runtime/alerts
GET  /api/v1/operations/effect-runtime/audit/export
POST /api/v1/operations/effect-runtime:sample
POST /api/v1/operations/effect-runtime:run-retention
POST /api/v1/operations/outbox/{message_id}:requeue
```

全部接口都要求本地 Bearer session，写请求还要求可信 Origin/Fetch Metadata；retention 和 DLQ requeue 另要求 `Idempotency-Key`。snapshot 只返回身份、状态、revision/fence、时间和摘要，不返回 control reason、Outbox payload/error 原文或 Tool 参数。自动 retention 只清理安全终态图的派生运维数据、已发布 Outbox 与旧 Inbox receipt；DLQ、active/compensating/blocked graph 和 TaskEvent 永不自动删除。完整边界见 [`doc/50-受保护运行时运维面与Retention审计.md`](../doc/50-受保护运行时运维面与Retention审计.md)。

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

受保护 checkpoint 的完整存储、证明与不重放边界见 [`doc/38-结构化Tool请求与可证明跨重启检查点.md`](../doc/38-结构化Tool请求与可证明跨重启检查点.md)，多步图、原子节点转换与 saga 补偿见 [`doc/39-版本化Tool-effect-graph与Saga补偿.md`](../doc/39-版本化Tool-effect-graph与Saga补偿.md)，跨实例 graph 所有权、统一事务命令和图级恢复见 [`doc/40-跨实例Graph所有权与图级Reconciliation恢复.md`](../doc/40-跨实例Graph所有权与图级Reconciliation恢复.md)，数据库 claim、Outbox fencing 与 v2 DAG 并行恢复见 [`doc/41-数据库原子Claim与DAG并行恢复证明.md`](../doc/41-数据库原子Claim与DAG并行恢复证明.md)，dispatcher/reducer 与可靠投递见 [`doc/42-DAG并行Dispatcher与可靠消息投递.md`](../doc/42-DAG并行Dispatcher与可靠消息投递.md)，v2 逐节点账本与并行补偿见 [`doc/43-v2可信Tool账本与并行补偿执行.md`](../doc/43-v2可信Tool账本与并行补偿执行.md)，条件边、分支决策内容证明与重启恢复见 [`doc/44-条件边与内容寻址分支决策证明.md`](../doc/44-条件边与内容寻址分支决策证明.md)，在途取消与 graph/node fence 见 [`doc/45-在途Runner取消与Fence语义.md`](../doc/45-在途Runner取消与Fence语义.md)，进程内公平 admission 与 ready proof 分页见 [`doc/46-DAG公平调度分页与Backpressure.md`](../doc/46-DAG公平调度分页与Backpressure.md)，跨 API 持久化取消路由见 [`doc/47-跨实例Graph取消控制邮箱.md`](../doc/47-跨实例Graph取消控制邮箱.md)，数据库协调的集群级 admission 与 node-claim 容量 fence 见 [`doc/48-集群级DAG-Admission与容量Fence.md`](../doc/48-集群级DAG-Admission与容量Fence.md)，数据库增量 ready 索引和 v4 页证明见 [`doc/49-增量Ready投影与v4分页证明.md`](../doc/49-增量Ready投影与v4分页证明.md)，受保护运维面、retention 和审计链见 [`doc/50-受保护运行时运维面与Retention审计.md`](../doc/50-受保护运行时运维面与Retention审计.md)，ready v5 keyset 与 PostgreSQL 验收门禁见 [`doc/51-Ready-v5-Keyset与PostgreSQL验收门禁.md`](../doc/51-Ready-v5-Keyset与PostgreSQL验收门禁.md)，backend terminate、锁超时与多主幂等门禁见 [`doc/52-PostgreSQL连接终止与多主幂等门禁.md`](../doc/52-PostgreSQL连接终止与多主幂等门禁.md)，对应的前端受保护运维台见 [`doc/53-前端受保护运行时运维台.md`](../doc/53-前端受保护运行时运维台.md)，Docker PostgreSQL 17.10 真库验收与兼容修复见 [`doc/54-Docker-PostgreSQL真库验收与兼容修复.md`](../doc/54-Docker-PostgreSQL真库验收与兼容修复.md)。

最新 ready membership/count、TTL 接管、漂移门禁与 PostgreSQL v6 JSON plan 见 [`doc/58-Ready-membership-count投影与漂移门禁.md`](../doc/58-Ready-membership-count投影与漂移门禁.md)；admission 分片、跨 shard 容量证明和 16000-ticket plan 见 [`doc/59-Admission分片与PostgreSQL原生调度.md`](../doc/59-Admission分片与PostgreSQL原生调度.md)；graph-control 原生批量领取、TTL/fence 接管和 16000-control plan 见 [`doc/60-Graph-control-PostgreSQL原生批量Claim.md`](../doc/60-Graph-control-PostgreSQL原生批量Claim.md)；告警生命周期通知与冻结 audit 导出见 [`doc/61-运行时告警通知与Audit冻结导出.md`](../doc/61-运行时告警通知与Audit冻结导出.md)。

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

## Agent Contract 与只读 Registry

应用启动会严格加载四个固定 Prompt Package，并额外注册只读 `builtin.web_researcher`；所有 Agent 都会交叉校验 I/O Schema、Prompt/Tool digest、Model capability 与无环 Handoff 声明后冻结 Registry。Supervisor 不属于 Agent，Contract 也不代替 Policy/Approval/Runner 授权。

```text
GET /api/v1/agents
GET /api/v1/agents/registry-snapshot
GET /api/v1/agents/{agent_id}/versions/{version}
```

接口只返回脱敏 Descriptor/Schema digest，不返回 Prompt 正文或本地根路径。阶段 70 的研究结果仍严格停在待验证状态；阶段 71 已新增独立 Claim/Citation Verification、ArtifactRevision/PatchReceipt、隔离 Browser evidence 和 DeliveryManifest。阶段 72 为实际 Model Turn 增加 ContextManifest 与短期 Working Memory；阶段 73 新增受保护长期 MemoryProposal/MemoryItem、冲突/TTL/遗忘和 Agent/Provider usage ledger，详见 [`doc/73-长期记忆确认冲突与遗忘.md`](../doc/73-长期记忆确认冲突与遗忘.md)。

阶段 68 验证：专项 8 项全通过；默认后端全量 `429 passed, 12 skipped, 1 warning`（441 collected）；Ruff、mypy 176 个生产源码、Alembic check、依赖锁、wheel Prompt 资源和 evaluation baseline compare 全部通过。

阶段 70 验证：Agent Research Runtime、SSRF/fencing/redirect-DNS、Registry、Plan Compiler 与 `0031` migration 专项 25 项通过；默认后端全量 `445 passed, 12 skipped, 1 warning`，随后新增的两项安全用例随阶段专项复跑通过。Ruff、mypy 189 个源码、Alembic check、依赖锁、Workflow YAML 和冻结 evaluation baseline compare 全部通过。

阶段 71 验证：后端全量 `451 passed, 12 skipped, 1 warning`（真实 Browser 烟测加入前），新增阶段 71 专项 4 项全通过，其中包含真实本机 Edge 隔离 profile 渲染/截图烟测。Ruff、mypy 193 个源码、`0032` migration 往返/metadata check、Workflow YAML 和冻结 evaluation baseline compare 通过；前端 20 文件/139 项测试、type-check 和 build 通过。

阶段 72 验证：后端全量 `455 passed, 12 skipped, 1 warning`（467 collected）；Conversation/Working Memory/ContextManifest 对抗测试和 `0033` migration 往返通过，恶意网页快照只以 `untrusted_external_content` 进入研究上下文，不进入 retained memory。Ruff、mypy 195 个生产源码、Workflow YAML、冻结 evaluation baseline compare 以及前端 20 文件/139 项测试、type-check、build 全部通过。

阶段 73 验证：后端全量 `458 passed, 12 skipped, 1 warning`；长期记忆确认/冲突/过期/删除/密文/Context usage 对抗测试、verified delivery 回归和 `0034` migration 往返通过。最终加入真实 ModelRequest 内容绑定和存储 digest 重验后，研究/Context/verified-delivery 相关 17 项再次通过。Ruff、mypy 199 个生产源码以及前端 21 文件/141 项测试、type-check、build 全部通过。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

阶段 67 的脱敏 telemetry 查询为 `GET /api/v1/telemetry/traces`（真实 OTel `trace_id` 或现有 `task_correlation_id` 二选一）和 `GET /api/v1/telemetry/metrics`。本地 store 有界、进程级、`no-store`，不参与领域恢复或 Evaluation 证明。黄金回归只读门禁运行：

```powershell
.\.venv\Scripts\python.exe -m deskpilot.evaluation_gate compare
```

基线位于 `tests/baselines/evaluations/`；CI 禁止 `record` 且检查 baseline diff。完整脱敏、阈值和显式新版本 record 流程见 [`doc/67-脱敏OpenTelemetry与回归基线CI门禁.md`](../doc/67-脱敏OpenTelemetry与回归基线CI门禁.md)。

阶段 61 将 operations 稳定告警持久化为 opened/updated/resolved hash-chain 通知，并以冻结数据库 head、opaque cursor、after/through digest 和 page digest 提供完整脱敏 audit 导出；PostgreSQL 双 engine 唯一 opened 与并发 append 冻结门禁已实际通过。RabbitMQ 仍为可选 transport，默认 local 模式不联网，也不参与告警或导出正确性。默认后端全量为 `396 passed, 12 skipped, 1 warning`（408 collected）；Ruff、mypy 140 个生产源码、依赖锁和 Alembic check 通过，Alembic head 为 `0026_alert_notifications`。前端为 17 文件/134 项测试，type-check 与 build 通过。

默认 `DESKPILOT_EVENT_TRANSPORT=local`，不会连接外部 broker。启用 RabbitMQ 时先复制 `infrastructure/rabbitmq/.env.example`，启动专用容器，并配置 `DESKPILOT_EVENT_TRANSPORT=rabbitmq` 与 SecretStr `DESKPILOT_RABBITMQ_URL`。真实门禁还要求 loopback/test-vhost/二次确认：

```powershell
$env:DESKPILOT_TEST_RABBITMQ_URL = "amqp://user:password@127.0.0.1:5672/deskpilot_test"
$env:DESKPILOT_TEST_RABBITMQ_ALLOW = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_rabbitmq_fault_injection.py -vv
```

详细语义和未覆盖边界见 [`doc/57-RabbitMQ真实Broker重投与Inbox门禁.md`](../doc/57-RabbitMQ真实Broker重投与Inbox门禁.md)。

PostgreSQL 门禁只应指向可抛弃测试库；它要求 `postgresql+asyncpg` URL、库名包含 `test` 和二次确认：

```powershell
$env:DESKPILOT_TEST_POSTGRESQL_URL = "postgresql+asyncpg://user:password@127.0.0.1:5432/deskpilot_test"
$env:DESKPILOT_TEST_POSTGRESQL_ALLOW = "1"
.\.venv\Scripts\python.exe -m pytest -m postgresql_integration -vv
```

它会迁移该库并运行 1000-node keyset `EXPLAIN ANALYZE/BUFFERS`、双独立 engine claim 竞争、engine-pool drop 后 TTL/fence 接管、未提交 claim 事务的 backend terminate 回滚、graph `lock_timeout`/SQLSTATE `55P03` 和双实例同键审计幂等验证。以上门禁已在 Docker PostgreSQL 17.10 实际通过；详细安全边界与真库结果见 [`doc/51-Ready-v5-Keyset与PostgreSQL验收门禁.md`](../doc/51-Ready-v5-Keyset与PostgreSQL验收门禁.md)、[`doc/52-PostgreSQL连接终止与多主幂等门禁.md`](../doc/52-PostgreSQL连接终止与多主幂等门禁.md)和[`doc/54-Docker-PostgreSQL真库验收与兼容修复.md`](../doc/54-Docker-PostgreSQL真库验收与兼容修复.md)。

1000-node 用例默认还会把当前 JSON plan 与 `tests/baselines/postgresql/` 下的 PostgreSQL 17 版本化基线比较；只在有意更新并准备审阅 diff 时，临时设置 `DESKPILOT_TEST_POSTGRESQL_PLAN_BASELINE_MODE=record` 重新生成。指标、阈值和安全流程见 [`doc/55-PostgreSQL-JSON-Plan版本化基线.md`](../doc/55-PostgreSQL-JSON-Plan版本化基线.md)。

同一专用 PostgreSQL marker 还包含 `test_postgresql_process_fault_injection.py`：最小 API claimant 子进程在 graph/node claim commit 后、Runner 派发前报告 checkpoint，父测试强杀实际解释器 PID，随后只使用数据库时间等待 TTL，并断言 graph/node fence 从 1 精确接管到 2、旧 owner/旧 node fence 全拒绝且 Tool ledger 始终为零。

`test_postgresql_container_restart_fault_injection.py` 会实际重启专用 PostgreSQL 容器，因此还需额外显式选入。它只接受固定的 `deskpilot-postgres`，并在重启前核对 Compose project/service、loopback 端口、健康状态和专用数据卷；Docker CLI 不在 PATH 时必须给出其绝对路径：

```powershell
$env:DESKPILOT_TEST_POSTGRESQL_RESTART_ALLOW = "1"
$env:DESKPILOT_TEST_DOCKER_CLI = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
.\.venv\Scripts\python.exe -m pytest tests/test_postgresql_container_restart_fault_injection.py -vv
```

该门禁证明 restart 使旧 backend 连接失效且 postmaster 启动时间递增；恢复后只依据 PostgreSQL `current_timestamp` 等待 graph/node/Outbox TTL，三个 fence 都从 1 接管为 2。旧 graph owner、旧 node owner/fence 和旧 publisher owner/fence/delivery 全部被拒绝，当前 fence 仍可 ack Outbox 并完成 recovered node。

`test_postgresql_transaction_fault_injection.py` 覆盖 `statement_timeout`、多行 deadlock 和 terminal commit 连接中断。实测精确命中 SQLSTATE `57014` 与 `40P01`，并证明 graph/node/witness 失败事务整体回滚。连接中断场景直接调用生产 `finish_tool_call()`，在 transaction commit 前终止测试自身 backend；新连接看不到部分 terminal ledger/event/Outbox，startup recovery 只生成一次 `unknown` 和 pending reconciliation，不生成新 call 或 `tool.completed`。详细证明见 [`doc/56-PostgreSQL事务超时死锁与连接中断门禁.md`](../doc/56-PostgreSQL事务超时死锁与连接中断门禁.md)。

`test_runner_commit_boundary_fault_injection.py` 另使用真实独立 Runner 和每调用 worker，在 `file.move` prepared、committing 和 OS move 已完成但 receipt 尚未写入的精确 checkpoint 强杀实际 PID。重启后只查询 durable receipt，分别断言确定 `no_effect`、已提交效果的 receipt 恢复，以及不可证状态进入单一 pending reconciliation 且不透明重放。故障 observer 只注册在测试 Runner，生产 `file.move` provider 默认无 observer。
