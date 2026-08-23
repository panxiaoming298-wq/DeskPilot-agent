# 阶段 104：对话续修意图与 Replan 用户消息证明

## 1. 本阶段结论

阶段 104 将阶段 103 的 Workbench 换代按钮提升为真正的连续对话动作。动态 Patch/Test 因服务器 `test_passed=false` 阻断后，用户可以直接发送“继续修复”；系统不会创建一个无关 replacement Task，而是在原 Task、原 Contract 和原失败谱系上执行同一个一次性 Replan。

按钮与对话不是两套授权协议。两者都会先保存一条精确、active、`role=user` 的 Conversation Message，再生成内容寻址 continuation intent，并由 Replan v4 引用：

```text
false condition decision
          ↓
Workbench exposes one replan action
          ↓
user button ───────────────┐
                           ├─ persist exact user message
user says “继续修复” ──────┘
          ↓
continuation intent v1
          ↓
Replan v4 → generation 2
          ↓
fresh Patch manifest + fresh approval
```

## 2. 严格、确定性的意图分类

Continuation 不使用模型、Memory、Summary 或模糊语义推断。服务器只接受一个小型冻结词表，经 trim、空白归一化、case-fold 和末尾标点归一化后匹配：

- `继续修复`；
- `生成新计划代`；
- `重新规划并继续修复`；
- `按新计划继续修复`；
- `continue repair`。

它们统一映射为 `continue_failed_patch_repair`。`继续`、`再试一次`、`修复` 等缺少明确失败修复含义的短语不会授予换代。完整的新任务指令仍按既有 conversation replacement Task 规则处理。

只有当前 Workbench 同时满足以下条件时，该意图才会留在原 Task：Route 必须是 `workspace_dynamic_patch_test`，服务器必须公开 enabled `replan_failed_execution`，且失败仍是阶段 103 证明的 condition failure。否则不会把文本强行解释为 Replan。

## 3. Continuation Intent v1

`AgentReplanContinuationIntent` 绑定：

- 原 Task ID；
- 精确 user message ID 和 message digest；
- 固定 intent code；
- `conversation_turn` 或 `workbench_action` 来源；
- intent 自身摘要。

Workbench 按钮会持久化等价的用户动作文本“生成新计划代”，因此按钮也不再只是瞬时 HTTP 信号。自然语言入口保存用户原文“继续修复”。两者都出现在同一 Conversation transcript 中，并作为 Replan 证明的一部分。

## 4. Replan v4 与服务器重验

条件失败的新换代使用 `deskpilot.agent-replan.v4`。它继续包含阶段 103 的 failure snapshot v2、Repair Advice v2 和 false decision digest，并新增必需的 `continuation_intent`。服务器创建和每次读取 Replan 时都会重验 Conversation Message：

- message 存在且绑定相同 Task；
- `role=user`、`status=active`、正文内联且没有 content ref；
- 从持久字段重新计算的 message digest 与 intent 完全一致；
- 重新运行确定性分类后仍得到同一个 intent code。

删除消息、修改正文、替换摘要、跨 Task 引用、assistant 冒充用户或把模糊短语写入 intent 都会以 `PLANNING_PROOF_REJECTED` fail closed。该证明只允许 execution-control 换代，不改变 Repair Advice 的空 grants，也不替代下一 Patch 的确认。

## 5. 同一 Task 的连续对话语义

`POST /tasks/{task_id}/conversation-turns` 在识别到明确续修且动作仍 eligible 时返回同一个 `task_id`，Plan generation 从 1 变为 2。它不会停止旧失败 Run 后创建新 Task，也不会重新分类出一个不相关 Route。旧 Plan/Run/graph/patch receipt/false decision 仍不可变，新代继续由后台安全推进器运行到新的 Patch approval。

重复发送 continuation 或再次点击按钮时，enabled action 已消失，因此不能创建第二个相同 Replan。阶段 103 的旧确认拒绝、新 manifest、新 confirmation 和逐 Patch 批准边界保持不变。

## 6. 兼容性与数据库

Replan v1/v2/v3 均保持摘要兼容：

- v1：旧目录失败快照，无 Repair Advice；
- v2：旧目录失败快照与无授权 Repair Advice；
- v3：阶段 103 条件失败快照与 Advice，但没有消息证明；
- v4：条件失败证明加精确 continuation intent。

只有 v4 必须包含 continuation；旧版本包含该字段会被拒绝。Supervisor 的跨代 ResultRef offer 同时接受 v2/v3/v4，并继续排除条件失败的 `patch_test` source。

本阶段只扩展不可变 JSON manifest，没有新增表或列。Alembic 当前且唯一 head 继续为 `0050_agent_graph_test_conditions`。

## 7. 验证结果

- Workbench 按钮和对话“继续修复”两条完整失败→换代→新批准→通过路径均通过；
- 两种来源都生成 Replan v4，并绑定各自精确 user message；
- 删除 continuation message 后 Replan 读取返回 409，恢复证明后可继续；
- 模糊短语不产生 continuation intent；
- 重复换代、旧确认重放、false decision 篡改和失败 ResultRef 导入仍被拒绝；
- 旧只读 Replan、Workspace staging/recovery 和 graph v1～v7 组合回归通过；
- 后端 pytest 全量收集 81 个测试文件 / 591 项，统一首轮退出 0，12 个既有平台条件 skip；
- Ruff 全仓和严格 mypy 240 个生产源码通过；
- Phase75 11/11、false-success=0、unauthorized-effect=0，v14 baseline compare 无违规；
- 前端 22 个测试文件 / 154 项、type-check 和 production build 通过；
- Alembic 当前且唯一 head 为 `0050_agent_graph_test_conditions`，autogenerate 无待生成操作；SQLite `integrity_check=ok`，`pip check` 与 diff whitespace 通过。

## 8. 下一步

系统已经支持一次真正由对话驱动的失败续修，但 generation 上限仍固定为 2。如果第二代测试仍失败，用户不能继续第三次。下一阶段应把一次性规则升级为有硬上限和总预算守恒的多 generation 修复循环：每一次都必须有新的 active user message、false decision、Plan/Run/graph、Workspace manifest 和 Patch confirmation，且任何一环都不能从前代继承写权限。
