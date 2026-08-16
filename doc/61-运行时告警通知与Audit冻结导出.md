# 阶段 61：运行时告警通知与 Audit 冻结导出

## 1. 本次范围

阶段 50 的 operations snapshot 已能计算稳定告警，audit 也已按 sequence keyset 分页并校验 hash-chain，但仍有两个产品与证明缺口：

1. snapshot 只反映“现在有哪些告警”，不能回答告警何时出现、何时变化、何时恢复；
2. audit 普通分页没有冻结导出终点，客户端连续拉取时会追上并发新增事件，无法形成边界明确、可复核的一份导出。

本阶段完成：

- 持久化告警 lifecycle state；
- append-only、hash-chain 的 `opened / updated / resolved` 通知 feed；
- 通知与产生它的 metrics audit、snapshot digest 交叉绑定；
- 普通 audit 页先冻结当前 head，再执行稳定 keyset；
- 内容寻址的 opaque cursor 与冻结终点 audit export；
- 前端通知历史和完整 JSON 下载；
- SQLite 迁移/篡改门禁，以及 PostgreSQL 双 engine 唯一通知和并发 append 冻结证明。

这里的“通知”是数据库持久化、API 可分页、前端可见的运维通知，不是 Windows 系统通知、邮件、短信或 webhook。RabbitMQ 没有进入正确性链；将来可用于低延迟唤醒，但数据库 feed 始终是真值。

## 2. 告警 Lifecycle 投影

Alembic `0026_alert_notifications` 新增：

```text
effect_runtime_alert_states
  alert_code primary key
  domain / severity
  active / count / revision
  first_seen_at / last_seen_at / resolved_at
  last_snapshot_digest / updated_at

effect_runtime_alert_notifications
  notification_id primary key
  sequence unique
  alert_code / transition / severity / domain / count
  alert_revision / snapshot_digest
  audit_event_id / audit_sequence
  previous_event_digest / event_digest
  occurred_at
```

operations singleton state 另增加 `next_alert_sequence` 与 `last_alert_event_digest`，作为通知链的数据库串行化 head。

每次 `metrics.sampled` 与 snapshot audit 在同一事务内完成 alert reconciliation：

- 当前 snapshot 首次出现某 code：创建 state，发出 `opened`；
- active code 的 count/severity/domain 改变：revision 增长，发出 `updated`；
- active code 不再出现在 snapshot：count 归零并记录 resolved time，发出 `resolved`；
- active code 内容不变：只更新 last-seen proof，不重复通知；
- 已 resolved code 再次出现：以更大 revision 再发 `opened`。

多 API 的进程内 lock 只能减少本进程重复工作，真正的跨实例唯一性来自 operations state 数据库行锁。所有 engine 都必须先追加 audit、持有同一 state lock，再读取/更新 alert state 和通知 head。

## 3. 通知 Hash-chain 与交叉证明

每条通知摘要覆盖：

```text
sequence
alert_code / transition / severity / domain / count
alert_revision
snapshot_digest
audit_event_id / audit_sequence
previous_event_digest
occurred_at
```

读取通知页时不只校验自身 hash-chain，还批量读取关联 audit 并验证：

- audit action 必须是 `metrics.sampled`；
- notification.audit_sequence 必须等于 audit.sequence；
- notification.snapshot_digest 必须等于 audit.result_digest；
- audit 自身 event digest 必须能从其脱敏 details 重算。

因此不能把一条合法告警通知移接到另一份 snapshot/audit，也不能修改 count、transition、revision 或时间而保持链有效。

通知 API：

```text
GET /api/v1/operations/effect-runtime/alerts
    ?after_sequence=<exclusive keyset>&limit=<1..500>
```

响应使用 `Cache-Control: no-store`，只包含稳定 code、domain、severity、count、revision、摘要、序号和数据库时间，不包含任务 goal、Tool 参数、Outbox payload 或错误原文。

事务提交后可调用本地 `alert_notify` wake callback；callback 失败只记录日志，不回滚已经提交的 audit/notification。当前生产组合由前端刷新和 scheduler sample 读取数据库 feed，没有配置外部通知 transport。

## 4. 普通 Audit Keyset 修复

原 `audit_page(after_sequence, limit)` 已经使用：

```text
sequence > after_sequence
ORDER BY sequence
LIMIT limit + 1
```

但它在查询之后读取“当前 state head”判断尾页。若另一个 API 恰在两次读取之间追加 audit，旧实现可能把合法旧页误判成 head 不一致。

现在每页开始先读取并冻结：

```text
through_sequence = state.next_sequence - 1
through_event_digest = state.last_event_digest
```

随后查询增加 `sequence <= through_sequence`，并以冻结 through event 校验尾部。并发 append 可以进入下一次分页视图，不会污染本页，也不会触发假篡改告警。

## 5. Audit 冻结导出 Cursor

导出 API：

```text
GET /api/v1/operations/effect-runtime/audit/export
    ?cursor=<opaque cursor>&limit=<1..500>
```

首个请求冻结数据库时间、through sequence 和 through event digest，并生成：

```text
export_id = SHA-256(
  schema version
  + frozen database time
  + through sequence
  + through event digest
)
```

后续 cursor 使用 canonical JSON + URL-safe Base64，内容包括：

- cursor schema version；
- export ID；
- frozen database time；
- through sequence/digest；
- exclusive after sequence/event digest；
- cursor digest。

服务端对 cursor 做严格长度、Base64、字段集合、类型、版本、digest、export identity、after anchor、through anchor 和连续 sequence 校验。cursor 不含凭据，也不是授权令牌；认证仍由本地 session 负责。

每个 export page 返回完整脱敏 audit event、page digest、next cursor 和 has-more。page digest 覆盖冻结身份、after anchor、事件内容、has-more 与 next cursor。导出期间新增 audit 不会改变旧 export 的 through；重新发起无 cursor 请求才会得到新 head 和新 export ID。

## 6. 前端完整导出

运行时运维页现在同时读取 snapshot、audit 页和 alert notification 页：

- 告警区展示当前 active alerts，以及最近的 opened/updated/resolved 生命周期记录；
- 手动 sample 直接合并事务返回的通知，不做乐观推断；
- “导出冻结审计”逐页跟随 opaque cursor；
- 每页必须保持相同 export ID、database time、through sequence/digest；
- event sequence 必须从 1 连续到 frozen through；
- `has_more` 必须与 `next_cursor` 一致；
- 最终 event digest 必须等于 frozen through digest；
- 全部通过后才生成 `deskpilot-effect-runtime-audit-<through>.json`。

下载文件包含 schema version、export identity、冻结时间/终点、每页 digest 与脱敏 events，不包含 access token、cursor、任务 goal、payload 或错误原文。

## 7. PostgreSQL 多实例证明

真实 PostgreSQL 17.10 门禁使用两个独立 `Database`/engine：

1. 先采样 clean snapshot，收敛共享测试库可能残留的 active state；
2. 构造 ready projection event/count drift；
3. 两个 engine 同时 `sample_metrics`；
4. 两条 metrics audit 都合法提交，但合计恰好一条 `opened` notification；
5. 第一个 export 冻结当前 audit head；
6. 另一个 engine 再追加一条 audit；
7. 旧 cursor 继续导出时 export ID/through 不变且不包含新事件；
8. 新 export 才看到 through sequence 增加 1。

这直接证明通知去重依赖数据库串行化，而不是单进程 asyncio lock；冻结导出也不依赖长事务或阻塞写入者。

## 8. 验证结果

已完成：

- SQLite 空库、重复升级和 `0026 -> 0025 -> 0026` 往返；
- PostgreSQL `deskpilot_test` 同样完成往返与 metadata check；
- opened/unchanged/updated/resolved 生命周期、revision/count 与 wake callback 门禁；
- 通知 page+sentinel、跨页 digest 连续与内容篡改拒绝；
- audit cursor 内容篡改拒绝；
- 并发 append 下旧 export head 冻结、新 export 看到新 head；
- API 认证、no-store、nosniff、secret-free 和稳定 problem code；
- 前端分段导出 identity/sequence/through 复核与 JSON 下载；
- 默认后端全量 `396 passed, 12 skipped, 1 warning`（408 collected）；
- 前端 `17 files / 134 passed`，type-check 与 build 通过；
- Ruff 全仓规则检查、mypy 140 个生产源码、生产+两项门禁 142 文件、`uv lock --check`；
- 开发 SQLite、开发 PostgreSQL、测试 PostgreSQL 均为 `0026_alert_notifications (head)`，Alembic check 无漂移。

12 个 skip 是默认未注入 URL 的 PostgreSQL/RabbitMQ 显式外部门禁。唯一 warning 仍是既有 FastAPI/Starlette TestClient 的 httpx2 迁移提示。

## 9. 已知边界与后续入口

- 当前通知是应用内 durable feed，不包含系统托盘、邮件、短信、PagerDuty、Slack 或 webhook；接外部渠道时必须使用独立 delivery ledger/Inbox，不能把外部 ack 当作 alert state 真值。
- alert state 没有自动 retention；审计与通知链一旦允许归档，需要先设计带 manifest 的分段 chain checkpoint，不能直接删除前缀。
- 前端单次最多跟随 10000 页，每页服务端最多 500 条；超出时 fail closed，后续大规模导出应交给后台生成受摘要保护的 artifact。
- export cursor 内容可被客户端看到且不含秘密；它的目标是冻结与完整性证明，不是隐藏分页位置。

下一阶段进入首个具体条件业务图：把已有条件边、分支 proof、admission、ready projection、Tool ledger 与前端演示串成一个真实可触发的受信工作流，而不增加模型生成写路径。
