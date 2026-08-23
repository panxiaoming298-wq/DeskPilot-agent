# 阶段 87：确定性对话 Route 自然语言参数提取

阶段 87 扩展现有 `TurnRouter` 的参数绑定层，让用户不必记住“读取工作区文件：…”等固定命令句式，也能调用已经存在的研究、Artifact、文件和测试能力。本阶段没有新增 Tool、Route、权限、可执行参数或写入协议；自然语言只会被绑定成原 Route 已允许的字段，之后仍由原 Contract、Policy、隔离 Runtime、确认摘要和证据链决定能否执行。

## 1. 支持的普通表达

显式阶段 78～84 命令语法继续兼容，并优先于自然表达匹配。新增的确定性表达覆盖：

| 用户表达示例 | 绑定结果 |
| --- | --- |
| `帮我看看 README.md` | `workspace_file_read(path=README.md)` |
| `把 README.md 里的 "旧文本" 改成 "新文本"` | `workspace_file_replace` 的精确三参数 |
| `创建 notes/todo.md，内容是“第一行”` | `workspace_file_create` 预览 |
| `把 notes/old.md 改名成 notes/new.md` | `workspace_file_rename` 预览 |
| `看看 backend/src 目录里有什么` | `workspace_directory_list` |
| `检查 backend/src 里的 Python 语法` | `workspace_snapshot_check(python-syntax)` |
| `验证 config.json 是不是合法 JSON` | `workspace_snapshot_check(json-parse)` |
| `在 backend 里运行 tests/test_api.py` | 单个显式 pytest 文件 |
| `帮我跑一下 frontend 里的 tests/api.test.js` | 单个显式 `node:test` 文件 |
| `查一下量子计算，整理成 PDF 报告` | `research_to_html(goal=量子计算)` |
| `做一份关于量子计算的 Markdown 报告` | 同一 verified HTML/Markdown/PDF Artifact 链 |

研究参数会剥离礼貌词、查询动词和受支持的报告格式包装，只把主题写入 `goal`。格式词不会缩减或扩大 Artifact Contract：新研究任务仍同源生成 HTML、Markdown、PDF，HTML 继续作为 Browser Verifier 主 revision，PDF 继续要求真实渲染证明。

## 2. 确定性和安全边界

自然参数提取使用受信代码中的锚定规则，不调用模型，也不从 Memory、网页、MCP 内容或 Assistant 消息取得路径和授权：

- 每条写入表达必须完整匹配；替换前后文本和新建内容仍需成对引号界定；
- Python 测试只接受 `tests/` 下单个 `test_*.py`/`*_test.py`，Node 只接受单个 `*.spec.js`/`*.test.js`；“运行所有测试”只会进入澄清；
- 参数进入原 Workspace Runtime 后仍需通过相对路径、扩展名、大小、reparse point、版本和快照验证；
- 修改、新建和重命名仍只产生预览，必须使用原确认摘要提交；
- “把报告导出到某路径”不会被自然语言直接执行。精确 Artifact 导出继续要求已经交付的 revision、Artifact 选择、prepare/commit 两步确认、正确后缀和目标不存在；
- 未完整匹配、混合多个 Route 或要求自由 Shell、目录创建、覆盖、删除、npm/npx 的话语继续澄清或拒绝。

Classifier 标识升级为 `deskpilot.turn-router.rules.v2`。新 Turn 的候选摘要绑定 v2；读取既有数据库时仍接受已经持久化且完整摘要一致的 v1 决策，避免规则升级使历史 Workbench 失效。Route manifest、Route version 和参数摘要格式没有变化。

## 3. 验收

自动化覆盖 11 组自然表达、两个 fail-closed 反例和旧 v1 classifier 摘要回读，并把文件读取、Python 测试和 Node 测试的 Workbench API 用例切换为普通对话表达。Workspace/测试 Runtime 与 Workbench 合跑 42 项通过，migration 25 项通过；Ruff 全仓、mypy 222 个生产源码、`uv lock --check` 和 `git diff --check` 通过。

阶段 87 没有修改前端协议、数据库结构、Artifact 格式或运行时依赖，因此没有重复前端构建。默认开发 SQLite 当时仍停在 `0037_turn_routes`，直接 `alembic check` 正确报告未升级；没有擅自修改该本地数据库，独立 migration 测试已验证 `0038` head 往返与 metadata。

当前仍不支持模型自由参数提取、自由组合多个动作、自然语言直接导出、未加引号的写入正文、Vitest/npm scripts、目录创建、覆盖、删除、自由 Shell、登录态浏览器或联网安装。
