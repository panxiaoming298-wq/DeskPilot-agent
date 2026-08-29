# 阶段 115B：Provider 探针 Runner 离线实现检查点

## 状态

已完成 runner 的离线实现与 MockTransport 验证，未执行真实 Provider 请求。本检查点不授权联网、凭据解析、费用消费、Production Admission、cloud activation 或 116C-B。

116C-A 已在更早的提交中完成 8 个仓库、20 个任务和 60 个 trial 的冻结，因此本批没有重复创建真实仓库任务；它实现的是 ADR-019 之后、真实探针运行之前缺失的执行安全层。

## 不可变执行套件

新增 `deskpilot.provider-probe-execution-suite.v1`，绑定 ADR-019 的 v2 policy digest：

```text
probe policy: 0b221968240375def2ee886c4f73e937bf399db3f7009d86330892ed7c58a141
execution suite: 5096f22c0d600a1282d6121437476dec2999be45bad93b09c8003d623fa1f326
```

套件只含公开合成 strict JSON：非流式和流式各重复 2 次，每家恰好 4 次。输入继续受 4096 字符上限约束，输出固定最多 256 tokens，`store=false`，不允许 tool、external retrieval、服务端会话状态、自动重试或隐藏重试。

## 一次性执行许可

`deskpilot.provider-probe-execution-permit.v1` 必须同时绑定：

- v2 policy、execution suite、operator binding 和当次 readiness report 的 digest；
- exact Provider family、Provider ID、4 次请求和本轮最大预算预留；
- `offline_mock` 或 `live_provider` 模式及其精确授权布尔值；
- operator 的固定确认短语、身份、批准时间和最长 15 分钟有效期；
- 固定为 false 的 Production Admission、cloud activation 与完整 116C-B 权限。

许可 JSON 使用严格 loader，拒绝重复 key、未知字段、符号链接、过大文件、digest 漂移和过期时间；Runner 构造时还会重算 policy/suite bundle digest，不信任调用方手工拼装的 bundle。Runner 在解析凭据前，以 `O_CREAT | O_EXCL` 在 operator-staged 本地 ledger 创建不可覆盖的 claim 标记；claim key 绑定 policy、suite、binding 与 Provider，因此同一 24 小时 binding 即使换一份 permit 也只能执行一轮。claim 后即使凭据或 Provider 构造失败也不能重用，避免崩溃后重复计费。

## 执行与预算顺序

Runner 的固定顺序为：

1. 重新运行 24 小时 binding readiness，并绑定本次时间产生的 report digest；
2. 校验 15 分钟 permit、execution mode、Provider identity 和本轮预算；
3. 原子消费 permit；
4. 仅 `live_provider` 模式在此时解析一次 Windows CredentialReference；`offline_mock` 模式拒绝任何 credential resolver；
5. 逐请求先按该 Provider 的单请求上限保守预留，再直接调用 exact adapter；
6. 串行执行，首个错误、usage 缺失、模型/响应身份漂移、strict JSON 不匹配或 permit 运行中到期立即停止；
7. 不经过带 retry/fallback 的 ModelGateway 路径，`max_attempts=1`、retry delay 为 0。

每家 4 次的最大预留仍为 OpenAI USD 1、DeepSeek USD 0.4、百炼 CNY 8 等值的 microunit 计数；它们没有扩大 ADR-019 的费用授权。

## 脱敏证据

每个请求 receipt 只保存 request digest、case/顺序、预算预留、usage、latency、native response ID digest、structured output digest 或归一化错误码。最终 report 不保存：

- Prompt、响应正文或 strict JSON 原文；
- Base URL、Header、API Key 或 Credential identifier；
- 原始 native response ID；
- Provider 错误正文。

报告继续固定 `production_admission=false`、`cloud_activation=false`、`full_116c_b=false`。Mock report 还固定 `credentials_resolved=false`、`network_request_count=0`、`real_model_capture=false`。

## 当前可达性边界

代码中存在 `LiveProviderProbeFactory` 作为受约束 HTTP composition，但应用和 CLI 都没有接线。`python -m deskpilot.phase115_provider_probe_gate` 仍只有 `manifest/preflight`，`run` 命令继续不存在；manifest 显示 library 已实现，同时明确 `live_run_cli_available=false` 和全部现行 execution boundary 为 false。

因此，本批只证明 runner 合约在离线 MockTransport 下可执行，不能证明 OpenAI、DeepSeek 或百炼真实兼容，也没有产生真实模型 capture、费用或成功率。

## 验收

- 覆盖三家各 4 次的精确非流式/流式顺序、一次性 permit、首个 429 停止且零重试、usage 缺失 fail-closed、过期 permit 在 claim 前拒绝、严格 suite/permit loader 与脱敏报告。
- 执行专项 9/9、连同 readiness 30/30、完整受影响联合回归 85/85；Ruff 全仓与 strict mypy 320 个生产源码通过。
- Python compileall、`pip check`、Evaluation/Phase75 immutable compare、workflow YAML、manifest 与 diff whitespace 通过。上一检查点的完整后端基线 `890 passed + 12 skipped` 未因本批隔离新增代码重复运行。
- frozen CI 增加执行专项，并验证 wheel 包含 execution suite YAML；本机仍不临时安装缺失的 build backend。

## 下一步

下一步仍不是 116C-B。operator 需要先在本机完成百炼北京 Workspace/API Host、三家专用 Windows credential 和三份当前 v2 binding；随后应单独确认是否给某一家签发 15 分钟 `live_provider` permit，并为 live CLI/报告原子落盘另开检查点。任何一次真实运行都必须再次取得明确授权。
