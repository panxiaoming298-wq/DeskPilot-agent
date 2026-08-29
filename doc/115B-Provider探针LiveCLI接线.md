# 阶段 115B：Provider 探针 Live CLI 接线检查点

## 状态

已完成受多重授权约束的 live CLI 接线，未执行任何真实 Provider 请求。本检查点不提供 operator binding、live permit、API Key 或费用授权，也不生成 Production Admission、cloud activation 或 116C-B 结论。

上一检查点只实现 runner library，CLI 没有 `run`。本检查点新增 `run` 子命令，但默认拒绝，且不能由 CI、readiness report 或普通 manifest 自动打开。

## 命令与固定输入

未来 operator 明确授权时，命令形状为：

```powershell
$env:DESKPILOT_PHASE115_PROVIDER_PROBE_LIVE_ALLOW = "1"
python -m deskpilot.phase115_provider_probe_gate run `
  --binding <current-v2-binding.json> `
  --permit <current-live-provider-permit.json> `
  --ledger <existing-local-ledger-directory> `
  --output <new-sanitized-report.json> `
  --confirm-run-id <exact-permit-run-id>
```

这只是接口说明，不是当前运行指令。环境变量不是充分授权，单独设置它不会解析凭据或联网。

## 执行前门禁

CLI 按以下顺序 fail-closed：

1. `CI` 为 `1/true/yes` 时永久拒绝，即使专用环境变量为 `1`；
2. 要求 `DESKPILOT_PHASE115_PROVIDER_PROBE_LIVE_ALLOW=1`；
3. 严格加载当前 v2 binding 与一次性 permit，拒绝重复 JSON key、未知字段、digest 或 Schema 漂移；
4. permit 必须是 `live_provider`，不能把 `offline_mock` permit 升格；
5. `--confirm-run-id` 必须与 permit exact run ID 相等；
6. output 必须是现有非 reparse 父目录下尚不存在的 `.json` 文件；
7. 再加载 execution suite、构造 live runner，并由 runner 重新执行 readiness、预算、permit 时效和持久 claim 校验；
8. claim 成功后才允许解析 Windows CredentialReference；不发 health 请求，不经过 retry/fallback ModelGateway。

任一前置门失败都不会构造 live runner、创建报告、解析 credential 或发请求。

## 报告落盘

Runner 返回后，CLI 再次核对 report 的 policy、suite、binding、readiness、permit、run ID、execution mode 与 Provider identity。只有全部相等才以 `O_CREAT | O_EXCL` 创建最终 JSON；不存在覆盖旧报告的路径。写入后 flush + fsync，失败时 permit 已消费且不得透明重放。

stdout 只输出 run/report digest、终态、请求计数、预算预留和固定为 false 的 Production/activation/116C-B 字段。完整报告仍只含脱敏 receipt，不含 Prompt、响应正文、Header、URL、Credential identifier、API Key 或原始 native response ID。

## 自动化证明

- runner/readiness 联合门覆盖默认环境拒绝、CI + allow 仍拒绝、run-id 不匹配、输出已存在、报告 authority 漂移和不可覆盖落盘；所有 Provider 交互仍使用 MockTransport 或零网络 stub。
- GitHub Actions 显式设置 `CI=true` 和 live allow，再调用 `run`；预期退出码固定为 2，且不得生成报告文件。
- 本检查点没有在开发机设置 live allow，没有创建 live permit，没有读取 Windows Credential Manager，也没有调用 OpenAI、DeepSeek 或百炼。
- 执行专项 13/13、连同 readiness 34/34、受影响联合回归 89/89；Ruff 全仓与 strict mypy 320 个生产源码通过。
- Python compileall、60 包 `pip check`、Evaluation/Phase75 immutable compare、workflow YAML、manifest 和 diff whitespace 通过。上一完整后端基线 `890 passed + 12 skipped` 未因本批 CLI 隔离改动重复运行。

## 下一步

代码接线完成后，下一步是 operator 外部准备，而不是自动运行：

1. 百炼北京独立 Workspace/API Host；
2. 三家探针专用 Key 写入本机 Windows Credential Manager，不发到聊天或仓库；
3. 三份最长 24 小时的 v2 binding 逐一通过无网 preflight；
4. 选择只先运行哪一家，并再次明确授权该 Provider 的 15 分钟 permit 和实际费用；
5. 单家 4 次通过后再决定是否授权下一家。Production 和 116C-B 仍是后续独立门。
