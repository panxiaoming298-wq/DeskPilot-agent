# 34. `file.move` 显式任务入口与一次性审批

## 1. 阶段结果

DeskPilot 已把阶段 33 完成的 `file.move@1.0.0` Runner 受控提交能力接入真实任务与前端审批主干。用户现在可以从任务表单明确选择“移动单个文件”，填写 source/destination，并在任何外部写入发生前查看一次性审批卡。

本阶段没有让模型解析路径或决定可写工具。文件移动分支完全来自结构化 `tool_request`，使用受信任应用计划模板；模型输出不参与 source/destination、资源版本、Policy 事实或 Runner 参数构造。

## 2. 公共任务契约

`POST /api/v1/tasks` 新增可选字段：

```json
{
  "goal": "移动我选择的文件",
  "privacy_mode": "local_only",
  "constraints": ["single_file", "no_overwrite", "no_cloud"],
  "tool_request": {
    "kind": "file_move",
    "source": "D:\\Documents\\draft.txt",
    "destination": "D:\\Documents\\archive\\draft.txt"
  }
}
```

`tool_request` 使用 strict schema，禁止额外字段，只开放 `kind=file_move`。API 在创建任务前通过线程执行只读规范化检查：

- source 必须是当前存在的普通文件，且不能是符号链接；
- destination 父目录必须存在；
- destination 必须尚不存在；
- source/destination 必须不同且位于同一卷；
- 返回 Processor 的两个路径均为规范绝对路径。

无效请求返回 `422 FILE_MOVE_REQUEST_INVALID`，不会留下 Task、Event 或审批记录。

## 3. 受信任计划与参数来源

普通磁盘容量任务仍走 Fake/Model Gateway 的分类和计划。带 `file_move` 请求的任务不调用模型，而是由控制面生成固定三步计划：

1. 规范化并锁定源文件与目标路径；
2. 审批后执行 `file.move@1.0.0`；
3. 验证提交回执与文件版本。

对应事件带有：

- `task.classified.payload.source=explicit_user_request`；
- `plan.proposed.payload.source=trusted_application_template`。

该分支不会产生 `model.started/model.usage`。授权 actor 固定为 `local_user`，而不是伪装成模型身份。

## 4. 版本预览、幂等与 Policy

在 `tool.requested` 前，Processor 在线程中计算精确资源投影：

- source：`filesystem.file.move_source` + 内容/文件身份版本；
- destination：`filesystem.file.move_destination`，要求 absent；
- `expected_resource_versions={source: <digest>, destination: absent}`。

每个任务生成一次高熵 `key_required` 幂等键。原始键只存在于当前 Processor 内存和 Runner 调用中；控制面调用账本、事件、Outbox 与 Runner journal 均只保存 SHA-256 摘要。

默认 `BuiltinPolicyEngine` 新增窄范围动态资源规则。只有同时满足以下事实才可越过静态路径 allowlist，并且结果仍是 R1 `require_approval`：

- actor 精确为 `local_user`；
- Tool/版本/Contract digest 精确匹配 `file.move@1.0.0`；
- side effect、reversible、双 capability 与两个资源角色完全匹配；
- source 带版本、destination 不带已有版本；
- local、interactive、single-call、无网络、无数据外发。

模型 actor、插件/MCP origin、批量、能力扩张、资源角色错配或 Contract 变化都会 fail closed。

## 5. 一次性审批与执行

审批卡复用现有不可改写状态机，显示：

- `file.move@1.0.0` 与 R1；
- 两个规范绝对路径；
- source 版本摘要；
- 精确 capability；
- no-overwrite、版本变化拒绝和反向移动需重新审批等后果；
- 本机执行、无数据外发、仅本次有效。

拒绝、过期、取消或 preview hash 不匹配时，Runner 不会收到调用。批准后，旧 approval 被一次性消费，Runner 仍会重新读取 source 版本与 destination absent 状态，然后进入阶段 33 的 prepare/commit/receipt 边界。

## 6. 前端入口

任务表单新增任务类型选择：

- 读取磁盘容量（只读）；
- 移动单个文件（需要审批）。

文件移动模式显示 source/destination 字段与安全边界提示。两个路径为空或文本相同时提交按钮保持禁用；前端只负责基本交互校验，最终路径、卷、文件类型、目标不存在与版本检查全部由后端重新执行。

## 7. 数据与恢复边界

- `task.created` 不复制 source/destination，模型提示词也不包含路径。
- 一旦进入 Policy，规范路径会作为审批资源范围持久化，供用户审计；成功控制面 commit receipt 仍只投影版本，不复制路径。
- `tool_request` 当前属于内存任务检查点；API 重启会按既有规则取消未消费审批，不会尝试从 goal 或旧事件重建写操作。
- 批量移动、目录移动、覆盖、跨卷复制、自动撤销和模型生成路径仍未开放。

## 8. 验收

```text
Ruff:  All checks passed
mypy:  Success, 100 source files
pytest: 267 passed
Alembic: 0009_tool_commit_receipts (head)
frontend vitest: 11 files, 101 passed
frontend type-check/build: passed
```

新增覆盖包括：

- 无效 source 在任务创建前返回稳定 422，且不创建任务；
- 动态文件路径只对精确 `local_user file.move` Policy 事实开放；
- 模型身份使用同一资源时被 Policy 拒绝；
- 真实 API 创建任务后停在 `waiting_approval`，文件保持不变；
- 审批卡包含两个精确路径、source 版本、R1、reversible 与双 capability；
- 批准后真实 Runner 只移动一次并返回 committed receipt；
- 文件任务使用受信任计划模板且不产生模型调用事件；
- 前端路径未完整时禁止提交，并发送结构化 `tool_request`。

## 9. 下一步

1. 在 `unknown` Reconciliation 打开时主动调用 Runner `tool.commit_receipt.get`，将 receipt/no-receipt/查询失败作为后验证据展示，但不改写原 Tool 账本。
2. 基于 committed receipt 提供显式反向 `file.move` compensation API；必须重新检查当前 destination 版本、source absent，并创建新的 call、幂等键和一次性审批。
3. 持久化任务图、结构化工具请求与阶段检查点，为安全的跨 API 重启恢复建立可证明语义。
