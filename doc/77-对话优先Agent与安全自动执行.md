# 阶段 77：对话优先 Agent 与安全自动执行

阶段 76 的 verified-edge、Claim/Citation、Artifact Workspace、Browser Verifier 和精确导出没有做错，但用户交互仍是“建立任务后手工点五个步骤”，它更像研究流程操作台，而不是用自然语言委派工作的 Agent。阶段 77 不重写已验证的执行底座，而是在其上增加 Conversation Turn 和服务器授权的自动推进，并把首页改成对话主界面。

## 1. Conversation Turn 成为用户入口

新会话的一条用户消息会在服务器内原子地建立 Conversation、Task、受信 `research_to_html` 计划和 Execution Run，然后记录 Agent 的受理回复。客户端不再自己拼接 Conversation/Task/Planning 调用。

```text
POST /api/v1/conversation-turns
POST /api/v1/tasks/{task_id}/conversation-turns
```

后续指令使用同一 Conversation，但建立新的不可变 Task/Contract/Run。若旧运行尚未结束，服务器会先 cancel 并提升 fencing token，再按新指令规划；这样既保留完整对话历史，也不就地篡改旧合同。

## 2. 安全步骤由 Agent 自动推进

`POST /api/v1/tasks/{task_id}/workbench:advance` 每次只执行服务器投影中当前唯一 `enabled=true` 的安全动作：

1. 受控研究与页面读取；
2. 独立 Claim/Citation 核验；
3. 隔离工作区构建 HTML Artifact；
4. 断网、无登录态浏览器验收；
5. 形成 DeliveryManifest 并提案 verified episode。

每个完成节点都由服务器写入一条 Assistant message，前端仅显示持久化结果。`workbench:stop` 仍是常驻用户控制；精确写入用户路径仍必须 prepare 后单独确认，不属于自动步骤。

## 3. 对话主界面

进入工作区后默认打开“Agent 会话”，而不是旧任务表单。界面同时展示三种时态：

- 过去：用户与 Agent 的完整 Conversation transcript；
- 现在：当前 Run、正在执行的节点、原因和常驻停止按钮；
- 未来/结果：排队节点、Claim/Citation、Artifact、Browser proof 和需确认的导出。

研究工作台因此被降为右侧“证据与交付”层，不再把内部实现步骤当成用户的主导航。旧“执行详情”仍保留，用于调试传统 Task Processor 和事件流。

## 4. 设计与响应式边界

对话工作台使用暖灰纸面、墨绿控制栏和朱红中断/确认信号，与旧冷色 HUD 形成明确的产品层次。动态只用于新消息反馈和一个运行中生命信号，并支持 reduced-motion。宽屏为会话+证据三栏，普通桌面将证据移到下方，手机为单列。

## 5. 验收与保留边界

自动化和真实浏览器验收覆盖：

- Conversation Turn 建立、五步自动推进、服务器 Assistant message 和同会话 follow-up；
- 旧阶段 76 手动端点与精确导出兼容性；
- Vue 对话主界面、导出二次确认、API 身份编码、type-check 和 production build；
- recorded provider 下的真实页面执行：7 条消息、5 个完成节点、verified Claim、Artifact/PatchReceipt 和 Browser proof；
- 宽屏、普通桌面和窄屏响应式检查，新鲜页面控制台无 warning/error。

本阶段完成的是“通用对话委派形态 + 已有研究能力”，不是全部通用能力。当前受信计划仍只有 `research_to_html`；本地知识库、MCP、文件操作和更多 Artifact 类型尚未组入同一 Turn 路由。因此产品方向已纠正，能力面仍需逐个增加受信 contract/builder/verifier。
