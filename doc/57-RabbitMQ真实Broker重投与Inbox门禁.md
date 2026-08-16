# 阶段 57：RabbitMQ 真实 Broker 重投与 Inbox 门禁

## 1. 本次范围

阶段 56 已完成 PostgreSQL 事务超时、死锁与 terminal commit 连接中断。本阶段把数据库 Outbox 接到真实 RabbitMQ，并验证两个无法靠进程内 fake 证明的不确定边界：

1. RabbitMQ 已确认持久消息，但 publisher 在返回 Outbox 前丢失响应；
2. Inbox 事务已经提交，但 consumer 在发送 broker ack 前断开连接。

同时覆盖 poison delivery 达到尝试上限后进入数据库 DLQ、人工 requeue 生成新 delivery/fence，以及旧 publisher owner/fence/delivery 对 ack/fail 的拒绝。

## 2. 生产拓扑与默认边界

`RabbitMqDeliveryPublisher` 使用 durable direct exchange、durable queue、persistent message、mandatory routing 和 publisher confirms。Outbox 只有在 confirm 返回后才尝试按 owner/fence/delivery/TTL 标记 `published_at`；confirm 后响应丢失会保留“不知道 broker 是否已收到”的 at-least-once 语义，下一 publisher 允许重发同一 logical `message_id`，但使用新 `delivery_id` 和新 fence。

`RabbitMqInboxWorker` 使用独立连接、固定 prefetch 和 manual ack。处理顺序为：

```text
RabbitMQ delivery
  -> 校验 Outbox envelope
  -> Inbox transaction + handler commit
  -> 本地 EventBroker 实时扇出
  -> before-ack boundary
  -> basic.ack
```

连接在 ack 前丢失时，RabbitMQ 将未确认消息重新入队；Inbox 以 `consumer_name + logical message_id` 唯一约束抑制重复 handler。数据库 task event/replay 仍是 WebSocket 真值，本地 EventBroker 只负责低延迟扇出。

`DESKPILOT_EVENT_TRANSPORT=local` 仍是默认值，不启动网络连接、不要求 RabbitMQ。只有显式选择 `rabbitmq` 且提供 `DESKPILOT_RABBITMQ_URL` 时，应用组合根才先启动 RabbitMQ publisher/consumer，再启动 Outbox polling；关闭时先停 Outbox，再停外部 transport，最后释放数据库。

## 3. 容器与安全门

仓库新增：

```text
infrastructure/rabbitmq/compose.yaml
infrastructure/rabbitmq/.env.example
backend/tests/test_rabbitmq_fault_injection.py
backend/tests/test_rabbitmq_verification.py
```

真实门禁默认 skip。它只接受：

- `DESKPILOT_TEST_RABBITMQ_ALLOW=1` 的显式二次确认；
- `amqp` URL；
- 明确的临时用户名和密码；
- `127.0.0.1`、`::1` 或 `localhost`；
- vhost 名以 `_` 或 `-` 分词包含 `test`。

测试使用每次随机 exchange/queue，连接 URL 与密码不进入命令行输出、断言或文档。本次实际容器使用 `rabbitmq:3.13.7-alpine`、loopback 5672 和专用 test volume；验证完成后已删除专用容器及 test-only volume。

运行入口：

```powershell
$env:DESKPILOT_TEST_RABBITMQ_URL = "amqp://user:password@127.0.0.1:5672/deskpilot_test"
$env:DESKPILOT_TEST_RABBITMQ_ALLOW = "1"
./.venv/Scripts/python.exe -m pytest tests/test_rabbitmq_fault_injection.py -vv
```

## 4. confirm 响应丢失与 publisher fence

门禁先让旧 publisher 获取 fence=1 并发布 persistent message。测试 observer 只在 RabbitMQ confirm 已返回后抛出“响应丢失”，因此 broker 队列中已有消息，但 Outbox 尚未 ack。

旧 claim TTL 到期后，当前 publisher 为同一 `message_id` 获取 fence=2 和新 `delivery_id`。此时旧 publisher 的 `_mark_published()` 返回 false，迟到 `_mark_failed()` 返回 `fenced`；当前 publisher 再次发布并且只有当前 owner/fence/delivery 能标记 Outbox published。队列因此真实包含两个不同 delivery、同一 logical message 的副本。

## 5. ack 前断连、redelivery 与 Inbox 去重

第一个 consumer 使用 prefetch=1，首次消息完成 Inbox handler 和本地扇出后，在 ack 前关闭真实 AMQP 连接。第二个 consumer 随后得到两个 delivery，其中未 ack 的消息由 RabbitMQ 明确标记 `redelivered=true`。

最终数据库与应用证据为：

- 两个 delivery ID 都被观察到；
- 第二轮全部返回 `duplicate=true, processed=false`；
- Inbox 只有 1 行；
- handler 只执行 1 次；
- 本地实时扇出只执行 1 次；
- Outbox 只接受 fence=2 的最终 ack。

这证明系统依赖 logical message Inbox 幂等，而不是错误假设 broker exactly-once。

## 6. DLQ 与人工 requeue

门禁关闭 production RabbitMQ publisher 后创建新 Outbox message。连续两次真实 transport-not-started 发布失败使消息达到 `max_attempts=2`，数据库记录 `dead_lettered_at/dead_letter_reason`，后续轮询不再自动发布。

显式 `requeue_dead_letter()` 清除 DLQ 状态、重置 attempt、清除旧 delivery，并提升 fence。RabbitMQ publisher 恢复后，消息以新 delivery ID 和更高 fence 发布，Inbox 将该新 logical message 精确处理一次。人工 requeue 不复用旧 delivery 身份，也不给旧 publisher 恢复写权限。

## 7. 实测结果与未覆盖边界

RabbitMQ 3.13.7 真实专项：

```text
1 passed, 1 warning in 3.72s
```

配置守卫与既有可靠投递回归：

```text
11 passed, 1 warning
```

默认未注入外部 URL 的后端全量：

```text
385 passed, 8 skipped, 1 warning in 581.44s
```

本阶段没有 schema 变更，Alembic head 仍为 `0022_effect_runtime_ops`。新增运行依赖为 `aio-pika 9.6.2`，默认 local 模式不建立外部连接。

当前证据只覆盖单节点 RabbitMQ classic durable queue 的 confirm/ack/connection-loss 语义。尚未证明 quorum queue、RabbitMQ 节点重启或磁盘损坏、网络 blackhole/半开连接、DNS 故障、跨可用区复制或 broker 集群 failover；也不把 persistent+durable 冒充绝对零丢失保证。

## 8. 后续入口

下一阶段优先做 Ready membership count 投影化：

1. 从稳态 ready page proof 中移除全局 `COUNT`；
2. 用事务内增量计数与 graph revision/content proof 绑定，漂移时 fail closed；
3. 为 rebuild、并发 node transition 和取消/skip 分支补一致性门禁；
4. 在 PostgreSQL 17 上新增版本化 plan/scan-row 基线，证明页查询工作量只与 page size 相关。

之后再推进 admission 分片、graph-control PostgreSQL 原生批量 claim，以及 audit 游标/导出和告警通知。RabbitMQ quorum/restart 与真实网络分区保持为独立基础设施阶段。
