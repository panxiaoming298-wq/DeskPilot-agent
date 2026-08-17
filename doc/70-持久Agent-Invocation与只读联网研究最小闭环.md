# 阶段 70：持久 Agent Invocation 与只读联网研究最小闭环

## 1. 已完成范围

阶段 70 把 `research_to_html` 的前半段从禁用声明推进为显式开关控制的真实运行链：

1. `research.read.v1@1.0.0` 永久保持禁用；新增 `1.1.0`，只有启动配置和 SearchProvider 同时存在时才启用。
2. 研究计划节点由普通 Capability 节点改为精确绑定 `builtin.web_researcher@1.0.0` 的 Agent 节点，Agent/Prompt/Capability digest 都进入 Executable Plan 与 Handoff。
3. 数据库持久化 Execution Run/Node/Edge、Handoff、Invocation、Agent Model Turn、Agent Result，以及 Research Session/Search Call/Page Snapshot/Claim/Citation。
4. 调度采用 `ready -> claimed -> running`、数据库 lease、owner 和 fencing token；并发上限固定为 3。过期 dispatch 转为 `outcome_unknown`，旧 owner/fence 不能提交结果。
5. SearchProvider 与 ModelGateway 是两个独立端口。SearXNG adapter 只返回外部不可信 SearchHit；模型不能选择或授权搜索后端。
6. PageReader 只允许无凭据 HTTP(S)，拒绝非公网、loopback、private、link-local、metadata 和混合 DNS 结果；连接固定到已校验 IP，同时保留 Host/SNI，并在每次 redirect 重新校验。正文限制 MIME、编码、大小和超时，不执行 JavaScript。
7. Agent Model Loop 每次只接受一个严格 `ResearchAgentDecision`。模型输出没有 `success`/`verified` 字段，只能引用已提供的 Page Snapshot ID。
8. Research Claim、Citation Evidence 和 Agent Result 都只能以 `awaiting_verification`/`candidate` 写入。研究完成不会解锁 HTML Builder，也不会把顶层 Task 改为成功。

## 2. 启用方式

默认配置仍然不联网：

```text
DESKPILOT_RESEARCH_RUNTIME_ENABLED=false
```

真实启用需要显式设置 SearchProvider：

```text
DESKPILOT_RESEARCH_RUNTIME_ENABLED=true
DESKPILOT_RESEARCH_SEARCH_BASE_URL=https://your-searxng.example
```

有限模型费用预算还要求在 `DESKPILOT_MODEL_GATEWAY_POLICY` 中配置所选 Provider 的 pricing；缺失时 fail closed，不会绕开费用门禁。

## 3. 显式 API

```text
POST /api/v1/tasks/{task_id}/execution-runs
POST /api/v1/execution-runs/{run_id}/research:run
GET  /api/v1/execution-runs/{run_id}
GET  /api/v1/tasks/{task_id}/execution-runs
GET  /api/v1/research-sessions/{research_session_id}
```

命令只在研究运行时启用后可用；默认返回 503。读取投影均为 `Cache-Control: no-store`。

## 4. 证据与恢复边界

- 普通运行表只保存模型 request/response digest、Provider、Model、Token/费用和稳定错误码，不保存 Prompt 正文。
- Page Snapshot 保存受大小限制的外部不可信正文，因为 Citation locator 与后续独立验证需要该证据。
- Search Call 只保存 query digest 与有界 SearchHit；不把搜索词复制到普通审计字段。
- `dispatching` lease 过期必须进入 `outcome_unknown`；不能猜测 Provider 是否处理成功。
- 阶段 70 没有 Completion Verification，所以 verified edge 永不满足，Builder/Browser/Delivery 都保持阻塞。

## 5. 自动化门禁

`tests/test_agent_research_runtime.py` 覆盖能力版本开关、完整研究前半链、候选状态边界、顶层 Task 不变、陈旧 fence 拒绝，以及 SSRF 地址拒绝。`0031_agent_research_runtime` 有 upgrade/downgrade/Alembic check 往返门禁。

CI 工作流 `phase-70-agent-research-runtime-gate.yml` 同时运行专项测试、Ruff、mypy、迁移/锁文件校验，以及阶段 67 已冻结的 evaluation baseline compare；CI 不允许 record 新基线。

## 6. 明确未完成

阶段 71 才实现独立 Claim/Citation Verification、Artifact Workspace、HTML Builder、PatchReceipt、隔离 Browser Verifier 和最终交付。当前不能把 `candidate`、网页原文、模型摘要、OTel Span 或离线评测当作 verified truth。
