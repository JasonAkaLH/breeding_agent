# User MCP CP7-A 精简设计

日期：2026-08-14

状态：已实施并通过自动验收，待项目负责人人工测试

适用范围：`main` 分支、开发环境、单机 Docker Compose

不适用范围：`prod`、生产灰度、生产证据与审批流程

## 规范优先级

本设计是 `main`/开发环境 CP7-A 的当前实施依据，并取代
`2026-08-13-user-mcp-cp7-manual-retirement-design.md` 中与 CP7-A/G004
部署、候选证据、staging 和阶段信任有关的要求。旧设计仅保留为历史背景，以及
CP7-B 删除范围的参考；若两份文档冲突，以本设计为准。

## 背景与目标

CP7-A 的目标是让开发环境只装配 user-scoped MCP，停止装配 legacy global
MCP，并把可运行版本交给项目负责人进行人工测试。此前设计把生产级证据、候选
生命周期、阶段信任根和隔离恢复流程一并纳入本地开发交付，导致实现范围偏离
这一目标。

本设计采用已批准的方案 A：保留已经提交且通过 CP7 定向回归的 durable MCP
authority 与前端行为，撤回尚未提交的复杂 G004 部署/信任链改动，再用最小
单机 Compose 完成 CP7-A。

CP7-A 完成后，开发环境必须满足：

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

这些提交中的持久化 intent/outbox、terminal receipt、恢复、无 Server 收敛、
durable safety guard 和前端事件语义不在本轮重新设计或拆分。它们形成了相互
依赖的运行时合同，手术式删除会增加数据与恢复不一致风险。

本轮保留 CP7 local-safety 代码，但不启用候选/epoch/approval evidence
lifecycle。默认 Compose 不构造 `CP7RuntimeIdentity`、Ready epoch 或审批快照；
请求安全继续由 owner mutation guard、权限边界、terminal receipt 和 no-replay
合同保证。这一取舍仅适用于本设计限定的开发环境，不改变生产门禁。

已知验证基线：上述提交通过 CP7 定向回归。仓库 broad API 套件存在已在旧基线
复现的非 CP7 失败，因此不得把“CP7 定向回归通过”表述成“仓库全部测试通过”；
实施结果必须分别报告本轮测试结果和既有基线失败。

## 撤回范围与备份协议

完整撤回 `1ebc641` 之后尚未提交的 G004 工作树改动，包括：

- 四目标 Docker 构建和 validation runner；
- hardened runtime-sidecar 容器文件系统策略；
- phase/candidate-scoped staging volume；
- Sidecar manifest、allowlist、SBOM、provenance 绑定；
- live image binary extraction verifier；
- offline/no-fetch Rust candidate gate；
- `MAF_CONFIG_PATH` 的 CP7 phase trust preflight；
- G004 新增的部署、staging、trust 和 live-verifier 测试。

撤回前必须在仓库外建立可恢复备份，且只处理明确列出的 G004 路径：

1. 对 tracked G004 文件生成限定路径的 binary patch；
2. 对 untracked G004 文件生成独立归档；
3. 保存两类备份的文件清单和 SHA-256；
4. 验证备份可读后，才允许从工作树移除对应 G004 改动；
5. 不得读取、备份、修改、暂存或删除用户的根 `AGENTS.md`、`周报.md` 或
   `docker_cmd.md`。

不得使用 broad reset、`stash --all`、全仓清理或未限定路径的恢复命令。

## 最小部署设计

单机 Docker Compose 只包含三个长运行服务：`runtime-sidecar`、`backend` 和
`frontend`。不包含 stager、validation runner、PostgreSQL validation profile、
候选 lifecycle、evidence 或 approval 服务。

### runtime-sidecar

- Dockerfile 必须增加一个最小 Rust build/runtime target，通过
  `cargo build --release -p maf_runtime_sidecar --bin maf-runtime-sidecar` 构建并
  运行 Sidecar；
- 监听 `unix:///run/maf-runtime-sidecar/runtime.sock`；
- SQLite 路径为 `/var/lib/maf-runtime-sidecar/runtime.sqlite3`；
- Unix socket 和 SQLite 数据分别使用命名卷；
- Sidecar image 必须提供最小 `--probe` 命令，依次验证 Version、Compatibility
  和 Readiness；Compose 健康检查调用该命令，不能只检查容器进程存在；
- 不引入 artifact provenance、SBOM、阶段信任根或候选证据合同。

### backend

- 只读挂载 user MCP credential key；
- 共享 Sidecar Unix socket 卷；
- 在 Sidecar 健康后启动；
- 使用项目现有 SQLite runtime 卷和只读 Skill 挂载；
- 健康检查继续使用 backend HTTP 健康入口。

### frontend

- 仅依赖 backend 健康；
- 保持现有端口、base path 和静态构建方式。

## 开发环境 Sidecar 信任例外

保留基线 `1ebc641` 会在 MCP `enforce` 模式下要求 Sidecar artifact manifest 和
allowlist。为保持本地 CP7-A 简单，本轮必须增加一个闭合的开发环境例外：

- `MAF_API_ENV` 精确为 `dev`；
- Endpoint 精确为本机 Unix socket
  `unix:///run/maf-runtime-sidecar/runtime.sock`；
- `MAF_RUST_RUNTIME_STORE_MODE`、`MAF_RUST_EVENT_LOG_MODE` 和
  `MAF_RUST_TASK_DISPATCHER_MODE` 均精确为 `off`；
- manifest 和 allowlist 环境变量均未配置；
- 同时满足以上条件时，backend 可跳过 Sidecar artifact attestation，但仍必须
  完成 Version/Compatibility 握手；
- 任一条件不满足时继续使用既有 fail-closed manifest/allowlist 门禁。

该例外不得在 `prod`/`production`、TCP/远程 Sidecar 或任一 Rust authority
`shadow`/`enforce` 模式生效。

## 配置合同

backend 的七项 MCP 路由值必须精确为：

| 环境变量 | 值 |
| --- | --- |
| `MCP_USER_SCOPED_GATEWAY_ENABLED` | `true` |
| `MCP_ROUTING_MODE` | `enforce` |
| `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED` | `false` |
| `MCP_ENFORCE_COHORTS` | 空字符串 |
| `MCP_ENFORCE_PERCENT` | `100` |
| `MCP_ENFORCE_HASH_SALT` | `main-cp7a-user-scoped-v1` |
| `MCP_ENFORCE_COHORT_CONFIG_FILE` | 空字符串 |

同时必须提供以下最小运行配置：

| 配置 | 要求 |
| --- | --- |
| `MAF_API_ENV` | 精确为 `dev` |
| `MAF_STATE_STORE_BACKEND` | 精确为 `sqlite` |
| `MAF_STATE_PLATFORM_CONFIG_BRIDGE` | 精确为 `0` |
| `MAF_RUNTIME_SIDECAR_ENDPOINT` | `unix:///run/maf-runtime-sidecar/runtime.sock` |
| `MCP_CREDENTIAL_KEY_FILE` | 精确为 `/run/secrets/mcp-credential.key`；Compose 通过必填的 `MCP_CREDENTIAL_KEY_FILE_HOST` 只读挂载，宿主文件不进入 Git |
| `MAF_USER_MCP_MAX_ACTIVE_CALLS` | 开发默认值 `8` |
| `MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES` | 开发默认值 `1048576`（1 MiB） |
| 三项 Rust authority mode | `MAF_RUST_RUNTIME_STORE_MODE=off`、`MAF_RUST_EVENT_LOG_MODE=off`、`MAF_RUST_TASK_DISPATCHER_MODE=off` |

credential key 不得写入镜像、仓库、Compose 文件内容或日志。现有 user MCP
credential、terminal-result 容量和 SQLite 数据继续使用项目当前开发存储，不另建
phase trust 配置层。

## 自动验收与人工测试

自动验收只证明下表内容：

| 验收项 | 通过条件 |
| --- | --- |
| G004 撤回 | tracked patch、untracked 归档及 SHA-256 已生成；工作树中仅移除已列明 G004 改动；受保护文件不变 |
| Compose | 默认配置可展开；仅有三个长运行服务；可以构建并启动 |
| Sidecar | Unix socket 兼容性握手和健康检查通过；SQLite 数据可跨容器重启保留 |
| backend/frontend | 两个 HTTP 健康检查通过 |
| assembly-off | 既有 assembly test 证明 global MCP Client、startup discovery 和 global capability registration 均未装配 |
| 路由 | 七项 MCP 配置精确匹配；user-scoped MCP 为 enforce 100% |
| 后端行为 | ordinary task、无 Server、user-scoped dispatch、重启恢复的定向回归通过 |
| 前端行为 | 类型检查及 unavailable、unknown、late-result 相关测试通过 |
| 范围 | 未切换或修改 `prod`，未执行 CP7-B |

自动验收通过后必须停止实施，由项目负责人测试：

- Server CRUD；
- credential 重启；
- 授权允许/拒绝；
- 跨用户隔离；
- 长调用恢复；
- 远程任务；
- 无 Server 行为。

无法在当前机器执行的容器或网络测试必须明确列为人工测试项，不能伪装成自动
通过。

## CP7-B 退役门

CP7-A 不删除 legacy global MCP 源码。只有在当前 CP7-A 版本完成项目负责人
人工测试后，项目负责人发送一条新的顶层消息，完整内容精确为：

```text
可以退役
```

才开始 CP7-B：删除 global MCP runtime 的装配、注册表与 revision 生命周期代码，
同时保留 user-scoped MCP 仍使用的协议 adapter、transport、历史 DTO/枚举与解析
兼容层。

## 错误处理与回滚

- CP7-A 启动或定向回归失败时，停止当前三个服务，重新部署 CP7-A 前的
  backend/Compose 配置，或对精简部署提交执行显式 revert；不得 reset 工作树到
  `1ebc641`；
- 不自动替换用户配置或密钥；
- 不迁移或删除现有 legacy 数据；
- 不对已可能发出副作用的 MCP 调用进行重放；
- 所有恢复操作使用显式文件清单，不使用 broad reset、`stash --all` 或全仓清理；
- 回滚后必须确认 legacy-on backend 可启动，受保护文件仍存在且保持未跟踪/忽略
  状态。

## 风险与假设

- 开发环境 Sidecar attestation 例外是明确接受的本地风险，不代表生产策略；
- Sidecar 是 MCP `enforce` 的运行依赖，即使三项 Rust authority mode 为 `off` 也
  不能删除；
- G002/G003 的数据表和恢复合同保持不变，本轮不做数据迁移；
- broad API 已知基线失败单独记录，不阻断无新增回归的 CP7-A 定向验收；
- 人工测试通过前，legacy global MCP 源码必须保留。
