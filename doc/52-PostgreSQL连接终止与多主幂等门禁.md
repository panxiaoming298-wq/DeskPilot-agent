# 阶段 52：PostgreSQL 连接终止与多主幂等门禁

## 1. 本阶段目标

阶段 51 建立了 ready v5 keyset、1000-node `EXPLAIN ANALYZE/BUFFERS`、双 engine claim 竞争与 TTL/fence 接管入口，但当时的故障只是丢弃 SQLAlchemy engine pool，并不会中断正在 PostgreSQL backend 中执行的事务。本阶段继续建设三个可重复、显式选入的实库门禁：

- 在 graph revision 和 node claim/fence 已更新但事务尚未提交时，终止该 PostgreSQL backend，证明整个事务回滚；
- 用长事务持有 graph 行锁，让竞争者命中 `lock_timeout`，证明不会产生部分 graph/node claim；
- 将 retention/DLQ 的幂等读放到 audit state `FOR UPDATE` 之后，证明两个 API 实例同时首次提交相同幂等键会归一为同一持久化结果。

本阶段最初没有发现可用 PostgreSQL 服务，因此两个 PostgreSQL 门禁在当时的本地全量中按设计 skip。随后阶段 54 已使用 Docker Desktop 的专用 `deskpilot_test` 真库实际执行并通过全部门禁；实测环境、发现的兼容问题与修复见 [`54-Docker-PostgreSQL真库验收与兼容修复.md`](54-Docker-PostgreSQL真库验收与兼容修复.md)。

## 2. 实库目标的四重保护

实库配置校验收敛到 `infrastructure/postgresql_verification.py`，所有 PostgreSQL 门禁共用同一 fail-closed guard：

1. 没有 `DESKPILOT_TEST_POSTGRESQL_URL` 时只 skip，不猜测本地端口或使用应用库；
2. 必须显式设置 `DESKPILOT_TEST_POSTGRESQL_ALLOW=1`；
3. URL driver 必须精确为 `postgresql+asyncpg`；
4. database name 必须把 `test` 作为 `_` 或 `-` 分隔的独立 token，`contest` 之类偶然子串不会通过。

错误信息不回显 URL，因此不会把 password 写入 pytest 或 CI 日志。本地纯函数测试覆盖缺 URL、缺二次确认、错误 driver、不安全库名和合法目标。

## 3. PostgreSQL backend terminate 回滚证明

`test_postgresql_fault_injection.py` 用五个独立 engine 分离 control、victim、admin、blocker 和 contender 连接池。backend terminate 演练执行：

1. 创建两个并行 root 的图，取得 graph lease 和 v5 ready proof；
2. victim 事务按生产 PostgreSQL claim 顺序提升 graph revision，执行 node `FOR UPDATE SKIP LOCKED`，再执行 `UPDATE RETURNING` 将 node 置为 active 并颁发 fence 1；
3. 在 commit 之前取得 victim `pg_backend_pid()`；admin 先确认目标 pid 属于同一 `current_database/current_user`，且不是 admin 自身，才执行 `pg_terminate_backend(pid)`；
4. victim 连接必须抛出 DBAPI disconnect；新连接重读后 graph revision 不变，node 仍为 pending/revision 1/无 owner/fence 0；
5. 原 v5 proof 仍可由正常 `TaskService` 成功 claim，第一个可见 node fence 必须是 1，证明被终止事务没有泄漏“幽灵 fence”。

该演练刻意停在 claim-before-runner 边界：它不启动 Runner，不注入外部 Tool 效果，也不会把一次未知提交猜测成成功。

## 4. 长事务与锁等待超时

同一门禁继续使用第二个 root：

1. blocker 事务对目标 graph 执行 `SELECT ... FOR UPDATE` 并保持不提交；
2. contender 事务设置 `SET LOCAL lock_timeout = '250ms'`，再按生产顺序首先 CAS graph revision；
3. contender 必须以 PostgreSQL SQLSTATE `55P03` 失败，然后回滚；
4. 释放 blocker 后重读，graph revision 不变，第二个 node 仍为 pending/无 owner/fence 0；
5. 继续使用受保护 ready proof 正常 claim，首个可见 fence 仍必须为 1。

这个顺序证明 graph lease/revision 门是 node claim 之前的事务闸门；锁超时不会绕过 ready/admission/node fence，也不会产生 Runner IPC。

## 5. 多主幂等与审计锁顺序

阶段 50 的 retention 与 DLQ requeue 会先查询幂等审计事件，在追加 audit 时才锁 `effect_runtime_operations_state`。两个 PostgreSQL 实例同时首次使用同一 key 时，两者可能先后读到“不存在”，第二个最终在 audit 唯一约束处失败，而不是回放第一个结果。

现在 retention 和 DLQ requeue 统一执行：

```text
BEGIN
  SELECT operations_state FOR UPDATE
  query idempotency receipt
  if replay: return the durable result
  mutate protected records
  append audit + advance state head
COMMIT
```

`_append_audit` 也复用同一 state-lock helper，继续验证 sequence、previous digest 和 state head。门禁用两个独立 `EffectRuntimeOperationsService` 并发执行同 key retention，两个返回必须具有同一 audit event ID/digest 和 manifest digest。

该锁顺序同时使 retention 与 DLQ requeue 在跨 API 实例中先串行 audit head，再锁它们各自的业务记录，避免“副作用已做完但审计幂等冲突”。事务回滚时业务变更和 audit head 仍一起回滚。

## 6. 执行方式

必须使用可抛弃的专用测试库：

```powershell
$env:DESKPILOT_TEST_POSTGRESQL_URL = "postgresql+asyncpg://user:password@127.0.0.1:5432/deskpilot_test"
$env:DESKPILOT_TEST_POSTGRESQL_ALLOW = "1"
.\.venv\Scripts\python.exe -m pytest -m postgresql_integration -vv
```

执行账号必须能查看并终止它自身在该测试库中的其他 backend；不应为此给应用运行账号授予生产超级权限。门禁会迁移指定库、创建和删除自己的 Task 图，但 append-only operations audit 会保留，因此仍应对一次性库运行。

## 7. 本地验收结果

```text
Ruff:  All checks passed
mypy:  Success, 131 source files
pytest: 366 passed, 2 skipped in 489.10s
PostgreSQL integration: 2 skipped (no configured PostgreSQL service)
frontend vitest: 15 files, 126 passed
frontend type-check/build: passed
```

本阶段无 schema 变更，Alembic head 仍为 `0022_effect_runtime_ops`。新增的六个本地 guard 用例全部通过；两个实库用例未配置 URL 时明确 skip，没有伪造 PostgreSQL 成功证明。

## 8. 阶段 54 真库复验

Docker PostgreSQL 17.10 上的专项复验结果为 `2 passed, 367 deselected, 1 warning in 5.32s`。复验实际命中 backend terminate 回滚、SQLSTATE `55P03`、1000-node keyset plan、dual-engine 单获胜、pool drop TTL/fence 接管和 retention 多主同键幂等。期间修复了 PostgreSQL 默认 Alembic version 列过短与原生 `UPDATE RETURNING` identity-map 提前同步两项 SQLite 隐藏问题。

## 9. 已知边界与下一步

1. backend terminate 已在 Docker 真库实际执行；下一步仍需保存版本化 JSON plan/BUFFERS 与时间趋势基线。
2. `pg_terminate_backend` 演练不等价于杀死整个 API/Runner OS 进程，也不覆盖 server restart/failover、TCP blackhole、DNS 故障或网络半开连接。
3. lock timeout 覆盖 graph 闸门前竞争，尚需在真库上演练 node/admission/control/Outbox 多行锁组合、deadlock detection 和 statement timeout。
4. 真实外部 broker 仍未对接；响应丢失、重投、Inbox 去重、DLQ 人工 requeue 和旧 publisher fence 拒绝尚只有本地可靠投递抽象证明。
5. ready membership 仍执行全局 COUNT；admission 仍由全局 state CAS 串行；这两项应基于真实 PostgreSQL 基线再决定是否引入 count projection 或分片调度域。

下一阶段入口：**保存可比较的 PostgreSQL JSON plan/BUFFERS/时延基线；然后增加 API/Runner 进程杀死、server restart/failover、deadlock/statement timeout、TCP 分区与真实外部 broker 演练，继续保持内容证明、所有 fence、claim-before-runner 与 prepare/commit/unknown 语义。**
