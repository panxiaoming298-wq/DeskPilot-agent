# 阶段 76：统一研究工作台与精确 Artifact 导出

阶段 76 把阶段 71～75 已存在但分散的 Conversation、Research、Claim/Citation Verification、Artifact Workspace、PatchReceipt、Browser Verifier 与 DeliveryManifest 组合成同一份服务器真值投影，并增加用户指定绝对路径的两步 HTML 导出。界面不根据请求返回顺序猜测节点完成；只有持久化执行节点的 `verified` 状态才能使后继动作可用。

## 1. 统一 Task Workbench

`TaskWorkbenchService` 返回 `deskpilot.task-workbench.v1`：

- 精确 task 绑定的 Conversation message，不读取同会话的其他任务内容；
- 当前 Planning/Task Contract/Executable Plan 与最新 Execution Run；
- Research Claim/Citation 和独立 Verification verdict；
- Workspace、ArtifactRevision、PatchReceipt、隔离 Browser evidence 与 DeliveryManifest；
- Artifact export 历史、阶段枚举、动作可用性和整个投影的内容摘要。

前端“研究交付台”直接消费该投影。左栏创建目标和停止运行，中栏显示 verified-edge 执行链与两步导出，右栏陈列证据卷宗。动作按钮只接受服务端 `enabled=true`；停止运行会取消未完成节点、递增 claim fencing token，并使迟到 Agent 结果无法解锁后继节点。

主要接口：

```text
POST /api/v1/research-workbench/tasks
GET  /api/v1/tasks/{task_id}/workbench
POST /api/v1/execution-runs/{run_id}:cancel
```

## 2. 精确路径导出协议

导出不扩大 Artifact Workspace 权限，也不允许覆盖。Task Contract 必须显式声明 `allow_user_path_export=true`，且只接受满足下列条件的目标：

- 绝对 `.html` 路径；
- 父目录已经存在；
- 路径组件不包含符号链接或 Windows junction；
- 目标文件尚不存在；冲突策略固定为 `fail_if_exists`。

第一步 `prepare` 重新验证 DeliveryManifest、Task/Contract、Workspace/Artifact/Revision/PatchReceipt 血缘和源 blob 摘要，只持久化预览，不写文件。第二步 `commit` 必须提交预览生成的 `confirmation_digest`，再次验证全部证明后用 exclusive create 写入精确目标。成功后持久化包含源摘要、字节数、目标路径和提交时间的不可变 receipt；同一幂等键可安全回放，不同键或不同请求绑定会被拒绝。

```text
POST /api/v1/deliveries/{delivery_id}/exports:prepare
POST /api/v1/artifact-exports/{export_id}:commit
GET  /api/v1/artifact-exports/{export_id}
```

## 3. 持久化与失败边界

Migration `0036_artifact_exports` 新增 `artifact_exports`，保存 prepare/commit 幂等键摘要、精确目标、源/请求/确认/回执摘要、状态和错误码。写入前先把状态提交为 `committing`；若进程在文件创建后退出，重试会用目标文件摘要恢复成功或在摘要不一致时 fail closed。任何已存在目标都不会被截断或覆盖。

读取 committed export 时会同时复核不可变 receipt 和实际目标文件摘要。SQLite 重载的无时区 UTC 时间会先规范化再参与摘要，保证首次响应与重放响应一致。

## 4. 界面与验收

研究交付台采用深海任务栏、冷灰任务卷宗和氧化红单一强调色。界面没有装饰性图片或伪浏览器框；Browser 区域只显示真实验收数字。动态仅用于 `ready` 状态反馈，并提供 reduced-motion 静态回退。

本阶段自动化覆盖：

- verified edge 逐步解锁完整研究到交付链；
- prepare 不写文件、错误确认不写文件、commit 精确写入、幂等回放和拒绝覆盖；
- stop fencing 阻断未完成执行；
- `0036` upgrade/downgrade/upgrade 与 metadata check；
- Vue 组件独立确认导出流程、全套前端测试、type-check 和 production build；
- 静态界面检测以及 320/375/414/768/桌面真实浏览器溢出、触控尺寸和控制台检查。

## 5. 保留边界

- 真实 Research Provider 仍默认关闭；阶段测试使用 recorded provider，不把录制数据冒充实时联网结果。
- 目前精确导出只开放单个已交付 HTML，不支持覆盖、批量、目录或其他媒体类型。
- 本地知识库和 MCP 尚未并入这条统一任务链；第三方 Agent/插件供应链仍需独立信任、撤销和 cohort 门禁。
- 前端证据视图是投影，不是新的正确性来源；Memory、Summary、UI event 或网页文字都不能替代 Verification verdict。

下一阶段建议把本地知识库/MCP 作为受控 Research source 接入同一 Task Workbench，并扩展多类 Artifact；每种新产物仍需独立 builder/verifier/export contract。
