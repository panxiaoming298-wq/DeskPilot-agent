# 阶段 56：PostgreSQL 事务超时、死锁与连接中断门禁

## 1. 本次范围

阶段 55 已完成 PostgreSQL JSON plan 版本化、API/Runner 进程强杀和专用容器 restart 三域 TTL/fence 接管。本阶段继续补齐三类数据库交易边界：

1. `statement_timeout` 在 graph 与 node 写入已进入同一事务后取消当前 SQL；
2. 两个独立 backend 反序更新两个 node，构造真实多行 deadlock；
3. Tool 已按真实 Policy 与授权进入 `running`，生产 `TaskService.finish_tool_call()` 已在事务内写入 terminal ledger/event/Outbox，但 backend 在 commit 前被终止。

验收目标是失败事务零部分写入；不确定 terminal commit 只能稳定收敛为 `unknown` 和 pending reconciliation，不得生成新 call、`tool.completed` 或透明重放。

## 2. 真库入口与安全边界

门禁文件：

```text
backend/tests/test_postgresql_transaction_fault_injection.py
```

运行命令：

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_postgresql_transaction_fault_injection.py -vv
```

它复用 `load_postgresql_verification_url()` 的 fail-closed guard：只接受 `postgresql+asyncpg`，数据库名必须以 `_` 或 `-` 分词包含 `test`，并且必须显式设置 `DESKPILOT_TEST_POSTGRESQL_ALLOW=1`。未配置 URL 时三项全部明确 skip，不猜测默认端口或应用库。

所有数据只在随机可抛弃 task/graph/node/call 中生成，finally 按精确 `task_id` 删除。连接中断用例只终止当前测试 session 的 PID；终止前必须从 `pg_stat_activity` 再次验证目标与 admin 处于同一 database、同一 user，且不是 admin 自身 PID。连接 URL、用户名与密码不进入命令行、checkpoint 或日志。

## 3. statement_timeout 整事务回滚

用例先按生产 `TaskService` 创建单 node effect graph、获取 graph lease 并生成 ready proof，然后在独立 PostgreSQL 事务中：

1. 设置 `statement_timeout=250ms`；
2. 提升 graph revision；
3. 把 node 暂存为 `active`、revision+1、owner 与 fence=1；
4. 执行 `SELECT pg_sleep(2)`。

PostgreSQL 17.10 实测返回 SQLSTATE `57014`。回滚后 graph/node revision 精确回到超时前，node 仍为 `pending`、owner 为空、fence=0，Tool ledger=0，且没有 `effect.node.claimed`。原 ready proof 随后仍能通过生产 claim 路径颁发第一个可见 node fence=1，证明没有 ghost revision 或 ghost fence。

## 4. 多行 deadlock 与受害者零部分提交

两个真实 AsyncSession 各自在一个独立事务中执行：

- A 先锁 node 1，再更新 node 2；
- B 先锁 node 2，再更新 node 1。

每个事务在进入 node 互锁前，还会更新一条只属于自己的 witness task。PostgreSQL 精确选出一个 SQLSTATE `40P01` 受害者，另一个事务成功提交。最终证据为：

1. 恰好一个 committed，恰好一个 `40P01`；
2. 两个 node 都只有胜者 owner、revision=2、fence=1，不存在受害者 ghost owner；
3. 胜者 witness 已提交，受害者在死锁前已执行的 witness 写入回到原值，直接证明整事务回滚；
4. graph revision 不变、Tool ledger=0、没有伪造 node transition 事件。

## 5. terminal commit 连接中断与 unknown 收敛

用例先使用真实 `record_tool_requested -> apply_policy_decision -> start_tool_call` 路径，把唯一 Tool call 持久化为 `running`。随后测试专用 session wrapper 调用生产 `TaskService.finish_tool_call(status=succeeded)`：事务 body 生成 terminal ledger、`tool.completed` event 和 Outbox，但 wrapper 在真实 transaction `__aexit__` 提交前终止该 backend。

提交返回 connection-invalidated `DBAPIError` 后，新连接只能看到：

- 原 call 仍为 `running`，`finished_at` 与 `terminal_event_id` 为空；
- task 仍为 `running`，`last_event_seq` 不变；
- `tool.completed` 数量为 0，terminal ledger/event/Outbox 没有部分提交。

新 `TaskService` 随后执行生产 `recover_incomplete_tool_calls()`，原 call 精确收敛为 `unknown`、`error_code=TOOL_RESULT_UNCERTAIN_AFTER_RESTART`、`resolution_source=startup_recovery`；task 进入 `waiting_reconciliation`，并且恰好一条 pending reconciliation。

连续第二次 recovery 产生 0 变化。整个任务始终只有一个 call、一次 `tool.requested`、一次 `tool.started`、一次 `tool.unknown`、一次 `task.waiting_reconciliation`，且没有 `tool.completed`；因此不可证 commit 不会被当成失败重试，也不会透明重放 Runner。

## 6. 实测结果

Docker PostgreSQL 17.10 专项：

```text
3 passed, 1 warning
```

与阶段 51～55 既有真库门禁合并：

```text
7 passed, 376 deselected, 1 warning
```

显式启用专用 PostgreSQL 和容器 restart 后的后端全量：

```text
383 passed, 1 warning in 682.67s
```

Ruff 全仓通过；mypy 检查 133 个生产源码文件通过，生产源码加新门禁 134 文件也通过。警告仍只是既有 Starlette/httpx 第三方弃用警告。本阶段无 schema 或生产代码变更，Alembic head 仍为 `0022_effect_runtime_ops`。

## 7. 后续入口

下一阶段进入真实外部 broker 故障演练：

1. at-least-once 重投与响应丢失；
2. Inbox 按 logical message/delivery 去重；
3. poison message 进入 DLQ，人工 requeue 使用新 delivery/fence；
4. 旧 publisher owner/fence/delivery 对 ack/fail 全拒绝；
5. 继续保持 claim-before-runner、prepare/commit/unknown 和 receipt 是已提交效果唯一恢复依据。

PostgreSQL 主备 failover、TCP blackhole/半开连接和 DNS 故障仍属于后续基础设施注入，不把本次单 backend 终止冒充为网络分区或 failover 证明。
