# 按需 MCP 灰度切换与旧 Runtime 下线 PRD

- **阶段**：三阶段改造第 3 阶段
- **范围**：灰度发布 / 兼容迁移 / 可观测性 / 回滚 / 旧 Runtime 下线
- **状态**：CP-0～CP-6 仓库实现已完成；CP-7 生产灰度观察与 CP-8 旧 Runtime 物理删除待完成
- **日期**：2026-08-12
- **强依赖**：`01-用户级MCP配置凭据与按需GatewayPRD.md`、`02-MCP两级路由授权与任务执行闭环PRD.md`

## 1. 一句话结论

通过 `off → shadow → enforce` 三档模式，把用户 MCP 流量逐步切换到“用户级配置 + 两级路由 + 任务级按需 Gateway”。灰度期间保留旧全局 MCP Runtime 作为管理员配置的回滚路径，但任何真实工具调用只能走一条链路，绝不双执行；全量门禁通过后，删除启动时全量发现、全局工具注册、进程级 Client/Bundle 与 revision 绑定，仅保留现有协议 Client、Transport、Rust Sidecar 和安全校验能力。

当前仓库完成的是开发期 CP-0～CP-6，包括闭合 8 条权威安全红线、正值红线原子阻断、PostgreSQL app/snapshot/evaluator/operator/drill 独立登录权限边界、受限 NOLOGIN definer owner、从 durable activation/sample/metric/drill 派生五个生产阶段的 snapshot，以及按 consumer × capability 验证的 legacy 迁移连续性。API Runtime 只允许持有 app DSN；migration 只允许使用独立 `MAF_MCP_LEGACY_MIGRATION_DSN` 和命名 SECURITY DEFINER API；生产 canonical ledger 和迁移使用 PostgreSQL，SQLite 仅限 local/test/CI。真实 PostgreSQL 的独立角色/权限/竞态测试与 migration atomic E2E 只是仓库实现门禁，不是 production evidence。旧 Runtime 启动期 connect/list/protocol/discovery 尚无完整同源 telemetry family，因此不得宣称新旧路径观测已完整闭环。本地测试、CI evidence 和 SQLite 演练不能替代真实生产观察。CP-7/CP-8 的准入、顺序与未完成边界以 `docs/runbooks/user-mcp-phase3-rollout.md` 为准，以下验收清单在对应生产 evidence 归档前保持未勾选。

## 2. 阶段前提

进入本阶段前，前两阶段必须已完成并有自动化证据：

1. 用户 MCP 配置已按认证用户隔离，凭据已使用 AES-256-GCM 加密入库。
2. 任务级 `MCPGateway` 已支持远程 HTTP(S) 的连接、发现、调用、取消和资源释放。
3. 主 Planner 只进行 Server 级路由，Tool Selector 只在目标 Server 的 Tool List 内决策。
4. “允许一次 / 始终允许 / 拒绝”、20 次调用上限、120 秒长调用提示和 30 天审计保留已落地。
5. 同一任务内串行调用、同一用户不同任务并行，以及任务结束资源清理已通过并发测试。
6. 前端已具备配置、授权、运行状态、取消和断线恢复入口，后端审计链路已落地。

如果任一前提不成立，只允许继续开发或 shadow 观测，不得扩大 enforce 流量。

## 3. 产品目标

1. 在不产生重复副作用的前提下验证新旧链路的路由、发现和执行差异。
2. 让新配置的用户 MCP 可以按用户、按固定分组、按流量比例逐步开放。
3. 出现安全、正确性或稳定性问题时，可以快速停止新任务进入新链路。
4. 全量切换后，后端不再因用户数量增长而在进程中常驻用户 MCP Client、连接和 Tool List。
5. 删除旧全局 Runtime 后，保留成熟的 MCP 协议实现和 Rust Sidecar，不重写协议栈。
6. 用逐版本 conformance 证明新增 `2026-07-28` 不破坏现有四版本，并让五个版本都进入同一用户级 Gateway 安全边界。

## 4. 非目标

1. 不在本阶段新增 MCP Transport family；仍只支持已确认的远程 HTTP(S)，但 Streamable HTTP 增加 `2026-07-28` 无状态协议形态。
2. 不把 legacy 全局 MCP 配置自动复制给全部用户。
3. 不迁移或持久化旧 Runtime 运行期发现到的 Tool List、Schema、Client 或 Bundle。
4. 不为普通 `tools/call` 增加自动重试或故障后重放。
5. 不引入加密主密钥轮换。
6. 不取消用户授权确认、Endpoint Policy、SSRF 防护或审计要求。
7. 不以“关闭浏览器”作为单一取消信号；仍使用任务 SSE 在线租约与 5 分钟断线窗口。

## 5. 现状与目标架构对比

| 项目 | 当前旧链路 | 本阶段完成后的目标链路 |
|---|---|---|
| 配置来源 | 服务器文件 / 环境变量 | 用户级数据库配置；系统配置仅保留受控运维入口 |
| 配置归属 | 进程全局 | 认证用户所有权 |
| 发现时机 | API Runtime 启动时全量 `tools/list` | Planner 选中 Server 后，任务内首次使用时 `tools/list` |
| Tool List | 进程级 Bundle | 任务内临时 Catalog，任务结束释放 |
| 工具注册 | 全局 `CapabilityRegistry` | 单一公共 `mcp.dispatch`，Tool Selector 动态选择 |
| Client/连接 | 进程长期持有 | 任务级按需创建，同一任务复用，任务结束释放 |
| 凭据 | 环境变量为主 | 数据库密文 + 服务器密钥文件，使用时解密 |
| 用户授权 | 非用户级统一闭环 | 每次调用检查允许一次 / 始终允许 / 拒绝 |
| 执行恢复 | revision-bound Bundle | 任务状态 + Gateway scope；普通调用重启后标记 unknown，不重放 |
| 协议版本 | 当前四版本、2025 lifecycle/session 为主 | 五版本 Adapter；2026 无 initialize/session/GET stream，与旧四版隔离 |

## 6. 发布模式与配置契约

### 6.1 后端模式

后端只接受以下明确枚举：

| 配置 | 允许值 | 含义 |
|---|---|---|
| `MCP_USER_SCOPED_GATEWAY_ENABLED` | `true` / `false` | 是否装配用户级配置、Gateway 与相关 API |
| `MCP_ROUTING_MODE` | `off` / `shadow` / `enforce` | 新两级路由的运行模式 |
| `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED` | `true` / `false` | 是否装配旧全局 Runtime，仅供迁移期系统配置使用 |
| `MCP_ENFORCE_COHORTS` | 逗号分隔的后端 cohort ID | `enforce` 时允许进入新链路的固定分组 |
| `MCP_ENFORCE_PERCENT` | `0` 至 `100` 的整数 | cohort 内稳定流量比例 |
| `MCP_ENFORCE_HASH_SALT` | 非空固定字符串 | 稳定哈希盐；所有实例一致，灰度周期内不得变更 |

非法枚举、非法百分比或彼此冲突的组合必须在启动时 fail closed，不得自动猜测。

### 6.2 合法组合

| Gateway | Routing mode | Legacy | 行为 |
|---|---|---|---|
| `false` | `off` | `true` | 仅旧链路；只用于迁移开始前或紧急回滚 |
| `true` | `shadow` | `true` | 旧链路执行，新链路只比较无副作用决策 |
| `true` | `enforce` | `true` | 命中灰度用户走新链路；未命中且仅使用系统 MCP 的任务可走旧链路 |
| `true` | `enforce` | `false` | 全量目标态；所有用户 MCP 只走新链路 |

以下组合禁止启动：

- Gateway 关闭但 Routing mode 为 `shadow` 或 `enforce`。
- 新旧链路都关闭但系统仍声明 MCP 可用。
- 全量下线后重新开启 legacy，却没有对应 legacy 配置和回滚证据。

### 6.3 稳定分流

灰度分流使用后端认证用户 ID 和 `MCP_ENFORCE_HASH_SALT` 计算稳定哈希，不使用前端 Cookie、设备 ID 或随机数。相同配置版本下，同一用户始终落入相同分组，避免任务之间来回切换。

`MCP_ENFORCE_COHORTS` 非空时，cohort 归属只从后端受控 rollout 配置读取；为空时，全部认证用户都是比例分流候选。前端不能提交或修改自己的 cohort。

单个任务在创建时固化 `mcp_execution_mode`，任务执行中不因配置热更新切换路径。

## 7. 单路径执行不变量

这是本阶段最高优先级约束：

1. 每个真实任务只能选择 `legacy` 或 `user_scoped` 一条执行路径。
2. shadow 路径不得调用 `tools/call`，不得触发用户授权弹窗，也不得写“始终允许”授权。
3. 同一用户请求不得在新旧链路各执行一次来比较结果。
4. 发生模式切换时，已经开始的任务沿固化路径完成或取消；新任务使用新模式。
5. 普通工具调用因进程重启、网络断开或结果未知时，不允许自动换链路重放。
6. 用户自定义 Server 没有 legacy 等价物，因此不能静默回退到全局 Server。

## 8. Shadow Compare 设计

### 8.1 可以比较的内容

shadow 只比较无副作用的控制面决策：

- 主 Planner 是否判断需要 MCP。
- 新 Server Router 选择的 Server 与旧工具级路由所对应 Server 是否一致。
- 新链路 `tools/list` 是否成功、耗时、工具数量和 Catalog 指纹。
- Tool Selector 计划选择的工具名、参数是否通过 Schema 校验。
- 配置归属、Endpoint Policy、授权检查是否给出预期状态。
- 任务结束后的 Client、连接、临时文件是否全部释放。

只有完全缺少受控 mapping 时才记为未批准的 `not_comparable`；已存在但被篡改、无效或不唯一的 mapping 必须记为 `mismatched`。两者都不进入 promotion 样本并阻止进入 enforce。只有已批准且验证的 retire 项可以记为已批准 `not_comparable`，但仍不计入场景样本配额。

### 8.2 禁止比较的内容

shadow 禁止：

- 双重执行 `tools/call`。
- 为比较而向远端 Server 发送业务写请求。
- 为 shadow 弹出授权确认或改变用户授权状态。
- 将完整 Tool List、Schema、工具参数、工具结果或凭据写入日志。
- 将 shadow 结果注入主任务模型上下文或改变最终回答。

### 8.3 Shadow 数据

只记录下列安全字段：

- `task_id`、`owner_user_hash`、`server_id`。
- legacy/new route decision、可比较状态和差异类别。
- Catalog 工具数量、名称集合 HMAC、Schema 指纹集合，不记录原文。
- connect/list/selector 时延分桶和结果状态。
- Endpoint Policy、ownership、grant check 的布尔结果与错误码。
- 资源清理计数，不记录进程对象地址或连接细节。

Shadow 记录沿用 MCP 审计默认 30 天保留策略。

## 9. 灰度步骤

### 9.1 Step A：内部账号 Shadow

- 只对测试和内部账号启用 shadow。
- 旧链路承担真实执行。
- 新链路运行 Server 路由、只读 `tools/list`、Selector dry-run 和资源清理。
- 至少覆盖 HTTPS Streamable HTTP、HTTPS Legacy HTTP+SSE、public HTTP Legacy HTTP+SSE、认证失败、超时、拒绝授权和大输出模拟；历史 allowlisted HTTP 样本只做读取兼容，不满足新 deployment gate。

退出条件：第 12 节所有安全红线为零，shadow 数据足以定位差异，无持续资源泄漏。

### 9.2 Step B：内部账号 Enforce

- 内部账号真实任务进入新链路。
- 每个真实调用仍经过用户授权。
- 旧链路保留，但只用于非命中账号或人工回滚后的新任务。
- 完成取消、120 秒提示、5 分钟断线取消、进程重启 unknown 和排队公平性演练。

退出条件：核心正确性指标达到门禁，回滚演练通过，连续观察窗口内无安全红线。

### 9.3 Step C：固定用户分组 Enforce

- 按 `MCP_ENFORCE_COHORTS` 开放明确用户分组。
- 分组内使用稳定哈希从低比例逐步扩大。
- 每次扩大比例前必须复核上一档指标，不得在同一观察窗口内连续跳档。
- 用户自定义 Server 只在新链路内运行；旧链路仅承接既有系统 MCP。

退出条件：新链路在真实负载下满足容量、成功率、授权、取消、清理和审计要求。

### 9.4 Step D：全量 Enforce 与 Legacy 关闭

- `MCP_ENFORCE_PERCENT=100`。
- 关闭旧全局 Runtime 装配和启动发现。
- 观察期通过后删除旧代码、配置和 revision 绑定。
- 删除完成后再次执行全量回归、容量测试、回滚演练和启动资源基线对比。

## 10. 旧配置迁移

### 10.1 不自动复制给用户

现有 `mcp_server_config.json` 或环境变量中的 Server 视为系统级 legacy 配置。不得将其凭据和 Endpoint 自动复制到所有用户记录，因为这会改变所有权、权限和凭据暴露边界。

### 10.2 允许的迁移方式

管理员可通过一次性受控迁移命令，将选定系统 Server：

1. 迁移为指定服务账号拥有的用户级 Server；或
2. 迁移为后端管理的 system-owned Server Profile，再通过显式 ACL 分配给用户或用户组。

每条迁移必须指定目标 owner/ACL，重新加密凭据，生成新的 `server_id` 和 `security_version`，并在同一数据库事务内写入不可变、精确幂等的 `mcp.legacy.config_migrated` 权威审计；本地文件只能作为提交后的非权威导出。迁移前还必须用 secret-safe fingerprint 逐项证明每个 consumer × exposed capability 的源/目标 input/output schema contract 相容；只有工具名相同不算连续。没有明确 owner/ACL、缺少源 input schema 或无法证明 contract 连续性的配置不得迁移。

### 10.3 不迁移的状态

以下旧状态一律丢弃并在新任务中重新生成：

- Tool List 与工具 Schema。
- `_clients`、连接池和 SSE 会话。
- `_bundles`、`_active_revision` 与任务 `mcp_bundle_revision`。
- 旧工具名到全局 Capability ID 的运行期绑定。
- 任何缺乏用户归属的旧授权推断。

## 11. 代码下线边界

### 11.1 删除或停止装配

全量门禁通过后，移除下列旧职责：

1. `src/api/runtime.py`
   - 删除 API 启动时 `prepare_refresh_sync(reason="startup", force=True)` 的全量 MCP 发现。
   - 删除将所有 MCP Tool Descriptor/Binding 同步到全局 Registry 的逻辑。
   - 删除新对话触发全局 MCP refresh 的可选路径。
2. `src/integrations/mcp/runtime_state.py`
   - 删除进程级 `_clients`、`_bundles`、`_active_revision` 和全局 revision retention。
   - 任务级资源改由 `MCPGateway` scope 管理。
3. `src/capabilities/mcp_tool/executor.py`
   - 删除依赖 `mcp_bundle_revision` 解析全局 Binding/Client 的执行路径。
   - MCP 入口统一收敛为认证上下文内的 `mcp.dispatch` 和任务协调器。
4. `src/api/routes/capabilities.py`
   - 不再把用户 MCP Tool 作为公共全局 Capability 返回。
   - 用户 Server Profile 和 Grant 通过认证后的专用 API 返回。
5. 任务与会话元数据
   - 删除 `mcp_bundle_revision` 的写入、继承、retain/release 和恢复逻辑。

### 11.2 保留并复用

下列成熟能力保留，不因 Runtime 状态模型变化而重写：

- MCP JSON-RPC Client、`tools/list` 分页和 `tools/call`。
- Streamable HTTP 与 Legacy HTTP+SSE Transport。
- Rust MCP Sidecar、protobuf/facade 与协议兼容 adapter。
- 输入/输出 Schema 校验、Header Policy、SSRF 校验和安全错误映射。
- 输出清理、审计脱敏、取消传播和标准 MCP async task 适配。

如文件职责混合，先通过小范围重构将可复用协议能力与全局状态管理分离，再删除全局状态；不得复制一套新 Client 规避拆分。

## 12. 发布门禁

### 12.1 安全红线

以下任一正值违规出现一次，系统即自动持久化 promotion block 并停止扩大灰度，但不自动改写当前路由。授权运维必须按 Runbook 追加可证明严格降低 exposure 的 rollback activation；回滚只影响新 Task，在途 Task 继续沿固化路径完成或取消。

- 跨用户读取、修改或执行 MCP 配置。
- 凭据、Authorization Header、Cookie、API Key、Nonce 或解密明文进入前端、日志、事件、模型 Prompt 或审计明细。
- 同一业务请求发生新旧链路双 `tools/call`。
- 未授权工具调用，或“拒绝”后通过改名/换参数绕过同一授权决策。
- Endpoint Policy/SSRF 校验被重定向、DNS rebinding 或特殊地址绕过。
- 普通调用结果未知后自动重放。

### 12.2 正确性门禁

- 认证用户只能看到自己的 Server Profile 和 Grant。
- Server Router 的选择可解释，`server_id` 在执行前重新校验所有权与启用状态。
- Tool Selector 只选择当前 Catalog 中存在且 Schema 校验通过的工具。
- 20 次 `tools/call` 上限跨多个 Server 统一计数。
- “始终允许”授权在安全版本和 Schema 指纹匹配时复用，变更时失效。
- 用户拒绝后可寻找替代方案，但不重复相同调用。
- 普通业务结果不做字段级脱敏，凭据与协议内部信息必须移除。
- 版本协商只在 `server/discover` 明确不支持时回退，任何 `tools/call` 不因降级、shadow 或重启而双执行。
- `2026-07-28` 不发送 initialize/session/GET stream/Last-Event-ID；前四版本仍遵守各自已冻结生命周期。
- MRTR `requestState`、远端 Task ID 和每请求协议 metadata 不进入模型、前端或普通审计。

### 12.3 稳定性门禁

- `tools/list` 每次 60 秒，暂时错误最多重试一次，重试时间独立计算。
- `tools/call` 不设最长时间且默认不重试；每 120 秒产生一次非阻塞停止提示。
- SSE 断开后 5 分钟内重连可恢复任务状态，超过窗口取消在途调用。
- 任务完成、失败、拒绝、取消、断线超时和进程关闭均释放 Client、连接和任务临时文件。
- 同一用户多任务并行时，不串用 Catalog、凭据、Grant 或 Tool Result。
- 全量切换后，API 进程启动不连接任何用户 MCP Server。
- 五版本 Fake Server 与真实受控样本均通过发现、`tools/list`、`tools/call`、取消和资源释放矩阵；2026 同时覆盖 MRTR 与 Tasks Extension。

### 12.4 资源门禁

在相同空闲负载下，对比迁移前后：

- 后端常驻 MCP Client 数量目标为 0。
- 后端常驻用户 Tool Catalog 数量目标为 0。
- 用户数量增加但没有任务时，MCP Runtime 内存不应随用户数线性增长。
- 只允许数据库中的最小配置、凭据密文、Grant 与 30 天审计随用户量增长。
- 排队中的任务不得提前持有连接、Client 或 Tool Catalog。

## 13. 可观测性

### 13.1 指标

至少提供以下指标并按新旧路径、Transport 和错误类别分组：

- `mcp_route_requests_total`
- `mcp_route_shadow_mismatch_total`
- `mcp_gateway_active_scopes`
- `mcp_gateway_connect_duration_seconds`
- `mcp_tools_list_duration_seconds`
- `mcp_tools_list_attempts_total`
- `mcp_tool_calls_active`
- `mcp_tool_call_duration_seconds`
- `mcp_tool_call_unknown_total`
- `mcp_permission_decisions_total`
- `mcp_disconnect_lease_expired_total`
- `mcp_temp_spill_bytes`
- `mcp_resource_cleanup_failures_total`
- `mcp_protocol_negotiation_total`
- `mcp_server_discover_duration_seconds`
- `mcp_mrtr_rounds_total`
- `mcp_remote_tasks_active`

允许使用 `protocol_version`、transport family、adapter 和结果类别等固定枚举标签。所有标签必须是低基数字段，不得用原始 URL、用户名、工具参数或凭据作为指标标签。

canonical metric bucket 必须按 UTC 分钟边界对齐且恰好持续 60 秒。瞬时 counter / event histogram 先归入事件所在的完整 UTC 分钟；同一完整闭合标签 identity 不得以重复或重叠 bucket 扩大有效观察覆盖，不同闭合标签 identity 可以在同一分钟共存。

### 13.2 审计事件

新增或统一以下事件：

- `mcp.rollout.route_assigned`
- `mcp.rollout.shadow_compared`
- `mcp.rollout.mode_changed`
- `mcp.rollout.rollback_triggered`
- `mcp.legacy.config_migrated`
- `mcp.legacy.runtime_disabled`
- `mcp.legacy.runtime_removed`

事件只记录配置版本、任务/用户安全标识、差异类别和结果，不记录原始凭据、完整 Schema 或业务结果。

## 14. 回滚策略

### 14.1 Legacy 尚未删除时

1. 将 `MCP_ROUTING_MODE` 调为 `off`，阻止新任务进入新路由。
2. 保持正在执行的新链路任务沿原路径完成或由用户/运维取消，不迁移在途调用。
3. 对仍有 legacy 等价配置的系统 MCP，新任务可恢复旧链路。
4. 用户自定义 MCP 不得回退到不属于该用户的全局配置；回滚期间应明确显示暂不可用。
5. 用户配置密文、授权和审计记录保留，不做破坏性数据库回滚。

### 14.2 Legacy 已删除后

代码删除前必须创建可部署的最后 legacy release tag，并完成一次版本回滚演练。真正回滚时：

- 回滚应用版本和兼容数据库 migration；新增用户表保持向前兼容，不删除数据。
- 恢复 legacy 只用于系统 MCP，不自动接管用户自定义 MCP。
- 加密密钥文件继续保留，避免用户配置在恢复新版本后无法解密。
- 回滚期间所有普通在途调用标记为 `unknown` 或取消，不自动重放。

## 15. 数据与生命周期

1. 新增用户配置表、凭据密文、Grant 和审计表使用 additive migration。
2. 关闭新链路不删除这些数据。
3. 删除旧 Runtime 只删除代码和运行期状态，不删除用户 MCP 配置。
4. MCP 审计默认保留 30 天；过期按现有后台清理机制批量删除。
5. 任务临时 Tool Catalog 不落数据库；超大结果临时文件按任务生命周期清理，已显式提升为 Artifact 的文件遵循 Artifact 生命周期。
6. 旧 `mcp_server_config.json` 在 legacy 下线后不再作为用户 MCP 配置源；如仍保留，只能用于明确的开发/协议测试，不得在生产启动时自动加载。

## 16. 前端要求

1. 前端不感知 legacy 与 new 的内部实现差异，只展示用户可理解的 Server、授权和任务状态。
2. 灰度分组、执行路径和安全开关由后端决定，前端不得通过参数强制选择路径。
3. 回滚导致用户自定义 MCP 暂不可用时，返回明确的可恢复状态，不把它伪装为“没有工具”。
4. 前端仍不持久化 Tool List、Schema、凭据或 Grant 真值。
5. 运行中模式变化不改变现有任务页面；任务从后端事件恢复固化的执行状态。

## 17. API 与兼容性要求

1. 第 1、2 阶段新增的用户 MCP API 在灰度期间保持向后兼容。
2. `/api/v1/capabilities` 只返回公开平台 Capability，不返回其他用户或当前用户的动态 Tool List。
3. 新旧任务历史都能读取；旧历史中的 `mcp_bundle_revision` 只作为历史内部字段忽略，不触发重新发现或重放。
4. 新任务不再写 `mcp_bundle_revision`。
5. 客户端断线恢复只依赖稳定 Task/Event API，不依赖原进程中的 MCP Client。

## 18. 验收标准

### 18.1 功能验收

- [ ] `off`、`shadow`、`enforce` 三种模式行为与合法组合一致。
- [ ] 稳定哈希保证同一用户在同一 rollout 配置下路径稳定。
- [ ] 单个任务创建后固化执行路径。
- [ ] shadow 从不调用真实工具、不弹授权、不改变回答。
- [ ] 用户自定义 Server 不会回退到全局 Server。
- [ ] legacy 配置迁移必须指定 owner/ACL，且凭据重新加密。
- [ ] 全量切换后，应用启动不执行用户 MCP `tools/list`。
- [ ] `/api/v1/capabilities` 不包含用户动态工具。
- [ ] 新 Gateway 对五个目标协议版本都有 `version + transport + adapter` conformance 证据，前四版无回归。
- [ ] `2026-07-28` 的 Discovery、ordinary tools、MRTR 与 Tasks Extension 都通过能力门控；Roots/Sampling/Logging 未被声明。

### 18.2 安全验收

- [ ] 新旧链路不会双执行。
- [ ] 跨用户配置、Grant、Catalog 和结果访问全部拒绝。
- [ ] 日志、指标、SSE、Prompt 和审计中无凭据明文。
- [ ] shadow 数据不保存完整工具参数、Schema 或结果。
- [ ] 公网 HTTP runtime-enforced 策略、DNS/Redirect/SSRF 保护在灰度和回滚路径一致生效；私网/特殊地址始终拒绝。

### 18.3 资源验收

- [ ] 空闲 API 进程的 MCP Client 和 Tool Catalog 常驻数为 0。
- [ ] 新增大量仅配置未调用 MCP 的用户后，运行时内存不随用户数线性增长。
- [ ] 排队任务不预建连接。
- [ ] 完成、失败、取消、拒绝、断线和进程关闭路径均通过资源泄漏测试。

### 18.4 运维验收

- [ ] 内部 shadow、内部 enforce、固定分组、全量四个放量步骤均有记录。
- [ ] 安全红线告警可以阻止扩大灰度。
- [ ] legacy 尚在时完成开关回滚演练。
- [ ] legacy 删除前完成 release tag、数据库兼容和版本回滚演练。
- [ ] 删除旧 Runtime 后完成一次全量回归和启动资源基线对比。

## 19. 测试矩阵

| 测试层 | 必测内容 |
|---|---|
| 单元测试 | 配置合法组合、稳定哈希、任务路径固化、shadow 禁止副作用、差异分类、指标脱敏 |
| Repository | legacy 迁移 owner/ACL、凭据重新加密、additive migration、30 天审计清理 |
| API | 灰度用户选择、前端不能强制路径、Capabilities 无动态工具、回滚不可用状态 |
| Planner | legacy/new 路由对比、不可比较分类、Tool Selector dry-run 不影响主结果 |
| Integration | 新旧链路单路径、用户自定义无 legacy fallback、在途任务不迁移、unknown 不重放 |
| Security | 跨用户、凭据泄漏、SSRF/Redirect/DNS rebinding、日志/指标/审计敏感字段扫描 |
| Concurrency | 同用户多任务并行、任务内串行、模式切换时任务隔离、队列不持有连接 |
| Lifecycle | 完成/失败/拒绝/取消/断线/重启资源清理、临时文件回收、Artifact 例外 |
| Load | 大用户量仅配置不调用、突发路由、按需发现、长调用与超大结果落盘 |
| Rollback | flag 回滚、版本回滚、legacy 系统 MCP 恢复、用户自定义明确不可用、无数据删除 |
| Protocol conformance | 五版本协商与 pin、2026 无状态 POST/header/body、JSON/SSE 响应、Discovery/List Cache Hint、MRTR、Tasks、`x-mcp-header`、取消和安全降级 |

## 20. 实施完成定义

本阶段只有在以下条件全部成立时才算完成：

1. 用户 MCP 已 100% 走用户级按需 Gateway。
2. 旧全局 MCP Runtime 已停止装配并从生产代码路径删除。
3. 启动时全量 MCP 发现、全局 Tool Capability 注册和 revision-bound Client/Bundle 已删除。
4. 现有 MCP Client、Transport、Rust Sidecar 和安全校验被复用且回归通过。
5. `SUPPORTED_MCP_PROTOCOL_VERSIONS` 与 Gateway/Sidecar adapter 明确支持五个目标版本，并有逐组合证据；2026 能力未被错误下放到旧版本。
6. 用户数量增长但不调用 MCP 时，Runtime 资源保持近似常量。
7. 安全、正确性、稳定性、资源和回滚门禁全部有可审计证据。
8. 相关 PRD、API 文档、部署配置、Runbook、`AGENTS.md` 和 `CHANGELOG.md` 已同步。

## 21. 官方协议与项目约束说明

- MCP 协议负责 `tools/list`、`tools/call`、变更通知和标准 async task 等线协议能力；用户所有权、授权、灰度、资源预算、重试和超时是本项目策略。
- 新链路继续遵守项目已冻结的 MCP compatibility 与 official SDK adapter 轨道；本 PRD 不用产品灰度设计替代协议 conformance。
- 当前设计依据 MCP `2026-07-28` 工具与生命周期语义，同时保留项目现有旧版本 Client compatibility；协议升级和本三阶段状态模型改造必须分别验收。
- 旧全局 Runtime 下线只删除进程级常驻状态和全局注册，不删除 `2024-11-05` 至 `2025-11-25` 的协议兼容；`2026-07-28` 作为第五个独立 Adapter 增量交付。
- shadow 可以比较 Discovery、版本选择、Catalog 指纹和 Selector dry-run，但不得为比较对同一业务请求执行两次 `tools/call`。

参考：

- [MCP 2026-07-28 Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MCP 2026-07-28 Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [MCP 2026-07-28 发布说明](https://blog.modelcontextprotocol.io/posts/2026-07-28/)（无握手/Session、List Cache Hint、Tasks Extension）
