# User-scoped MCP CP7 Manual Retirement Design

## 状态

设计于 2026-08-13 获批，只适用于开发分支 `main`。生产分支 `prod`、生产容器、生产流量和生产数据均不在本设计的执行范围内。

本设计取代原 Phase 3 Runbook 中“CP-7 定时灰度观察后再执行 CP-8”的执行顺序：不再等待 24 小时、48 小时或 7 天观察窗，也不以 production evidence 作为本次开发分支退役门禁。原 CP-8 物理删除并入 CP-7，但仍保留一个不可跳过的人工确认点。

这项流程调整不能被表述为生产 CP-7 已完成。未来把结果合入或部署到 `prod` 时，仍应由生产发布流程独立决定其准入、回滚和验收要求。

## 目标

在 `main` 上分两步退役进程级全局 MCP Runtime：

1. CP7-A 交付只装配 user-scoped MCP Gateway 的 Docker Compose 测试候选，同时保留 legacy 源码。
2. 用户完成人工测试并明确回复“可以退役”后，执行 CP7-B，物理删除 legacy 全局 Runtime 的代码路径。

在两个步骤中都必须保留 user-scoped MCP 所需的 Client、Transport、协议 Adapter、远程任务恢复、Rust Sidecar、凭据、Grant、审计、安全校验和用户数据。

## 明确不做的事项

- 不修改、切换、合并或部署 `prod`。
- 不操作真实生产容器、镜像仓库、DSN、密钥或流量。
- 不实现或伪造原 CP-7 的定时观察窗和 production evidence。
- 不把本地测试、SQLite 数据或人工测试结果标记为 production evidence。
- 不在 CP7-A 删除 legacy 源码。
- 不改变单机 Compose 的停机、代理或端口切换策略。
- 不删除历史记录、用户 Server、密文、Grant、Task、audit 或 rollout ledger。

## 方案选择

采用“单个 Compose 候选 + 两阶段代码退役”：

- CP7-A 修改现有开发 Compose 默认值，使默认启动即为 user-scoped `enforce=100%`、legacy assembly off。
- 不创建 production rollout ops 镜像，因为本轮不产出 production evidence，也不执行 production approval/activation。
- 用 OCI revision label、Git commit 和本地 image ID/digest 固定人工测试工件。
- CP7-A 完成后停止实施，等待用户人工验收。
- CP7-B 只能由用户明确的“可以退役”触发。

相比“直接删除 legacy”方案，这一方案保留了人工测试期间的代码级回退点；相比继续维护多阶段灰度配置，它不保留已经被明确取消的观察复杂度。

## CP7-A：assembly-off 测试候选

### 运行配置

开发 Compose 的默认 MCP 配置为：

```text
MCP_USER_SCOPED_GATEWAY_ENABLED=true
MCP_ROUTING_MODE=enforce
MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=false
MCP_ENFORCE_COHORTS=
MCP_ENFORCE_PERCENT=100
MCP_ENFORCE_HASH_SALT=main-cp7a-user-scoped-v1
```

CP7-A 期间仍保留 `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED`，但它只用于证明 legacy assembly 已关闭。候选配置不得把它设为 `true`。代码级回滚依赖退役前 Git commit 或候选镜像，而不是运行时重新打开 legacy。

`MAF_API_ENV` 仍保持开发环境语义。CP7-A 不要求 production rollout admission ID 或 production role DSN。

### 必须证明的 assembly-off 边界

启动和运行期间必须满足：

- 不构造 `MCPRuntimeState` 全局实例。
- 不创建 legacy 全局 MCP Client。
- 不执行启动期 legacy `server/discover` 或 `tools/list`。
- 不把 legacy Server/Tool 动态注册为全局 capability 或 instance。
- 不为新 Task 写入或依赖 legacy bundle revision。
- 新 Task 只可选择 user-scoped 路径；没有可用用户 Server 时返回现有的明确不可用结果，不得静默回落到 legacy。
- 已固化为 legacy 的非终态历史 Task 不得换路径重放；应 fail closed。终态历史仍可读取。

### Docker 候选工件

backend 和 frontend 仍从当前多阶段 `Dockerfile` 构建。候选 manifest 必须分别记录两个镜像的：

- 构建所用的精确 `main` Git commit；
- OCI `org.opencontainers.image.revision`；
- 本地镜像名称和 image ID；本轮不推送镜像仓库；
- 只含上述六个 MCP 非敏感环境项的 canonical JSON 与 SHA-256；
- 构建和测试时间。

以上非敏感记录写入 Git-ignored 的 `runtime/cp7-a/manifest.json`，由交付摘要引用，不提交到版本库。

候选镜像只能从候选 commit 的干净 Git archive 构建，不能直接把当前工作树作为 Docker build context。这样镜像内容与 revision label 一致，并确保 Git-ignored 的本地文件、未跟踪报告和无关用户改动不会进入构建上下文。构建流程不得读取这些本地文件的内容。

不得把密钥、DSN、真实凭据或本地敏感配置写入镜像标签、记录文件或 Git。

### 自动验证

CP7-A 用以下固定入口产生自动化证据：

| 证明项 | 固定验证入口与判定 |
|---|---|
| Compose 默认配置 | 新增 `tests/deployment/test_user_mcp_cp7a_compose_contract.py`，解析 Compose 展开结果并精确断言上述六项值。 |
| assembly-off 装配 | `tests.api.test_user_mcp_runtime_wiring` 加 CP7-A 回归：legacy Client factory 调用数、startup discovery 数和动态 MCP capability 数均为 0。 |
| 用户 Server 与授权调用 | 运行 `tests.integrations.mcp.test_user_mcp_credentials`、`tests.integrations.mcp.test_user_mcp_gateway`、`tests.integrations.mcp.test_dispatch_coordinator` 和 `tests.capabilities.mcp_tool.test_executor`，结果必须全部通过。 |
| 2025/2026 与恢复 | 运行 `tests.integrations.mcp.test_2025_11_25_task_recovery`、`tests.integrations.mcp.test_2026_07_28_adapter`、`tests.integrations.mcp.test_user_mcp_recovery_worker` 和 `tests.api.test_user_mcp_recovery_startup`，结果必须全部通过。 |
| authority 与 no-replay | 运行 `tests.storage.test_mcp_task_route_assignment`、`tests.storage.test_mcp_recovery_claims`、`tests.orchestration.test_fake_capability_flow` 和 `tests.orchestration.test_runtime_replanning`，结果必须全部通过。 |
| Docker 构建与健康 | 依次执行 Compose config 校验、backend/frontend build、`up -d`；两个服务必须为 `running` 且 Docker health 状态为 `healthy`，backend `/api-doc` 与 frontend `/seedpilot/` 均返回 2xx。 |
| legacy 零活动 | 新增 `scripts/verify_user_mcp_cp7a_candidate.py`，只扫描本次启动时间之后的 backend stdout 与 audit 输出，拒绝 legacy Client construction、startup discovery/`tools/list` 和动态 MCP capability registration；同时验证 public capability 列表没有 legacy 动态描述符。 |

验证脚本必须把 Git commit、两个镜像的 revision label/image ID、六项 MCP 配置及其摘要、健康结果、日志起止时间和测试命令结果写入 `runtime/cp7-a/manifest.json`。任何命令失败、日志区间不完整、镜像 revision 不等于当前 commit、服务不健康或 legacy 活动命中都返回非零状态，不生成成功 manifest。

自动验证只证明候选实现符合仓库契约，不代替用户人工测试。

## 人工测试门禁

CP7-A 完成后，实施必须停止。向用户交付候选 Git commit、镜像标识、Compose 启动方式和以下人工检查表：

- 新增、修改、禁用和删除用户 MCP Server。
- 凭据保存与重启后继续可用。
- 健康检查、工具发现、授权允许/拒绝和普通调用。
- 长调用继续/取消、remote task、MRTR/input-required 和结果恢复。
- backend 容器重启后的 Task/Node/结果恢复。
- 无可用用户 Server 时的明确不可用反馈。
- capability 列表和启动日志中没有 legacy 动态 MCP。
- 测试期间没有跨用户访问、未知结果重放或 secret 暴露。

只有用户在该候选交付后明确回复“可以退役”，才能进入 CP7-B。“测试通过”“看起来正常”或其他近似表述不能自动触发物理删除。

## CP7-B：物理退役

### 删除范围

收到明确触发词后，从 `main` 删除：

- 进程级 `MCPRuntimeState` 的全局 Server/Client/bundle/active revision/retention 生命周期；
- API 启动期 legacy refresh、discovery、动态 capability/instance 同步和相关 audit 路径；
- Task 上 legacy bundle revision 的新写入、retain/release 和恢复依赖；
- legacy 全局 executor 的 Server/Tool binding 解析与调用路径；
- legacy fallback 路由及用于重新装配旧 Runtime 的配置入口；
- 只服务于以上路径的测试、配置和文档。

### 保留范围

必须保留：

- user-scoped `mcp.dispatch` 及其 Gateway、Coordinator 和授权流程；
- User MCP Client Factory、Transport 和五版本协议 Adapter；
- 2025/2026 remote-task 与 MRTR 恢复；
- Endpoint Policy、SSRF、Header、Schema、Credential 和 audit 安全边界；
- Rust Sidecar Task/TaskNode authority 与 durable continuation；
- 所有用户 Server、Credential、Grant、Task、结果、audit 和 rollout ledger 数据；
- 历史终态 legacy Task、metric 和 audit 的只读解析兼容。

数据库中的历史 `legacy`/`legacy_global_runtime` 枚举值不得因为源码退役而做破坏性删除。非终态 legacy Task 在重启时只能以固定安全错误收敛，不能改派到 user-scoped 路径。

### 旧环境变量

CP7-B 从 Compose、配置模型和功能分支中删除 `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED`。为防止旧部署配置被静默接受，保留一个无功能的启动 tombstone：只要环境中仍出现该变量，就以固定错误 `legacy_runtime_retired` 拒绝启动。该 tombstone 不得重新装配任何 legacy 对象。

### 回滚

CP7-B 的回滚只允许切回 CP7-A 记录的 Git commit/镜像。数据层保持 additive，禁止删除或回滚用户数据、credential key 和 append-only ledger。

如果 CP7-B 后发现未知或在途 legacy 调用，系统只做 fail-closed 收敛，不跨执行路径重放。

## 文档与状态同步

CP7-A 实施时更新 Compose、人工测试说明、相关 AGENTS 索引和 CHANGELOG，但继续将状态标记为“开发候选，等待人工验收”。

CP7-B 实施时更新 Phase 3 PRD、Runbook、docs 索引和 CHANGELOG：

- 删除原 CP-8 独立阶段；
- 记录定时观察窗已由项目负责人明确取消；
- 把人工确认记录为代码退役门禁；
- 明确该结论只发生在 `main`，不宣称已部署到 `prod`。

## 完成标准

### CP7-A 完成

- 所有 assembly-off、user-scoped、恢复、安全和 Docker 候选检查通过。
- 候选 commit、镜像标识和人工清单已交付。
- legacy 源码仍存在。
- 实施已停止并等待用户回复“可以退役”。

### CP7-B 完成

- 已收到并记录用户明确的“可以退役”。
- legacy 全局 Runtime 的可执行代码和重新装配入口已删除。
- user-scoped MCP、恢复、安全与历史只读兼容测试通过。
- Docker 候选可构建并启动。
- `prod` 未被修改、切换或部署。
- Git 提交边界中不包含本地敏感文件或无关用户改动。
