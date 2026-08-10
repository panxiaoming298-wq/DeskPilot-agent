# 33. `file.move` 受控提交与持久化回执

## 1. 阶段结果

DeskPilot 已实现第一个真实可逆写 Tool：`file.move@1.0.0`。它不是让低信任 worker 直接写文件，而是把一次调用拆成无副作用 prepare、父 Runner 复核、单次 commit 和 durable receipt 四段。

本阶段完成：

- `file.move` 的 R1、`key_required`、`brokered` Contract；
- source/destination 规范化、同卷、普通文件、目标不存在和 source 外部版本约束；
- AppContainer worker 只消费 `BrokeredFileMove` facts 并生成 `FileMovePrepare`；
- 父 Runner 把 prepare 精确绑定到 authorization ID、一次性审批 preview hash、expected resource versions 和幂等键摘要；
- Windows 父 Runner 使用不带 replace flag 的 `MoveFileExW` 提交，不覆盖并发出现的目标；
- Runner 自有 SQLite journal 在外部写入前持久化 prepare/committing 边界，在写入后持久化 committed receipt；
- Runner 启动恢复可区分“目标版本已出现且 source 消失”和“source 原版本仍在且目标不存在”；
- `tool.commit_receipt.get/result` 签名 IPC 可跨 Runner 换代查询 committed receipt，不会重放旧调用；
- Alembic `0009_tool_commit_receipts` 将成功回执的无路径投影与 Tool 终态/事件放在同一控制面事务；
- timeout/cancel 根据共享 commit boundary 收敛：提交前可确定失败/取消，committing 且无法证明时为 `unknown`，durable committed receipt 已存在时返回成功。

核心不变量是：**worker 只能提议，只有父 Runner provider 能写；没有精确审批、版本和 durable commit ownership 时不会移动文件。**

## 2. 执行链

```mermaid
flowchart LR
    AUTH["签名 authorization\npreview hash + expected versions"] --> BROKER["父 Runner resource broker\n重读 source version / destination absent"]
    BROKER --> WORKER["一次性 AppContainer worker\n无副作用 FileMovePrepare"]
    WORKER --> VERIFY["父 Runner 精确比较\nprepare + facts + approval"]
    VERIFY --> STAGE["SQLite journal\nstate = prepared"]
    STAGE --> BOUNDARY["state = committing\ncommit boundary"]
    BOUNDARY --> MOVE["MoveFileExW\nno replace / same volume"]
    MOVE --> RECEIPT["state = committed\nversions before/after"]
    RECEIPT --> RESULT["FileMoveOutput\n签名 Runner result"]
    RESULT --> CONTROL["tool_commit_receipts\n控制面事务投影"]
```

`FileMovePrepare` 只包含规范 source/destination、source version 和 `destination=absent`。worker 不接收 Runner receipt 数据库路径、完整 authorization、preview 正文、幂等键或任何写句柄。

## 3. 资源版本

source version 是以下事实的 canonical SHA-256：

- 内容 SHA-256；
- 文件系统 device/volume identity；
- file ID/inode；
- 大小；
- nanosecond modification time。

读取在一个 descriptor 上进行，并比较读取前后 metadata；版本计算期间变化会失败。移动限定在同卷，因而成功后 destination version 必须与 source version 完全相等。

授权要求 `expected_resource_versions` 精确为：

```json
{
  "destination": "absent",
  "source": "<64 hex>"
}
```

Policy resource scope 同时绑定两个角色不同的 capability：

- source：`filesystem.file.move_source`，带 version digest；
- destination：`filesystem.file.move_destination`，审批与提交时都要求不存在。

source 在审批后变化、destination 出现、父目录不再规范、路径变为 symlink/reparse alias 或卷发生变化，都会在 commit 前 fail closed。

## 4. Durable journal 与回执

Runner journal 默认位于：

```dotenv
DESKPILOT_RUNNER_COMMIT_RECEIPT_DATABASE_PATH=./data/runner/commit-receipts.db
```

`controlled_commit_attempts` 唯一绑定：

- receipt ID；
- call ID；
- tool name/version/idempotency-key digest；
- authorization ID、approval ID、preview hash；
- prepare digest 与完整 binding digest；
- `prepared / committing / committed / no_effect / unknown`；
- 恢复所需的规范资源 facts；
- committed receipt。

原始 idempotency key 不落库。Runner journal 为本机恢复保留规范路径；控制面 `tool_commit_receipts` 只投影 receipt identity、授权/审批摘要和 source/destination 的 before/after version，不复制路径。

`ToolCommitReceipt` 的 before/after 不变量：

```text
before: source=<version>, destination=absent
after:  source=absent,    destination=<same version>
```

控制面成功事件内仍包含 Tool output；`0009_tool_commit_receipts` 是可独立查询和约束的一次提交证据投影，不代替原 Tool 调用账本。

## 5. 崩溃、timeout 与 cancel

| 观测点 | 结果 |
| --- | --- |
| worker prepare 前/中取消 | `cancelled`，无 commit record 或 `no_effect` |
| prepare 已持久化、尚未进入 committing | 可证明未提交，失败/取消 |
| state=committing，source 原版本仍在且 destination absent | 启动恢复为 `no_effect` |
| state=committing，source absent 且 destination 为原版本 | 启动恢复并补写 committed receipt |
| 两端状态不符合以上任一证明 | `unknown`，禁止自动重放 |
| timeout/cancel 时内存 boundary 已有 committed output/receipt | 返回 `succeeded`，不伪装成 unknown |

取消标志检查与 `before_commit -> committing` 转换在同一把锁内完成。因此，即使大文件版本复核恰好跨过 timeout，Runner 也不会先给出“确定未提交”后再进入 OS move。journal 的 `prepared` 状态同样可直接证明 no-effect；只有已持久化为 `committing` 的记录才根据外部 source/destination 版本恢复。

Runner result 丢失后，控制面或人工对账代码可以向当前 Runner 代际发送签名 `tool.commit_receipt.get`。新代会先恢复同一路径的 journal，再返回相同 receipt；查询不会再次执行 `file.move`。

原始 `tool_calls.status=unknown` 仍不可改写。把后验 receipt 接入 Reconciliation 自动裁决/前端证据中心留在后续控制面阶段。

## 6. 可逆边界

本 Tool 的外部效果可通过一次新的、重新预览和审批的反向 `file.move` 补偿。系统不会在失败后让模型自行生成反向命令，也不会复用旧 approval、call ID 或 idempotency key。

当前尚未提供“一键撤销”API 或自动 compensation scheduler；`reversible=true` 表示 provider 有确定性的反向资源语义，不表示旧授权可以重复使用。

## 7. 平台与安全边界

- 发布目标 Windows 使用 `MoveFileExW` 且不设置 `MOVEFILE_REPLACE_EXISTING`；目标并发出现时不覆盖。
- 跨卷移动被拒绝，避免 copy/delete 的部分提交语义。
- 非 Windows 兼容测试路径使用标准 rename；生产配置仍要求 Windows sandbox，不把该兼容路径作为同等安全承诺。
- journal 当前依赖单一受信任 Runner owner；Supervisor 会先终止旧代再启动新代。多独立 Runner 进程共享同一 journal 尚未设计租约。
- 同一 Windows 用户下的恶意高权限进程仍不在当前威胁模型内；桌面发布仍需要代码签名、安装目录 ACL 与可信更新链。
- TaskProcessor 仍只从 Fake Planner 派发 `computer.disk_usage`；`file.move` 已进入静态 Registry/Runner allowlist 和真实集成测试，但尚未开放为自然语言任务入口。

## 8. 验收

```text
Ruff:  All checks passed
mypy:  Success, 100 source files
pytest: 264 passed
Alembic: 0009_tool_commit_receipts (head)
frontend vitest: 11 files, 100 passed
frontend type-check/build: passed
```

新增覆盖：

- 真实独立 Runner 完成一次 no-overwrite 文件移动；
- source 审批后变化在 worker/commit 前被拒绝；
- receipt 与外部 before/after version 一致；
- 原始 idempotency key 不进入 Runner journal；
- Runner 停止并重启后查询到相同 receipt；
- 崩溃发生在外部 move 后、receipt 前时启动恢复为 committed；
- 仅 prepared 的调用不会把其他进程后续执行的移动误归因为自身 committed；
- timeout/cancel 发布与 commit boundary 转换具备原子互斥；
- committing 但外部状态未变化时恢复为 `no_effect`；
- 控制面 commit receipt 与 Tool 成功终态原子投影；
- 并发 receipt 查询互不串线，未派发调用返回无 receipt。

## 9. 下一步

1. 将 `file.move` 接入明确的文件任务入口、Policy allowlist 和审批卡，先支持用户提供的单文件 source/destination，不让模型扩展批量范围。
2. 为反向 move 增加基于 receipt 的显式 compensation API，并重新执行版本检查与一次性审批。
3. 把 Runner receipt query 接入 unknown Reconciliation 的自动证据采集，同时保持原 Tool 账本不可改写。
4. 持久化任务图和阶段检查点，使审批、受控写和证据查询具备可证明的 API 重启恢复语义。
