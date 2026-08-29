# 阶段 116C-A：离线真实仓库任务与预检 harness

## 结果与边界

本检查点完成了 116C 的离线准备层，不是真实模型质量验收。新增不可变 `deskpilot.workspace-repository-task-suite.v1`，冻结 8 个公开上游仓库的 20 个历史修复/功能任务：Python 10 个、Node 10 个。suite digest 为 `8260414a0f4ed8cc513d8519e6ebe9afd4ad6d228054a6574b4a947d72afffa9`。

本批没有执行 Candidate/Judge Provider 调用、真人评审、Production Admission 或 cloud activation；没有修改任务 Runtime、开放自由 Shell、安装依赖或 push。cloud-only cohort 保持 disabled，因此不得将本检查点解释为 115B、116C-B、真实模型成功率或 Codex 等价已完成。

## 真实仓库任务矩阵

任务来源是上游真实提交，而不是将玩具 fixture 命名为“真实仓库”。每项绑定 credential-free GitHub HTTPS 来源、base/reference commit、base tree listing SHA-256、reference binary diff SHA-256、精确变更路径、验收测试路径、固定 Command Profile 与自然语言轮次。

| 生态 | 仓库 | 任务数 | 冻结内容 |
| --- | --- | ---: | --- |
| Python | `pypa/sampleproject` | 1 | 新增最小模块与回归测试 |
| Python | `pallets/itsdangerous` | 1 | FIPS 环境下延迟访问 SHA-1 |
| Python | `python-hyper/h11` | 2 | Content-Length 上限、chunk footer 校验 |
| Python | `jazzband/prettytable` | 6 | tab 对齐、列配置、颜色换行、CSV formatter、字典校验和类型覆盖 |
| Node | `unjs/defu` | 4 | 继承属性、原型污染、Module namespace、plain object 合并 |
| Node | `unjs/destr` | 2 | 无 `String.at` 兼容与已知字面值快速路径 |
| Node | `unjs/ohash` | 3 | falsy diff、locale-independent 序列化、Map 对象键 |
| Node | `unjs/ufo` | 1 | `withoutBase` 连续前导斜杠归一化 |

完整矩阵包含 3 个 single-file 任务和 17 个 multi-file 任务；其中各 4 个任务绑定有界 Repair、用户中途 amendment、进程重启 checkpoint 和两 Reader 并行调查。对应关系受 Pydantic 严格模型交叉校验，不允许通过改标签虚构覆盖。

## 质量阈值与零容忍

- 每个任务重复 3 次，总计 60 个 trial；单任务至少 2/3 通过才算成功。
- 生产质量门同时要求至少 16/20 任务成功、至少 48/60 trial 成功，即不低于 80%。
- `false-success=0`、`unauthorized-effect=0`、越界路径写入、网络效果和 Git remote 写入均为 0。
- 失败运行必须有可检查终态；不能用隐藏重试、手工改库或参考 diff 字节相同替代服务器测试与安全 Oracle。

阈值已被冻结，但本批没有产生任何 trial 质量结果。

## 只读离线预检

`WorkspaceRepositoryOfflinePreflight` 只接受 operator 事先放置的 bare Git mirror。上游 URL 只是 provenance，harness 不执行 clone、fetch、checkout、dependency install 或任何 Provider 请求。

预检会：

1. 校验 mirror root 与逐级路径没有 symlink/reparse、路径逃逸、特殊文件或超限内容；
2. 以 `GIT_OPTIONAL_LOCKS=0`和固定本地 Git 参数只读解析 base/reference/frozen-head commit；
3. 复算 base tree listing SHA-256、reference diff SHA-256 和变更路径顺序；
4. 验证 reference 内测试路径、base 内许可证和 Node `pnpm-lock.yaml`；
5. base/reference 任一侧含 submodule 或 Git LFS pointer 时 fail closed；
6. 输出只含 suite/repository/task 身份和边界布尔值的 `deskpilot.workspace-repository-preflight.v1`。

mirror 目录布局由 manifest 固定为 `<mirror-root>/repositories/<repository-id>.git`。操作命令：

```powershell
python -m deskpilot.phase116c_offline_gate manifest
python -m deskpilot.phase116c_offline_gate preflight --mirror-root <operator-staged-root>
```

冻结过程中曾一次性从公开上游取得 commit/archive 身份，然后将 8 个仓库转换为本地 bare mirror 执行完整无网预检。结果为 8/8 仓库、20/20 任务、60 个冻结 trial 身份全部对账；该过程没有模型或 Judge 网络请求。bare mirrors 不进入源码仓库或 wheel，生产验收时必须由 operator 重新 staging 并通过同一只读预检。

## 一次性物化与清理规则

- 物化目标必须是新的唯一 run 目录，不存在时才能从已验证 mirror 的 exact base commit 建立 detached snapshot。
- 拒绝 symlink/reparse、submodule、Git LFS pointer，最多 20,000 文件 / 256 MiB。
- 运行时继续断网，不安装依赖，不允许 Shell 或 remote Git 效果。
- 清理只能删除已解析的 exact unique run directory，不能对工作区、仓库根或计算路径做宽泛递归删除。
- 清理最多重试 3 次；仍失败时记录 `cleanup_pending`、永不复用，残留必须位于源仓库之外。

这条规则吸收了 Windows 删除策略可能拒绝即时清理的现实边界。已知外部临时残留 `C:\Users\29832\AppData\Local\Temp\deskpilot-frozen-concurrent-kill-84556-1787946066085229200` 不在仓库或本提交中，本检查点没有触碰或尝试删除它。

## 自动化验收

- 新增 9 项专项用例，覆盖 20 任务矩阵、CLI manifest、YAML alias/未知字段/缺任务、阈值与 cloud 边界漂移、本地 bare mirror 完整预检、缺 mirror 和 reference diff 漂移 fail-closed。
- 专项 pytest 9/9；默认后端 892 项，完整运行 `880 passed + 12 skipped`、失败/错误为 0，用时 1:34:10。
- Ruff 全仓、strict mypy 313 个生产源码、frozen lock、60 包 `pip check`、wheel YAML 资源、workflow YAML、baseline immutable compare 和 diff whitespace 通过。
- 新增 Windows CI workflow，只允许 manifest/本地预检门、静态检查、wheel YAML 资源和 baseline/diff 不变性；没有 capture 入口。

## 下一步

116C-A 只到离线资产冻结为止。下一条真实产品路径仍是：

1. 用户明确给出 ADR-016 要求的 Candidate/Judge、代码出站、费用、真人评审和 activation actor 五项授权；
2. 完成 115B 真实 capture、盲审/真人评审、Production Admission 与激活/回滚验收；
3. 由 operator staging 冻结 mirrors，先运行本检查点的只读预检；
4. 在 116C-B 按 20 任务 / 60 trial 执行真实模型质量验收，仍不自动 push。
