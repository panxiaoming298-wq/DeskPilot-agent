# 阶段 54：Docker PostgreSQL 真库验收与兼容修复

## 1. 本阶段目标

阶段 51～52 已建成显式选入、fail-closed 的 PostgreSQL 性能与故障门禁，但此前只完成本地契约和默认 skip 验证。本阶段直接使用 Docker Desktop 中已有的 `deskpilot-postgres`，仅连接可抛弃的 `deskpilot_test`，实际执行迁移、1000-node 查询计划、并发 claim、连接终止、锁超时、连接池丢失接管和多主幂等门禁。

本阶段不连接 `deskpilot_dev`，不在文档或测试日志记录连接密码，也不放宽既有测试库名与二次确认保护。

## 2. 实测环境

```text
容器: deskpilot-postgres
PostgreSQL: 17.10 (Debian 17.10-1.pgdg12+1)
测试库: deskpilot_test
迁移 head: 0022_effect_runtime_ops
alembic_version.version_num: VARCHAR(128)
```

测试连接仍由 `DESKPILOT_TEST_POSTGRESQL_URL` 与 `DESKPILOT_TEST_POSTGRESQL_ALLOW=1` 显式选入。共享 guard 精确要求 `postgresql+asyncpg` 且数据库名把 `test` 作为 `_`/`-` 分隔 token；错误不回显 URL。

## 3. 真库发现与修复

### 3.1 Alembic 长 revision ID

第一次真库迁移在 revision `0010_reconciliation_receipt_evidence` 暴露 PostgreSQL 默认 `alembic_version.version_num VARCHAR(32)` 过短，写入 revision ID 时触发 `StringDataRightTruncationError`。SQLite 没有执行同样的长度约束，因此此前回归没有发现。

现在 online migration 在运行 revision 前执行 PostgreSQL 专用兼容准备：

- 空库直接创建 `VARCHAR(128)` 的 Alembic version table；
- 已存在且小于 128 的列使用无损 `ALTER COLUMN ... TYPE VARCHAR(128)` 扩宽；
- 非 PostgreSQL 方言不改变既有路径；
- 自动化断言所有现有 revision ID 都能容纳，并在真库迁移后核验列长度。

该修复只扩宽 Alembic 自身的版本键，不修改业务数据语义。

### 3.2 PostgreSQL 原生 node claim identity-map 同步

迁移通过后，第二次真库执行暴露 `UPDATE ... RETURNING` 的 ORM session synchronization 会提前把已加载 node 的 revision/fence 更新到 identity map。生产校验随后再次按“旧值 + 1”比较，导致真实获胜 claim 被误判为 fence 拒绝；SQLite 使用另一条 claim 路径，因此也未暴露。

PostgreSQL 原生 node claim 现在显式设置 `synchronize_session=False`。返回行仍作为数据库真值验证 revision/fence，已加载对象不会在校验前被隐式改写；claim-before-runner、graph/admission/node fence 和事务回滚边界保持不变。

## 4. 实际通过的门禁

专项命令最终结果：

```text
pytest -m postgresql_integration -vv
2 passed, 367 deselected, 1 warning in 5.32s
```

两项集成测试实际覆盖：

1. 迁移空库并把旧 `VARCHAR(32)` version table 扩宽到 `VARCHAR(128)`；
2. 1000-node ready v5 keyset `EXPLAIN ANALYZE/BUFFERS`，目标页实际返回 101 行（100 行加 sentinel）；
3. 两个独立 engine 对同一 ready proof 竞争，只有一个 node claim 获胜；
4. engine pool 丢失后按数据库时间 TTL 接管，旧 fence 不能续租或提交；
5. claim 已写但未 commit 时终止 PostgreSQL backend，事务全回滚且没有幽灵 fence；
6. graph 行锁竞争命中 `lock_timeout` 与 SQLSTATE `55P03`，没有部分 graph/node claim；
7. 两个独立 operations service 首次并发提交同一 retention 幂等键，归一为同一 audit event、digest 与 manifest。

这些门禁没有启动外部 Tool Runner，也没有制造文件系统或网络副作用；所有断言停留在数据库 claim、审计和 fence 边界。

## 5. 验证结果

```text
Ruff: All checks passed
mypy: Success, 132 source files
PostgreSQL integration: 2 passed, 367 deselected, 1 warning
backend pytest with PostgreSQL enabled: 369 passed, 1 warning in 644.93s
Alembic: 0022_effect_runtime_ops (head), no new upgrade operations
```

第一次完整回归另暴露 `test_task_event_vertical_slice` 自行硬编码了约 1 秒轮询，而同文件已有统一的 5 秒有界等待助手。Windows Runner 启动负载下任务正确进入 `running`，但未能在 1 秒内完成。该用例已改为复用统一等待助手；目标用例和随后完整 369 项回归均通过。这只修正测试时间假设，不放宽生产任务状态、fence 或超时语义。

## 6. 已知边界与下一步

1. 已覆盖单个 PostgreSQL backend terminate，但未覆盖整个 API/Runner OS 进程被杀、PostgreSQL server restart、主备 failover 或容器强制重启。
2. 已覆盖 graph 行锁超时，但尚未覆盖 admission/control/Outbox 多行死锁、`statement_timeout` 与事务取消组合。
3. 1000-node `EXPLAIN ANALYZE/BUFFERS` 已现场断言形态和行数，但尚未把 JSON plan、BUFFERS 和时延趋势保存为可比较的版本化基线。
4. engine pool 丢失不等价于 TCP blackhole、半开连接、DNS 故障或代理层断流。
5. 外部 broker 仍未接入，at-least-once 重投、响应丢失、Inbox 去重、DLQ 人工 requeue 和旧 publisher fence 尚需真实环境演练。

下一阶段入口：**保存可重复比较的 PostgreSQL JSON plan/BUFFERS/时延基线，并扩展 API/Runner process kill、server restart/failover、deadlock/statement timeout、TCP 分区和真实外部 broker 故障注入；继续保持内容证明、所有 fence、claim-before-runner 与 prepare/commit/unknown 语义。**
