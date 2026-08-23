# 阶段 106：可组合动态图 Patch/Approval 节点

## 1. 本阶段结论

阶段 106 将阶段 101～105 的单 Patch 图内批准链提升为可重复组合的节点协议。用户可以在同一个动态任务图中声明一或两个精确目标文件；服务器为每个目标签发不同的 `patch_slot_n`，模型只能为每个 Patch 节点选择一个现有槽位。槽位选择不授予写权，每个节点仍必须独立生成内容寻址 Workspace manifest 并等待用户确认。

```text
directory_context
        ↓
patch_slot_1 → preview/manifest A → confirmation A → fixed test
        ↓ server test_passed
patch_slot_2 → preview/manifest B → confirmation B → fixed test
        ↓ server test_passed
directory_output → verified join
```

当前 Task Contract 的图结构上限仍是每代 4 个 Child，因此本阶段开放最多两个 Patch/Approval 节点；这是受预算和结构上限的安全能力区域，不是任意数量的自由写图。

## 2. 服务器签发的节点输入

Turn Router rules v5 允许 `workspace_dynamic_patch_test` 的“文件”参数使用一或两个字符串组成的 JSON 数组。Router 拒绝空值、重复路径和超过两项的数组，并将规范化结果以 `patch_paths_json` 纳入 Route parameter digest。旧 rules v1～v4 的 candidate digest 仍可读。

Supervisor 从已绑定 Route 派生不可伪造的 `patch_slot_1` / `patch_slot_2`，并将每个槽位以 `AgentTaskGraphCapabilityInput v4` 提供给 Coordinator：

- 节点绑定 key；
- 精确目标文件、项目路径、固定测试路径和测试类型；
- 节点级 objective；
- Route parameter digest 与 input digest。

新 Contract 使用 `composable_patch_approval_nodes_v1` 和 `distinct_server_bound_patch_input_per_node_v1`。Supervisor 强制图精确消费全部槽位且每个槽位只出现一次；重复、遗漏、未知 key，或给非 Patch 节点附加 binding key 都会在原子封图之前拒绝，不产生 Child 工作或 Workspace 写入。

## 3. graph v8 批准槽位证明

`deskpilot.agent-task-graph.v8` 为每个 Patch 节点封存 `AgentTaskGraphApprovalBinding v1`：

- `approval_binding_id`；
- graph/local key/runtime node ID；
- exact capability input digest；
- `fresh_user_confirmation_per_node_v1`；
- `content_addressed_workspace_manifest_v1`；
- 内容寻址 approval-binding digest。

graph v8 要求 Patch 节点同时具有 CapabilityInput v4 和匹配的 approval binding，非 Patch 节点不得夹带批准槽位。Workbench 读取时重算 graph/node/input/binding 摘要和语义关系。即使攻击者同时改写 binding、node 和 graph 摘要，只要 binding 不再指向节点的 exact input digest，仍会 fail closed。

graph v1～v7 的摘要计算保持兼容；旧 CapabilityInput v1～v3 不携带 `binding_key`。

## 4. 逐节点暂停与确认

多个 Patch 节点按服务器裁决的条件边顺序执行。每次 Patch Planner 只读取当前节点 CapabilityInput 绑定的文件，生成一份新 staging manifest 后将 graph/node/Run 暂停在 `waiting_user`。用户确认后，Runtime 重验 Handoff、input binding、Decision、Observation、preview 和当前活跃 confirmation，再原子写入并运行固定测试。

第二个 preview 激活后，第一个 confirmation 不能提交第二个节点。每个已验证 Patch 仍形成独立 `patch_test` ResultRef；下游只能由 exact `test_passed=true` decision 解锁。

## 5. Replan 与跨代预算

可组合 Patch 图继续使用阶段 105 的 Replan v5 和三代总 TaskBudget。一代的双 Patch 图使用完整的每代 10 次 model-call allocation；换代前 budget proof 从持久节点重算累计值，新代仍必须为两个 Patch 节点重新签发 approval binding、Workspace manifest 和 confirmation。

固定纵向用例已覆盖：generation 1 的第二个 Patch 测试失败，用户输入“继续修复”后 generation 2 产生两个新槽位和四份互不复用的 staging/manifest/confirmation，失败 `patch_test` 仍不能导入新代。

## 6. 固定验收

本阶段新增并回归验证：

- 单 Patch graph v8 兼容闭环；
- 双 Patch 节点顺序暂停、两次独立确认、两份不同 manifest/staging 和 verified join；
- 第二节点激活后旧 confirmation 拒绝；
- 模型重复消费同一 input binding 时封图前拒绝且零写入；
- approval binding 语义篡改在同步重算内外摘要后仍拒绝；
- 双 Patch 条件失败→用户 continuation→新 generation 两个新批准槽位→成功交付；
- 阶段 105 单 Patch 失败换代、三代上限和 Replan v5 兼容回归。

最终统一后端全量收集 81 个测试文件 / 597 项，`585 passed + 12 skipped`、退出 0，耗时 2496.48 秒；Ruff 全仓和严格 mypy 240 个生产源码通过。Phase 75 首次 compare 正确拒绝了 graph/Contract 变更导致的 v14 plan/cohort 摘要漂移；审查确认 11/11、false-success=0、unauthorized-effect=0、precision/recall=1 后，以不可变前序摘要链签发 v15，compare 无违规，report digest 为 `5c0c2fb35f3bf5fcf28f8e8b521a6592c2ed69c992eb216e7783c67249463a30`。前端 22 个测试文件 / 154 项、type-check 和 production build 通过。Alembic 当前且唯一 head 为 `0050_agent_graph_test_conditions`，无待生成迁移；SQLite `integrity_check=ok`，`pip check` 和 diff whitespace 通过。本阶段没有新表或列。

## 7. 当前边界与下一步

当前开放的仍是服务器规范化的精确文件替换和固定 Python/Node 测试；不支持自由 Shell、动态 executable/argv、联网安装、目录删除、覆盖或模型自选写路径。

下一阶段优先引入 live model/Judge-human cohort，校准 Coordinator 对 server-offered input bindings 的图提案质量、Patch Planner 的修复质量与受控 retry 行为；同时保持 External Oracle、false-success=0、unauthorized-effect=0、固定执行边界与总预算守恒。
