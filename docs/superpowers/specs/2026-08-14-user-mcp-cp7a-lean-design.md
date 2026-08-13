# User MCP CP7-A 精简设计

日期：2026-08-14

适用范围：`main` 分支、开发环境、单机 Docker Compose

不适用范围：`prod`、生产灰度、生产证据与审批流程

## 背景

CP7-A 的目标是让开发环境只装配 user-scoped MCP，停止装配 legacy global MCP，并把可运行版本交给项目负责人进行人工测试。此前设计把生产级证据、候选生命周期、阶段信任根和隔离恢复流程一并纳入本地开发交付，导致实现范围偏离这一目标。

本设计采用已批准的方案 A：保留已经提交且通过回归的 durable MCP authority 与前端行为，撤回尚未提交的复杂部署/信任链改动，再用最小单机 Compose 完成 CP7-A。

## 目标

CP7-A 完成后，开发环境应满足：

- user-scoped MCP gateway 已启用；
- 路由为 `enforce`，覆盖率为 `100`；
- legacy global MCP runtime 不装配；
- 普通任务继续可用；
- 显式 MCP 任务使用当前用户自己的 MCP Server；
- 没有可用 Server 时按既有 durable no-server 合同失败，不回落 global MCP；
- 重启、远程任务和未知结果继续使用 G002/G003 已实现的恢复与 no-replay 语义；
- 前端继续展示既有 unavailable、unknown 与 late-result 状态；
- 项目负责人可以在单机 Compose 中进行人工功能测试。

## 保留范围

保留提交：

- `3852c0a cp7-0: add durable MCP authority foundations`
- `1ebc641 cp7-0: wire durable MCP runtime and safety`

这些提交中的持久化 intent/outbox、terminal receipt、恢复、无 Server 收敛和前端事件语义不在本轮重新设计或拆分。它们已经形成相互依赖的运行时合同，手术式删除会增加数据与恢复不一致风险。

## 撤回范围

完整撤回 `1ebc641` 之后尚未提交的 G004 工作树改动，包括：

- 四目标 Docker 构建和 validation runner；
- hardened runtime-sidecar 容器文件系统策略；
- phase/candidate-scoped staging volume；
- Sidecar manifest、allowlist、SBOM、provenance 绑定；
- live image binary extraction verifier；
- offline/no-fetch Rust candidate gate；
- `MAF_CONFIG_PATH` 的 CP7 phase trust preflight；
- G004 新增的部署、staging、trust 和 live-verifier 测试。

撤回前必须在仓库外生成完整二进制 patch 备份。撤回只允许作用于已列明的 G004 文件；不得触碰用户的根 `AGENTS.md`、`周报.md` 或 `docker_cmd.md`。

## 最小部署设计

使用单机 Docker Compose 的三个运行服务：backend、frontend 和 runtime-sidecar。`enforce` 启动门禁已明确要求兼容的 runtime-sidecar，因此 Sidecar 属于运行依赖；stager、validation runner、PostgreSQL validation profile、候选生命周期服务和证据服务不属于本轮部署。

backend 仅增加/固定以下开发环境变量：

```text
MCP_USER_SCOPED_GATEWAY_ENABLED=true
MCP_ROUTING_MODE=enforce
MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=false
MCP_ENFORCE_COHORTS=
MCP_ENFORCE_PERCENT=100
MCP_ENFORCE_HASH_SALT=main-cp7a-user-scoped-v1
MCP_ENFORCE_COHORT_CONFIG_FILE=
```

既有 user MCP credential、临时结果容量与状态存储配置继续沿用项目当前开发配置，不另建 phase trust 配置层。runtime-sidecar 只保留构建、Unix socket、兼容性握手、健康检查和运行所需的 SQLite 卷；不引入 artifact provenance、SBOM、阶段信任根或候选证据合同。

## 人工测试边界

自动验收仅证明：

- Compose 可以展开、构建并启动；
- backend/frontend 健康；
- global MCP runtime 未装配；
- user-scoped MCP 路由为 enforce 100%；
- ordinary task、无 Server、user-scoped dispatch、重启恢复的现有定向回归通过；
- 前端类型检查与相关事件测试通过。

自动验收通过后停止实施，由项目负责人测试 Server CRUD、credential 重启、授权允许/拒绝、跨用户隔离、长调用恢复、远程任务和无 Server 行为。

## CP7-B 退役门

CP7-A 不删除 legacy global MCP 源码。只有在当前 CP7-A 版本完成项目负责人人工测试后，项目负责人发送一条新的顶层消息，完整内容精确为：

```text
可以退役
```

才开始 CP7-B：删除 global MCP runtime 的装配、注册表与 revision 生命周期代码，同时保留 user-scoped MCP 仍使用的协议 adapter、transport、历史 DTO/枚举与解析兼容层。

## 错误处理与回滚

- CP7-A 启动失败时直接回到提交 `1ebc641` 对应的 legacy-on Compose 配置；不切换 `prod`。
- 不自动替换用户配置或密钥。
- 不迁移或删除现有 legacy 数据。
- 不对已可能发出副作用的 MCP 调用进行重放。
- 所有回滚操作使用显式文件清单，不使用 broad reset、stash-all 或清理命令。

## 验收标准

1. G004 未提交改动已备份并从工作树移除，受保护文件保持原状。
2. 精简 Compose 只包含运行所需服务，不包含 staging、validation、evidence 或 approval 服务。
3. 七项 MCP 配置精确匹配本设计。
4. backend 启动日志与定向测试证明 global MCP runtime 未装配。
5. 相关后端、前端和 Compose 测试通过；无法执行的环境测试明确列为人工测试项。
6. 未执行 CP7-B，未修改 `prod`。
