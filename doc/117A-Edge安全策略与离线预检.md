# 117A-A：Edge 安全策略与离线预检

## 目标

本检查点冻结阶段 117A Browser Agent 的第一份机器可读安全合同，并提供不启动浏览器的离线预检。策略 digest 为 `2aeb30b31161f41ba48841c86d6f80f7847327f58d31cfd500b2ed936633177f`。

它不是 Edge Operator，也没有 Profile 管理 API、Browser action runner 或真实网站验收。离线 readiness 为 ready 仍不会获得桌面控制或网络执行权。

## Profile 与域名边界

- 只允许 Microsoft Edge 的应用管理型独立 `DeskPilot` Profile，不复用个人 Profile。
- Edge 必须使用可见窗口；登录只能由用户在可见窗口手动完成。
- 默认域名 allowlist 严格为空。
- 自动验收只允许 `127.0.0.1`、`localhost` 或 `::1` 的 loopback origin；公网 origin 即使在本地 allowlist 中也不能冒充自动验收。
- 公网 origin 必须是无凭据、无路径、无 query、无 fragment 的规范 HTTPS origin。
- DOM 目标必须使用语义定位；任意坐标点击保持关闭。

## 动作矩阵

| 动作 | 风险 | 新审批 | 成功证据 |
| --- | --- | --- | --- |
| `navigate` | R1 | 否 | 地址与文档身份 |
| `dom_read` | R0 | 否 | DOM snapshot digest |
| `screenshot` | R0 | 否 | 图片 digest 与窗口身份 |
| `form_prefill` | R1 | 否 | DOM value readback |
| `submit` | R2 | 是 | 结果文档或 receipt |
| `upload` | R2 | 是 | 选定文件与 DOM 状态 |
| `download` | R2 | 是 | 目标文件 digest |
| `publish` | R2 | 是 | 发布后状态与 origin |

四项有后果的动作必须各自取得最长 5 分钟的新审批，并精确绑定持久 approval ID、用户看到的 preview hash、proposal digest、action、origin、target digest 与 content digest。审批不能跨目标、跨内容、跨动作或过期复用。当前代码不签发审批，后续只能从既有持久审批服务投影该 binding。所有动作的自动重试均为 0。

## 敏感数据与不可信输入

- Cookie 值、密码、一次性验证码、2FA secret 和验证码答案均不可读取或交给模型。
- 权限弹窗、登录挑战与验证码必须停下等待用户。
- 截图必须在后续 Runner 中实现敏感区域脱敏；本检查点尚未生成截图。
- 网页 DOM、截图 OCR 与 UI 文本始终是 `untrusted_external_input`，不能签发审批、扩大 allowlist 或改变 Policy。

## 离线预检

`BrowserActionOfflinePreflight` 校验冻结策略、内容寻址 allowlist snapshot、规范 origin、loopback 验收边界、敏感数据请求和精确审批 binding。返回结果只包含摘要与违反项，不包含页面路径、表单正文或凭据。

readiness 固定：

- `browser_profile_created=false`
- `browser_launched=false`
- `desktop_application_control=false`
- `network_access=false`
- `action_executed=false`
- `execution_authorized=false`

## 验收

- 23 项专项测试覆盖策略 digest、动作矩阵、YAML alias/策略漂移、危险 origin、默认空 allowlist、loopback 验收、公网验收冒充、敏感数据、四项逐次审批、审批跨目标复用、过期与最长有效期。
- Browser/Policy/Approval 联合回归 57 项通过；Ruff 全仓、strict mypy 329 个生产源码、Python compileall、60 包 `pip check` 与 frozen lock 通过。
- Evaluation Windows v2 和 Phase75 v21 只读 compare 通过，旧 baseline 未修改。
- Windows CI 固定检查 manifest、专项测试、全仓 Ruff/mypy 和 wheel YAML 资源。

## 下一检查点

117A-B 才实现本地持久化的 Browser Profile/allowlist 管理与只读 API。此后仍需单独实现 Edge Operator、ActionReceipt、权限弹窗暂停和 loopback 真实验收；在这些完成前不得创建或操控 Edge Profile。
