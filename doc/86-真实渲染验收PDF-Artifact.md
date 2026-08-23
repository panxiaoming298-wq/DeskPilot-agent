# 阶段 86：真实渲染验收 PDF Artifact

阶段 86 把研究交付扩展为同源 `index.html`、`report.md` 与 `report.pdf`。PDF 不是把任意字节标成 `application/pdf`，也不是复用 HTML 的 BrowserRenderRun 冒充验证；它由已经通过确定性静态检查的同一 HTML revision 在断网、禁用脚本、无登录态的 Chromium 上下文中打印，再由 Poppler 对全部页面实际栅格化。只有页数、A4 页面信息、PNG 尺寸和逐页摘要全部闭合，PDF ArtifactRevision 才能进入 Workspace。

HTML 仍是 DeliveryManifest 与隔离 Browser Verifier 的主 revision。PDF 是独立伴生 Artifact，拥有自己的内容摘要、revision、PatchReceipt 和 PDF render evidence。阶段 85 的精确 Artifact 选择与两步导出协议继续沿用，并新增 `.pdf` 与 `application/pdf` 的严格绑定。

## 1. 同源生成与真实渲染门禁

`build_html` 节点现在执行以下受信流程：

1. 只读取已经通过独立 Claim/Citation verification 的 Claim、Citation 与 PageSnapshot；
2. 生成静态 `index.html` 和 `report.md`；
3. 把自包含 HTML 复制到短生命周期临时目录，使用固定 Chromium 参数打印 PDF：禁用 JavaScript、扩展、同步和后台网络，DNS 映射到不可达地址并绑定失效代理；
4. 规范化 PDF 的时间元数据和文档 ID，避免这些非内容字段制造无意义 revision 漂移；
5. 使用固定 `pdfinfo` 读取页数与页面尺寸，再用 `pdftoppm -png -r 144` 渲染全部页面；
6. 拒绝无 PDF 头、过小文件、零页、页数不一致、无效 PNG、无效像素尺寸、超时或任一子进程失败；
7. 将 PDF bytes、每页 PNG 摘要、页尺寸、DPI、引擎和 evidence digest 一起绑定到 PDF revision。

PDF 的打印样式显式使用 A4 页面、固定页边距、`break-inside: avoid` 和打印背景；链接在打印版中显示完整 URL，因此 PDF 离开浏览器后仍能直接看到 Citation 目标。

正式实现没有加入 `--no-sandbox`。本次 Codex 外层受限环境会使 Edge 自身 GPU sandbox 子进程退出；为完成视觉样张验收，仅在一次独立验收命令中临时绕过外层冲突生成样张，产品代码仍保留浏览器沙箱、禁脚本和断网参数。Poppler 的实际逐页验收与产品实现使用相同命令和解析规则。

## 2. 不可变 PDF render evidence

`0038_pdf_render_evidence` 为 `artifact_revisions` 增加两个可空字段：

- `render_evidence`：严格的 `deskpilot.pdf-render.v1` 证明；
- `render_evidence_digest`：证明正文的内容摘要。

只有 `application/pdf` revision 可以携带该证明，且必须满足：

- `status` 固定为 `passed`，`issue_codes` 为空；
- `source_digest` 与 PDF revision 的 `content_digest` 完全相同；
- `rendered_page_digests` 和 `rendered_page_dimensions` 数量等于 `page_count`；
- evidence 自身摘要、数据库摘要和领域模型重新计算结果一致；
- 非 PDF revision 携带 PDF 证明、PDF revision 缺少证明或任一字段漂移时，Workspace 读取和导出都 fail closed。

升级前的 HTML/Markdown revision 两个字段均为空，不需要伪造回填。旧 Contract 没有声明 `.pdf` 时仍只构建旧格式；新建研究任务使用 `artifact.html.v1@1.2.0`，Contract 明确允许 `.pdf`。

## 3. 精确选择 PDF 导出

阶段 85 的 `artifact_id` 选择协议扩展到 PDF：

- 选择 `report.pdf` 时，目标必须是绝对 `.pdf` 新路径；
- prepare 会复核 Workspace、Artifact、active revision、PatchReceipt、PDF render evidence、blob 后缀、内容摘要与字节数；
- commit 再次绑定同一 revision，使用 exclusive create 写入，目标存在时绝不覆盖；
- 不传 `artifact_id` 时仍默认导出 DeliveryManifest 的主 HTML，兼容旧调用方。

对话工作台沿用现有证据区。Artifact Workspace 在 PDF 卡片上显示页数、DPI 和 render evidence 摘要；“选择交付物”新增 `report.pdf`，选择后路径提示切换为 `.pdf`。没有新增页面骨架、图片或装饰动效。

## 4. 验收结果与边界

自动化结果：

- 后端 Workbench/Plan/verified delivery/Phase75 发布门禁最终通过；其中 35 项合跑通过，修复后的 Phase75 4 项单独复跑通过；既有真实 Edge BrowserVerifier 用例在 Codex 嵌套沙箱内因 GPU sandbox 退出而未纳入通过数，未用 `--no-sandbox` 修改产品代码掩盖；
- migration 25 项通过，包含 `0038` upgrade/downgrade/head/autogenerate check；
- Ruff 全仓、mypy 222 个生产源码、`uv lock --check` 通过；
- 前端 22 文件、152 项测试、Vue type-check、production build 通过；工作台源组件静态检测 `p0: 0`；
- 实际生成 1 页 A4 PDF，`pdfinfo` 识别 PDF 1.4、无 JavaScript；`pdftoppm` 在 144 DPI 生成非空 PNG，人工查看确认中文、结论、来源 URL、边界和分页均正常；
- `git diff --check` 作为最终交付门禁执行。

当前仍不支持：

- DOCX、图片包、压缩包或一次导出整个 Workspace；
- PDF 的用户自定义模板、纸张、页眉页脚或任意 HTML/CSS 输入；
- 覆盖目标文件、目录导出、自由可执行文件或自由 argv；
- 登录态浏览器、联网安装或把网页文本/Memory/MCP 输出当成授权。

下一阶段优先改善确定性对话 Route 的自然语言参数提取，或增加受控的 Artifact 模板能力；不能为了“更像通用 Agent”放开自由 Shell、隐式写路径或未经证明的 PDF/DOCX 转换链。
