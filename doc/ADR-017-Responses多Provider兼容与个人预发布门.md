# ADR-017：Responses 多 Provider 兼容与个人预发布门

## 状态

已接受，2026-08-29 起生效。本 ADR 只授权离线实现和测试，不授权真实模型调用、凭据配置、费用消费、Production Admission、cloud activation 或完整 116C-B。

## 背景

DeskPilot 原有 `openai_compatible_chat` adapter 能覆盖本地 Ollama 和部分 Chat Completions 服务，但不能把“换一个 base URL”误当成跨厂商兼容：OpenAI、DeepSeek 与阿里云百炼当前都提供 Responses API，三者在支持参数、状态保存、事件和结构化输出能力上仍有差异。

个人开发阶段也不适合为了每次低风险预览都组织两名真人主审和一名仲裁人；但 Production 的证据强度不能因此降低。因此需要在既有生产门之外增加一个不可激活、短期有效的个人预发布门。

## 决策

### 1. 使用保守的 Responses 公共子集

新增 `openai_compatible_responses` 配置和 adapter，统一使用 `POST /responses`，并固定以下边界：

- 请求只携带文本消息、`temperature`、`max_output_tokens`、流式开关和 `text.format` strict JSON Schema；
- 固定 `store=false`，不依赖服务端 conversation、`previous_response_id`、托管工具或厂商私有状态；
- 不把 task ID、workspace 路径或其他内部 metadata 发给 Provider；
- 返回的 model 必须与配置的 exact model 相同，模型别名漂移 fail closed；
- 遍历整个 output 数组提取 `output_text`，不假设第一项就是 message；
- SSE 按语义事件和单调 `sequence_number` 验证，以 `response.completed`、`response.incomplete` 或失败事件收口，不依赖 `[DONE]`；
- refusal、content filter、非 completed 状态、结构化输出不合规、超限响应和脱敏 HTTP 错误继续进入统一失败模型。

adapter 不宣称覆盖图像、文件、工具调用、托管搜索、后台任务或服务端多轮状态。DeskPilot 的持续对话仍由本地不可变消息历史和 checkpoint 负责。

### 2. 三家 Provider 都先保持 disabled

离线 MockTransport 合约覆盖以下 profile，但不等于真实厂商已验收：

| Provider | Responses base URL 模板 | 离线 profile | 当前状态 |
| --- | --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | exact approved model | disabled，未实调 |
| DeepSeek | `https://api.deepseek.com` | `deepseek-v4-flash` | disabled，未实调 |
| 阿里云百炼 | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `qwen3.8-max` | disabled，未实调且地域未确认 |

百炼的 Workspace ID、地域、Base URL 和 API Key 必须来自同一业务空间/计费方案；不得把 Token Plan、Coding Plan、试用或按量付费的 Key 与 URL 混用。实际接入时以控制台显示的当前地址为准。

只含凭据引用的 disabled 配置示例：

```json
[
  {
    "kind": "openai_compatible_responses",
    "enabled": false,
    "provider_id": "deepseek-v4-flash",
    "display_name": "DeepSeek V4 Flash",
    "model": "deepseek-v4-flash",
    "base_url": "https://api.deepseek.com",
    "location": "cloud",
    "credential_ref": {
      "backend": "environment",
      "identifier": "DESKPILOT_CREDENTIAL_DEEPSEEK"
    }
  }
]
```

百炼配置使用同一 `kind`，但必须先把 `{WorkspaceId}`、地域、model 和 `DESKPILOT_CREDENTIAL_BAILIAN` 引用替换为该账户的精确值。API Key 不得出现在 JSON、工件、日志或 Git 中。

### 3. 增加 `personal_preview`，不修改 Production 门

个人预发布 bundle 固定要求：

- 使用完整 Calibration v3 三角色 suite、真实 cloud Candidate 和独立 Judge 证据；
- 只允许 `public_synthetic` 数据，禁止本地私有仓库、秘密或个人数据出站；
- 全部样本由同一名 `operator_ref` 真人主审，不能混入第二评审人或仲裁人；
- acceptance 与 Judge-human agreement 都为 100%，primary disagreement、false accept 和安全失败均为 0；
- 最长有效期 14 天，工件不可覆盖，`activates_runtime=false`；
- 不生成 Agent Admission，不满足 Production Admission，也不解锁 cloud cohort 或 116C-B。

Production 规则维持原样：每个样本必须有两名独立真人 primary reviewer；只有发生分歧时才需要第三名 arbiter。个人开发者无需为了本地/个人预览长期配置三个人，但一旦要对外发布、无人值守启用或宣称生产质量，就必须另行满足 Production 门。

个人预发布工件只能在已有 suite/run/packet/Judge/review 工件上离线构建：

```powershell
python -m deskpilot.phase115_personal_preview_gate `
  --suite <suite.json> --run <run.json> --packet <packet.json> `
  --judge <judge.json> --reviews <one-reviewer-reviews.json> `
  --operator-ref reviewer_operator_owner `
  --issued-at <ISO-8601> --valid-until <within-14-days> `
  --output <new-preview-bundle.json>
```

### 4. 费用与出站授权分层

用户已同意以下建议上限；它们是未来实调的硬上限，不代表本 checkpoint 已发起请求：

| 批次 | 数据范围 | 总上限 | 单请求上限 | 最大请求数 | 自动重试 |
| --- | --- | ---: | ---: | ---: | --- |
| OpenAI 115B 小样本 | `public_synthetic` | USD 5 | USD 0.25 | 16 | 禁止 |
| DeepSeek 兼容探针 | `public_synthetic` | USD 2 | USD 0.10 | 10 | 禁止 |
| 百炼兼容探针 | `public_synthetic` | CNY 20 | CNY 2 | 10 | 禁止 |
| 116C-B 两任务 canary | 冻结公开仓库、另行确认 | USD 10 等值 | 由 canary 清单冻结 | 仅两任务 | 禁止隐藏重试 |

完整 20 任务 / 60 trial 不在本授权内。未来实调前还必须确认 Provider 控制台硬限额、百炼地域/计费方案、Candidate/Judge 独立性和准确 credential reference。个人预发布 actor 固定为 `reviewer_operator_owner`，它只是评审人，不是 production activation actor。

## 验收边界

本 checkpoint 只接受：

1. 三家 profile 的无网络请求/响应/SSE/错误离线合约；
2. disabled 配置在缺少凭据时可加载但不会解析密钥或发起健康探测；
3. 单人预发布 replay 通过且无法进入 Production Admission；
4. 既有双人 Production review 的序列化与 digest 不漂移；
5. CI 持续执行 frozen wheel build；本地全量测试、Ruff、strict mypy、baseline compare 和依赖完整性继续通过。

## 后果

- 个人开发有了可操作的低治理预览层，但不会把“一个人评过”包装成生产批准。
- OpenAI、DeepSeek、百炼共用一个严格的协议 adapter；厂商特性若未来需要，应作为显式 capability 扩展，不能静默下放。
- 真实兼容性、质量、延迟和费用仍没有结论；需要凭据和 live capture 后才能签发。
- 115B Production 和 116C-B 完整质量门保持阻断。

## 官方依据

- [OpenAI Responses：Create a model response](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [DeepSeek：Using the Responses API](https://api-docs.deepseek.com/guides/responses_api/)
- [阿里云百炼：创建响应](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses)
- [阿里云百炼：Base URL 总览](https://help.aliyun.com/zh/model-studio/base-url)
