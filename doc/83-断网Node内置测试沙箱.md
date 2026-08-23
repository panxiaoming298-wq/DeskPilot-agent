# 阶段 83：断网 Node 内置测试沙箱

阶段 83 为对话式通用 Agent 增加第二种真实代码执行能力：用户指定 Conversation Workspace 内一个项目和一个 JavaScript 测试文件，系统在有界项目快照、固定 Node 24 内容寻址运行时和断网 Windows AppContainer 中执行 `node:test`，再把结果、摘要和隔离事实投影回原对话。

本阶段没有把“Node 测试”包装成自由终端，也没有宣称支持 Vitest。实际验证发现 Vitest/Vite 在当前 AppContainer 的单进程、无服务启动边界内不能稳定启动；因此本阶段只交付已真实跑通的 Node 内置测试 profile，继续禁止 `npm`/`npx`、package scripts、任意 argv、联网安装和自由 Shell。

## 1. 对话 Route

固定语法：

```text
运行 Node 测试：frontend tests/sample.test.js
运行 Node 测试：“带空格的项目” “tests/sample.test.js”
```

`workspace_node_test@1` 只接受项目相对路径和项目内测试相对路径。目标必须是显式 `*.spec.js` 或 `*.test.js`；绝对路径、`..`、点前缀路径段、reparse point、未知扩展和额外参数全部 fail closed。Route 绑定 `workspace.node.test.v1` Capability、Contract、Plan、原消息与参数摘要以及最终结果摘要。

测试可导入 Node 内置模块和快照内的相对 JavaScript 模块；`node_modules` 不进入快照，所以本阶段不支持第三方依赖测试。

## 2. 快照与固定运行时

控制面先建立最多 1024 文件、16 MiB、递归 12 层且扫描不超过 5000 项的文本快照。隐藏路径、`node_modules`、`coverage`、`data`、`dist`、`src-tauri` 和 reparse point 被排除；snapshot digest 绑定项目、测试文件、排序清单、内容与版本摘要。

Node 运行时只包含配置或 PATH 解析到的固定 `node.exe`。文件经 SHA-256 清单校验后原子发布到内容寻址目录，runtime digest 变化会使旧运行时证明失效。执行 argv 固定为：

```text
node.exe --preserve-symlinks --preserve-symlinks-main <relative-test-file>
```

用户不能追加 loader、require、环境变量或其他 Node 参数。

## 3. AppContainer 隔离

每次调用创建独立 AppContainer profile。运行时通过同卷 hard link 镜像到 profile，项目快照复制到 profile 内的当次 disposable workspace；测试只能看到该副本，不能读取原项目。副本允许不可信测试在自己的临时空间内写入，但任务结束即删除，写入不会回流到原项目。

固定边界：

- 无网络 capability，不继承应用凭据；
- 1 个活动进程、512 MiB 内存、60 秒超时；
- 输出最多 32 KiB，超限保留首尾并标记；
- 原快照、运行时和用户目录从输出中脱敏；
- AppContainer、Job Object 或运行时不可用时拒绝执行，不降级到普通子进程。

AppContainer 的 capability 与 profile 本地数据模型依据 Microsoft 官方说明；实现不硬编码机器内部 capability SID，而使用系统创建的 per-profile SID。参见 [Launch an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer) 与 [Capability SID troubleshooting](https://learn.microsoft.com/en-us/troubleshoot/windows-server/windows-security/sids-not-resolve-into-friendly-names)。

## 4. 结果证据与工作台

`WorkspaceNodeTestRead` 保存 project/test path、snapshot/runtime digest、status/exit code、pass/fail/skip/error 计数、用时、受限输出、`windows_appcontainer`、`network_access=false`、`process_limit=1` 和覆盖全部字段的 result digest。Route 重载时重新验证 schema 与摘要。

前端没有新增表单式测试控制台。统一对话仍是入口；当前 Run 显示固定执行步骤，右侧只增加一张 Node Test 证据卡，展示文件、计数、时长、隔离事实、两个摘要和输出。

## 5. 验收与边界

本阶段的真实 Windows 集成测试在快照内运行两个 `node:test` 用例：一个导入本地模块并断言结果，另一个尝试读取原项目文件并要求 Windows 拒绝。另有快照边界、对话 API → Route/Contract/Plan → Run → projection 和前端证据卡测试。

最终验证：阶段 83 与相邻路由/迁移 61 项定向后端测试、既有 Python/AppContainer worker 回归 5 项通过；Ruff 全仓、mypy 220 个生产源码、Alembic 单一 `0037` head/autogenerate check 和 `uv lock --check` 通过。前端 22 个测试文件/151 项、Vue type-check、production build 与 finesse 静态检测 `p0: 0` 通过，`git diff --check` 无空白错误。本阶段没有运行耗时数小时的后端全量 pytest，也没有自行打开用户页面做视觉断言。

当前能力是“一个显式、无第三方依赖的 `node:test` JavaScript 文件”，不是 Vitest、Jest、npm scripts 或通用代码终端。下一阶段优先增加可恢复的新建/重命名等工作区操作，或多类 Artifact；若以后支持 Vitest，必须建立独立运行时、受控服务进程模型和新的真实 AppContainer 门禁，不能把本阶段名称直接改成 Vitest。
