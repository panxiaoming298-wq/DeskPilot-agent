# Provider 探针桌面无网准备检查点

## 目标

本检查点把 ADR-019 已冻结的 OpenAI、DeepSeek 和阿里云百炼探针策略投影到桌面 Provider 设置页。用户可以在页面核对公开配置与费用边界，生成最长 24 小时有效的 operator binding，并运行既有的无网 preflight。

它不是 live runner 的图形界面，也不会因为 readiness 为 ready 而获得网络或模型调用权限。

## 桌面工作流

1. 本机 API 读取 v2 冻结策略，向前端返回三家 Provider 的 exact model、公开 Base URL 要求、Windows credential identifier、请求数与费用上限。
2. 用户确认模型、Key 存在性、Base URL/Key 归属、专用小额凭据、应用内预算上限和当日官方价格。
3. DeepSeek 额外确认当日预付费余额；百炼填写北京 Workspace Responses 兼容地址，并确认账单告警与账单延迟边界；OpenAI 可选已生效的项目硬限额或应用内小额包络。
4. 后端根据服务器中的策略生成 binding，而不是信任前端传入费用数字。
5. 既有 `ProviderProbeOfflinePreflight` 校验 binding。页面显示结果，并允许将不含密钥的 binding 下载为 JSON。

## API

- `GET /api/v1/model-providers/probe-preparation`：只读 manifest，`Cache-Control: no-store`。
- `POST /api/v1/model-providers/probe-preparation:preflight`：提交公开 Provider 配置与人工确认，返回 binding 和脱敏 readiness，`Cache-Control: no-store`。

没有新增 `run`、permit、capture、Admission 或 activation API。已有 live CLI 仍是唯一候选执行入口，且默认拒绝。

## 凭据边界

- 本流程不接收 API Key，不解析 Windows Credential Manager，只使用固定 identifier。
- API Key 仍通过已有 Provider 编辑器写入当前 Windows 用户的凭据管理器；密钥不进入 catalog、binding、readiness、日志或浏览器存储。
- `credential_presence_confirmed` 是 operator attestation，不是后端回读密钥的证明。

## 执行边界

manifest 与 readiness 均固定：

- `network_access=false`
- `credentials_resolved=false`
- `real_model_capture=false`
- `production_admission=false`
- `cloud_activation=false`
- `full_116c_b=false`

preflight 通过只表示“这份公开准备材料符合当前冻结策略”。它不表示 API Key 可用、Provider 兼容、模型质量达标，也不授权 12 次真实探针或 20 任务 / 60 trial。

## 验收

- 后端覆盖三家 manifest、OpenAI ready binding、百炼错误 Base URL 阻断和计费确认必填。
- 前端覆盖冻结边界、未完成项即时提示、公开请求体、ready 投影与百炼 Workspace 差异。
- 设置页没有真实模型执行按钮，也没有在加载页面时做健康检查或 Provider 请求。
