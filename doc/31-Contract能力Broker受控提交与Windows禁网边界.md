# 31. Contract 能力 Broker、受控提交与 Windows 禁网边界

## 1. 阶段结果

本阶段把 `ToolContract.security.capabilities` 从“授权事实”推进到了真实执行边界，并为尚未开放的写工具和网络工具建立 fail-closed 门槛。

已完成：

- `filesystem.metadata.read` 由常驻父 Runner 打开并核验精确资源句柄；
- 磁盘容量由受信任 broker 查询并转换为不可变 worker facts；
- worker 不再根据原始 `path` 打开文件系统资源；
- worker 校验 brokered resource operations 的并集必须与 Contract capabilities 完全一致；
- Contract 新增 `read_only / brokered` commit protocol；
- 声明副作用的 Tool 若未使用 brokered commit，Contract 无法注册；
- 尚无 commit provider 时，side-effect handler 在调用前被拒绝，不能以进程内回调冒充受控提交；
- Windows 新增无网络 capability 的 AppContainer 启动模式；
- Runner hello 明确报告进程隔离与网络隔离两种独立姿态；
- 强制禁网会预启动真实 worker，运行时不可访问时 Runner 启动失败，不会退回普通 Low Integrity；
- Job Object、挂起启动、每调用独立进程、精确三管道句柄白名单和环境秘密隔离继续保留。

核心边界是：**父 Runner 可以访问已授权资源，但一次性 worker 只能消费 broker 生成的窄化事实；如果某项 capability 没有对应 broker 或 OS enforcement，调用必须停止在 handler 之前。**

## 2. Brokered resource 执行链

```mermaid
flowchart LR
    AUTH["签名授权\n精确 resource scope"] --> BROKER["父 Runner resource broker"]
    BROKER --> HANDLE["CreateFileW\nFILE_READ_ATTRIBUTES"]
    HANDLE --> VERIFY["GetFinalPathNameByHandleW\n核对授权最终路径"]
    VERIFY --> FACTS["卷容量只读 facts"]
    FACTS --> FRAME["deskpilot.worker.v1\nBrokeredFilesystemMetadata"]
    FRAME --> CHECK["capability coverage\n完全相等"]
    CHECK --> HANDLER["一次性 worker handler\n不打开原始 path"]
```

父 Runner 使用授权中已经绑定的规范 `filesystem_path`：

1. `CreateFileW` 以 `FILE_READ_ATTRIBUTES` 打开目录或文件，允许 read/write/delete sharing，避免干扰正常宿主操作；
2. `GetFinalPathNameByHandleW` 从已打开句柄取得最终 DOS 路径；
3. 将最终路径与签名授权 identifier 做 Windows 规范化比较；
4. `GetVolumePathNameW` 找到对应卷；
5. `GetDiskFreeSpaceExW` 查询 total/free，并在父进程计算 used；
6. 所有成功、异常和取消路径都关闭资源句柄；
7. worker 只收到 provider、kind、identifier、operation 和容量数值。

这里没有把目录句柄交给 Python worker。原因是当前唯一工具所需的 `GetDiskFreeSpaceExW` 官方接口接受目录路径而不是句柄；让 worker 从句柄重新取路径再调用同一 API，既没有增加最小权限，也会重新暴露 ambient filesystem access。当前实现让句柄只存在于受信任 broker，worker 接收已经窄化的元数据事实。

`BrokeredFilesystemMetadata` 额外验证 `used + free == total`。worker executor 再计算所有 resource operations 的集合，并要求它与当前 allowlisted Contract capabilities 完全相等。缺失、额外或错误 operation 都返回 `TOOL_RESOURCE_CONTEXT_INVALID`，handler 不运行。

## 3. 受控提交门槛

`ToolExecutionContract.commit_protocol` 目前有两种值：

| 值 | 语义 |
| --- | --- |
| `read_only` | handler 只能产生可验证输出，不允许声明副作用 |
| `brokered` | Tool 需要未来受信任 commit provider 执行最终外部写入 |

`ToolContract` 在模型验证阶段强制：只要 `side_effects` 非空，commit protocol 就必须是 `brokered`。

当前还没有真实写工具，因此系统没有伪造一个“prepare 后直接在 worker 内写入”的半成品。即使 Contract 正确声明 `brokered`，executor 也会在调用 handler 前返回 `TOOL_CONTROLLED_COMMIT_UNAVAILABLE`。后续写工具必须补齐：

1. worker 生成无副作用的 prepare result；
2. 父 Runner 校验 prepare result 与授权、预览和资源版本；
3. 受信任 commit provider 执行一次提交；
4. 持久化 commit receipt 和外部资源版本；
5. timeout/cancel 后按是否越过 commit 边界收敛为 failed 或 unknown。

## 4. Windows AppContainer 网络隔离

Job Object、受限令牌和 Low Integrity 都不负责网络授权。本阶段新增经典 AppContainer 路径：

- 每次调用创建唯一 AppContainer profile；
- `SECURITY_CAPABILITIES` 的 capability list 为空，因此不授予 `internetClient`、`internetClientServer` 或 `privateNetworkClientServer`；
- `STARTUPINFOEXW` 同时携带 `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 和 `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`；
- 子进程仍只继承 stdin/stdout/stderr；
- 子进程以 suspended 状态创建，先加入原有资源受限 Job Object，再恢复首线程；
- `LOCALAPPDATA/TEMP/TMP` 指向该次 AppContainer profile；
- 进程、Job、管道和 profile 在 `finally` 中回收。

Runner 握手现在分别报告：

```text
isolation_mode = process_only | windows_restricted | windows_appcontainer
network_isolation_mode = none | appcontainer
```

配置：

```dotenv
DESKPILOT_RUNNER_REQUIRE_WINDOWS_SANDBOX=true
DESKPILOT_RUNNER_REQUIRE_NETWORK_ISOLATION=false
```

开启 `RUNNER_REQUIRE_NETWORK_ISOLATION` 后，常驻 Runner 会用真实 worker command 做一次 AppContainer 预启动。只有 worker 可以加载解释器、DeskPilot 模块并通过私有管道返回帧，Runner 才发送 hello。任何 AppContainer API、运行时 ACL 或模块加载失败都会使本代 Runner 启动失败，Supervisor 可以观测并重试，但不能降级到 `windows_restricted`。

开发环境默认仍为 `false`，原因是当前工作区 Python 位于用户缓存目录，venv 与源码也位于开发目录；这些路径没有授予 AppContainer read/execute。真实探针返回 `0xC0000135`，说明动态加载器无法访问 Python DLL。系统 `bfscfg.exe` 的只读策略在本机只提供 Query Only，完整 broker 策略也没有解决动态加载。因此当前不能声称开发态 Python worker 已默认禁网。

发布态要启用强制禁网，必须先提供 AppContainer 可读/可执行的专用 worker runtime（例如安装阶段受控 ACL、可验证的 BFS 投影或稳定后的系统 sandbox API），然后把该配置改为发布默认 `true`。

## 5. 系统 API 探针结论

本机 Windows 版本导出了 `processmodel.dll!Experimental_CreateProcessInSandbox`，微软文档也描述了 AppContainer、BFS read-only/read-write 与 network policy。但该 API：

- 仍标记为 experimental；
- 当前接口明确禁止 `inheritHandles=TRUE`，不能直接复用现有私有三管道协议；
- 本机实际调用返回 `ERROR_CALL_NOT_IMPLEMENTED (120)`。

因此主干没有依赖该实验 API，也没有增加 FlatBuffers 运行时依赖。后续只有在 API 真正可用、schema 稳定且通信协议完成适配后才重新评估。

参考的 Microsoft 官方资料：

- [Launch an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
- [AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)
- [UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)
- [GetDiskFreeSpaceExW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getdiskfreespaceexa)
- [Create Process in Sandbox](https://learn.microsoft.com/en-us/windows/win32/secauthz/createprocessinsandbox)

## 6. 验收

完成时：

```text
Ruff:  All checks passed
mypy:  Success, 92 source files
pytest: 247 passed
Alembic: 0008_tool_reconciliation (head), no schema drift
frontend vitest: 11 files, 100 passed
frontend type-check/build: passed
```

新增自动化覆盖：

- worker 使用 broker facts 时不打开 arguments 中的路径；
- capability resource 缺失时在 handler 前拒绝；
- 容量事实不一致时协议模型拒绝；
- Windows 连续 32 次资源核验后进程句柄数不增长；
- side-effect Contract 不能使用 read-only commit；
- 无 commit provider 时 side-effect handler 绝不运行；
- AppContainer 启动器报告正确网络姿态；
- 父进程在 loopback 开放监听时，容器内系统 `curl.exe` 两秒内无法建立连接，父进程未收到任何连接；
- 所有 AppContainer 集成测试结束后没有残留 `DeskPilot.Worker.*` profile。

## 7. 已知边界与下一步

- 当前真实 Tool 的读能力已经 broker 化，但只覆盖卷容量元数据，不代表已经支持任意文件内容读取。
- AppContainer enforcement 已实现并通过系统可执行文件证明；开发 Python worker 因运行时 ACL 不可达而只能在强制配置下 fail closed，不能默认启用。
- profile 使用每调用唯一 identity；正常路径会清理，宿主进程被强杀时仍需发布态启动 reaper 清理孤儿 profile。
- controlled commit 目前是 Contract 与 executor 门槛，还没有 prepare/commit/receipt 实现；在此之前继续禁止注册真实写 Tool。
- 非 Windows 兼容 launcher 仍只有进程分离，没有可证明网络隔离；强制网络隔离会拒绝启动。
- 下一安全阶段应先构建 AppContainer-compatible 的专用 worker bundle/安装 ACL 与孤儿 profile reaper，使真实 Python Tool 可以在 `network_isolation_mode=appcontainer` 下端到端运行；之后再为第一个可逆写 Tool 实现 prepare/commit/receipt。
