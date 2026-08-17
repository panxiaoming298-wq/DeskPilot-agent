# 阶段 71：独立 Verification 与 Artifact/Browser 交付闭环

## 1. 已完成范围

阶段 71 将 `research_to_html` 从候选研究结果推进为首个可验证交付闭环：

1. 新增独立 `ModelRole.VERIFIER` Citation 语义 grader。可信 reducer 在 grader 前重验 AgentResult、Claim、Citation、PageSnapshot 的 digest、Task 血缘、locator 原文和来源下限；grader 只生成 observation，不能改 Node。
2. 新增 `VerificationRun`、immutable Evidence Snapshot 和逐 Claim Verdict。只有 reducer 产生的 `verified` 才会将 Research Node 置为 `verified`。
3. 扩展 Execution Run/Node/Invocation/Research 状态机，并实现通用 verified-edge reducer。目标 Node 的全部入边源节点都是 `verified` 时才会进入 `ready`；`partial`、candidate、网页文本和 OTel 都不能解锁。
4. 新增 Task Artifact Workspace，使用受控根目录、固定相对路径、扩展名/字节配额、内容寻址 blob、immutable ArtifactRevision 和可重算 PatchReceipt。
5. HTML Builder 只消费 verified Claim/Citation，使用确定性静态模板生成单页 HTML；包含 charset、viewport、lang、heading、严格 CSP，不包含 JavaScript、CDN、远程字体或热链资源。
6. 隔离 Browser Verifier 每次创建新的 Chromium/Edge profile，禁用 JavaScript/扩展/同步/后台网络，并用 host resolver 拒绝外网。启动前还会确定性拒绝 script、远程资源、重复 ID、缺失 CSP/标题/语言/viewport 等问题；记录 DOM 和截图 digest。
7. Final Acceptance 重验 verified Claim、当前 Artifact revision、Browser revision 一致性和零外网不变量，然后生成 DeliveryManifest 并将 Task/Run 提升为 `succeeded`。

## 2. 显式 API

```text
POST /api/v1/execution-runs/{run_id}/claims:verify
GET  /api/v1/execution-runs/{run_id}/claim-verification
POST /api/v1/execution-runs/{run_id}/artifacts:build
GET  /api/v1/task-workspaces/{workspace_id}
GET  /api/v1/patch-receipts/{patch_receipt_id}
POST /api/v1/execution-runs/{run_id}/browser:verify
GET  /api/v1/execution-runs/{run_id}/browser-verification
POST /api/v1/execution-runs/{run_id}/final-acceptance:run
GET  /api/v1/execution-runs/{run_id}/delivery
```

所有投影使用 `Cache-Control: no-store`。这些命令故意分步暴露，以便测试和 UI 清楚展示每道验证门；任何越级调用都返回稳定 409 Problem Details。

## 3. 工作区与浏览器配置

```text
DESKPILOT_ARTIFACT_WORKSPACE_ROOT=./data/task-workspaces
# 可选；默认自动发现 Windows Edge/Chrome 或 Linux Chromium/Chrome
DESKPILOT_BROWSER_EXECUTABLE_PATH=...
```

Browser 可执行文件缺失、超时或渲染失败时 fail closed，不会退化为“仅 HTML 解析即通过”。

## 4. 持久化与恢复边界

`0032_verified_artifact_delivery` 新增：

```text
verification_runs
verification_evidence_snapshots
claim_verdicts
task_artifact_workspaces
artifacts
artifact_revisions
artifact_patch_receipts
browser_render_runs
delivery_manifests
```

Artifact blob 先以 exclusive create 写入内容寻址存储，再在单一数据库事务中写 revision/receipt 并激活。数据库事务失败可留下未引用 blob，但不会产生假 active revision；后续 retention/reconciliation 可清理这类 orphan。

## 5. 自动化门禁

`tests/test_verified_artifact_delivery.py` 覆盖未验证越级拒绝、逐边解锁、完整交付、PatchReceipt/revision 绑定、Citation 篡改拒绝和 HTML 外部资源/script 对抗。迁移往返、Ruff、mypy、冻结 evaluation baseline 和 lock/Alembic check 由阶段 71 CI 工作流程统一执行。

## 6. 明确未完成

- 尚无 Conversation/Workspace 统一前端工作台；本阶段提供可验证 API 投影。
- 尚无用户路径导出/覆盖；交付仅激活受控 Workspace 内 revision。
- HTML v1 仍是无 JavaScript 单页 profile；不执行 npm、bundler、Shell 或任意生成代码。
- Browser Verifier 的基础规则不代表完整 WCAG 合规。
