# 阶段 51：Ready v5 Keyset 与 PostgreSQL 验收门禁

## 1. 本阶段目标

阶段 49 将 DAG ready 成员集改为数据库增量投影，阶段 50 建立了受保护运维、retention 与审计面，但 ready 页仍使用 offset cursor，真实 PostgreSQL 验收也没有可重复入口。本阶段完成两项基础建设：

- ready checkpoint 升级为内容寻址 v5 ordinal keyset，稳态页查询不再使用 `OFFSET`；
- 增加显式选入、安全拒绝非测试库的 PostgreSQL 大图 `EXPLAIN ANALYZE`/BUFFER、双引擎竞争、连接池丢失后接管和审计行锁验收门禁。

本阶段最初没有发现可用 PostgreSQL 服务，因此当时只声明“验收入口已建成且方言契约已通过”。后续阶段 54 已在 Docker Desktop 的专用 `deskpilot_test` 上实际执行通过；真库环境、兼容修复与结果见 [`54-Docker-PostgreSQL真库验收与兼容修复.md`](54-Docker-PostgreSQL真库验收与兼容修复.md)。

## 2. Ready v5 keyset 证明

### 2.1 Cursor 不再是行偏移

`EffectReadySetCheckpointRead.cursor` 现在是前一页内容寻址 checkpoint ID：

```text
ter_<64 hex proof digest>
```

服务端不信任客户端提供的 ordinal。继页时它首先读取 cursor 指向的持久化 checkpoint，然后重算并校验：

- checkpoint ID 必须等于 `ter_ + proof_digest`；
- graph ID/fencing token/event sequence 必须与当前图一致；
- projection revision/content digest 必须一致；
- 全局 ready 成员数摘要和前页 proof 必须可重算；
- cursor checkpoint 必须确实还有下一页，且 `last_ordinal` 必须与页尾 node 一致。

通过后才从已验证前页取得 `database_time` 和 `last_ordinal`，执行：

```sql
... WHERE ready.ordinal > :after_ordinal
ORDER BY ready.ordinal
LIMIT :page_size_plus_one
```

额外一行只用于判定 `has_more`，不进入 proof。当前仍执行全局 `COUNT` 来绑定 membership digest，但已消除大 offset 跳过成本。

### 2.2 v5 proof 绑定

v5 页摘要绑定：

- graph fence/event 与 projection revision/digest；
- 首页冻结的数据库时间；
- 全局 membership digest 和 `total_ready`；
- `cursor/page_size/after_ordinal/last_ordinal/has_more`；
- 当页每个 node 的 ordinal/status/revision/event/claim fence、前驱和 branch decision proof。

claim 事务不盲信已存 checkpoint：它重新验证 cursor，以同一 `database_time + after_ordinal + page_size` 重读当页，重算 membership/page digest，然后才串接 cluster admission proof、graph CAS 和 node claim CAS。跨图 cursor、旧 event/projection cursor、checkpoint ID/digest/ordinal 篡改都 fail closed。

v4 的数字 offset cursor 不与 v5 互通；升级时尚未 claim 的 v4 页应在当前 graph lease 下从 v5 首页重新 checkpoint，不尝试翻译或信任旧 offset。已派发/running Tool 仍走原 node fence 和 unknown 恢复语义，不因页升级被重放。

## 3. 可解释的查询契约

ready count 和 page statement 收敛到 `infrastructure/effect_ready_queries.py`，运行时和 PostgreSQL 验收使用同一查询构建器，避免测试重写一份相似 SQL。方言编译测试明确断言：

- 存在 `ordinal > 900`；
- 按 ordinal 排序；
- 查询上限为 `page_size + 1`；
- SQL 中不存在 `OFFSET`。

该构建器仍保留 pending/active、claim expiry、branch rejected、remaining predecessor 和 unresolved branch 的原谓词，只替换分页定位方式。

## 4. PostgreSQL 显式选入验收

验收测试位于 `backend/tests/test_postgresql_runtime_integration.py`。默认没有环境变量时显式 skip；它只接受：

- `postgresql+asyncpg` URL；
- database name 包含 `test`；
- `DESKPILOT_TEST_POSTGRESQL_ALLOW=1` 二次确认。

建议只对一次性专用测试库执行：

```powershell
$env:DESKPILOT_TEST_POSTGRESQL_URL = "postgresql+asyncpg://user:password@127.0.0.1:5432/deskpilot_test"
$env:DESKPILOT_TEST_POSTGRESQL_ALLOW = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_postgresql_runtime_integration.py -m postgresql_integration -vv
```

门禁会将该库升级到 Alembic head，然后执行：

1. 建立 1000 个 ready root 的大图，读取前两个 v5 keyset 页；
2. 对同一运行时 statement 执行 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`，确认实际返回 101 个有界候选行；
3. 用两个独立 `Database` engine/`TaskService` 同时争抢同一已证明 node，必须精确一个成功；
4. 丢弃获胜者连接池，注入 claim expiry，由恢复实例接管并确认 node fence 单调加一；
5. 用旧 owner/fence 提交 transition，必须被拒绝；
6. 用两个独立运维服务并发追加 metrics audit，验证 PostgreSQL state 行锁下 sequence/previous digest 连续。

测试结束后删除它创建的 Task 图；metrics audit 作为 append-only 真值保留，因此应使用可抛弃测试库，不应指向开发或生产库。

## 5. 不变安全语义

- cursor 只是持久化 proof 的身份，不是绕过 graph lease/admission/node fence 的授权；
- ready checkpoint 和 `EXPLAIN` 验收都不调用 Runner，双实例竞争演练只到 claim-before-runner 边界；
- 接管只重新颁发 node fence，不重放任何已派发 Tool；
- prepare/commit/receipt/unknown、intent-before-IPC、graph/node/admission/Outbox 所有 fence 语义未改变；
- 本阶段无 schema 变更，Alembic head 仍为 `0022_effect_runtime_ops`。

## 6. 本地验收结果

```text
Ruff:  All checks passed
mypy:  Success, 130 source files
pytest: 360 passed, 1 skipped in 742.63s
PostgreSQL integration: skipped (no configured PostgreSQL service)
Alembic: 0022_effect_runtime_ops (head), no new operations
frontend vitest: 15 files, 126 passed
frontend type-check/build: passed
```

新增本地覆盖包括 v5 三页 cursor/ordinal 链、跨图 cursor 拒绝、持久化 ordinal 篡改拒绝、claim 当页重算，以及 PostgreSQL 方言编译不含 `OFFSET`。全量回归继续覆盖 graph-control、cluster admission、Outbox/DLQ、retention/audit、Runner cancel 和 prepare/commit/unknown。

## 7. 已知边界与下一步

1. keyset 已消除 ready 页的 `OFFSET`，但全局 membership proof 仍执行 `COUNT`；更大集合可将精确 ready count 维护进 projection state/hash-chain，再用实际 plan 决定是否值得引入。
2. 选入门禁已在 Docker PostgreSQL 17.10 真库执行；仍应保存可版本比较的 JSON plan/BUFFERS 与时延趋势基线。
3. 当前故障演练是“已提交 claim 后丢弃 SQLAlchemy engine pool + TTL 接管”，不等价于 OS 进程杀死、PostgreSQL backend terminate、长事务中断或网络分区。
4. 尚无真实外部 broker 适配器及 broker 响应丢失/重投/DLQ 人工处置故障演练；当前可证明边界仍是事务 Outbox + at-least-once + Inbox 去重。
5. 下一阶段应在可用基础设施中执行该门禁，再增加 process/database kill、长事务、timeout/network partition 和真实 broker 脚本；同时评估 admission 分片、graph-control 原生批量 claim 和 ready count 投影化。

下一阶段入口：**保存大图/keyset/dual-engine/audit 的可比较 EXPLAIN BUFFERS 基线，并扩展进程杀死、server restart/failover、长事务/deadlock 和网络分区注入以及真实外部 broker 演练；继续保持内容证明、所有 fence、claim-before-runner 与 prepare/commit/unknown 语义。**
