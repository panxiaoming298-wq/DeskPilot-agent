# ADR-018：三 Provider 探针授权与离线就绪门

## 状态

已接受，2026-08-29 起生效。本 ADR 落实 ADR-017 的已批准上限，但本 checkpoint 本身不授权网络请求。

## 背景

OpenAI、DeepSeek 和阿里云百炼的 Responses 公共 adapter 已通过离线合约，但在真实调用前仍缺 exact model、Base URL、credential reference、Key/URL 配对、当前价格和 Provider 控制台硬限额等 operator 事实。把这些事实直接塞进环境变量后运行，会让“配置错误”和“模型不兼容”混在同一次付费请求中，也无法证明请求数和重试策略没有漂移。

官方 OpenAI 文档还表明 SDK 可能默认重试可恢复错误；DeskPilot 的兼容探针已获批为零自动重试，因此不能依赖 SDK 默认值。Responses 在 `store=false` 下可以无状态使用，未来证据只应保存公开配置摘要、usage、latency 和请求/响应标识摘要，不记录提示、正文、Header 或密钥。

## 决策

### 1. 冻结不具执行权的策略 manifest

新增 `deskpilot.provider-probe-policy.v1`，digest 为：

```text
51b9b24743508f6546f37e3274e0a8f748b2424369c6c2e93b3449ab1472bb47
```

策略只包含 `public_synthetic` 数据，禁止仓库内容，且固定两个用例：

1. strict JSON Schema 非流式，重复 2 次；
2. strict JSON Schema 流式，重复 2 次。

因此每家计划 4 次、三家共 12 次。批准的硬上限仍为：

| Provider | 计划请求 | 最大请求 | 总费用上限 | 单请求上限 | 自动/隐藏重试 |
| --- | ---: | ---: | ---: | ---: | --- |
| OpenAI | 4 | 16 | USD 5 | USD 0.25 | 0 / 禁止 |
| DeepSeek | 4 | 10 | USD 2 | USD 0.10 | 0 / 禁止 |
| 阿里云百炼 | 4 | 10 | CNY 20 | CNY 2 | 0 / 禁止 |

最大请求数是不可突破的授权上限，不是要消耗完的配额。当前计划只使用 12/36。

### 2. exact operator binding 最长有效 24 小时

每家真实探针前必须单独创建 `deskpilot.provider-probe-operator-binding.v1`，内容只允许：

- policy digest、Provider family/ID、exact model 与 HTTPS Base URL；
- `CredentialReference`，不得包含 secret；
- 与策略完全一致的币种、总额、单请求上限、最大请求数和 `automatic_retries=0`；
- operator 对 exact model、凭据存在、Key/Base URL 配对、控制台硬限额和当前价格来源的显式确认；
- `confirmed_by`、带时区时间、最长 24 小时有效期和内容摘要。

OpenAI model 必须由 operator 根据账户当前可用模型明确填写；本策略不替用户猜。DeepSeek 当前只接受策略冻结的 `deepseek-v4-flash`，百炼只接受 `qwen3.8-max`。模型变化时应版本化更新策略，而不是在 binding 中绕过。

### 3. Provider-specific Base URL 与凭据命名

- OpenAI 只接受 `https://api.openai.com/v1`；
- DeepSeek 只接受 `https://api.deepseek.com`；
- 百炼只接受业务空间专属的 `https://{WorkspaceId}.{region}.maas.aliyuncs.com/compatible-mode/v1`，地域限当前策略列出的北京、新加坡、弗吉尼亚、法兰克福和东京；
- 百炼显式拒绝 `token-plan`、`trial` 和 `coding-plan` 前缀，防止不同计费方案的 Key/Base URL 混用；
- environment credential identifier 固定为 `DESKPILOT_CREDENTIAL_OPENAI_RESPONSES`、`DESKPILOT_CREDENTIAL_DEEPSEEK`、`DESKPILOT_CREDENTIAL_BAILIAN`；Windows Credential Manager 对应 `OPENAI_RESPONSES`、`DEEPSEEK`、`BAILIAN`。

### 4. readiness report 仍然没有网络权力

`ProviderProbeOfflinePreflight` 只加载 manifest 与 binding，构造 disabled 的公开 Provider config 并输出摘要。report 固定：

- `network_access=false`；
- `credentials_resolved=false`；
- `real_model_capture=false`；
- `production_admission=false`；
- `cloud_activation=false`。

report 不包含 Base URL 或 credential identifier，只保存它们的绑定摘要。`ready=true` 仅表示 operator 输入与冻结策略一致，不代表 endpoint 可达、模型兼容或已获准立即联网。

CLI 只有：

```powershell
python -m deskpilot.phase115_provider_probe_gate manifest
python -m deskpilot.phase115_provider_probe_gate preflight `
  --binding <operator-binding.json> --now <ISO-8601>
```

没有 `run`、`capture` 或 `activate` 子命令。

## 未来真实探针仍需实现/检查

1. operator 安全提供三份短期 binding 和真实 credential reference；
2. live runner 必须显式禁用重试，并在请求前、每次请求后重验剩余请求数/费用；
3. 固定 `store=false`，不发送 repository/private data、内部 metadata 或托管工具；
4. 保存 Provider/model/config、usage、latency、响应 ID 摘要，以及上游提供时的 request ID 摘要；正文与 Header 不入日志；
5. 每家第一个请求失败时停止该家剩余请求，不跨 Provider fallback；
6. compatibility 通过仍不生成 personal preview、Production Admission、activation 或 116C-B 结论。

## 官方依据

- [OpenAI Responses：Create a model response](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [OpenAI API：请求日志、request ID 与重试](https://developers.openai.com/api/reference/ruby)
- [DeepSeek：Using the Responses API](https://api-docs.deepseek.com/guides/responses_api/)
- [阿里云百炼：创建响应](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses)
- [阿里云百炼：Base URL 总览](https://help.aliyun.com/zh/model-studio/base-url)

## 后果

- 下一次联网前可以把配置、计费方案和 operator 确认错误全部离线暴露。
- 探针实际请求数从“上限 36”收敛为“计划 12”，降低个人开发成本。
- readiness 不解析凭据、不联网，因此仍严格满足用户暂不执行真实 capture/Admission/activation 的要求。
- 真正的 live runner 和证据工件仍是下一 checkpoint，不能由本 ADR 推断完成。
