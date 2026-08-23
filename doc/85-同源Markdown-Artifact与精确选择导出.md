# 阶段 85：同源 Markdown Artifact 与精确选择导出

阶段 85 把研究交付从“只有一个 HTML 文件”扩展为一个受控的多 Artifact Workspace。新的研究任务仍以 `index.html` 作为浏览器验收和 DeliveryManifest 的主交付，同时从完全相同的 verified Claim、Citation 与 PageSnapshot 生成 `report.md` 伴生交付。两份文件分别拥有内容摘要、immutable ArtifactRevision 和 PatchReceipt，不能用 HTML 的验证结果冒充 Markdown 的文件身份。

本阶段没有把工作台改成文件管理器，也没有开放任意格式、任意路径写入或覆盖。Markdown 只是现有 `research_to_html` 纵向闭环的第二种确定性表示；HTML 的隔离 Browser Verifier、verified-edge 解锁顺序和默认导出行为保持不变。

## 1. 同一份已验证事实，两份独立 Artifact

`build_html` 节点现在执行一次确定性多文件提交：

- `index.html`：`text/html`，继续作为 DeliveryManifest 主 revision，并进入断网、无登录态浏览器验收；
- `report.md`：`text/markdown`，按相同 Claim 顺序生成结论和编号来源，供文本编辑器、版本控制或后续文档流水线使用；
- 每份文件分别计算内容摘要、Artifact ID、revision ID、PatchReceipt ID 和字节数；
- Workspace 配额按两份文件的总字节数和文件数检查，Contract 显式允许 `.md`；
- Claim、标题和目标中的 Markdown 控制字符及换行会被转义，来源 URL 只来自已绑定 PageSnapshot。

升级前已经持久化的 HTML-only Contract 仍可完成原任务：如果旧 Contract 没有声明 `.md`，Builder 只生成 `index.html`，不会因为新版本要求伴生文件而卡死。新建研究任务使用扩展后的 Contract，必须生成两份 Artifact。

## 2. 导出来源不再写死为主 HTML

`PrepareArtifactExport` 新增可选 `artifact_id`：

- 不传时继续选择 DeliveryManifest 的主 HTML，兼容原调用方；
- 传入时只允许选择同一已交付 Workspace 的 active ArtifactRevision；
- `.html` 只能导出 `text/html`，`.md` 只能导出 `text/markdown`；
- Artifact、revision、PatchReceipt、blob 后缀、内容摘要、字节数和目标路径都进入预览证明；
- 陌生 Artifact、跨 Workspace Artifact、后缀错配、目标已存在、symlink/junction 或内容漂移全部 fail closed。

提交协议仍是两步：prepare 只返回目标、来源和 `confirmation_digest`，不会写文件；commit 复核同一 ArtifactRevision 后使用 exclusive create 写入，不覆盖，并形成不可变导出回执。

## 3. 对话工作台交付区

前端继续使用“对话历史 + 当前 Run + 证据与交付”结构，没有新增表单式研究页面。右侧 Artifact Workspace 会同时列出 `index.html` 与 `report.md` 的 revision 和 PatchReceipt。精确导出区新增“选择交付物”：

- 默认选择 DeliveryManifest 的 HTML 主交付；
- 选择 Markdown 后，目标路径提示切换为 `.md`；
- 切换交付物会清空旧路径和旧预览，避免把 HTML 确认摘要误用于 Markdown；
- 选择、目标路径、预览和最终确认保持在同一个交付证据区。

视觉上沿用阶段 77 已确定的暖纸、墨绿、朱砂工作台，不增加图片、装饰动效或新的页面骨架。变化只用于让多 Artifact 的来源和导出对象可见。

## 4. 验收与当前边界

专项测试覆盖：双 Artifact 构建、两份独立 PatchReceipt、HTML 主 revision 继续通过 Browser Verifier、Markdown 精确选择、后缀错配、陌生 Artifact、确认前零写入、确认后内容一致、默认 HTML 兼容和导出后篡改拒绝。前端测试覆盖 Artifact 选择器、Markdown 请求体、两步确认和既有自动推进。

最终验证：WorkBench/Plan/verified delivery/发布门禁四组 36 项后端用例与 24 项 migration 用例通过；Ruff 全仓、mypy 220 个生产源码、Alembic 单一 `0037` head/autogenerate check 和 `uv lock --check` 通过。前端 22 文件/152 项、Vue type-check、production build 与工作台源组件静态检测 `p0: 0` 通过，`git diff --check` 无空白错误。本阶段没有自行打开页面做视觉断言。

本阶段仍不支持：

- PDF、DOCX、图片、压缩包或任意 MIME；
- Markdown 的独立渲染器或浏览器视觉验收；
- 一次导出整个 Workspace 或覆盖现有文件；
- 从网页文本、模型输出或 Memory 自动取得用户路径写入确认；
- 登录态浏览器、自由 Shell、自由 argv 或联网安装。

下一阶段可以增加经过真实渲染验证的 PDF Artifact，或把确定性的研究 Route 参数提取做得更自然；不能把 Markdown 伴生文件当成绕过 Claim/Citation verification、Browser 主交付验收或用户路径确认的捷径。
