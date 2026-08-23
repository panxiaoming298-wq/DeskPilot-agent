# 阶段 55：PostgreSQL JSON Plan 版本化基线

## 1. 本次范围

阶段 55 先把阶段 54 已实际运行的 1000-node ready v5 keyset
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 从一次性现场断言升级为可生成、可审阅、可比较的版本化基线；随后增加 API 进程在 node claim 已提交、Runner 尚未派发边界被强杀的真库门禁，以及 Runner 在 `file.move` prepare/commit 持久化边界被强杀的三态恢复门禁。

PostgreSQL 容器 restart 与 graph/node/Outbox 三域 TTL/fence 接管现已补齐；deadlock、`statement_timeout` 与 terminal commit 连接中断也已在后续阶段 56 完成。真实外部 broker 响应丢失、重投、Inbox 去重和 DLQ requeue 继续作为下一故障注入范围。

## 2. 版本化产物

PostgreSQL 17 的首份真库基线保存在：

```text
backend/tests/baselines/postgresql/ready-v5-keyset-1000-nodes.postgresql-17.json
```

基线 schema 当前为 v1，保存：

- 完整原始 JSON plan；
- 参数化 SQL shape 的 SHA-256，不把 graph ID 或数据库时间作为查询身份；
- workload：1000 个根节点、page size 100、1 个 sentinel、`after_ordinal=898`、期望 101 行；
- `version()`、`server_version`、`server_version_num` 与捕获时间；
- planning/execution time、root rows、累计 scan rows、filter/recheck 丢弃行数；
- shared buffer hit/read/total；
- node type 计数、每个 scan node、relation 与 index name；
- 写入基线自身的比较阈值。

`scan_actual_rows` 是各 scan plan node 按 `Actual Rows × Actual Loops` 的累计值，不等价于去重后的业务行数。当前计划通过 ready ordinal 索引取得 101 行，再以 node 主键执行 101 次单行 lookup，因此累计为 202。

## 3. 自动生成与比较

门禁仍只接受共享 fail-closed guard 允许的可抛弃测试库：精确使用 `postgresql+asyncpg`、数据库名以 `_`/`-` 分隔包含 `test`，并显式设置二次确认。

有意更新基线时：

```powershell
$env:DESKPILOT_TEST_POSTGRESQL_PLAN_BASELINE_MODE = "record"
.\.venv\Scripts\python.exe -m pytest tests/test_postgresql_runtime_integration.py::test_large_ready_keyset_dual_engine_claim_and_connection_drop_recovery -vv
Remove-Item Env:\DESKPILOT_TEST_POSTGRESQL_PLAN_BASELINE_MODE
```

`DESKPILOT_TEST_POSTGRESQL_URL` 与 `DESKPILOT_TEST_POSTGRESQL_ALLOW=1` 仍须按阶段 54 的方式另外注入。`record` 只应在已确认查询或 PostgreSQL 版本变化后人工执行，生成后必须审阅 JSON diff。

默认未设置 mode 时就是 `compare`：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_postgresql_runtime_integration.py::test_large_ready_keyset_dual_engine_claim_and_connection_drop_recovery -vv
```

比较规则：

1. schema、baseline ID、workload、参数化 SQL shape 与 PostgreSQL major 必须一致；major 升级应保存独立基线，不能静默覆盖。
2. root rows、node type、scan nodes、relation/index name 严格一致；索引退化或 planner 形态变化直接失败。
3. scan rows 最多为基线的 1.05 倍。
4. execution time 上限取“基线 5 倍”和“基线 + 5 ms”的较大值，兼顾极短查询的调度抖动。
5. shared hit/read 分别完整记录；比较使用 hit+read 总量，上限取“基线 1.5 倍”和“基线 + 32 blocks”的较大值，避免冷/热缓存互换产生假失败。

比较失败会列出具体回归项；不会自动重写基线。

## 4. PostgreSQL 17.10 首份实测

```text
root rows:          101
scan actual rows:   202
shared hit/read:    311 / 0
planning time:      0.335 ms
execution time:     0.292 ms
indexes:
  uq_effect_dag_ready_nodes_ordinal
  tool_effect_nodes_pkey
plan nodes:
  Limit -> Nested Loop -> Index Scan + Index Scan
```

生成后使用一次全新 1000-node graph 立即以默认 compare 重跑，基线比较通过。单元测试另覆盖指标提取、SQL shape 规范化、阈值内缓存波动、结构/时延/buffer 回归报告、原子 JSON 写入与畸形 plan 拒绝。

加入 API process-kill 门禁后的完整验证结果：Ruff 全仓通过，mypy 133 个源码文件通过，PostgreSQL 专项 3 项通过；启用专用 PostgreSQL 的后端 `376 passed, 1 warning in 532.81s` 且无 skip；工作区 Node 24.19.0 下前端 17 文件/133 项测试、type-check 与 production build 通过。Runner kill 门禁加入后的最终全量结果见第 6 节。

## 5. API claim-after-commit 进程强杀

可重复故障注入入口：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_postgresql_process_fault_injection.py -vv
```

`tests.fault_injection.api_claim_after_commit` 是最小 claimant API worker：它使用与控制面相同的 `Database + TaskService`，依次提交 graph lease、ready v5 proof 和单 node claim；`claim_effect_dag_nodes` 返回后才向父测试输出一条无凭据 JSON checkpoint，随后无限等待且不构造 Runner、Tool call 或外部副作用。父测试只有收到并核对 task/graph/owner/fence/PID 后才强杀实际执行进程，因此故障点稳定处于：

```text
graph/node claim committed -> process killed -> Runner not dispatched
```

Windows 虚拟环境 `python.exe` 可能是启动器，其 asyncio process handle PID 与实际解释器 PID 不同。checkpoint 同时报告实际 PID 和 parent PID；门禁核对父子关系后终止实际 PID，并确认实际进程与启动器都已退出，避免只杀 launcher 留下 claimant。

PostgreSQL 17.10 实测逐项证明：

1. 强杀后数据库可见且仅可见一次 `effect.node.claimed`；graph fence=1、node fence=1、node=`active`，owner 精确等于被杀进程身份。
2. Tool ledger 行数为 0 且没有 `tool.requested`，证明 claim-before-runner 边界没有越过。
3. TTL 未到时第二 API 不能获取 graph lease；等待条件完全使用 PostgreSQL `current_timestamp`，不依赖应用时钟或直接改写 expiry。
4. graph/node TTL 到期后，新 API 获取 graph fence=2，新 node owner reclaim 得到 node fence=2，且只有一次 `effect.node.reclaimed`。
5. 旧 graph owner+fence 写入命中 `EffectGraphFenceRejectedError`；携带当前 graph fence 但使用旧 node owner+fence 时命中 `EffectNodeFenceRejectedError`。
6. 两次拒绝均整事务回滚，graph/node revision 保持在 takeover/reclaim 后的精确值；没有幽灵 claim、幽灵 fence 或部分 transition。
7. 当前 owner/fence 最终可以把 recovered node 提交为 `succeeded`，证明接管不只是可见而是可继续推进。

任务清理仍由测试父进程按精确 `task_id` 删除，连接 URL 只通过现有 fail-closed 环境保护继承，不进入命令行、checkpoint 或日志。

## 6. Runner prepare/commit 边界进程强杀

可重复故障注入入口：

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_runner_commit_boundary_fault_injection.py -vv
```

`FileMoveCommitProvider` 现在可选接收一个边界 observer；生产组合仍构造无 observer 的单例，不读取故障环境变量。只有测试 Runner `tests.fault_injection.runner_commit_boundary` 注册观察器，并在指定的持久化边界原子写入无路径、无凭据 checkpoint 后阻塞。父测试核对 call/receipt/state 和实际 PID 后才发送 `SIGTERM`；Windows 同样核对 venv launcher 与真实解释器的父子关系。

| 强杀点 | kill 前可见证据 | 重启恢复 | 外部效果 |
| --- | --- | --- | --- |
| `prepared` | journal=`prepared` | `no_effect`，receipt 为空 | source 原位，destination 不存在 |
| `external_effect_applied` | journal=`committing`，OS move 已完成 | 根据 destination 版本恢复 `committed` receipt | source 不存在，destination 与批准版本一致 |
| `committing` 后外部状态变为不可证 | journal=`committing`，move 尚未发生 | `unknown`，receipt 为空 | 不猜测、不重放 |

三条门禁都使用真实独立 Runner 和真实每调用 worker，不是在单进程内手工改 journal。重启后只调用签名 `get_commit_receipt`，连续查询两次并核对文件状态，没有再派发 `call_tool`。已移动场景依据 prepare 版本生成并持久化 recovered receipt；仅 prepared 场景不会把后续外部变化归因于该 attempt。

不可证场景还走完真实 `TaskService` Policy/一次性审批/Tool ledger：Runner 退出后原 running call 原子转为 `unknown`，任务转为 `waiting_reconciliation`，且恰好产生一条 pending reconciliation。随后执行 startup recovery 不再改写该终态，事件始终只有一次 `tool.requested`/`tool.started`/`tool.unknown`，没有 `tool.completed`。

专项验证为 `3 passed`；与既有 file.move 和 Tool ledger 契约合并回归为 26 项全通过，Ruff 全仓与 mypy 133 个源码文件通过。

最终使用 Docker PostgreSQL 17.10 显式启用全部真库门禁，后端完整 `380 passed`、1 条既有第三方弃用警告、无 skip；Alembic 仍为 `0022_effect_runtime_ops (head)` 且 `check` 无新操作。全量还暴露两个 Windows 测试 harness 竞态：慢 Runner 在第 4 秒仍正常 `running`，以及 SQLite 短暂持有 rollback journal 时直接读文件被拒绝。前者统一为 5 秒有界等待，后者只对瞬态 `PermissionError` 做 2 秒有界重试；持续锁定仍失败，明文 key 不落库断言未放宽。两个用例各连续三次专项通过后，完整回归通过。前端继续为 17 文件/133 项测试、type-check 和 production build 全通过。

## 7. PostgreSQL 容器 restart 与三域 fence 接管

可重复真库入口：

```powershell
$env:DESKPILOT_TEST_POSTGRESQL_RESTART_ALLOW = "1"
$env:DESKPILOT_TEST_DOCKER_CLI = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
.\.venv\Scripts\python.exe -m pytest tests/test_postgresql_container_restart_fault_injection.py -vv
```

它在共享可抛弃测试库 guard 之上再要求独立 restart 确认，且只能命中 `deskpilot-postgres`。测试在执行 `docker restart` 前精确验证 `deskpilot-storage/postgres` Compose 标签、URL 的 loopback host port、healthy 状态和 `deskpilot_postgres_data` 数据卷；连接 URL 只通过环境传入，不进入 Docker 命令、checkpoint 或日志。

重启前同一任务已提交 graph lease、active node claim 和 Outbox delivery claim，三者 fence 均为 1，并保持一条真实 backend 连接跨越 restart。PostgreSQL 17.10 实测证明：

1. restart 后旧 backend 连接必须抛出 invalidated DBAPI disconnect，新连接观测的 `pg_postmaster_start_time()` 必须递增；
2. SQL 恢复可用与 Docker healthcheck 分别有界等待，最终仍必须回到 healthy；
3. 不直接改写 expiry，只用恢复后 PostgreSQL `current_timestamp` 同时判定 graph/node/Outbox TTL 到期；
4. 新 API 接管 graph fence=2，新 node owner reclaim 得到 node fence=2，新 publisher 接管同一 message 并得到 publisher fence=2；
5. 旧 graph owner+fence 命中 `EffectGraphFenceRejectedError`；当前 graph fence 配旧 node owner+fence 命中 `EffectNodeFenceRejectedError`；旧 publisher owner+fence+delivery 的 ack 返回 false；
6. 三次旧写入都不改变 graph/node revision 或 Outbox 当前 claim identity，Tool ledger 仍为 0；
7. 当前 publisher 能 ack，当前 graph/node fence 能把 recovered node 提交为 `succeeded`，事件仍只有一次 claim 和一次 reclaim。

首次真库运行暴露 Docker healthcheck 比 SQL 恢复稍慢的正常时序：PostgreSQL 已接受查询时容器仍可短暂为 `starting`。门禁因此分开等待这两个事实，不放宽最终 healthy 断言。修正 harness 后专项实测 `1 passed`，全部 PostgreSQL marker 为 `4 passed, 376 deselected`。

## 8. 安全与后续入口

数据库门禁只执行查询计划采样和可抛弃数据的 claim/recovery。Runner kill 门禁只在 pytest 的 `tmp_path` 内移动显式创建的测试文件，故障注入 observer 不进入生产组合。基线文件不保存数据库 URL、用户名或密码；原始 plan 只包含本次可抛弃 workload 的随机 graph ID 和冻结数据库时间。

阶段 55 后续按以下顺序继续：

1. 接入真实外部 broker，演练 at-least-once 重投、响应丢失、Inbox 去重与 DLQ requeue。

所有故障点继续保持 claim-before-runner、prepare/commit/unknown 和 receipt 是已提交效果唯一恢复依据的既有语义。

## 9. 阶段 110 Checkpoint 的重复运行修正

阶段 77～110 汇总门禁在复用同一 `deskpilot_test` 数据库时发现两类测试环境漂移，冻结 baseline、查询和阈值均未改写：

1. ready-v6 原始 plan 记录于空/旧统计状态，planner 估算 1 行而实际返回 101 行；对单一 1000-node graph 显式 `ANALYZE` 后，`graph_id` 没有选择性，PostgreSQL 合理选择 Seq Scan，不能把它误报成索引回归。
2. 16000-control 等随机主键 workload 虽在 finally 中 `DELETE`，重复运行仍保留 B-tree 物理膨胀，buffer 计数会逐次偏离“fresh database”基线。

门禁现在先在共享 fail-closed guard 已确认的可抛弃测试库内 `TRUNCATE tasks CASCADE`，同时重置逻辑数据与 task 子表索引的物理 footprint。ready-v6 另显式建立 16 个 graph 的 planner context、固定统计采样并执行 `ANALYZE`，使目标 `graph_id` 约占 1/16；随后分三层验证：

- `pg_catalog` 精确断言 ordinal、membership 与 node primary-key 三个索引的列顺序、unique、valid、ready 与 live 状态；
- 默认 PostgreSQL planner 必须返回 101 行、累计扫描不超过 303 行、使用预期索引且不得出现 Seq Scan；
- 只为重放不可变历史 shape，在同一事务关闭 Bitmap scan，再使用原 comparator 严格比较旧的双 Index Scan baseline。

这不会通过关闭 Seq Scan 隐藏默认优化器退化：真实默认 plan 在前一层单独执行并受有界扫描断言保护；索引缺失或失效也会先被 catalog gate 拒绝。长期如需把 analyzed 默认 Bitmap plan 本身版本化，应追加不同名称的新 baseline，不得覆盖现有 PG17 文件。

修正后两份计划专项连续通过，完整 PostgreSQL marker 为 `11 passed + 604 deselected`；固定容器 restart 同轮通过，原本停止的 `deskpilot-postgres` 在门禁后恢复为停止。数据库 URL 与凭据不进入 JUnit、文档或提交。
