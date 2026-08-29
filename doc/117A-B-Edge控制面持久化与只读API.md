# 117A-B：Edge 控制面持久化与只读 API

## 目标

本检查点把 117A-A 的冻结 Browser Policy 投影为本地数据库真值，供受认证桌面前端读取。它解决“重启后如何确认当前 Profile 合同、allowlist 版本和动作风险矩阵没有漂移”，不提供 Edge Operator 或 Browser action runner。

启动时只会初始化或验证固定配置 `edge-deskpilot-v1`：Microsoft Edge、应用管理型独立 `DeskPilot` Profile、revision 1、空 origin allowlist。初始化不创建 Profile 目录、不启动 Edge、不读取个人浏览器数据，也不访问网站。

## 持久化合同

Alembic `0066_browser_control_plane` 新增两张表：

- `browser_control_plane_state`：保存冻结 policy digest、Profile 公开描述、当前 revision、活跃 allowlist digest 和控制面 digest；数据库约束强制 `profile_created=false`、`operator_enabled=false`。
- `browser_origin_allowlist_snapshots`：保存内容寻址的 allowlist revision。当前只有启动时写入的 revision 1 空列表；没有公开修改 API。

每次启动和每次读取都会重新加载不可变 Browser Policy，并重验 Profile 字段、policy binding、allowlist snapshot digest、活跃 revision 和整个控制面 snapshot digest。任一持久字段被篡改都会 fail closed，应用不会把损坏状态解释为权限。

数据库不保存 Profile 路径、Cookie、密码、OTP、2FA、登录信息、网页 URL/path、DOM、截图、表单内容、模型输入或任何浏览器进程句柄。

## 只读 API

`GET /api/v1/browser/control-plane` 使用既有本地会话认证，返回：

- 固定 Edge/Profile 描述和 policy digest；
- 当前 allowlist revision、规范 origin 列表及 snapshot digest；
- 八项动作的 capability、风险等级、新审批要求、零自动重试和成功证据类型；
- 固定为 false 的 Profile 创建、浏览器启动、Operator、网络执行和动作执行状态。

响应设置 `Cache-Control: no-store` 和绑定 revision/snapshot digest 的 `ETag`。本检查点没有 POST、PUT、PATCH 或 DELETE 路由，因此前端不能扩大 allowlist 或启用 Operator。

## 安全边界

- 数据库初始化不是创建 Edge Profile 的授权。
- allowlist 中存在 origin（未来阶段）也只代表可进入动作预检，不代表允许导航或执行。
- action metadata 是只读风险说明，不签发 Approval、permit 或 execution authority。
- 117A-A 的 loopback-only 自动验收、可见窗口/人工登录、语义 DOM、敏感数据禁令和四项逐次审批合同保持不变。
- 本检查点未调用模型，未执行真实网站 capture、Production Admission、cloud activation 或 116C-B。

## 验收

- 控制面专项覆盖首次初始化、幂等重复启动、跨服务读取、默认空 allowlist、所有运行能力关闭和无 Profile 目录副作用。
- 篡改 policy binding 或 allowlist JSON 后读取均拒绝。
- API 专项覆盖认证、`no-store`、ETag、八动作投影以及写方法不存在。
- 迁移专项覆盖 `0066 → 0065 → 0066` 往返、列、约束和外键。
- Windows CI 同时运行 117A-A 策略、117A-B 控制面、0066 迁移专项与全仓 Ruff/mypy。
- 本地门禁最终通过控制面/策略/迁移 26 项、Browser/Policy/Approval 联合 40 项、空库完整迁移断言、Ruff、strict mypy 331 个生产源码、compileall、frozen lock、60 包依赖检查、18 份 workflow YAML、wheel 资源及两组只读 Evaluation compare。

## 下一检查点

117A-C 可在继续保持 Operator 缺席的前提下，实现本地 allowlist 写管理：规范化 origin、ETag 乐观并发、幂等键、明确确认、append-only revision 和无敏感值审计。完成该管理面后，仍需独立检查点实现应用管理型 Profile 的显式创建/发现，再单独实现可见 Edge Operator 与 ActionReceipt；这些阶段不得合并成一次隐式授权。
