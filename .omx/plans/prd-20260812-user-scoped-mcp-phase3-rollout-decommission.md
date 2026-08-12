# 用户级按需 MCP 第 3 阶段：灰度切换与旧 Runtime 下线实施计划

- **日期**：2026-08-12
- **模式**：`$plan` direct
- **需求来源**：`docs/prd/MCP/user-scoped-on-demand/03-按需MCP灰度切换与旧Runtime下线PRD.md`
- **状态**：已完成 5 轮 `document-perfectization`，第 5 轮独立复审 `PASS`；可进入实施
- **范围**：阶段二恢复缺口、发布模式、稳定分流、shadow compare、单路径 enforce、迁移工具、可观测性、回滚、五版本门禁、旧全局 Runtime 物理下线

文中 `03...md` 指 `docs/prd/MCP/user-scoped-on-demand/03-按需MCP灰度切换与旧Runtime下线PRD.md`，`02...md` 指同目录的 `02-MCP两级路由授权与任务执行闭环PRD.md`；引用冒号后的数字均为当前仓库行号。

## 1. 目标结果

在不双执行任何真实 `tools/call` 的前提下，把新任务从旧全局 MCP Runtime 逐步切换到认证用户级 `mcp.dispatch -> MCPGateway` 链路，并在可审计的生产门禁通过后删除启动发现、全局工具注册、进程级 Client/Bundle/revision 与 `mcp_bundle_revision` 绑定。协议 Client、Transport、Python 五版本 Adapter、Rust Sidecar、安全校验、临时结果与用户级 Gateway 必须保留并复用（PRD：`03...md:9-11,100-109,210-242,428-446`）。

本计划把完成状态拆成两个独立终点：

1. **开发完成**：代码、自动化测试、迁移 dry-run、发布门禁验证器、Runbook 和部署配置全部就绪；只能证明可以进入内部 shadow。
2. **阶段三完成**：真实内部 shadow、内部 enforce、固定分组、100% enforce 的观察证据均通过，最后 legacy release tag 与回滚演练完成，旧 Runtime 才可停止装配并物理删除（PRD：`03...md:149-183,404-410,428-439`）。

本计划不把本地合成指标、mock 测试或 CI 结果冒充生产观察证据。

## 2. 计划置信标准

只有同时满足下列标准，计划才可进入实现：

| 维度 | 计划要求 |
|---|---|
| 目标与范围 | 每个检查点都能追溯到阶段三 PRD，且不重写协议栈、不新增 Transport family（`03...md:26-43`）。 |
| 前置依赖 | 阶段二 MRTR 密封恢复、标准 Tasks durable worker 和普通调用重启 unknown 收敛先完成；未完成时只允许开发或 shadow（`02...md:364-412,503-526,564-571`；`03...md:13-24`）。 |
| 单路径安全 | 每个真实任务的执行路径创建时固化；shadow 永不调用 `tools/call`、永不弹授权、永不改变回答；未知结果不重放（`03...md:92-109,126-147`）。 |
| 可回滚性 | legacy 仍存在时可以只阻止新任务进入新链路；在途任务不迁移；用户自定义 Server 不回退到全局配置（`03...md:330-347`）。 |
| 可测试性 | 所有功能性验收项都绑定自动化测试；真实发布步骤绑定机器可读 evidence 与 Runbook 演练记录。 |
| 可观测性 | 指标标签低基数，审计字段 allowlist，不记录 URL、凭据、完整 Schema、参数或结果（`03...md:290-328`）。 |
| 下线安全 | 先停止新任务、再观察/排空、再停止装配、最后删除代码；保留 Client/Transport/Sidecar/安全能力（`03...md:210-242,270-288`）。 |
| 文档一致性 | PRD、API 文档、Runbook、部署配置、相关 `AGENTS.md` 索引与 `CHANGELOG.md` 同步（`03...md:428-439`；根 `AGENTS.md` 的文档维护规则）。 |

## 3. 当前仓库证据与差距

| 领域 | 当前证据 | 对计划的影响 |
|---|---|---|
| 阶段状态 | `docs/AGENTS.md:23-29` 和 `02...md:3-8` 明确阶段一完成、阶段二核心闭环完成、MRTR/Tasks 跨重启恢复仍待补、阶段三待实施。 | CP-0 是 enforce 的硬前置，不得跳过。 |
| 用户级基础 | `src/api/routes/user_mcp.py:21-180`、`src/integrations/mcp/gateway.py:195-266,440-524`、`src/capabilities/mcp_dispatch/` 已有 owner-scoped API、任务级 Gateway、Router/Selector 和 `mcp.dispatch`。 | 阶段三复用现有链路，不新建第二套 Client/Gateway。 |
| 恢复数据层 | `src/storage/sqlite/models.py:118-157` 已有加密 remote-task binding 与 sealed-state 表；`src/storage/sqlite/repositories.py:2907-3028` 已有保存/查询接口。 | CP-0 重点是把现有持久化 seam 接入 Adapter/Coordinator/worker，不重复建表。 |
| 恢复运行时缺口 | `src/integrations/mcp/tasks.py:31-40` 仍是 `durable = False` 的内存 registry；`src/integrations/mcp/adapter_2026.py:164-170,488-501,564-574` 仍用进程内字典保存 `requestState`/remote task ID。 | 必须实现密封恢复、lease/claim worker、重启只查询不重发。 |
| 当前发布开关 | `src/api/runtime.py:7470-7481` 只解析 `MAF_USER_MCP_ENABLED` / `MAF_USER_MCP_ROUTING_ENABLED`；阶段三要求的六个 `MCP_*` 变量尚无实现（PRD：`03...md:60-98`）。 | 新增单一 fail-closed 配置对象，禁止散落读取环境变量。 |
| 任务路径未持久化 | `src/core/models.py:331-343` 和 `src/storage/sqlite/models.py:402-419` 的 Task 没有 MCP 执行路径字段；`src/api/runtime.py:989-1069` 只把 runtime revision 放入一次执行 metadata。 | 需要 additive task 字段或同等权威记录，保证重启/interrupt resume 不重新分流。 |
| 旧启动发现 | `src/api/runtime.py:7688-7719` 构建全局 `MCPRuntimeState` 并同步执行 startup `prepare_refresh_sync()`。 | 100% enforce 后先停止装配和启动发现，再进入物理删除。 |
| 全局工具注册 | `src/api/runtime.py:8731-8764` 把旧 Bundle descriptor/binding 注册到全局 Registry；`src/api/routes/capabilities.py:15-32` 返回 public registry。 | shadow/enforce 必须保证用户工具不进入 registry；legacy 删除后移除同步函数与测试期望。 |
| 进程级状态 | `src/integrations/mcp/runtime_state.py:113-187,478-497,965-1010` 持有 `_clients`、`_bundles`、`_active_revision` 和 retain/release。 | 最后阶段删除进程级状态，保留协议 Client/Transport。 |
| revision 执行链 | `src/api/runtime.py:1021-1024,1841-1844,1877-1895,7051-7109` 写入/保留/释放 revision；`src/capabilities/mcp_tool/executor.py:205-213` 用 revision 找 Binding。 | 只有在 legacy 不再装配后才能移除 metadata 与 executor 路径。 |
| 审计 | `src/integrations/mcp/audit.py:12-31,54-74,105-123` 已有 30 天保留，但 allowlist 没有 rollout mode/config version/diff 分类。 | 扩展安全字段而不是另建不受控日志。 |
| 发布门禁脚手架 | `src/integrations/mcp/promotion.py:6-59` 有 7 天/1000 样本/回滚与 conformance 的既有门禁先例，但它服务于另一条 sidecar/SDK promotion 语义。 | 可以复用门禁结构，不能直接复用 `can_shadow_replay_tool()`；用户级 shadow 明确禁止任何 `tools/call`。 |
| Runbook | `docs/runbooks/user-mcp-gateway.md:3-35` 只描述阶段一启用、密钥、容量和基础回滚。 | 增补 off/shadow/enforce、cohort、证据、rollback、tag 和 legacy 删除流程。 |
| 现有回归 | `tests/api/test_user_mcp_phase_boundary.py:63-115` 保护用户工具不进全局 registry；`tests/integrations/mcp/test_2024_legacy_runtime_discovery.py:90-161` 仍正向要求 legacy 全局发现/调用。 | 前者长期保留，后者在物理删除检查点改为协议 Transport/Client conformance，而非启动注册行为。 |

## 4. 范围与非目标

### 4.1 本计划范围

1. 补齐阶段二跨重启恢复硬前置。
2. 解析并验证阶段三六个后端配置项；生成稳定、可解释、任务级固化的路由决定。
3. 实现无副作用 shadow control-plane compare 和安全差异分类。
4. 实现 cohort/百分比分流、单路径 enforce、自动停止扩大与新任务回滚。
5. 提供 legacy 配置的一次性、显式 owner 迁移工具；凭据重新加密，旧运行期状态不迁移。
6. 提供低基数指标、审计事件、发布 evidence validator 与 Runbook。
7. 完成五版本用户级 Gateway conformance、资源/泄漏/容量/重启/回滚门禁。
8. 在外部证据通过后停止装配并删除旧 Runtime 状态、全局注册和 revision 绑定。

### 4.2 非目标

1. 不新增 MCP Transport family，不重写现有 Client、Transport、Python Adapter 或 Rust Sidecar（`03...md:35-43,232-242`）。
2. 不自动把 legacy 配置复制给所有用户，不迁移 Tool List/Schema/Client/Bundle/revision/授权推断（`03...md:185-208`）。
3. 不为 ordinary `tools/call` 添加自动重试、未知结果重放或跨链路 fallback（`03...md:100-109`）。
4. 不让前端提交 cohort、执行路径或安全开关（`03...md:358-365`）。
5. 不在开发检查点自动操作生产流量、创建 release tag、切换远端配置或删除部署中的 legacy；这些是凭据/生产权限受控的运维步骤。
6. 不新增依赖，不在本阶段引入第三方 metrics exporter；生产门禁使用 PostgreSQL durable metric bucket、现有 MCP 审计和追加式 evidence/approval ledger。

## 5. 已确认的业务、运维与证据决策

以下口径来自第 1 轮 `document-perfectization` 审查，并已获用户批准。实现、测试、Runbook 与阶段三 PRD 必须使用同一口径。

### D-1 — Cohort 权威来源与多实例一致性

1. 新增 canonical 环境变量 `MCP_ENFORCE_COHORT_CONFIG_FILE`，指向运维只读、权限不高于 `0440` 的原子替换文件；文件 closed schema 为 `maf.mcp.rollout_cohorts.v1`，只含 `config_version` 与 `user_cohorts[user_id] -> cohort_ids[]`。
2. 应用只在启动时原子加载 cohort 文件；变更通过带新 immutable deployment ID 的滚动发布生效，不做 hot reload，也不在同一 deployment 内原地替换。文件缺失、不可读、schema/权限非法，或 `MCP_ENFORCE_COHORTS` 非空但无可用映射时实例不 Ready。
3. cohort 文件 digest 纳入 rollout config fingerprint。每个实例把 fingerprint 写入数据库 instance-config lease；同一 deployment 内出现不同 fingerprint 时新实例不 Ready，既有实例阻止 promotion。滚动期间旧/新 deployment 各自一致，Task 仍以创建时 assignment 跨 deployment 固化。
4. 路由决定在 Task 创建事务内固化；后续文件变化、实例切换、interrupt/resume 或 worker 重启都不得重算。日志、指标和审计不记录 raw user ID，只记录 HMAC owner 与低基数 cohort/category。

### D-2 — Legacy 迁移范围与完整性证明

1. 首版只支持迁移到显式服务账号 owner；缺 owner 拒绝执行。system-owned Profile + ACL 不在本阶段推断或实现，必须另立 PRD。
2. inventory 为每个 legacy server 记录且只允许一种处置：`migrate_owner`、`retain_for_rollback`、`retire`，并记录 `consumer_scope=service_account_only|multi_user|unknown`、目标消费者集合的安全摘要和受影响 capability；dry-run 必须证明全量覆盖，未知/重复/遗漏均 fail closed。
3. `migrate_owner` 只适用于经证据确认仅由目标服务账号消费的 Server。`multi_user|unknown` Server 在 system-owned ACL PRD 落地前必须保持 `retain_for_rollback` 并阻止 assembly-off；唯一例外是授权运维明确 `retire`，同时提交用户影响验收和替代/下线说明。
4. 在停止 legacy assembly 前，`retain_for_rollback` 必须归零；每项必须已迁移并证明原目标消费者能力仍可用，或按上一条完成可审计 retire。不得迁移 Tool List、Schema、Client、Bundle、revision 或历史授权推断。

### D-3 — 分档观察窗口与 promotion 门槛

1. **A 内部 Shadow**：连续 24 小时；HTTPS Streamable HTTP、HTTPS legacy HTTP+SSE、白名单 HTTP legacy HTTP+SSE，以及认证失败、超时、拒绝授权、大输出的每个场景至少 3 个 `comparable expected-result match` 样本；该术语指 legacy visible 与 shadow observer 分别命中窗口开始前版本化 manifest 预注册的结果，并非要求两个 lane 都成功。真实 shadow `tools/call`、双执行、凭据/身份泄漏、持续资源泄漏均为 0。
2. **B 内部 Enforce**：连续 48 小时；取消、120 秒、5 分钟、重启 unknown、MRTR/Tasks recovery、公平排队、flag rollback 各至少成功演练 1 次；安全红线为 0。
3. **C 每个固定比例档**以及 **D 的 100% enforce、legacy assembly-off**：每档必须同时满足连续 7 天和至少 1000 个 terminal 真实 user-scoped `tools/call`；新链路错误率不高于已批准的 legacy/shadow 基线，p95 不超过该基线的 110%，安全红线为 0。
4. 有效样本只计已经 dispatch 到目标 Server 且进入 terminal 的真实调用；shadow、selector 未选中、授权前拒绝和 dispatch 前取消不计分母。错误率分子为 transport/protocol/server/unknown terminal failure；用户主动取消单列，不掩盖为成功。p95 使用从 Gateway dispatch 到 terminal 的 wall time，MRTR/remote Tasks 与 ordinary call 分桶比较，不混合稀释。
5. 低流量、零分母、缺测、窗口中断、样本不足都记为 `blocked`，不得人工豁免为通过；只能延长同一档观察窗口。

A 阶段 scenario manifest 使用 closed `scenario_id` 与预期结果；manifest 在观察窗开始前绑定 fixture/mapping/config fingerprint，窗口内不可追认修改：

| `scenario_id` | Legacy visible 预期 | Shadow observer 预期 | 关键附加断言 |
|---|---|---|---|
| `https_streamable_success` | `tool_call_succeeded` | `control_plane_ready` | route、Catalog HMAC/schema fingerprint、selector 一致。 |
| `https_legacy_sse_success` | `tool_call_succeeded` | `control_plane_ready` | transport=`legacy_http_sse`。 |
| `allowlisted_http_legacy_sse_success` | `tool_call_succeeded` | `control_plane_ready` | Endpoint Policy=`allowed_by_enterprise_allowlist`。 |
| `authentication_failure` | `authentication_failed` | `authentication_failed` | raw code 归一化；shadow 不产生授权 UI/event。 |
| `timeout` | manifest 固定 checkpoint 的 `timeout` | manifest 固定 checkpoint 的 `timeout` | 其他阶段 timeout 算 mismatch。 |
| `permission_denial` | `tool_call_succeeded` | manifest 预注册只读 denial fixture 的 `permission_denied_suppressed` | 这是允许的 lane-specific expected policy delta：legacy 没有 Grant seam，正常执行 1 次；shadow 不弹授权、不写 Grant/event/interrupt，outbound call 为 0。 |
| `large_output` | `tool_call_succeeded_large_result` | `control_plane_ready` | 仅 visible lane spill；shadow 不读/复制结果，最终 answer 与 off 相同。 |

样本只有在双方分别命中 manifest 的 lane-specific 预期、所有共同到达的控制面检查点一致、预注册 expected policy delta 精确匹配、审计完整且 digest 有效时为 `matched`。`permission_denial` 的 visible/shadow 结果差异不能泛化为忽略其他 policy mismatch；只有同一 manifest/fixture/config fingerprint 的这一 closed scenario 可接受。有 verified mapping 但结果/cleanup 不匹配为 `mismatched`；`not_comparable` 仅允许已批准 retire 项；非 terminal、窗口外、旧 config 或重复 nonce 为 excluded。Validator 必须处理窗口内全部 eligible 样本，任一 mismatch/evidence invalid 不能靠额外 3 个 matched 样本掩盖。

### D-4 — 生产指标、证据来源与防伪

1. 权威后端是 PostgreSQL durable metric buckets、现有 MCP audit 与追加式 evidence/approval ledger；不依赖第三方 exporter，也不以进程内计数器作为 promotion 依据。
2. 每份 evidence 至少包含 environment ID、git SHA、deployment ID、stage、config fingerprint、观察起止时间、producer、单调 snapshot ID、唯一 nonce、payload digest 与 `source=ci|production`。
3. Production snapshot 只能由受限数据库角色从 durable buckets 在事务内生成；`source`、producer 和 digest 由服务端写入，operator CLI 不接受 caller-supplied production JSON。Ledger 数据库角色只有 append/consume 所需权限，普通应用与 CI 不能改写历史 evidence/approval/block。
4. 数据库约束拒绝重复 `(deployment_id, stage, snapshot_id)` 与 nonce；validator 校验 digest、连续窗口、单调性和 source。`source=ci` 永远不能满足生产 promotion/删除门禁。
5. 指标 bucket、审批、阻断与 evidence 至少保留到 legacy 物理删除后的版本回滚窗口关闭并归档 release evidence；现有 MCP 审计仍按 30 天策略清理。
6. PostgreSQL 角色边界固定为：普通 app 只可写 metric bucket/instance lease；snapshot producer 只可从 bucket 读取并 append evidence；自动 gate evaluator 只可读 metric/evidence/activation 并 append block；rollout operator 只可 append approval/block-resolution/activation 与执行降级；read-only validator 只读 ledger；CI 角色不能写任何 `source=production` 数据。历史 evidence/approval/activation/block/resolution 对所有运行角色均禁止 UPDATE/DELETE。

### D-5 — 自动阻断与人工回滚边界

1. 红线或门槛失败持久化 `promotion_blocked`，阻止任何提高暴露面的新配置/审批；它不自动改写环境变量、cohort 文件或当前路由比例。
2. 当前违规请求按既定安全语义失败关闭；已创建 Task 始终沿固化路径完成/取消/unknown，不迁移、不重放。
3. “降低暴露面”必须可证明不会让任何用户从 `legacy|unavailable` 新增进入 `user_scoped`：模式按 `enforce > shadow > off`，同一 salt/cohort mapping 下 enforce 候选 cohort 只能取子集且 percent 只能降低。salt、cohort mapping 或不可比较字段变化一律按扩大处理。合法降低始终允许授权运维通过滚动配置更新执行，且只影响新 Task。
4. 解除阻断或扩大比例必须通过 operator CLI，提交 reason、evidence snapshot 与 approver。`off -> shadow` 只可使用 CP-1 至 CP-6 的 CI evidence；`shadow -> enforce` 及后续扩大必须使用上一档 `source=production` evidence。
5. 这是经用户批准的运维口径；阶段三 PRD 中“自动切回安全路径”的表述必须在 CP-9 同步为“自动阻止继续扩大，授权运维执行新任务回滚”。
6. Approval 是不可变审查记录，不由实例消费。唯一原子事务把 approval 消费为 `(environment_id, deployment_id, stage, config_fingerprint)` 唯一 activation；所有实例只读取同一 activation 并登记 instance lease。部分滚动失败不产生第二份 activation，rollback 生成新的低暴露 deployment/activation 记录而不篡改历史。
7. Block 也是不可变记录；自动 evaluator 只能 INSERT。解除阻断通过 append-only `block_resolution(block_id, approval_id, evidence_id, reason, approver)` 表达，`block_id` 唯一且原行不变。`active block` 定义为不存在有效 resolution 的 block；resolution 只恢复“可申请 activation”，绝不自动扩大流量。
8. 每个 `(environment_id, rollout_program=user_mcp_phase3)` 有一行无业务状态的 `gate_scope`。所有 stage 共用该锁，避免 B 阶段 block 与 C 阶段 activation 并发穿透。block、resolution、approval->activation、rollback activation 和 instance lease 都在 PostgreSQL 事务内先 `SELECT ... FOR UPDATE` 该 scope：activation 在锁内重新验证 evidence、approval 唯一引用和无 active block；并发 block 若先取得锁则 activation 失败，activation 若先提交则随后 block 阻止新实例准入与下一次扩大，但不改写当前路由。

### D-6 — Legacy tag、观察与删除顺序

顺序固定为：发布仍包含 legacy 的最后 tag -> 验证该 tag 的可部署 artifact -> 完成 version rollback drill -> 100% enforce 且 legacy 仍可装配的观察窗口 -> legacy assembly-off 观察窗口 -> 物理删除 legacy 状态/注册/revision 代码。任一步证据缺失都不得越级。

### 阶段准入表

| 阶段 | 最低准入条件 | 明确禁止 |
|---|---|---|
| 默认关闭代码合并 | CP-1 至 CP-3 自动化测试与 CI evidence 通过。 | 把 CI evidence 当生产证据。 |
| A 内部 Shadow | CP-1/2/3/4/6 通过；shadow 零 `tools/call` 门禁通过；所有计划保留的 legacy Server 已有 verified mapping。CP-0 可并行收口。 | 任何真实 user-scoped execute；以 `not_comparable` 充当有效样本。 |
| B/C/D 任意 Enforce | CP-0 至 CP-6 全部通过，A 阶段 evidence 已审批。 | CP-0 未完成时启用 enforce。 |
| Legacy assembly-off | C 全部比例档与 D 100% enforce 通过；inventory 无 unresolved/retain，且 retained capability 对原目标消费者仍可用或有已批准退役验收；最后 legacy tag/artifact/rollback drill 完成。 | 同时删除 legacy 代码，或让 multi-user/unknown consumer 能力静默消失。 |
| 物理删除 Legacy | assembly-off 独立 7 天/1000 样本窗口通过，且最后 legacy tag/artifact 在剩余版本回滚窗口内仍可部署、evidence 已归档。 | 在 assembly-off 同一发布中删除，或提前销毁回滚 artifact。 |

## 6. 验收标准

| ID | 可测试验收项 | 证据 |
|---|---|---|
| AC-01 | 六个 PRD 配置项及 `MCP_ENFORCE_COHORT_CONFIG_FILE` 只接受 closed schema/枚举/范围；cohort 文件权限、digest、映射或同 deployment instance-config fingerprint 非法/不一致均不 Ready。 | 配置/权限单测 + 多实例 runtime assembly/readiness test（`03...md:60-90`）。 |
| AC-02 | 稳定哈希仅使用认证 user ID、固定 salt 和版本化算法；同一 rollout config 下重复/跨实例结果一致，前端输入无法改变结果。 | 单元 property matrix + API negative test（`03...md:92-98,358-365`）。 |
| AC-03 | 新 Task 与创建事务原子持久化完整 Task snapshot 及 `real_path`、`shadow_enabled`、`config_version`、closed `reason_code`；assignment write-once，Task 可变字段只允许合法状态推进。`off` 以 Python store 为权威，Runtime Sidecar `shadow` 双写双读比较，`enforce` 的 save/get 只走 Sidecar；interrupt/resume、进程重启、配置竞态均不改变已有 assignment。 | SQLite/PostgreSQL repository concurrency + Sidecar Submit/GetTask proto/kernel/adapter contract + off/shadow/enforce API/runtime restart tests。 |
| AC-04 | `shadow` 的真实执行仅走 legacy；旁路最多执行 route/discover/list/selector dry-run/cleanup，不调用 `tools/call`、不创建/修改 Grant、不发授权事件、不改变 plan/result/answer；所有 Transport 和认证/超时/selector/cleanup 失败分支均保持零 call。 | hostile fake-server integration + transport/failure matrix + event/audit assertions（`03...md:111-145`）。 |
| AC-05 | 每个真实任务只能进入 `legacy` 或 `user_scoped` 一个 executor；网络断开、重启、模式切换和错误 fallback 均不造成第二次 `tools/call`。 | single-path spy transport + restart/concurrency tests（`03...md:100-109`）。 |
| AC-06 | 用户自定义 Server 在 off/rollback/未命中 cohort 时明确不可用，不映射到全局 Server；只有存在受控 legacy 等价配置的系统 MCP 可走 legacy。 | routing/API/frontend status tests（`03...md:81-84,169-176,330-347`）。 |
| AC-07 | MRTR `requestState` 与原 tool/arguments 以任务私有密文保存；重启后用新 request ID 恢复原调用，每轮计入 20 次，不泄漏 opaque state。 | encryption/storage/adapter/restart tests（`02...md:375-385`）。 |
| AC-08 | 标准 MCP Task binding 持久化；worker 重启后只 `get/update/cancel`，不会重新发起原 `tools/call`；2025/2026 DTO/方法表隔离。 | worker lease/restart/version-cross-negative tests（`02...md:387-412`）。 |
| AC-09 | ordinary active call 在进程重启后 durable 收敛为 unknown，永不自动重放，Main Agent/前端只显示状态无法确认。 | crash-window test + event/frontend test（`02...md:366-373`）。 |
| AC-10 | migration inventory 全量且每个 server 恰有一种 disposition、closed consumer scope 和 capability impact；`migrate_owner` 只接受 `service_account_only`，`multi_user|unknown` 未完成可审计 retire/影响验收时阻止 assembly-off。dry-run 不输出 secret；apply 必须显式 owner、重新加密、生成新 server/security version、写安全审计，不迁移运行期状态。迁移健康检查须完成握手、发现、完整分页 `tools/list`、至少一个合法 tool，凭据失败不得建 mapping；最多两次、每次 60 秒。 | CLI/repository/inventory/consumer-impact/health/secret scan tests（`03...md:185-208`）。 |
| AC-11 | rollout 指标以 PostgreSQL durable buckets 持久化，标签仅固定枚举；七个 rollout/legacy 审计事件只携带 allowlist 字段并保留 30 天。Evidence 具备 D-4 provenance、唯一性、角色权限和 digest 校验，CI/普通 app 无法伪造 production source，缺测/零分母无法通过生产门禁。 | 真实 PostgreSQL role/transaction integration + observability/storage/validator contract + replay/high-cardinality/secret negative scan（`03...md:290-328`）。 |
| AC-12 | 五个目标版本在用户级 Gateway 上通过 version + transport + adapter 矩阵；2026 能力不下放到旧版；Sidecar 不宣称未验证版本。 | fixtures/conformance validator（`03...md:257-278,374-388,412-426`）。 |
| AC-13 | 最后 legacy tag/artifact/rollback drill、100% enforce 观察窗先通过；随后 legacy assembly disabled 的独立观察窗内，应用启动不创建 legacy Client、不执行 `tools/list`、不注册动态 MCP capability，空闲 Client/Catalog 数为 0。 | artifact provenance + startup spy + registry/resource baseline tests（`03...md:270-288,397-402`）。 |
| AC-14 | legacy 物理删除后不存在 `mcp_bundle_revision` 的新写入/继承/retain/release，旧历史字段只读忽略且不触发 discovery/replay。 | storage/runtime/history tests + scoped `rg` allowlist（`03...md:366-372`）。 |
| AC-15 | 红线自动持久化 promotion block，但不自动改写路由；授权运维降低暴露面只影响新 Task。开关/版本回滚保留新增表、密文、Grant、审计和 evidence，且在途任务不迁移、不重放；用户自定义 MCP 返回明确可恢复不可用状态。 | block/approval/rollback integration + Runbook drill evidence（`03...md:330-355`）。 |
| AC-16 | 完成/失败/拒绝/取消/断线/重启/shutdown 都释放用户级 Client、连接、Catalog、lease 和临时文件；配置用户量增长但无任务时内存不线性增长。 | lifecycle/load/resource tests（`03...md:270-288,397-402`）。 |
| AC-17 | `/api/v1/capabilities` 永不包含用户动态工具；legacy 删除后也不再包含旧全局动态 MCP tool，唯一公共入口仍是按请求可见的 `mcp.dispatch`。 | API/registry tests（`03...md:366-372`）。 |
| AC-18 | PRD、API 文档、Runbook、部署配置、`AGENTS.md` 和 `CHANGELOG.md` 与最终代码一致。 | docs contract tests + final file review（`03...md:428-439`）。 |
| AC-19 | 提高 exposure 只有 immutable approval 已原子转为唯一 deployment activation 且无 active block 时才能 Ready；各实例读取同一 activation 并登记 lease。自动 evaluator 可 append block；解除使用唯一 append-only resolution，且不会自动 activation。所有 stage 的转换以 rollout gate scope 串行化，审批/activation/block/resolution 均不可重放。 | activation/block/resolution state-machine + 跨 stage/多实例并发/部分滚动失败/block 竞态/rollback PostgreSQL tests + operator CLI negative tests。 |
| AC-20 | A shadow 前，所有计划保留的 legacy Server 都有 verified mapping；每个必测场景至少 3 个 `comparable expected-result match` 样本。每个样本须分别匹配预注册 visible/shadow category、stage、transport/policy；任一未解决 mismatch、非法 evidence 或非 retire 项的 `not_comparable` 都阻止进入 B。 | migration mapping + versioned scenario manifest + shadow validator scenario/negative tests。 |

## 7. 实施检查点

### CP-0 — 补齐阶段二恢复硬前置

**目标**：让已有 `mcp_remote_task_binding` / `mcp_sealed_state` 持久化模型真正进入 Gateway/Adapter/Coordinator 恢复链；未通过不得启用任何 enforce。

**主要文件**：

- `src/core/models.py`
- `src/storage/interfaces.py`
- `src/storage/sqlite/models.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- `src/integrations/mcp/credentials.py`
- `src/integrations/mcp/adapter_2026.py`
- `src/integrations/mcp/tasks.py`
- `src/integrations/mcp/dispatch_coordinator.py`
- `src/api/runtime.py`
- `tests/storage/test_mcp_phase2_repository.py`
- `tests/integrations/mcp/test_2026_07_28_adapter.py`
- 新增 `tests/integrations/mcp/test_user_mcp_recovery_worker.py`
- `tests/api/test_user_mcp_grants_and_call_control.py`

**任务**：

1. 先写重启失败测试：MRTR 等待输入、2025/2026 remote task working/input_required、ordinary call `may_have_dispatched=true` 三种场景。
2. 建立任务私有 seal/unseal service，AAD 至少绑定 owner/task/node/call/state kind；复用现有 MCP credential key，不输出 raw state/remote ID。
3. Adapter 不再把进程字典作为跨重启权威；Coordinator 在产生 `input_required`/`task_created` 时先 durable save，再对外发 safe ref。
4. 为 due remote-task binding 增加数据库 claim/lease/CAS，避免多实例重复 poll；worker 只调用对应版本允许的 get/update/cancel。
5. startup 先把 may-have-dispatched ordinary calls 收敛为 unknown，再启动 remote-task worker；worker shutdown 释放 claim，不重发原调用。
6. 2025 实验 Tasks 与 2026 Tasks Extension 分 handler/DTO/测试，禁止共享错误方法表。

**退出门禁**：AC-07/08/09 全通过；阶段二 PRD 状态、`docs/AGENTS.md` Future Work 和 `CHANGELOG.md` 同步后，才允许任何 enforce 激活。CP-1 至 CP-3 可并行开发并以默认关闭方式合并，A 内部 shadow 只受阶段准入表约束。

### CP-1 — 发布配置、任务级路由决定与持久化

**目标**：用单一、fail-closed、可版本化的配置对象替换当前散落的布尔开关；任务创建时一次性固化路由。

**主要文件**：

- 新增 `src/integrations/mcp/rollout.py`
- `src/core/enums.py`
- `src/core/models.py`
- `src/storage/sqlite/models.py`
- `src/storage/sqlite/bootstrap.py`
- `src/storage/postgres/bootstrap.py`（复用 `src/storage/sqlite/models.py` 的共享 SQLAlchemy metadata）
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- `src/storage/runtime_sidecar_grpc_client.py`
- `src/storage/runtime_sidecar_facade.py`
- `src/storage/runtime_sidecar_shadow.py`
- `src/storage/rust_contracts/runtime_sidecar_contract.json`
- `native/proto/maf/runtime/v1/runtime.proto`
- `native/crates/maf_runtime_store/src/lib.rs`
- `native/crates/maf_runtime_sidecar/src/lib.rs`
- `native/crates/maf_runtime_sidecar/src/sqlite_adapter.rs`
- `src/api/runtime.py`
- 新增 `tests/integrations/mcp/test_user_mcp_rollout_config.py`
- 新增 `tests/storage/test_mcp_task_route_assignment.py`
- `tests/storage/test_rust_runtime_sidecar_contract.py`
- `native/crates/maf_runtime_sidecar/tests/runtime_sidecar_sqlite.rs`
- `tests/api/test_user_mcp_runtime_wiring.py`

**任务**：

1. 定义 `MCPRolloutConfig`：gateway enabled、routing mode、legacy enabled、cohorts、percent、salt、cohort file digest、deterministic config fingerprint/version。
2. 实现 D-1 的只读 cohort file loader、权限/schema 校验和数据库 instance-config lease；启动时验证 PRD 合法组合，`enforce` 缺 salt、percent 越界、未知枚举、gateway/legacy 冲突、非空 cohorts 无映射或同 deployment fingerprint 不一致全部 fail closed/not Ready。
3. 定义 `MCPTaskRouteAssignment(real_path, shadow_enabled, config_version, reason_code)`；真实路径只允许 `legacy|user_scoped|unavailable`，`shadow_enabled` 独立表示旁路。
4. 给 Task 增加 additive、nullable 兼容字段：`mcp_execution_mode`、`mcp_shadow_enabled`、`mcp_rollout_config_version`、`mcp_route_reason_code`；数据库 enum/CHECK 拒绝开放字符串，旧历史 null 只读解释为 legacy-history，不可重新执行。
5. 稳定桶使用版本固定的 HMAC/SHA-256 算法，输入只含认证 user ID + salt；记录 bucket/category，不记录原 username 作为 metric label。
6. 选择“扩展 Runtime Sidecar Task contract”作为唯一权威方案：新增 closed `TaskRecord`（完整 Python `Task` 字段与 optional `TaskRouteAssignment`），`SubmitTaskRequest/Response` 在保留原 wire field number 的基础上追加 record；Sidecar kernel/SQLite adapter 在同一事务创建/更新 Task record，assignment 首次写入后不可变，Task 状态只允许合法推进。每次 save 的 idempotency key 为 `task_id + canonical TaskRecord SHA-256`；同 key + 同 payload 返回原 record，payload 变化返回新的非重试错误 `runtime_store_idempotency_conflict`。
7. 新增 `GetTask(GetTaskRequest) -> GetTaskResponse{found, task, error}` RPC。Sidecar SQLite `submitted_tasks` 保存可重建 Python `Task` 的全部 11 个字段和 assignment。全 null assignment 的旧 Python 行仅在展示时解释为 `legacy_history` 且不回写；部分 null 视为损坏。旧 Sidecar 行若缺完整 Task 字段只能进入迁移 inventory，完成 additive backfill 前禁止 enforce，不能构造伪 Task。
8. 明确 repository 权威路由：`off` 的 `save_task/get_task` 只走 Python store；`shadow` 先由 Python store 提交并返回，再对 Sidecar 做同 snapshot 写入和 GetTask 双读比较，mismatch 只阻止 Runtime Sidecar promotion；`enforce` 的 save/get 只走 Sidecar，Sidecar unavailable/not-found/invalid snapshot 均 fail closed，绝不回读 Python store。active-task/list 查询也必须使用同一权威或明确在 enforce 禁止调用，不能混合来源。
9. 更新 gRPC client/facade/shadow helper、`SCHEMA_HASH`、`PROTO_HASH`、`ERROR_CODE_TABLE_HASH`、Rust contract JSON、artifact attestation 与 `task_read` feature/`task_get` operation。native/Python 测试覆盖 create/update、合法/非法状态推进、idempotent retry、payload conflict、assignment mutation、并发 CAS、GetTask found/not-found/unavailable、关闭重开/进程重启、shadow 的 not-found/identity/assignment/sidecar-error mismatch、enforce 禁止 Python fallback 与旧 nullable history。
10. planner profile、executor 注册、interrupt resume、recovery worker 都只读权威 Task snapshot 的 assignment，不重新读取当前比例决定。
11. Canonical `MCP_*` 是阶段三唯一路由权威。兼容发布中 `MAF_USER_MCP_ENABLED` 仅作为 subsystem assembly 前置且与新 gateway flag 冲突时 fail closed；`MAF_USER_MCP_ROUTING_ENABLED=true` 而 canonical routing mode 缺失时拒绝启动，不把旧布尔值静默推导为 `enforce`。CP-8 删除旧 routing flag。

**退出门禁**：AC-01/02/03 通过；所有默认值仍为安全关闭；未接 shadow 前不改变线上真实执行。

### CP-2 — Shadow control-plane compare

**目标**：旧链路继续真实执行，新链路只执行无副作用路由/发现/选择/校验/清理，并产出安全差异。

**主要文件**：

- 新增 `src/integrations/mcp/shadow_compare.py`
- `src/integrations/mcp/dispatch_coordinator.py`
- `src/capabilities/mcp_dispatch/server_router.py`
- `src/capabilities/mcp_dispatch/selector.py`
- `src/integrations/mcp/gateway.py`
- `src/integrations/mcp/audit.py`
- `src/api/runtime.py`
- 新增 `tests/integrations/mcp/test_user_mcp_shadow_compare.py`
- `tests/api/test_user_mcp_phase_boundary.py`

**任务**：

1. 定义 shadow 输入/输出 contract：legacy route 摘要、安全用户 Server profiles、new route、comparability、Catalog count/name-set HMAC/schema fingerprint、latency bucket、policy/grant booleans、cleanup counts。
2. Comparator 作为 observer 接收已经生成的 legacy plan/route，不能修改 plan、metadata、dependency result 或 answer。
3. 新链路允许 `server_router -> gateway list_tools -> selector dry-run -> schema/policy/grant read check -> close`；Gateway 提供显式 read-only shadow scope，接口层没有 `call_tool` 能力。
4. 禁止复用 `src/integrations/mcp/promotion.py:62-66` 的 `can_shadow_replay_tool()`；该函数允许部分 replay，与本 PRD “零 `tools/call`”不变量不兼容。
5. legacy/user Server 没有受控 mapping 时记录 `not_comparable`，不计 mismatch；比较 mapping 只来自显式迁移 ledger，不按 URL/名字猜测。该运行语义不等于 promotion 通过：A 阶段前 CP-4 mapping 准备必须完成，非 `retire` 项的 `not_comparable` 一律阻止进入 B。
6. 对 shadow 网络/selector/audit 失败 fail-open 到 legacy 用户结果，但记录安全错误码并确保资源关闭；跨用户、secret leak、双 call 等红线仍 fail closed/触发 promotion block。
7. 测试矩阵覆盖 Streamable HTTP、legacy HTTP+SSE，以及认证失败、连接/读取超时、selector 异常、audit 异常和 cleanup 异常；每条分支都由 spy transport 证明新链路 `tools/call == 0`、Grant/authorization event 写入为 0。
8. `permission_denial` 场景不改造即将下线的 `MCPToolExecutor`：legacy visible 按当前行为成功调用；shadow comparator 只读取观察窗开始前签名/版本化 manifest 的 denial fixture，产出 `permission_denied_suppressed`，不调用 Coordinator 的交互 `_resolve_approval`、不创建 interrupt/event/Grant，也不调用 `gateway.call_tool`。只有这个 closed lane-specific 组合计为 matched。

**退出门禁**：AC-04/05/20 通过；fake server 断言 shadow 的 `tools/call` 次数严格为 0；主 plan/answer snapshot 与 off 模式相同。代码可在无 mapping 时安全记录 `not_comparable`，但生产 A 阶段 promotion 仍须满足 AC-20。

### CP-3 — Rollout telemetry、审计与自动停止扩大

**目标**：让每档灰度具有低基数、可机读、可阻止 promotion 的证据。

**主要文件**：

- 新增 `src/integrations/mcp/observability.py`
- `src/integrations/mcp/audit.py`
- `src/integrations/mcp/promotion.py`（提取通用 gate 结构，不混淆 sidecar 指标）
- `src/core/models.py`
- `src/storage/sqlite/models.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- deployment PostgreSQL role/GRANT migration、`SECURITY DEFINER` 函数与连接配置模板
- 新增 `scripts/validate_user_mcp_phase3_evidence.py`
- 新增 `scripts/control_user_mcp_rollout.py`
- 新增 `docs/prd/MCP/user-scoped-on-demand/evidence/README.md` 或复用项目既有 evidence 约定
- 新增 `tests/observability/test_user_mcp_rollout_metrics.py`
- 新增 `tests/integrations/mcp/test_user_mcp_phase3_evidence.py`
- 新增 `tests/storage/test_user_mcp_rollout_postgres_integration.py`（真实 PostgreSQL，不允许 SQLite/fake 替代）

**任务**：

1. 新增 PostgreSQL 权威表/对应 SQLite contract：`mcp_rollout_metric_bucket`、`mcp_rollout_evidence_snapshot`、`mcp_rollout_gate_scope`、`mcp_rollout_stage_approval`、`mcp_rollout_deployment_activation`、`mcp_rollout_promotion_block`、`mcp_rollout_block_resolution`、`mcp_rollout_instance_config`；schema 使用 closed stage/source/category，唯一 nonce、单调 snapshot、唯一 deployment activation、唯一 approval/activation 引用、每个 block 至多一个 resolution 和 config fingerprint 约束。`gate_scope` 只提供 `(environment, rollout_program)` 事务行锁，不保存可变业务状态。
2. 实现 PRD 列出的指标名；label allowlist 仅含 execution path、mode、transport、protocol version、adapter、result/error category 等固定枚举。请求完成时写 durable count/latency bucket，进程内指标只能作为非权威诊断。
3. 扩展审计 allowlist，支持 rollout config version、hashed owner、route reason、comparability/diff category、count/latency bucket；仍禁止 raw URL/user/tool args/schema/result/credential。
4. 统一七个 rollout/legacy audit 事件，并验证 30 天清理继续适用。
5. Evidence schema 分开记录 CI conformance、内部 shadow、内部 enforce、cohort 档位、rollback drill、resource baseline、release tag；实现 D-4 provenance/digest/replay 校验，`source=ci`、缺测、零分母或断窗永远不产生 production pass。
6. 实现 D-5：安全红线或阈值失败原子写 `promotion_blocked`，readiness 拒绝更高 exposure 的 config；不得由观测进程自动改写环境变量/cohort/当前比例。授权运维的降低 exposure 始终允许且只影响新 Task。
7. 自动 gate evaluator 在持有 rollout gate scope 行锁的事务中 append block；它没有 UPDATE/DELETE/resolve/activate 权限。Operator 解除阻断必须 append 唯一 resolution，绑定 approval/evidence/reason/approver；resolution 只使该 block 不再 active，不自动改变 activation。任何历史行均不可修改。
8. operator CLI 对扩大要求 reason、匹配的 evidence snapshot 与 approver；单一事务锁定 rollout gate scope，把 immutable approval 以唯一逻辑引用“消费”为 deployment activation，并在 INSERT 前重新查询全部 stage 的 active unresolved block。配置 fingerprint 不匹配、重复 activation、并发 block 或重放均拒绝；合法降低 exposure 写新的 rollback activation，可在 active block 下执行但仍保留 previous activation 链和 operator reason。Partial rollout 只登记 instance lease，不生成第二份 activation。
9. 所有 runtime 写操作通过固定 `search_path`、`REVOKE ALL FROM PUBLIC` 的 `SECURITY DEFINER` 函数完成；运行角色没有底表直接 DML。由于现有 runtime schema 禁止 foreign key，approval/evidence/block 引用用唯一约束与锁内逻辑校验实现，不引入 FK。
10. 交付 D-4 角色/GRANT 配置，并以独立 PostgreSQL 连接运行集成测试：普通 app/CI 伪造 `source=production`、evaluator 越权 resolve/activate、operator 越权修改历史、越权 INSERT/UPDATE/DELETE、nonce/snapshot 并发、approval->activation 并发、跨 stage block-vs-activation 锁竞态、重复 resolution、部分滚动失败和 rollback 均 fail closed。SQLite contract/unit test不得替代此门禁。

**退出门禁**：AC-11/19 通过；本地只能生成 CI evidence，production source、观察窗口和审批字段必须保持 pending。

### CP-4 — Legacy 配置迁移 dry-run/apply

**目标**：提供显式、可审计、默认 dry-run 的一次性迁移；不改变所有权边界，不迁移运行态状态。

**主要文件**：

- 新增 `src/integrations/mcp/legacy_migration.py`
- 新增 `scripts/migrate_legacy_mcp_config.py`
- `src/integrations/mcp/config.py`
- `src/integrations/mcp/user_config.py`
- `src/integrations/mcp/credentials.py`
- `src/integrations/mcp/audit.py`
- 如需 mapping ledger：`src/core/models.py`、SQLite/PostgreSQL additive schema/repositories
- 新增 `tests/integrations/mcp/test_legacy_mcp_config_migration.py`
- 新增 `tests/integrations/mcp/test_legacy_mcp_migration_cli.py`

**任务**：

1. dry-run 读取现有 legacy config parser 的结构化结果，不打印配置全文、headers、endpoint query 或凭据。
2. inventory 为每个 legacy server 强制指定 disposition、`consumer_scope`、目标消费者安全摘要与 capability impact；apply 仅允许 `service_account_only + migrate_owner` 逐 server 指定服务账号 owner，不支持 system-owned/ACL 猜测。`multi_user|unknown` 自动阻止 assembly-off，除非显式 retire 并绑定用户影响验收。未知、重复、遗漏或缺 owner 全部拒绝。
3. 使用用户级 credential cipher 重新加密并绑定新 owner/server/security version；不得复制 legacy ciphertext 或环境变量原值到日志/evidence。
4. 生成新的 server ID，运行有界健康测试：每次至多 60 秒、最多两次，必须完成握手、发现、完整分页 `tools/list` 且至少一个 tool 通过合法性检查；凭据或任一步失败均不得写 mapping，并保持 inventory 未完成。
5. 保存 legacy source fingerprint -> new server ID 的安全 mapping/audit，供 shadow comparability 使用；不保存完整 Tool List/Schema/Client/Bundle。
6. 命令幂等：同一 source fingerprint + target owner 重跑给出 already migrated，不重复创建或覆盖凭据；生成可机读 inventory/consumer impact report，供 assembly-off 门禁验证 `retain_for_rollback == 0`、无 unresolved consumer，并证明每个原需保留 capability 对目标消费者仍可用或已有退役批准。

**退出门禁**：AC-10 通过；dry-run 证明 inventory 和 consumer/capability impact 全量覆盖，计划保留项已有 verified mapping。真实配置 apply/retire 仍需显式运维授权，开发代理只交付工具与测试。

### CP-5 — Cohort enforce 与单路径执行

**目标**：命中用户的新任务只走用户级 Gateway，未命中任务按受控规则走 legacy 或明确 unavailable；不允许 executor fallback。

**主要文件**：

- `src/integrations/mcp/rollout.py`
- `src/api/runtime.py`
- `src/orchestration/registry.py`
- `src/orchestration/llm_workflow_provider.py` 或当前 planner 能力过滤入口
- `src/orchestration/composite_executor.py`
- `src/capabilities/mcp_dispatch/executor.py`
- `src/capabilities/mcp_tool/executor.py`
- `src/api/routes/capabilities.py`
- `frontend/src/components/MCPRuntimeStatus.tsx`
- 新增 `tests/integrations/mcp/test_user_mcp_single_path_enforce.py`
- `tests/api/test_user_mcp_runtime_wiring.py`
- `tests/api/test_capabilities_list.py`
- `frontend/src/components/MCPRuntimeStatus.test.tsx`

**任务**：

1. 由 task route assignment 决定本次 registry/request context：`user_scoped` 只暴露 `mcp.dispatch`，`legacy` 只暴露旧动态 MCP descriptors，`unavailable` 两者都不暴露并提供安全状态。
2. Composite executor 在 task 级过滤另一条 MCP executor；异常不得从 `mcp.dispatch` fallback 到 `MCPToolExecutor`，反之亦然。
3. 用户自定义 Server 仅在 `user_scoped` 可见；off/rollback/未命中时前端得到明确可恢复不可用状态，不伪装成无工具。
4. 模式热变更只影响新 Task；在途 legacy/user-scoped/MRTR/remote task 均沿固化路径完成或取消。
5. 加入并发 spy tests：多用户、多任务、模式切换、network failure、unknown、取消，按业务请求 correlation 断言最多一次真实 `tools/call`。

**退出门禁**：AC-05/06/15/17 通过；D-1 cohort 文件、instance-config lease 和任务级 assignment 均通过跨实例/配置竞态测试。未取得 A 阶段生产 evidence 时不得进入任何 enforce。

### CP-6 — 五版本、容量、资源与回滚门禁

**目标**：建立进入真实 shadow/enforce 的完整自动化基线，并避免把旧 Sidecar promotion 与用户链路 promotion 混为一谈。

**主要文件**：

- `tests/fixtures/mcp/contracts/conformance_matrix.json`
- `tests/integrations/mcp/test_2026_07_28_adapter.py`
- `tests/integrations/mcp/test_protocol_version_negotiation.py`
- `tests/integrations/mcp/test_streamable_http_versions.py`
- `tests/integrations/mcp/test_2024_legacy_runtime_discovery.py`
- `tests/integrations/mcp/test_user_mcp_gateway.py`
- 新增 `tests/integrations/mcp/test_user_mcp_phase3_conformance.py`
- 新增 `tests/integrations/mcp/test_user_mcp_rollout_rollback.py`
- 新增 `tests/integrations/mcp/test_user_mcp_resource_baseline.py`
- `scripts/validate_user_mcp_phase3_evidence.py`

**任务**：

1. 把五版本 fixture 组合绑定到**用户级 Gateway**，覆盖 discovery/list/call/cancel/cleanup；2026 额外覆盖 MRTR/Tasks，旧版覆盖各自 lifecycle。
2. `test_2024_legacy_runtime_discovery.py` 继续在 legacy 存活阶段证明协议兼容；物理删除后拆成 Transport/Client conformance，不再要求 startup global registration。
3. 模拟大量仅配置未调用用户，比较 runtime memory/client/catalog count；排队任务断言不提前 decrypt/connect/list。
4. 覆盖完成/失败/拒绝/取消/断线 5 分钟/重启/shutdown/磁盘耗尽的 close 与临时文件 Janitor。
5. 执行 flag rollback、在途任务不迁移、用户自定义 unavailable、ordinary unknown 不重放；生成机器可读 drill evidence。

**退出门禁**：AC-12/15/16 通过；CI evidence validator 通过；Runbook 可用于内部 shadow，但尚不允许物理删除 legacy。

### CP-7 — 内部 Shadow -> 内部 Enforce -> 固定分组 -> 100% Enforce

**目标**：按 PRD 四档顺序运行真实观察，不在同一窗口连续跳档。

**这是运维检查点，不由本地实现代理自动执行。**

1. **A 内部 Shadow**：先完成 CP-4 inventory/mapping 准备，所有计划保留的 legacy Server 均有 verified mapping；legacy 真实执行，新链路 route/list/selector dry-run。连续 24 小时，HTTPS Streamable HTTP、HTTPS legacy HTTP+SSE、白名单 HTTP legacy HTTP+SSE、认证失败、超时、拒绝授权、大输出每个场景至少 3 个 `comparable expected-result match` 样本；负向场景以双方分别命中 manifest 预期安全错误为 matched，不要求远端调用成功。安全/双执行/泄漏/持续资源泄漏红线为 0，未解决 correctness mismatch/evidence invalid 为 0。只有已批准 `retire` 项可出现 `not_comparable`，且不计入场景样本（`03...md:151-158`）。
2. **B 内部 Enforce**：内部用户真实走新链路；连续 48 小时，取消、120 秒、5 分钟、重启 unknown、MRTR/Tasks recovery、公平排队与 flag rollback 各成功演练至少 1 次，安全红线为 0（`03...md:160-167`）。
3. **C 固定分组 Enforce**：逐比例扩大；每档同时满足连续 7 天和至少 1000 个 terminal 真实 user-scoped call，错误率不高于批准基线、p95 不超过基线 110%、安全红线为 0，且单独审批；用户自定义 Server 不走 legacy（`03...md:169-176`）。
4. **D1 100% Enforce、Legacy 可装配**：先创建最后 legacy tag、验证可部署 artifact 并完成 version rollback drill；随后 percent=100 且 legacy 仍可装配，独立满足 7 天/1000 样本门槛。
5. **D2 Legacy assembly-off、代码保留**：inventory 无 unresolved/retain 项，且所有原需保留 capability 对目标消费者仍可用或已有退役影响验收后关闭旧装配；再次独立满足 7 天/1000 样本、启动零 legacy 连接和资源基线门槛，才允许 CP-8 物理删除（`03...md:178-183`）。

**退出门禁**：D-3 至 D-6 所有门槛有 `source=production`、可审计且未重放的 evidence/approval；最后 legacy release tag/artifact/rollback drill 完整，D2 独立观察窗通过。

### CP-8 — Assembly-off 证据通过后物理删除旧 Runtime

**目标**：在生产证据通过后删除旧全局状态模型，不删除协议兼容实现。

**主要文件**：

- `src/api/runtime.py`
- `src/integrations/mcp/runtime_state.py`
- `src/capabilities/mcp_tool/executor.py`
- `src/capabilities/mcp_tool/workflow.py`
- `src/api/routes/capabilities.py`
- `src/orchestration/registry.py`
- 任务/历史/storage 相关模型与 repository
- `tests/api/test_mcp_runtime_registration.py`
- `tests/api/test_capabilities_list.py`
- `tests/integrations/test_mcp_runtime_state.py`
- `tests/orchestration/test_mcp_capability_registry.py`
- `tests/capabilities/mcp_tool/test_executor.py`
- `tests/integrations/mcp/test_2024_legacy_runtime_discovery.py`

**删除顺序**：

1. 验证当前部署已经处于 gateway on + enforce 100 + legacy off，且 CP-7 D2 evidence 证明启动不创建 legacy state/client、不发 startup `tools/list`；本检查点不再变更流量配置。
2. 移除 `_sync_mcp_capability_registry()`、conversation-start refresh 与 legacy descriptor/instance registration。
3. 移除新 Task 的 `mcp_bundle_revision` 写入、retain/release、resume/cancel revision 路径；旧历史字段只读忽略。
4. 从 CompositeExecutor 移除 legacy `MCPToolExecutor`，删除其 bundle revision binding 逻辑和仅服务于全局注册的 workflow/instance。
5. 删除 `MCPRuntimeState` 中 `_clients`、`_bundles`、`_active_revision`、global revision retention 与 refresh activation；若文件只剩无职责内容则删除文件。
6. 保留 `client.py`、`transport_http.py`、`transport_legacy_http_sse.py`、`adapter.py`、`adapter_2026.py`、Sidecar/protocol/security 模块，并让用户级 Gateway 继续使用它们。
7. 删除或改写以“startup 全局发现/注册”为正确行为的测试；协议 conformance 测试继续保留全部旧版本。
8. 保持最后 legacy tag、artifact digest、回滚演练和 evidence ledger 可用，直至既定版本回滚窗口关闭；不删除用户 Server、Grant、密文或新增 additive 表。

**退出门禁**：AC-13/14/17 通过；全量回归、资源基线、版本回滚演练再次通过；禁止在同一 commit 中提前删除回滚 tag/evidence 或用户数据。

### CP-9 — 文档、部署契约与最终收口

**目标**：让代码、运行配置、运维步骤和项目索引一致。CP-9 是贯穿式工作：CP-1 至 CP-6 每次合并同步对应契约，CP-7 前必须交付可执行 Runbook；CP-8 后只做最终状态收口，不允许把必需运维文档拖到删除之后。

**主要文件**：

- `docs/runbooks/user-mcp-gateway.md`
- `docs/prd/MCP/user-scoped-on-demand/03-按需MCP灰度切换与旧Runtime下线PRD.md`
- `docs/AGENTS.md`
- `src/integrations/AGENTS.md`
- `src/capabilities/AGENTS.md`
- `src/api/AGENTS.md`
- `tests/AGENTS.md`
- `scripts/AGENTS.md`
- `CHANGELOG.md`
- `docs/api/api-doc.html`（如 status/error/API contract 变化）
- deployment manifests/config templates；不得读取、覆盖、跟踪或输出 `docker_cmd.md`

**任务**：

1. Runbook 写清合法组合、启动 fail-closed、salt 一致性、四档放量、红线、证据收集、flag rollback、version rollback、密钥/新增表保留、用户自定义 unavailable。
2. 添加 cohort 文件 schema/权限/原子替换、instance-config readiness、evidence validator、operator approval/block CLI、实际观察字段和 tag/artifact/rollback drill checklist。
3. 同步阶段三 PRD：加入 D-1 至 D-6 已确认口径，并把“自动切回安全路径”改为“自动阻止继续扩大，授权运维执行只影响新任务的回滚”。
4. PRD 阶段状态与 checkbox 只按真实证据更新；不能因本地代码合并就标记 100% rollout/legacy removed。
5. 更新部署模板、相关目录索引和 CHANGELOG；确认没有新增依赖/许可变化。

**阶段门禁**：A shadow 前 Runbook/部署模板/evidence schema 必须覆盖 CP-1 至 CP-6；每次 promotion 前对应观察与回滚章节已审批。**最终退出门禁**：AC-18 通过；文档不包含 secret、原始配置、URL、用户名或生产标识。

## 8. TDD 与提交顺序

每个代码检查点遵循：先写失败测试 -> 最小实现 -> targeted tests -> 邻接回归 -> 静态检查 -> 清理本次产生的孤儿代码。推荐提交检查点：

1. `phase2 recovery prerequisite`
2. `rollout config and task assignment`
3. `side-effect-free shadow compare and telemetry`
4. `legacy migration dry-run/apply`
5. `single-path cohort enforce and rollback`
6. `phase3 conformance/resource evidence gates`
7. `disable legacy assembly`（D1 100% enforce evidence、tag/artifact/rollback drill 与 inventory 清零后）
8. `remove legacy runtime state and revision binding`
9. `docs/runbook and final evidence`（文档随各检查点增量提交，此处仅最终收口）

共享热点 `src/api/runtime.py`、`src/integrations/mcp/dispatch_coordinator.py`、storage interfaces/repositories 不应由多个并行 lane 同时编辑。开发依赖为 CP-1 -> CP-2/3 -> CP-4/5 -> CP-6；CP-0 可与 CP-1 至 CP-3 并行，但必须在 CP-5 激活 enforce 前完成。CP-9 文档同步贯穿所有检查点；运维与删除严格执行 CP-7 -> CP-8，再由 CP-9 做最终状态收口。CP-2 与 CP-3、CP-4 的测试准备可以并行，但集成必须串行。

## 9. 验证矩阵

### 9.1 每检查点 targeted tests

```bash
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_2026_07_28_adapter
conda run -n multi_agent python -m unittest tests.storage.test_mcp_phase2_repository
conda run -n multi_agent python -m unittest tests.api.test_user_mcp_runtime_wiring
conda run -n multi_agent python -m unittest tests.api.test_user_mcp_phase_boundary
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_user_mcp_gateway
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_protocol_version_negotiation
```

新增测试文件落地后按模块运行：

```bash
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_user_mcp_recovery_worker
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_user_mcp_rollout_config
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_user_mcp_shadow_compare
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_user_mcp_single_path_enforce
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_user_mcp_rollout_rollback
conda run -n multi_agent python -m unittest tests.integrations.mcp.test_user_mcp_phase3_conformance
conda run -n multi_agent python -m unittest tests.storage.test_rust_runtime_sidecar_contract
cargo test -p maf_runtime_store -p maf_runtime_sidecar
```

### 9.2 阶段开发完成回归

```bash
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_dispatch -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
```

如前端状态/错误契约有变化：

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

最终静态与 evidence 检查：

```bash
git diff --check
python scripts/validate_user_mcp_phase3_evidence.py --evidence <redacted-evidence-path>
rg -n "mcp_bundle_revision|prepare_refresh_sync\(reason=\"startup\"|_sync_mcp_capability_registry|MCPToolExecutor" src tests
```

CP-3 另有不可替代的真实 PostgreSQL 门禁：使用测试环境分别注入 app、snapshot producer、rollout operator、validator 与 CI 角色 DSN，运行 `tests.storage.test_user_mcp_rollout_postgres_integration`；测试必须创建独立事务与并发连接，并证明越权、伪造、重放和 activation/block 竞态失败关闭。若没有这些独立连接，CP-3 只能报告 unit contract 通过，不能进入 A shadow。

最后一个 `rg` 使用显式 allowlist：旧历史读取/兼容 fixture 可以保留，任何新写入、retain/release、startup discovery 或 legacy executor 装配都必须为零。

### 9.3 生产证据（不能由 CI 替代）

1. 每个放量档位的 environment/deployment/git SHA、开始/结束时间、config fingerprint、producer、单调 snapshot、nonce/digest、样本量、比较结果、SLO 和红线计数。
2. recovery/rollback drill、五版本受控真实样本、容量/资源基线、启动零连接证据；每项必须由 production source 记录并绑定对应审批。
3. 最后 legacy release tag、artifact digest/可部署证明、version rollback drill、inventory consumer/capability impact report、目标消费者能力可用性或退役验收、数据库向前兼容说明。
4. D1 100% enforce 与 D2 assembly-off 分别满足 7 天/1000 样本；CI、低流量、零分母、缺测或窗口断裂均只能延长观察，不能进入 CP-8。

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 阶段二恢复缺口被灰度掩盖，重启后重复副作用 | CP-0 硬前置；ordinary call unknown、MRTR 密封恢复、remote task query-only 分开测试。 |
| shadow 意外调用工具或改变授权 | shadow scope 接口不暴露 call；fake server 断言零 call；禁止复用允许 replay 的旧 helper。 |
| 同一任务因配置变化切换路径 | route assignment 与 Task 原子持久化；resume/worker 只读记录。 |
| 多实例稳定盐/cohort/config version 不一致 | 文件 digest 纳入 fingerprint；数据库 instance-config lease 对比，不一致实例不 Ready并阻止 promotion。 |
| cohort 文件被部分写入或越权修改 | 只在启动时读取运维原子替换文件，closed schema + `0440` 权限检查；Task 决定写后不可变。 |
| system-owned ACL 扩大所有权模型并产生越权 | 本阶段仅服务账号 owner；system-owned + ACL 必须单独 PRD。 |
| 把共享系统 Server 迁到单一 owner 后普通用户静默丢能力 | inventory 强制 consumer scope/capability impact；仅 service-account-only 可迁移，multi-user/unknown 在 ACL PRD 或可审计 retire 前阻止 assembly-off。 |
| 生产指标进程崩溃后丢失或 exporter 口径漂移 | PostgreSQL durable bucket 是唯一 promotion 权威；第三方 exporter 与进程内指标只作诊断。 |
| evidence 被 CI 冒充、重放或跨部署复用 | source/provenance/digest/nonce/单调 snapshot/唯一约束；审批绑定 deployment、stage 与 config fingerprint。 |
| Sidecar enforce 模式绕过 Python Task assignment 写入 | 扩展 SubmitTask proto/kernel/SQLite adapter，使 Task 与 assignment 同一幂等事务；shadow/enforce 和 proto hash 门禁覆盖。 |
| 多实例竞争消费一次性 approval | approval 与 activation 分离；单事务生成唯一 deployment activation，实例只读 activation 并登记 lease。 |
| 指标标签或审计泄漏身份/参数 | 固定 label/field allowlist + secret/high-cardinality negative tests。 |
| rollback 把用户 Server 静默切到 global | custom server 明确 unavailable；只有受控 mapping 的 system MCP 可 legacy。 |
| 过早物理删除 legacy 失去回滚路径 | CP-7 真实 evidence、tag 和版本回滚演练是 CP-8 硬门禁。 |
| 删除 Runtime 时误删协议兼容 | 按状态/注册职责删除；Transport/Client/Adapter/Sidecar conformance 在删除前后都运行。 |
| 本地合成 evidence 被误标生产通过 | validator 区分 evidence environment/source；生产观察字段不能由 test fixture 满足。 |

## 11. 停止条件与交付口径

1. CP-0 失败时禁止任何 enforce 激活；CP-1 至 CP-3 的默认关闭开发和 A 内部 shadow 可以继续，直到依赖恢复。
2. 任一安全红线非零、SLO 不达标、低流量、零分母、缺测或窗口断裂时自动写 promotion block 并停止扩大；系统不自动改写路由，授权运维按 Runbook 降低新任务暴露面，在途任务保持原路径。
3. cohort 文件/权限/schema/fingerprint 或 instance-config lease 不一致时实例不 Ready，禁止新增档位审批。
4. inventory 未全量归类、仍有 `retain_for_rollback`、健康检查未通过，或最后 legacy tag/artifact/rollback drill 缺失时，禁止 assembly-off。
5. D2 assembly-off 独立 7 天/1000 样本 evidence 未通过时，禁止 CP-8 物理删除。
6. 完成 CP-0 至 CP-6 只能报告“阶段三开发就绪 / 可进入内部 shadow”；只有 CP-7 至 CP-9 全部有真实证据，才能把阶段三 PRD 标记完成。

## 12. 后续执行建议

这是跨 API runtime、storage、MCP protocol、lifecycle、frontend、observability 和 ops 的长周期工作。批准计划后，建议用 `$ultragoal` 持有 CP-0 至 CP-9 的 durable ledger；CP-0/CP-1 等共享热点由单一 owner 串行收口，独立的测试、observability、migration CLI、frontend status 与 docs lane 可用 `$team` 并行。当前 Codex App 不直接启动 OMX tmux team；需要并行执行时可使用 Codex native subagents，或在附着 tmux 的 OMX CLI 中启动 `$team`。
