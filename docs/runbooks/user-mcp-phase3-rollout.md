# 用户级 MCP Phase 3 灰度与下线手册

## 当前边界

仓库内 CP-0～CP-6 的恢复、路由、shadow、持久证据、最小权限 ledger、安全红线检测和 legacy 迁移已实现。这些本地实现不代表生产灰度已完成：

- CP-7 的内部 shadow、内部 enforce、固定分组、100% enforce 和 assembly-off 观察窗尚未由本地代码或测试代替。
- CP-8 的旧全局 Runtime 物理删除尚未执行；必须等待 CP-7 D2 生产证据通过。
- 生产变更必须使用 PostgreSQL canonical ledger。SQLite 仅限 local/CI 验证。

## Canonical 路由配置

阶段三路由只从以下环境变量解析。值为闭合枚举，非法布尔值、百分比、空盐或冲突组合都会拒绝启动。

| 变量 | 契约 |
|---|---|
| `MCP_USER_SCOPED_GATEWAY_ENABLED` | 只允许 `true|false`。`off` 必须为 `false`；`shadow|enforce` 必须为 `true`。 |
| `MCP_ROUTING_MODE` | 只允许 `off|shadow|enforce`；默认 `off`。 |
| `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED` | 只允许 `true|false`；`shadow` 必须为 `true`。 |
| `MCP_ENFORCE_COHORTS` | canonical 逗号分隔 cohort ID；不允许空项、重复项或空白。非空时必须配置 cohort 文件。 |
| `MCP_ENFORCE_PERCENT` | ASCII 十进制 `0..100`；空值按 `0`处理。 |
| `MCP_ENFORCE_HASH_SALT` | `enforce` 必填。全实例保持一致，灰度期间不更换。变更 salt 被视为 exposure increase。 |
| `MCP_ENFORCE_COHORT_CONFIG_FILE` | cohort 映射的只读 JSON 文件。 |

旧 `MAF_USER_MCP_ENABLED` 和 `MAF_USER_MCP_ROUTING_ENABLED` 只是兼容边界。它们与 canonical 值冲突时 fail closed；旧 routing flag 不能单独推导 `enforce`。

### 模式组合

| 操作目标 | Gateway | Mode | Legacy | Cohorts | Percent | 真实执行 |
|---|---:|---|---:|---|---:|---|
| 安全 off / legacy-only | `false` | `off` | `true` | 空 | `0` | 只走 legacy；用户自定义 Server 不可用。 |
| 内部 shadow | `true` | `shadow` | `true` | 空 | `0` | legacy 执行；新链路只做无副作用控制面比较。 |
| 内部/分组 enforce | `true` | `enforce` | `true` | 按审批值 | 按审批档位 | 命中用户走 user-scoped；其他任务仅在有受控等价系统 MCP 时可走 legacy。 |
| 100% enforce | `true` | `enforce` | `true` | 空 | `100` | 全用户走 user-scoped，但仍保留 legacy assembly 回滚能力。 |
| assembly-off | `true` | `enforce` | `false` | 空 | `100` | 只走 user-scoped；仅在 D1 门禁完成后使用。 |

`shadow` 不调用 `tools/call`、不弹授权、不写 Grant，也不改变模型回答。Task 创建时固化真实路径和 shadow 标记；配置更换不迁移在途 Task。

live shadow 只在真实 legacy `WorkflowPlan` 已生成后启动后台旁路，并按任务固化的 `mcp_bundle_revision` 解析计划实际选中的 legacy capability。retain mapping 只接受 CP-4 受控迁移写入且与 source/owner/target/config/security provenance 完全一致的记录；不按 URL、名称或描述猜测。缺失 mapping 记为未批准 `not_comparable`，篡改或无效 mapping 记为 `mismatched`；两者均不得进入 promotion 样本，且会阻止进入 enforce。只有已审批并验证的 retire 项可记为已批准 `not_comparable`，同样不计入 promotion 样本。

持久化 shadow sample 的 owner/task/call 安全引用只允许 `null` 或小写 `hmac-sha256:<64 hex>`，不保存原始 ID。`scenario`、legacy/shadow `outcome`、`transport`、Endpoint Policy、`comparison` 和 blocker 必须分别命中 Python/SQL 共享的固定闭集，且 comparison/blocker 组合必须通过闭合矩阵；开放字符串、明文引用或非法组合均 fail closed，不进入 production evidence。

### Cohort 文件

文件必须是 UTF-8 JSON 普通文件，不能是符号链接，大小不超过 4 MiB，权限不得宽于 `0440`。schema 是闭合的：

```json
{
  "schema_version": "maf.mcp.rollout_cohorts.v1",
  "config_version": "cohort-2026-08-13",
  "user_cohorts": {
    "authenticated-user-id": ["internal"]
  }
}
```

用同一内容的原子文件替换配合滚动发布；不要在进程读取时原地改写。进程会计算文件 SHA-256 并纳入 rollout config fingerprint。

## 生产实例准入

在 `MAF_API_ENV=prod|production` 且 mode 不是 `off` 时，每个实例还必须绑定同一个已消费 approval 的 activation：

| 变量 | 含义 |
|---|---|
| `MCP_ROLLOUT_ENVIRONMENT_ID` | canonical ledger 的环境 ID。 |
| `MCP_ROLLOUT_DEPLOYMENT_ID` | 本次部署 ID。 |
| `MCP_ROLLOUT_STAGE` | `internal_shadow|internal_enforce|cohort_enforce|full_enforce|legacy_assembly_off`。 |
| `MCP_ROLLOUT_ACTIVATION_ID` | 与部署、stage 和 config fingerprint 匹配的 activation ID。 |
| `MCP_ROLLOUT_INSTANCE_ID` | 实例唯一 ID；运维配置应显式给出。未配置时代码会回退到 Gateway instance ID。 |

stage 必须与 mode 一致：`shadow` 只接受 `internal_shadow`；`enforce + legacy on` 接受三个 enforce stage；`legacy_assembly_off` 必须对应 `enforce + legacy off`。启动时实例向 canonical storage 写入 60 秒 lease；activation、stage、deployment 或 fingerprint 不匹配时不 Ready。

`off` 会关闭 Gateway，因此 legacy-only 进程不消费上述 Gateway instance admission 环境变量。回滚 activation 仍保留在 ledger 中作为审计记录。

## 证据与 HMAC keyring

`scripts/validate_user_mcp_phase3_evidence.py` 离线校验 canonical artifact 的 closed schema、scope、digest、nonce/snapshot 单调性、观察窗、样本、基线、演练和安全红线。返回码为 `0` 才表示 `allowed=true`；被阻断或 artifact 无效返回 `2`。

生产 evidence 必须由 `production_snapshot_producer` 身份生成，携带 `attestation_key_id` 和 HMAC-SHA256 签名。PostgreSQL producer 不接收调用方编写的 production JSON：数据库从最新精确 deployment activation 派生 stage/config，并在同一个 `REPEATABLE READ` 事务内从持久 sample/metric/drill observation 派生 internal-shadow、internal-enforce、cohort-enforce、full-enforce 或 legacy-assembly-off 的 payload、identity 和 SHA-256；Python 校验该 digest 后做 HMAC，再由 finalize 重新派生并精确比对后写入。旧的 caller-authored production function 保留为显式拒绝边界。snapshot producer 从 `MAF_MCP_ROLLOUT_ATTESTATION_KEY_B64` 读取 canonical Base64 签名密钥，key ID 由 `--attestation-key-id` 显式给出。validator 和 operator CLI 从 `--attestation-keyring` 或 `MAF_MCP_ROLLOUT_ATTESTATION_KEYRING_PATH` 读取外部信任 keyring。keyring 只允许以下 schema，每个值是 canonical Base64 密钥。下例的值明文表示 `test-only-not-for-production`，只用于说明格式：

```json
{
  "schema": "maf.user_mcp_phase3_attestation_keyring.v1",
  "keys": {
    "rollout-key-id": "dGVzdC1vbmx5LW5vdC1mb3ItcHJvZHVjdGlvbg=="
  }
}
```

不要把签名密钥写入 evidence artifact、日志或版本库。CI evidence 不得携带 production attestation，且只能授权 `off -> internal_shadow`；之后的转换必须使用可验签的 production evidence。

当前代码门禁：

| 当前 stage | 最小观察 |
|---|---|
| `internal_shadow` | 连续 24 小时；7 个固定场景每个至少 3 个 matched 样本；红线、invalid 和未解决 mismatch 为 0。 |
| `internal_enforce` | 连续 48 小时；真实 terminal 样本非零；取消、120 秒提示、5 分钟断线、restart unknown、MRTR/Tasks 恢复、公平排队和 flag rollback 全部演练通过。 |
| `cohort_enforce` / `full_enforce` / `legacy_assembly_off` | 每档独立连续 7 天且至少 1000 个真实 terminal call；ordinary 和 remote-task 均有非零样本/基线；错误率不高于基线，p95 不超基线 110%。 |

Runtime 在权威生命周期边界写入 PRD §13.1 的低基数 metric family：Task assignment 负责每 Task 恰好一次的 route；Gateway 负责连接、协议协商、`tools/list` 尝试/耗时、活动 scope/call、临时落盘字节与清理失败；Coordinator 负责授权决策、MRTR 轮次、发现结果和 user-scoped ordinary terminal；legacy executor 负责实际旧链路 ordinary terminal 基线；recovery worker 负责 2025/2026 remote-task terminal/unknown/active；Presence 负责 5 分钟断线租约到期；shadow comparator 只在完成真实比较并返回 mismatch 时计数。活动 scope/call、同 closed dimension 汇总后的临时落盘字节和 remote task 使用覆盖式 gauge，不得累加旧采样。旧 Runtime 的启动期 connect/list/protocol/discovery 尚无完整同源 family；在它们接入权威 producer 前，不能把当前 telemetry 宣称为完整的新旧路径对比证据。

所有 canonical metric bucket 统一为 UTC 分钟边界对齐且恰好 60 秒；瞬时 counter / event histogram 先映射到事件所在的完整 UTC 分钟，再按完整 closed-label identity 聚合。同一 identity 的重复写入由 counter 累加或 gauge 覆盖，不得在 evidence payload 中展开为重复或重叠 bucket；不同 closed-label identity 可以在同一分钟共存。

每个完整结束的 UTC 分钟只为 gate 所需 ordinary/remote-task terminal 类别追加零值 bucket。零值通过 additive counter upsert 写入，因此不会覆盖同分钟真实事件；进程暂停、事件循环长时间停顿或存储失败的分钟不回填，必须在 evidence 中表现为断窗并阻断 promotion。

安全红线不再使用“没有正事件 detector”的旧口径。Runtime 已注册闭合的 8 个权威 hook：跨用户 owner 边界、审计 secret payload 边界、durable call idempotency、授权决策、Endpoint Policy、unknown 结果重放、shadow call 和资源清理。只有全部 detector 已注册、健康，且分别对该 UTC 分钟提供一次性、最长 1 分钟的精确 attestation 时，producer 才为 8 条红线写连续零值。缺注册、不健康、attestation 缺失或 metric 写失败都持久化 gap 并 fail loud；真实 violation 只能以 additive `+1` 写入闭合 reason code，安全红线的 gauge/setter 正值写入显式禁止。任一正值红线与对应的确切 activation/evidence 在同一事务内派生唯一、确定的 `safety_red_line_nonzero` promotion block；重复事件不创建可分叉 block。Runtime 启动成功后才启动 producer，shutdown 会先停止；本地合成 bucket 和 CI 结果仍不能充当 production evidence。

## Operator ledger

`scripts/control_user_mcp_rollout.py` 只向 append-only ledger 添加 approval、block、resolution 或 activation，不修改旧记录。它会先验证 artifact，再要求 evidence 已存在于 canonical storage 且关键字段一致；artifact 不会被 CLI 导入存储。

| 子命令 | 用途 | 主要必填参数 |
|---|---|---|
| `append-approval` | 由 operator 记录人工审批。 | evidence、target deployment/stage/config、approval ID、reason、approver |
| `activate` | 将未消费 approval 原子转为唯一 activation。 | 上述目标加 activation ID；非首次 activation 带 previous activation ID |
| `append-block` | 由 evaluator 持久化阻断。 | evidence、block ID、证据中实际出现的 blocker reason code、reason、approver |
| `resolve-block` | 追加 block resolution；不自动扩大流量。 | evidence、resolution/block/approval ID、reason、approver |
| `rollback` | 追加严格降低 exposure 的 rollback activation。 | current/candidate config、target、previous/new activation、approval、evidence、reason、approver |

扩大 exposure 前的固定顺序是：validator `allowed=true` -> append approval -> activate -> 滚动部署使用该 activation -> 每个实例准入成功。active block 会阻止扩大和新实例准入，但不自动重写当前路由。

查看实际 CLI 参数：

```bash
conda run -n multi_agent python scripts/validate_user_mcp_phase3_evidence.py --help
conda run -n multi_agent python scripts/produce_user_mcp_shadow_evidence.py --help
conda run -n multi_agent python scripts/control_user_mcp_rollout.py --help
conda run -n multi_agent python scripts/control_user_mcp_rollout.py activate --help
```

### PostgreSQL 最小权限

`scripts/postgres/user_mcp_rollout_permissions.sql` 是无凭据、additive/idempotent 的权限模板。先创建 runtime schema，再由数据库管理连接应用该模板；登录角色、凭据和角色 membership 由部署平台在模板外管理。模板不创建 login/password，不删除表、函数或角色。

| NOLOGIN 角色 | 权限边界 |
|---|---|
| `maf_rollout_app_writer` | 只能通过 SECURITY DEFINER API 写 counter/gauge metric、脱敏 shadow sample、sample 保留清理和 instance-config lease，并读取准入所需的最小表。 |
| `maf_rollout_snapshot_producer` | 只能读取脱敏 sample/metric/drill observation，并通过数据库派生的 prepare/finalize 边界追加 `source=production` evidence。 |
| `maf_rollout_ci_evidence_writer` | 只能追加 `source=ci` 的 CI evidence；显式无权调用 production evidence 函数。 |
| `maf_rollout_gate_evaluator` | 可读 rollout ledger，只能通过命名函数追加 promotion block。 |
| `maf_rollout_operator` | 可读 rollout ledger，并追加 approval、deployment activation 和 block resolution。 |
| `maf_rollout_validator` | 只读 rollout ledger，不获得 write API 执行权。 |
| `maf_rollout_drill_recorder` | 只能通过命名函数追加闭合、带 canonical digest 的 internal-enforce drill observation；无基表 DML。 |
| `maf_rollout_api_owner` | 固定 NOLOGIN/NOINHERIT 的 SECURITY DEFINER owner；无 membership、无 schema CREATE，只持有各静态函数需要的逐表权限。 |

生产在线路径必须为 app、snapshot、evaluator、operator 和 drill recorder 创建独立 LOGIN 与独立 DSN，每个 LOGIN 只继承一个对应的 NOLOGIN 执行角色；`maf_rollout_api_owner` 永远不得创建 LOGIN 或授予给登录身份：

该登录隔离契约要求 PostgreSQL 16 或更高版本，并依赖 `pg_auth_members.inherit_option` / `set_option`。部署平台授予 rollout 或 migration NOLOGIN 角色时必须显式使用 `WITH INHERIT TRUE, SET FALSE`，且不得授予 `ADMIN OPTION`；PostgreSQL 默认 `SET TRUE` 会被启动预检按 fail-closed 拒绝。例如：`GRANT maf_rollout_app_writer TO <app_login> WITH INHERIT TRUE, SET FALSE;`。

| 进程 | 唯一可接收的权限 DSN |
|---|---|
| API Runtime | `MAF_MCP_ROLLOUT_APP_DSN`；如果进程环境同时出现 snapshot/evaluator/operator/drill/migration DSN，启动立即拒绝。 |
| `produce_user_mcp_shadow_evidence.py` | `MAF_MCP_ROLLOUT_SNAPSHOT_DSN`。 |
| `control_user_mcp_rollout.py append-block` | `MAF_MCP_ROLLOUT_EVALUATOR_DSN`。 |
| `control_user_mcp_rollout.py` 其他子命令 | `MAF_MCP_ROLLOUT_OPERATOR_DSN`。 |
| 受控 drill recorder | `MAF_MCP_ROLLOUT_DRILL_DSN`；不得注入 API Runtime、snapshot 或 operator 进程。 |

CI evidence writer 或 validator 如在部署环境中运行，也必须分别使用独立 LOGIN/DSN，且不得与上述 5 个在线身份或彼此共用凭据。

所有角色对基表都没有 `INSERT|UPDATE|DELETE|TRUNCATE`；历史 evidence/approval/activation/block/resolution 表还有拒绝 update/delete 的 append-only trigger。所有 SECURITY DEFINER 函数固定 `search_path=pg_catalog`。连接时还会拒绝 superuser、`BYPASSRLS`、多 rollout 角色 membership 或基表 DML 权限。SQLite 只能在 local/test/CI 使用，不能用来代替上述生产角色分离。生产发布前必须在专用 PostgreSQL 测试 DSN 上执行真实权限门禁；未配置 `MAF_POSTGRES_ROLLOUT_PERMISSIONS_TEST_DSN` 时的 skip 不是生产通过证据。

## Legacy 配置迁移

`scripts/migrate_legacy_mcp_config.py` 对每个 legacy Server 要求唯一明确处置。backend 不由 CLI 自由指定，而是复用 canonical state platform 配置：

| 配置 | 迁移行为 |
|---|---|
| `MAF_STATE_STORE_BACKEND=postgresql` + `MAF_MCP_LEGACY_MIGRATION_DSN` | 使用已经 bootstrap 的 canonical PostgreSQL，但只通过独立 migration LOGIN 和 SECURITY DEFINER API 写入；禁止 `--database-path`，不得回退使用 state owner/runtime DSN。 |
| 未配置或 `sqlite|sqlite_legacy` | 仅允许非生产 local/test/CI；必须显式给出已 bootstrap 的 `--database-path`。 |

每个 Server 的 classification 规则如下：

- `migrate_owner`：仅允许 `consumer_scope=service_account_only`，必须提供 `owner_user_id`，且必须有唯一目标 consumer 的 `hmac-sha256:<64 lowercase hex>` 安全引用。
- `retain_for_rollback`：保留 legacy；只要存在任何 retained Server，apply/assembly-off 就会被阻断。
- `retire`：共享或未知 consumer 必须通过此路径显式退役，同时提供 approver、reason 和 `impact_accepted=true`。

classification 对每个 Server 都必须给出闭合的 `target_consumer_refs`，且只接受 HMAC 安全引用；不允许 raw consumer ID 进入产物。dry-run artifact 包含 secret-safe source fingerprint、consumer-set digest、owner mapping candidate、对每个 consumer × exposed capability 展开的闭合 obligation、每项 obligation 的源 input/output contract fingerprint、health policy、retirement evidence 和 plan fingerprint。`migrate_owner` 的 exposed tool 缺少源 input schema 会 fail closed。artifact 和 durable apply audit 不写 raw owner/consumer ID，只保留 HMAC 引用、集合 digest 和必要 fingerprint。它不迁移 Tool List、Schema、Client、Bundle、revision 或历史授权推断。artifact 中的 health result 只是审查信息，必须显式标记 `health_evidence_role=informational_only_not_security_attestation`；它不能取代 apply invocation 内的实时连续性重验。artifact 的 schema、fingerprint、health policy/role 和内部字段一致性仍会严格校验，禁止手工修改或重签字段。

正常顺序是“审查 dry-run artifact -> 同一次 apply 实时重验 -> 受控写入”：

1. dry-run 可由部署集成附带 informational health result，供审查 handshake/discovery/完整分页 `tools/list`/合法工具状态。standalone CLI 的默认 dry-run 不连接 Server，生成的 `apply_validation.ready=false` 只表达缺少审查信息，不阻止后续内置 apply；不得把该字段当作安全 attestation，或手工编辑、重签 health 字段。
2. 审查 artifact 的 fingerprint、完整 classification、无 retained Server、shared/unknown consumer 退役批准、consumer-set digest、owner mapping 和 consumer × capability obligation。apply 会重新读取 source config/classifications，并拒绝被篡改、陈旧或不一致的 artifact；即使 artifact health 显示健康，也不跳过下一步。
3. standalone `--apply` 默认构造 `BuiltInLegacyMigrationLiveHealthValidator`，通过 `UserMCPClientFactory` 和 `run_health_discovery` 在同一次 apply 中完成实时发现，无需 Python 显式注入。`run(...)` 仍保留 `live_health_validator`/`live_validator_provenance` 注入 seam，供测试或受控集成替换；直接自定义构造 `LocalLegacyMigrationApplier` 时仍必须提供 validator 和非空 provenance。
4. 每个 `migrate_owner` candidate 都必须在写入前完成同 invocation 实时校验。validator 收到将被持久化的精确 normalized endpoint、transport/protocol/auth metadata，以及本次 apply 从当前环境/static headers 解析出的当前 credential values；校验和加密写入使用同一份 desired snapshot。实时结果必须绑定 target Server、source fingerprint、consumer-set digest、完整分页 catalog fingerprint、capability contract-set fingerprint、可用 capability×contract 闭合集和 `observed_at`/`expires_at`。工具名相同但 input/output schema 不同不视为连续。policy 为最多 2 次、每次独立 60 秒、重试间隔 0.25 秒，并为每次 attempt 的 client close 设置 1 秒有界 cleanup timeout；外层 deadline 与 evidence 最大窗口统一由该 policy 派生为 `2 × (60 + 1) + 0.25 = 122.25` 秒。超时、非法结果、错误 Server ID 或不健康都不写入。所有 candidate 必须先全部通过 preflight，再通过 `apply_legacy_mcp_migration_atomic` 在单个数据库事务中整批创建 Server、credential 和 immutable durable migration record；任一冲突或写入失败均 all-or-zero，不留下部分迁移。
5. 内置 apply 根据现有 state platform 配置选择 canonical backend：PostgreSQL 要求独立 `MAF_MCP_LEGACY_MIGRATION_DSN`，登录身份必须且只能以 `WITH INHERIT TRUE, SET FALSE`、无 `ADMIN OPTION` 的 membership 继承 `maf_mcp_legacy_migrator`，启动前校验无任何基表读写、仅可执行命名 migration API，CLI 不接受 DSN、不做 schema bootstrap，且禁止同时传 `--database-path`。精确重放只通过完整 migration/plan/source/target identity 的受限 definer snapshot 检查；响应显式不含 credential ciphertext/nonce，只返回数据库从当前 ciphertext/nonce/version 重算的 SHA-256 storage binding。首次写入把该 binding 纳入 HMAC provenance；重放先比较当前 storage binding，再以本次进程解析的 plaintext credential 核验 keyed digest，并重新执行实时连续性检查。新增无关 Tool 可产生新的 catalog fingerprint 而不破坏幂等，但 target consumer、capability contract 与 obligation 必须继续完全一致。SQLite 只限 local/test/CI，要求 `--database-path` 指向已存在且已 bootstrap 的库。两个 backend 都只创建 canonical user-scoped Server 和重新加密的 credential，不迁移 runtime session、Task、Grant、Catalog 或 revision；写入以 plan fingerprint 幂等，既有目标不等价时 fail closed。
6. 内置 apply 必须提供 `--artifact`、`--credential-key-file` 和 `--service-account-owner`；只有 SQLite 还必须提供 `--database-path`。credential key 必须是一行 canonical Base64、解码后 32 bytes 且权限精确为 `0400`；service-account owner 必须与所有 mapping candidate 完全一致。数据库内 `mcp_legacy_migration_record` 是权威审计，固定 `event_type=mcp.legacy.config_migrated`，与 Server/credential 同事务写入且精确幂等；只保存安全引用、fingerprint、disposition 和证据时间。可选 `--audit-out` 仅在提交后导出 `0600` JSON，导出失败会在命令结果标记 `failed_non_authoritative`，不会把已经原子提交的迁移误报为失败。
7. source endpoint 必须无 query 且无 fragment；任一存在都拒绝为 `legacy_apply_endpoint_query_or_fragment_forbidden`，防止把 URL 内凭据或不稳定参数带入迁移。`--allowlist-domain` 和 `--allowlist-cidr` 可重复给出，用于明确放行受控 HTTP/企业地址；未通过 Endpoint Policy 的 source endpoint 不会进入实时校验或写入。legacy `bearer_env`/`api_key_env` 凭据必须在 apply 进程环境中存在；static headers 同样经过 header policy 校验。
8. 目标 Server ID 不复用 legacy ID，而是由 owner 和 source Server ID 确定生成：对 `legacy-migration-v1`、owner 和 source Server ID 以 NUL byte 分隔后求 SHA-256，取前 16 个十六进制字符并加 `migrated-` 前缀。相同 owner/source 重跑得到同一目标，不同 owner 不共用 ID。
9. 写入的 `auth_metadata.migration_provenance` 至少绑定 schema、source Server ID/fingerprint、owner、target Server ID、HMAC-SHA256 credential digest、credential security version，以及含 catalog/capability-contract/obligation/consumer-set fingerprint 的 validator provenance 和 `observed_at`/`expires_at` 时间。它不保存凭据明文；重跑会重做实时校验，并要求现有目标的静态 provenance、实际解密凭据、normalized endpoint、capability contract obligations 和将写入值一致，否则 fail closed。

缺少内置 backend 必填选项时 `--apply` 会 fail closed 为 `apply_backend_options_required`；同 invocation 实时校验超时或返回不健康结果时分别 fail closed 为 `live_health_validation_timeout` 或 `live_health_validation_failed`。仅直接自定义构造 `LocalLegacyMigrationApplier` 且遗漏 validator/provenance 时，才会分别得到 `live_health_validator_required` 或 `live_health_validator_provenance_required`。不得为绕过此边界而直接写用户 MCP 表。

```bash
conda run -n multi_agent python scripts/migrate_legacy_mcp_config.py --help
```

## CP-0 恢复硬前置

任何 `enforce` activation 前必须先验证阶段二跨重启恢复：

1. 启动先验证 credential sentinel 和 rollout instance admission。
2. `may_have_dispatched=true` 的普通调用在 recovery worker 启动前收敛为 `unknown`，并写安全事件/审计；不重发原 `tools/call`。
3. MRTR `requestState` 和 remote Task ID 使用任务私有 AAD 密封，不进入前端、Prompt 或普通审计。
4. 多实例 recovery worker 通过数据库 claim/lease/CAS 单拥有者查询 remote Task；轮询只调用对应版本的 `tasks/get`。2025 终态可追加 `tasks/result`，协议取消可调用 `tasks/cancel`；2026 recovery-only 仍只允许 `tasks/get`。任何恢复路径都不得调用 `tools/list` 或重发 `tools/call`。
5. terminal 或 `input_required` 状态持久化后停止 poll；worker shutdown 释放 claim。
6. remote Task terminal 事务写入确定性 continuation command，并持久化原始/最新重规划 plan 与内容寻址结果引用。command 只允许 `pending → claimed → running → completed`；`pending` 或过期 `claimed` 可通过 CAS 安全重领，`running` 后的 scheduler/执行歧义只能进入 `abandoning → failed`，以 `mcp_continuation_execution_unknown` 收敛 Task 与本 command 所属节点，禁止重新调度。
7. continuation 执行在每个节点副作用前、executor 返回后检查 durable command status/token/lease 与 Task `running` 状态，节点启动使用状态 CAS；lease 续约丢失、进程崩溃或权威 Sidecar 暂不可用时不得用内存状态绕过 fence。`abandoning` 在 Task/Node 权威收敛完成前保持可重试，完成后才写 terminal receipt。

本地 CP-0 回归通过只证明实现契约，仍需在 internal enforce 生产窗口中完成 MRTR 和 Tasks recovery drill。

## 回滚

### Legacy 仍可装配

1. 使用当前 production evidence 和不可变 approval，通过 `rollback` 追加新 activation；候选配置必须是可证明的严格 exposure decrease。
2. 对新 Task 降低 percent/cohort 暴露，或回到 legacy-only `off`。不改写在途 Task 的固化路径。
3. 用户自定义 Server 没有 legacy 等价物；off/未命中时必须显式返回可恢复不可用，不能伪装为“没有工具”。
4. 保留用户 Server、密文、Grant、audit、evidence/approval/activation/block 表和 credential key；不做破坏性数据库回滚。
5. 普通在途调用状态不明时标记 `unknown` 或取消，不换链路重放。

### Legacy 代码已删除

只能回滚到预先验证过的最后 legacy release tag/artifact，同时保留 additive 数据和 credential key。恢复 legacy 只承接系统 MCP，不自动接管用户自定义 Server。

## Assembly-off 与物理删除边界

顺序不得跳过：

1. 发布仍包含 legacy 的最后 release tag，验证 artifact digest 和可部署性，完成 version rollback drill。
2. `full_enforce` 在 legacy 仍可装配时独立满足 7 天/1000 样本。
3. legacy inventory 的 unresolved/retain 归零；原需保留的 capability 对目标 consumer 可用，或已有明确退役影响验收。
4. 只关闭 legacy assembly，保留代码；`legacy_assembly_off` 再独立满足 7 天/1000 样本，且启动时无 legacy Client、无 startup `tools/list`、无动态 MCP capability 注册。
5. 只在上一步生产 evidence/approval 已归档后执行 CP-8 物理删除。删除旧全局 state/registry/revision 路径，保留 Client、Transport、五版本 adapter、Rust Sidecar、SSRF/Header/Schema 安全能力以及所有用户数据。

assembly-off 和物理删除不得同发布。在版本回滚窗口关闭前，不删除最后 legacy tag/artifact 或 rollout evidence ledger。

## 本地验证

以下命令仅验证仓库实现和 CLI 契约，不产生 production evidence，不能标记 CP-7/CP-8 完成：

```bash
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_user_mcp_rollout_config tests.integrations.mcp.test_user_mcp_rollout_evidence tests.integrations.mcp.test_user_mcp_recovery_worker tests.integrations.mcp.test_legacy_migration_apply tests.api.test_user_mcp_recovery_startup tests.observability.test_user_mcp_safety_detectors
conda run -n multi_agent python -m unittest tests.scripts.test_user_mcp_phase3_evidence_validator tests.scripts.test_produce_user_mcp_shadow_evidence tests.scripts.test_user_mcp_phase3_rollout_control tests.scripts.test_migrate_legacy_mcp_config
conda run -n multi_agent python -m unittest tests.storage.test_mcp_rollout_ledger tests.storage.test_mcp_recovery_claims tests.storage.test_mcp_task_route_assignment tests.storage.test_user_mcp_server_atomic tests.storage.test_mcp_shadow_audit_samples tests.storage.test_user_mcp_rollout_postgres_permissions tests.api.test_mcp_rollout_postgres_runtime_wiring
git diff --check
```

真实 PostgreSQL 权限与竞态门禁不能由 SQLite 替代。对隔离测试库设置 `MAF_POSTGRES_ROLLOUT_PERMISSIONS_TEST_DSN` 与 `MAF_POSTGRES_ROLLOUT_INTEGRATION_TEST_DSN` 后运行：

```bash
conda run -n multi_agent python -m unittest tests.storage.test_user_mcp_rollout_postgres_permissions tests.storage.test_user_mcp_rollout_postgres_integration tests.storage.test_legacy_mcp_migration_postgres_integration
```

rollout 模块在真实 PostgreSQL 中为 app/snapshot/CI/evaluator/operator/validator/drill 创建独立 LOGIN 和连接，验证连接角色、受限 definer owner、基表 DML 越权拒绝、五个生产 stage 的数据库派生 evidence、必需序列 interval-union 完整性、replay、caller-authored production JSON 拒绝、approval/block/activation 串行、安全红线 setter 拒绝、additive 正值与 block 同事务、active-block 下严格降暴露 rollback、resolution 唯一性和 instance lease fingerprint 一致性。migration 模块验证专属 LOGIN/owner、Server/credential/durable record 同事务 all-or-zero、直接 UPDATE/DELETE 拒绝、精确 replay 幂等和冲突 replay 拒绝。任一专用 DSN 未配置时对应测试显示 skip，skip 不是门禁通过，上述测试结果也不是 production evidence。

上述命令全部通过后，再将实际观察窗、production attestation、approval/activation 和 rollback drill 作为独立发布证据审查。
