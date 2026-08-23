# 阶段 82：只读 Python 项目测试沙箱

阶段 81 只解析 Python/JSON 文本，不执行仓库代码。阶段 82 增加第一个真实代码测试 Route：用户从统一对话工作台指定一个 Python 项目和一个 pytest 文件，系统在独立只读快照、断网 Windows AppContainer 和有界内容寻址运行时中执行，再把结果作为可重放证据返回原 Task。

本阶段仍未开放自由 Shell、自定义 executable、任意 pytest 参数、node id、项目目录写入或联网安装依赖。`vitest` 与 Node 供应链是不同的安全 profile，留到下一阶段，不与 Python 运行时混合。

## 1. 对话 Route

固定语法为：

```text
运行项目测试：backend tests/test_workspace_file_runtime.py
运行项目测试：“带空格的项目” “tests/test_sample.py”
```

`workspace_python_test@1` 只接受两个字符串参数：Conversation Workspace 内的项目相对路径与项目内的测试相对路径。测试文件必须是 `tests/` 下的 `test_*.py` 或 `*_test.py`；绝对路径、`..`、点前缀路径段、符号链接、reparse point、额外命令行参数和未知文件类型全部 fail closed。

Route manifest 绑定 `workspace.python.test.v1` Capability、Task Contract、Executable Plan、原消息摘要、项目/测试参数摘要和结果摘要。每条新指令仍产生同会话内新的不可变 replacement Task。

## 2. 项目快照

控制面不把原项目目录直接交给测试进程，而是先建立一份独立内容快照：

- 项目必须是已配置 Conversation Workspace 内的目录；
- 最多递归 12 层、扫描 5000 项、包含 512 个文本文件；
- 总快照最大 8 MiB，单文件继续受工作区文件上限约束；
- 只选择 Python 项目和常用配置/文本后缀，排除隐藏目录、`__pycache__`、`data`、`dist`和 `node_modules`；
- 遇到链接/reparse point 直接拒绝；文件和目录都复核读取前后稳定 identity；
- snapshot digest 绑定项目路径、测试路径、排序文件清单、内容摘要和版本摘要。

测试进程只能读取每次调用独有的 snapshot wrapper。它不能读取原项目，也不能读取并发调用的其他快照。

## 3. 固定 pytest 运行时

运行时不使用当前开发虚拟环境的可变 `sys.path`。控制面从显式 distribution 白名单构建内容寻址 Python bundle，复用 worker runtime 的原子发布、完整性检查与 RX capability ACL。项目代码变化只改变 snapshot digest，不会伪装成新运行时；依赖或 Python runtime 变化才会改变 runtime digest。

固定 harness：

- 以快照根为 working directory，只将一个相对测试文件交给 pytest；
- 将 `src/` 加入项目导入路径；
- 清空项目 `addopts`，禁用自动第三方插件加载，只显式加载允许的 asyncio 插件；
- 禁用 pytest cache provider，将 rootdir/confcutdir 锁定在快照内；
- 固定 quiet/short traceback/maxfail/color/warning 参数，用户不能注入新 argv。

pytest 会执行目标测试、它导入的项目代码和快照内可见的 `conftest.py`；它们均按不可信代码处理，不因为“是测试”获得宿主权限。

## 4. Windows 强隔离

每次执行都创建新 AppContainer profile 和 Job Object，不可用时直接拒绝，不降级到普通子进程：

- AppContainer 无网络 capability；
- 仅当次 AppContainer SID 获得当次快照的只读/执行 ACL；
- 唯一可写位置是当次 profile 映射的 scratch，任务结束后删除；
- 测试进程环境经白名单重建，不携带 API key、Provider credential 或应用秘密；
- 内存上限 512 MiB，活动进程上限 1，超时 60 秒；
- 输出最多 32 KiB，超限保留首尾并显式标记；快照、运行时与用户路径会被替换为中性占位符。

真实 Windows 集成测试不只检查 `isolation_mode` 字段：快照内的测试会主动尝试读取原项目文件，只有读取被 Windows 拒绝且两个 pytest 用例都通过时才算验收成功。

## 5. 结果证据与前端

`WorkspacePythonTestRead` 记录：

- project/test path、snapshot digest 与 runtime digest；
- `passed` / `failed` / `error`、exit code 和 passed/failed/skipped/error 计数；
- 用时、受限输出与截断状态；
- `windows_appcontainer`、`network_access=false`、`process_limit=1`；
- 覆盖上述全部字段的 result digest。

Route 重载时会重新验证 Pydantic schema、状态/exit code 对应关系和 result digest。测试断言失败表示“测试能力成功执行并产生 failed 证据”；隔离或运行时故障才使 Route 本身失败。

前端仍是对话优先 Agent Workbench：用户在底部对话框下达测试任务，当前 Run 显示执行状态，右侧证据层显示测试文件、计数、时长、两个摘要、隔离事实和输出。没有新建表单式“测试控制台”，也没有把证据伪装成普通聊天文本。

## 6. 验收和边界

本阶段已通过：

1. 项目快照边界与摘要单元测试；
2. 固定 Route 的 Conversation API → Contract/Plan → Run → result projection 端到端测试；
3. 真实 Windows AppContainer 只读快照、原项目拒读、scratch 写入与 pytest 执行集成测试；
4. 前端 Python Test 证据卡组件测试与 Vue 类型检查；
5. Ruff 与生产源码 mypy。

最终定向验证为：阶段 76 对话工作台、工作区快照与 AppContainer worker 相关 27 项后端用例通过；独立真实 Python 测试沙箱用例通过。Ruff 全源码/相关测试、mypy 218 个生产源码、Alembic `0037_turn_routes` head/autogenerate check 和 `uv lock --check` 通过。前端 22 个测试文件/150 项、Vue type-check、production build 和工作台静态检测 `p0: 0` 通过，`git diff --check` 无空白错误。

本阶段复用 `0037_turn_routes` 的 result manifest，不需要数据库迁移。为了避免重复阶段 81 耗时数小时的全量高负载回归，本阶段按风险选择定向后端/真实沙箱/前端全量测试，并不宣称新的后端全量 pytest 结果。按界面技能的组件范围预检约束，本次没有自行打开用户页面做视觉断言。

当前边界是“一个显式 Python pytest 文件”，不是通用终端。下一阶段可以独立实现内容寻址 Node/Vitest profile；之后再评估新建/重命名等可恢复工作区操作。自由 Shell、任意 executable/argv、原项目写入、联网依赖安装和凭据继承仍不在权限边界内。
