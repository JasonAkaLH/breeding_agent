# SSE Auth Generation Invalidation Design

状态：document-perfectization reviewed；Ready for implementation planning
日期：2026-05-26
目标：长期稳健地移除 SSE 流式 chunk 热路径中的 token DB 查询，同时保留刷新 token / 登出后旧连接失效的安全语义。

## 1. 问题陈述

当前 SSE 连接在每条 frontend event 发送前都会调用 token 当前性校验。即使该校验已经改为 `touch=False`，仍会通过 `api_token_hash` 查询状态库。远端 PostgreSQL 下，这会把每个 LLM chunk 都串行绑定到一次数据库 roundtrip，导致不开启深度思考时正文流式输出也变慢。

需要把认证职责从 chunk 热路径中移除：token 用于请求入口认证；task ownership 用于 SSE 建连授权；chunk 路由由 `task_id -> subscriber queue` 决定；token 失效由连接级版本失效传播处理。

## 2. 目标

- SSE 建连后，每个 `main_agent.output_delta` / `main_agent.reasoning_delta` 发送不得访问数据库。
- 登录、刷新 token、登出、后续管理员踢下线必须能使旧 SSE 连接失效。
- 多 backend 容器 / 多进程部署时，任一实例上的 token 变更必须传播到其他实例。
- 不保存 raw token 到 SSE 连接上下文、日志、audit 或 cache。
- 保持现有 Authorization-only username token 产品语义：同一个 username 只有一个当前有效 token。
- PostgreSQL 作为生产状态库时使用生产级机制；SQLite 仅作为测试 / 开发兼容路径，不成为生产失效传播方案。

## 3. 非目标

- 不切换到 Cookie / Session 认证。
- 不改变 REST API 的 Authorization header contract。
- 不把 SSE 断连等同于用户取消任务。
- 不引入 Redis，除非后续业务明确需要独立分布式 cache / pubsub 基础设施。
- 不依赖“服务与 PostgreSQL 同机部署”来掩盖热路径 DB 查询。

## 4. 用户、干系人与受影响系统

| 对象 | 关注点 | 本设计影响 |
| --- | --- | --- |
| 业务对话用户 | 流式回答实时，登出 / 刷新后旧页面失效 | chunk 不再被 DB 查询拖慢；旧 SSE 能被关闭 |
| 前端业务对话台 | SSE 接收、刷新 token、登出 | API contract 不变；旧 SSE 失效后按现有错误处理重连或回到登录态 |
| 后端 API runtime | auth、SSE、event broker、runtime lifecycle | 增加连接授权上下文、auth generation cache、失效监听任务 |
| PostgreSQL state store | token 存储与跨实例通知 | `auth_user_token` 增加 generation 字段；auth write 事务发 NOTIFY |
| 运维 | 多容器部署、健康检查、故障定位 | listener 状态进入 readiness/health；通知断开后 reconcile |

## 5. 当前状态与证据

| 证据 | 文件 | 说明 |
| --- | --- | --- |
| SSE 建连时已解析用户并校验 task owner | `src/api/routes/tasks.py:120-130` | 建连授权已经存在 |
| SSE loop 每轮 event 前仍重新校验 token 当前性 | `src/api/routes/tasks.py:141-150` | 当前热路径仍有认证 DB 查询 |
| `touch=False` 仍通过 token hash 读 DB | `src/auth/services.py:76-83` | 只去掉写 last_used，未去掉读 roundtrip |
| transient event broker 已可不写 DB / audit | `src/api/sse.py:63-72`, `src/api/runtime.py:226-228` | chunk 发送本身已可纯内存 |
| 当前 auth model 无 generation 字段 | `src/core/models.py:60-66`, `src/storage/sqlite/models.py:72-84` | 需要 schema / contract 扩展 |
| StoragePort 当前只提供 token CRUD / rotate / clear / touch | `src/core/contracts.py:64-94` | 需要新增 generation-aware auth 方法或扩展返回值 |
| PostgreSQL runtime schema 由 SQLite metadata 生成 | `src/state/postgres/runtime_schema.py:46-77` | runtime schema 字段扩展会同步进入 PG fresh schema manifest |
| State Platform 已预留 auth command handler 分类 | `src/state/postgres/handlers.py:90-92` | auth writes 可纳入 partition / write queue 语义 |

## 6. 推荐方案

采用 **PostgreSQL `auth_generation` + LISTEN/NOTIFY + backend 本地 AuthGenerationCache + SseConnectionContext**。

### 6.1 Auth Generation

`auth_user_token` 增加：

```text
auth_generation BIGINT NOT NULL DEFAULT 0
auth_generation_updated_at TIMESTAMP NOT NULL
```

每次认证状态变化必须原子递增 generation：

- login：保存新 token hash，`auth_generation = old + 1`
- refresh：校验旧 token hash，替换新 token hash，`auth_generation = old + 1`
- logout：校验当前 token hash，清空 token hash，`auth_generation = old + 1`
- future admin revoke：清空或替换 token 状态，`auth_generation = old + 1`

### 6.2 AuthGenerationCache

每个 backend 进程维护本地 cache：

```text
username -> auth_generation
```

规则：

- SSE 热路径只做内存读取和整数比较。
- cache 不保存 raw token。
- cache 可以保存 `updated_at` 和 token fingerprint 作为脱敏观测字段，但不得用于 chunk 路由。
- cache miss 时不得在每 chunk 查询 DB；miss 只允许发生在建连初始化、listener reconcile 或明确的后台修复路径。

### 6.3 SseConnectionContext

SSE 建连时生成连接上下文：

```text
username
conversation_id
task_id
auth_generation_at_connect
connected_at
connection_id
```

连接建立流程：

1. 从 Authorization token 解析当前 username 和当前 auth_generation。
2. 校验 `task_id` 属于该 username。
3. 写入本连接上下文。
4. 订阅 `event_broker.subscribe(task_id)`。
5. 后续 event 发送前只比较：

```text
connection.auth_generation_at_connect == auth_generation_cache[username]
```

若不相等，关闭 SSE 并记录脱敏 audit / metric。

### 6.4 AuthInvalidationBus

生产路径使用 PostgreSQL LISTEN/NOTIFY：

```sql
NOTIFY maf_auth_generation_changed,
       '{"username":"alice","auth_generation":42,"changed_at":"..."}';
```

要求：

- auth write 与 token 状态更新在同一业务事务中决定 generation；NOTIFY payload 只包含 username、generation、changed_at、reason code，不包含 token hash 或 raw token。
- 每个 backend 实例启动 listener task，监听 `maf_auth_generation_changed`。
- 收到通知后更新本地 `AuthGenerationCache`。
- listener 连接断开、重连后必须执行 DB reconcile。

### 6.5 Reconcile 与健康状态

LISTEN/NOTIFY 不是 durable queue，必须有 reconcile：

- runtime 启动时：加载所有 auth generation 或按 active users 初始化 cache。
- listener 重连后：从 DB 读取 `username, auth_generation, auth_generation_updated_at` 并覆盖 cache。
- 定期后台 reconcile 可以作为防御性机制，但不得服务于每 chunk 校验。
- listener 当前状态进入 health/readiness：`connected`、`reconnecting`、`last_reconcile_at`、`last_notify_at`、`lag_seconds`。

## 7. 功能需求

| ID | 要求 | 验收 |
| --- | --- | --- |
| FR-1 | `auth_user_token` 必须持久化 `auth_generation` 与更新时间 | schema manifest / repository tests |
| FR-2 | login / refresh / logout 必须原子递增 generation | service tests 覆盖旧 token 失效与新 generation |
| FR-3 | auth 状态变化必须发布脱敏 generation changed event | PG NOTIFY integration / fake bus tests |
| FR-4 | backend 必须维护本地 AuthGenerationCache | unit tests 覆盖 apply notify、reconcile、cache miss 策略 |
| FR-5 | SSE 建连必须记录 SseConnectionContext | API tests 检查 context generation 和 task owner |
| FR-6 | SSE event loop 不得每 event 查询 token DB | spy storage tests：100 events 不调用 token lookup/touch |
| FR-7 | refresh / logout 后旧 SSE 必须关闭 | API tests 单实例与模拟多实例 |
| FR-8 | listener 重连后必须 reconcile 漏掉的 generation | listener tests 模拟断线期间 refresh |
| FR-9 | raw token / token hash 不得进入 SSE context、audit、notify payload | no-leak tests |

## 8. 非功能需求

| 类型 | 要求 | 验证 |
| --- | --- | --- |
| 性能 | 100 个 transient chunks 中，SSE event loop 不能触发 100 次 DB token lookup | CountingStorage / fake PG test |
| 安全 | token refresh / logout 后旧 SSE 最终关闭；raw token 不落日志 | auth/SSE/no-leak tests |
| 可靠性 | listener 断线后 reconcile，不能永久接受旧 generation | listener reconnect test |
| 多实例 | A 实例 refresh 后 B 实例 SSE 关闭 | fake bus multi-runtime test；真实 PG LISTEN/NOTIFY smoke |
| 可观测性 | listener 状态、通知数量、reconcile 时间、关闭原因可审计 | health endpoint / audit assertions |
| 兼容性 | REST Authorization-only contract 不变；SQLite 测试路径可用 fake/in-memory bus | existing API regression |

## 9. API / UX 行为

- 前端 REST / SSE 请求继续使用 `Authorization: Bearer <access_token>`。
- `/auth/refresh-token` 成功后，旧 token 对应的已存在 SSE 连接应关闭；前端应使用新 token 建立新的 SSE 或恢复状态。
- `/auth/logout` 成功后，旧 SSE 连接应关闭，前端进入未登录状态。
- SSE 断开本身不取消 task；task 继续按任务生命周期运行。
- chunk 路由仍由 `task_id` 订阅队列决定，不由 token 决定。

## 10. 数据、迁移与权限

- PostgreSQL fresh schema 需要新增 `auth_generation` 字段；当前生产库如已创建旧 schema，启动 schema reconciler 必须以 no-drop `ALTER TABLE ADD COLUMN IF NOT EXISTS` 补齐。
- SQLite 兼容 schema 同步新增字段，用于单元测试和本地 fake backend；但生产设计不依赖 SQLite 的跨进程通知。
- PostgreSQL 账号没有 drop table / drop database 权限；本设计不需要 DROP。
- LISTEN/NOTIFY 使用同一 PostgreSQL DB；需要确认应用账号具备 `LISTEN` / `NOTIFY` 权限。若权限不足，生产 readiness 必须 fail closed 或进入明确 degraded 状态，不能静默退回 per-event DB 查询。

## 11. 边界与失败模式

| 场景 | 期望行为 |
| --- | --- |
| SSE 建连时 token 已失效 | 401，不创建连接上下文 |
| SSE 建连时 task 不属于用户 | 404，不泄露 task 是否存在 |
| SSE 中途 refresh token | 旧连接因 generation mismatch 关闭 |
| SSE 中途 logout | 旧连接因 generation mismatch 关闭 |
| NOTIFY listener 暂断 | 记录 degraded；重连后 reconcile；不得每 chunk 查 DB 兜底 |
| NOTIFY payload 乱序 | cache 只接受更大的 generation，旧 generation 不覆盖新值 |
| cache miss | 对已有 SSE 连接视为 auth state unknown 并关闭或进入安全失败；不得查询 DB 后继续逐 chunk 发送 |
| 多实例同时 refresh | DB 条件更新确保只有当前 token 可 refresh；generation 单调递增 |
| backend 重启 | 旧 SSE 自然断开；新建连接重新查 DB 获取 generation |

## 12. 测试策略

### 单元测试

- `AuthGenerationCache`：apply newer generation、ignore older generation、cache miss policy。
- `AuthInvalidationBus` fake：publish/subscribe、payload redaction、reconnect reconcile hook。
- `UsernameTokenService`：login / refresh / logout generation increments。

### API / integration tests

- SSE 建连后 100 transient events：token lookup/touch count 为 0。
- refresh token 后旧 SSE 下一次 event 前关闭。
- logout 后旧 SSE 关闭。
- SSE 断开不取消 task。
- task owner 校验仍生效。
- no-leak：raw token、token hash 不出现在 audit / SSE payload / NOTIFY payload。

### PostgreSQL smoke

- 真实 PostgreSQL LISTEN/NOTIFY：实例 A refresh，实例 B listener 收到 generation。
- listener 断开重连后 reconcile 生效。
- schema reconciler 能 add columns，不需要 DROP 权限。

## 13. Rollout 计划

1. **Phase 0：PRD / test spec**
   固化需求、schema、failure mode、测试矩阵。

2. **Phase 1：schema + repository contract**
   扩展 `AuthUserToken` / StoragePort / SQLite+PostgreSQL schema，补 generation tests。

3. **Phase 2：auth service generation write**
   login / refresh / logout 原子递增 generation，返回 generation 给 runtime。

4. **Phase 3：cache + fake invalidation bus**
   实现本地 cache、fake bus、SSE connection context，先通过单进程 tests。

5. **Phase 4：PostgreSQL LISTEN/NOTIFY bus**
   listener task、reconnect reconcile、health/readiness、真实 PG smoke。

6. **Phase 5：移除 SSE per-event token DB 校验**
   SSE loop 改为 connection context + cache compare，回归 streaming performance。

## 14. 风险与假设

| 类型 | 内容 | 处理 |
| --- | --- | --- |
| 假设 | PostgreSQL 应用账号允许 LISTEN/NOTIFY | 在 readiness / smoke 中验证；不足则阻断生产启用 |
| 风险 | NOTIFY 非 durable，断线期间漏消息 | 强制 reconnect reconcile |
| 风险 | 多进程 cache 不一致 | generation 单调递增，通知 + reconcile 覆盖 |
| 风险 | 为兼容性保留 per-event DB fallback 会复发性能问题 | 明确禁止 chunk 热路径 DB fallback |
| 风险 | SQLite 本地开发无 NOTIFY | 使用 fake/in-memory bus，只用于测试/开发，不宣称生产等价 |

## 15. Open Questions

无阻断 open question。LISTEN/NOTIFY 权限属于部署环境验收项，不影响方案设计，但会影响生产 rollout gate。

## 16. Document-perfectization confidence review

### Audit findings addressed

| 级别 | 发现 | 处理 |
| --- | --- | --- |
| Blocking | 口头方案未明确 NOTIFY 非 durable 的漏消息处理 | 增加 reconnect reconcile、startup initialize、health 状态 |
| Major | 口头方案未明确 cache miss 安全行为 | 增加 cache miss 对已有 SSE 连接安全失败，不允许查 DB 后继续热路径发送 |
| Major | 口头方案未说明 PostgreSQL 权限和 no-drop 约束 | 增加 LISTEN/NOTIFY 权限验收与 ADD COLUMN IF NOT EXISTS/no DROP 约束 |
| Major | 口头方案未覆盖 StoragePort / schema manifest 影响 | 增加现有 evidence 与 Phase 1 contract/schema 工作 |
| Minor | 口头方案未拆 rollout phase 和测试矩阵 | 增加 Phase 0-5 与分层测试策略 |

### Confidence verdict

Pass with recorded assumptions：方案目标、边界、数据模型、跨实例传播、失败模式、测试和 rollout 已明确；唯一假设是生产 PostgreSQL 账号具备 LISTEN/NOTIFY 权限，该假设已转为 rollout gate。
