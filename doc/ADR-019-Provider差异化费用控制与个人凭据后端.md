# ADR-019：Provider 差异化费用控制与个人凭据后端

## 状态

已接受，2026-08-29 起生效。本 ADR 版本化补充 ADR-018；不授权联网、凭据解析、真实模型 capture、Production Admission 或 cloud activation。

## 背景

operator 已确认三家 API 账号存在，接受 OpenAI `gpt-5.6-luna`，并接受在个人 Windows 开发机使用系统凭据管理器。阿里云百炼尚未选择 Workspace；operator 可访问多个地域，但各地域的 API Key、Base URL 和模型列表不能混用。

ADR-018 v1 将三家统一要求为“Provider 控制台硬费用上限”。官方资料显示三家的可证明控制不同：OpenAI 有项目级 hard spend limit；DeepSeek 以充值/赠送余额扣费并在余额不足时拒绝请求；百炼提供费用告警、免费额度用完即停（适用时）和欠费停服，但按量账单存在出账延迟。继续要求三家勾选同一个布尔值会迫使 operator 对不存在的能力作虚假确认。

## 决策

### 1. 保留 v1，新增不可变 v2

`phase115_provider_probe_policy_v1.yaml` 保留不改，v1 canonical digest 仍为：

```text
51b9b24743508f6546f37e3274e0a8f748b2424369c6c2e93b3449ab1472bb47
```

默认 loader 改为加载 `deskpilot.provider-probe-policy.v2`，canonical digest 为：

```text
0b221968240375def2ee886c4f73e937bf399db3f7009d86330892ed7c58a141
```

operator binding 与 readiness report 同步升级为 v2；v1 binding 不能冒充 v2 输入。

### 2. 固定个人探针选择

| Provider | exact model | Base URL / 地域 | Windows Credential identifier |
| --- | --- | --- | --- |
| OpenAI | `gpt-5.6-luna` | `https://api.openai.com/v1` | `OPENAI_RESPONSES` |
| DeepSeek | `deepseek-v4-flash` | `https://api.deepseek.com` | `DEEPSEEK` |
| 阿里云百炼 | `qwen3.8-max` | 北京业务空间专属 `/compatible-mode/v1` | `BAILIAN` |

百炼只接受 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。新加坡、东京、法兰克福、弗吉尼亚以及 DashScope、Trial、Token Plan、Coding Plan URL 都不满足本版本 binding；改变地域必须发布新策略版本。

真实 Key 只能写入 Windows Credential Manager，不进入 binding、日志、仓库、环境文件或聊天。`CredentialReference` 只保存 `windows_credential_manager` 与上述 identifier。

### 3. Provider-specific 费用控制

每家都必须确认探针专用 Key 和应用侧预算包络；Provider 侧证据分别为：

- OpenAI：优先 `openai_project_hard_limit`，必须确认 enforcement 为 `enforcing`；若个人账户暂时无法启用，可显式选择 `openai_application_envelope`，不得伪报 hard limit。
- DeepSeek：固定 `deepseek_prepaid_balance`，operator 必须在 binding 前 24 小时内确认可用余额；这不是按 Key 的 hard limit。
- 百炼：固定 `bailian_billing_alert`，必须确认费用告警并承认后付费出账延迟；存在适用免费额度时建议开启“用完即停”，但不得把告警称为 hard limit。

三家计划仍各 4 次、共 12 次，费用上限不变。按“计划请求数 × 单请求上限”计算的本轮应用侧包络分别为 OpenAI USD 1、DeepSeek USD 0.4、百炼 CNY 8；更大的 USD 5 / USD 2 / CNY 20 仍只是不可突破的外层总上限。

### 4. 未来 runner 的共同护栏

v2 额外冻结：

- 每个用例最大输入 4096 字符、最大输出 256 tokens；
- `store=false`，禁用 tools 和 external retrieval；
- 串行执行、零自动/隐藏重试、首错停止；
- usage 缺失即停止；
- 不记录请求/响应正文或 Header。

这些护栏现已由 [115B Provider 探针 Runner 离线实现检查点](115B-Provider探针Runner离线实现.md) 落成独立 library：新增最长 15 分钟的一次性执行许可、持久 claim、逐请求保守预算预留和脱敏 receipt/report，并以 MockTransport 验证三家各 4 次的串行执行。当前 CLI 仍只有 `manifest/preflight`，没有 `run`；现行 readiness report 仍固定 `network_access=false`、`credentials_resolved=false`、`real_model_capture=false`、`production_admission=false`、`cloud_activation=false`。

## 仍需 operator 后续完成的事实

本 ADR 的离线实现不再需要用户输入。真正准备探针时仍需：

1. 在百炼北京地域创建独立 `deskpilot-probe` Workspace，复制其 API Host；
2. 三家分别创建探针专用 Key，并由 operator 在本机安全写入 Windows Credential Manager；
3. 在最长 24 小时的 binding 中确认当期价格、DeepSeek 余额、百炼费用告警/账单延迟，以及 OpenAI hard limit 或显式应用侧 fallback；
4. 再次单独授权 live runner 的接线与运行。library 已实现不等于 CLI 已接线，更不等于运行。

## 官方依据

- [OpenAI：GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [OpenAI：Retrieve project spend limit](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/projects/subresources/spend_limit/methods/retrieve)
- [DeepSeek：Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)
- [DeepSeek：Error Codes](https://api-docs.deepseek.com/quick_start/error_codes/)
- [阿里云百炼：Base URL 总览](https://help.aliyun.com/zh/model-studio/base-url)
- [阿里云百炼：地域与接入域名](https://help.aliyun.com/zh/model-studio/regions/)
- [阿里云百炼：账单查询与成本管理](https://help.aliyun.com/zh/model-studio/bill-query-and-cost-management)
- [阿里云百炼：qwen3.8-max](https://help.aliyun.com/zh/model-studio/qwen3-8-max)

## 后果

- 不再要求 operator 对三家并不存在的统一 hard-limit 能力作虚假声明。
- Windows 凭据管理器成为本轮个人探针唯一允许的 secret backend。
- `ready=true` 仍只说明公开配置与证据满足 v2；它不授予联网权限。
- 百炼北京 Workspace/API Host 与三家真实 Key 尚未配置，本轮真实请求和费用继续为零。
