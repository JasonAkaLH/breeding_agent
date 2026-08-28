# 统一 Agent Loop Skill Soft Binding 回归实施计划

依据：`2026-08-27-unified-agent-loop-skill-soft-binding-design.md`

设计基线提交：`81a8ec1`

计划日期：2026-08-28

状态：`in_progress`（Checkpoint A～D 已完成；Checkpoint E～H、镜像重建与部署尚未完成）

目标分支：`main`

## 1. 完成声明

本计划的唯一目标是把已批准 design 中的 Skill 单消息 soft binding 和 Capability 结果投影合同落实到当前统一 Agent Loop，同时保持 MCP `$Server` 显式绑定、现有 Skill runtime mode、旧 Skill 业务文件 Artifact 和 `prod` 不变。

只有以下事实同时成立，才可把功能状态改为 `complete`：

- Skill picker 与 `/skill-name` 显式提交 `routing_mode=hint + capability_id=skill.*`；普通消息提交 `auto + null`，MCP `$Server` 继续提交 `force_capability + mcp.dispatch`。
- `hint` 的 public/enabled/pinned Skill authority 在 Message、Task、附件、AgentRun 和 submission-scoped audit 副作用前完成校验；不可用目标统一低敏返回 HTTP 409 `skill_hint_unavailable`。
- 所有新 submission 都写 `maf.submission.prepared_execution.v2`；部署前 v1 只按原 exact keys 和 digest domain 读取，未知或混合版本 fail closed。
- hint 的 canonical activation 从 prepared v2 原子初始化为 `user_message + skill_activation` 两个 AgentItem；SQLite、PostgreSQL 和 Runtime Sidecar 三条 repository 路径保持 all-or-zero、CAS 和 exact replay 等价。
- hint 在初始化、普通执行、waiting resume 和 startup recovery 中始终使用 auto Tool choice；只有原始 `force_capability` Task 使用 required Tool。
- hint profile 在当前 user message 前进入首次模型上下文，但 raw `SKILL.md` 正文不进入首轮；informational 消息直接回答且零 Skill 调用，execution 消息才调用 Tool。
- contract-v2 PublicSkillProfile 包含允许公开的字段级输入、约束、默认值、示例和输出合同摘要，不泄漏 source policy、pattern、runtime、handler、脚本、内部路径、配置或 secret。
- delegated Skill 复用已有 activation；只有模型实际调用后，统一 `maf.agent.model_result.v1` 的 `model_view` 才携带 pinned、完整且有界的 instruction body。
- 任一 Capability outcome 在 repository 前经过唯一 `AgentCallResultProjector`；不再执行 `dict(output_payload)` 到 `safe_result` 的无界直通。
- 普通 Skill 的小结果形成 `inline` 投影；超预算 strict-JSON 结果以确定性 `skill_result.json` staged file + Agent outcome CAS 原子发布，模型只接收不超过 20,000 code points / 80,000 UTF-8 bytes 的视图。
- `skill_result` 只在 CAS 后可由 owner 枚举和下载，不参与 `skill_output` 的 conversation-wide supersede；CAS loser、exact replay 和 24 小时 janitor 不删除 winner/recoverable 文件。
- projector、staging 或最终 envelope 失败均提交 typed failed Tool outcome，并让 terminal Node/result 同一 CAS 收敛；不出现 Node terminal + result reserved、`AgentPayloadError`、通用 `execution_crash` 或长期 reserved 残留。
- 自动化门禁、真实 PostgreSQL、Runtime Sidecar/Rust、Frontend 和本地成对 UI/API smoke 全部有可复核证据；`prod` 未修改或部署。

## 2. 固定范围与非目标

### 2.1 本期允许修改的业务边界

- Frontend Skill picker、Slash intent、API client 与相应测试。
- `SubmitMessageRequest`、chat-message route 错误映射、`ApiRuntime.submit_message` admission/prepared/recovery wiring。
- PublicSkillProfile、skill activation codec、Agent context、Tool choice、delegated instruction activation。
- prepared execution v2 Python/SQLite/Runtime Sidecar/Rust 双读和 v2-only writer。
- PendingSkillContext 历史兼容的 exact consumed/superseded transition；不新增 pending writer。
- 三条 Agent repository 的 user + hint activation 原子初始化。
- Capability result projector、Skill result stager、Agent outcome CAS、terminal event reconciliation、Artifact API 与 janitor。
- audit/metrics、API 文档、索引与 CHANGELOG。

### 2.2 明确不做

- 不恢复 `main_agent.respond`、Soft Skill Decision LLM、Replanner、WorkflowPlan 或 DAG finalizer。
- 不改变 MCP `$Server` binding DTO、Router、Selector、授权、恢复或 raw result 公共安全边界。
- 不改 `bioinfo-daily`、`germplasm-mcp` 或其他 Skill 的脚本、schema、检索策略和输出质量。
- 不新增数据库表、列、physical migration 或 protobuf 字段；prepared v2 继续使用现有 opaque canonical bytes 字段。
- 不把 `SkillOutputArtifactManager.process*` 当作 result staging；不重构其随机 identity、提前 metadata 保存或 supersede 生命周期。
- 不新增跨 Tool 聚合预算、通用 Artifact 读取 Tool、Skill force UI、立即执行按钮或新的命令语法。
- 不新增第三方依赖，不修改部署到 `prod`。

## 3. 当前 HEAD 证据与精确改造缝

计划启动前已在 `main@81a8ec1` 核对以下事实；实施当天仍须重新检查，不能依赖本计划中的行号：

| 当前缝 | 当前行为 | 本计划中的目标 |
|---|---|---|
| `frontend/src/api/client.ts::submitMessage` | 只要 capability ID 非空就推导 `force_capability` | `routingMode` 由调用方显式提供；client 不推导 |
| `frontend/src/domain/slashCommands.ts` | intent 和 metadata 使用 `forced_by_slash_command` | intent 固定 `hint`，移除 forced authority metadata |
| `src/api/dto.py::SubmitMessageRequest` | 只闭合 MCP binding 组合，未闭合 hint | DTO 负责 routing shape 422；runtime 负责 target availability 409 |
| `src/api/runtime.py::submit_message` | 非 force、无 capability 时可读取 pending；任意 `skill.*` 都设置 defer | 只允许 auto continuation；hint supersede；defer 只对真正 auto continuation 为 true |
| `src/api/submission_admission.py` | 新 writer 和 validator 固定 prepared v1；任意 requested capability 派生 required Tool | schema-first v1/v2 decoder；新 writer 只写 v2；required 只由 routing mode 决定 |
| `src/integrations/agent_skills/public_profile.py` | contract-v2 schema summary 只有标题/描述/aliases；outputs 只有 ID | 从 pinned contract/schema 构建字段级 allowlist 与确定性 output 摘要 |
| `src/orchestration/agent_loop/skill_activation.py` | activation helper 只服务 delegated outcome，payload 无 `binding_mode` | 抽出共享 canonical activation payload/item builder；hint 与 delegated 复用 |
| `AgentAtomicWriter.commit_agent_user_message` | 原子写入一条 user item | 扩展同一初始化 writer，一次提交 user + 可选 hint activation |
| `AgentContextBuilder` | 严格按 sequence 渲染，activation 位于 user 后 | 仅 `binding_mode=hint` 在对应当前 user 前渲染；delegated 保持时序 |
| `AgentLoopOrchestrator.run_initialized` | requested capability 非空即 required | 使用唯一 routing-mode helper；hint/auto 均 auto |
| `AgentCapabilityInvoker` | `safe_result_payload = dict(result.output_payload)` | 调唯一 projector；typed failure 收敛 |
| `AgentTaskInvocationCommitPort` | projector 前先持久化 terminal Node/event | completed/failed/route-rejected 只返回 terminal candidate；Agent outcome CAS 是唯一 terminal writer |
| 三条 Agent repository | outcome CAS 已能同事务提交 result、Node、Artifact refs 和 Run revision | 复用并加 `skill_result` closed metadata、exact replay 和 fault injection |
| `LocalArtifactFileStore` / Artifact routes | active managed output 仅 `skill_output`/`mcp_result`；下载只允许 `skill_output`，现有 route 未按 metadata 复验 regular file、size/SHA | 加 closed `skill_result` allowlist、deterministic bytes staging、owner 校验与基础文件完整性验证 |
| `ApiRuntime.start` | Agent recovery 先于现有 MCP janitor | 在 Agent recovery 后运行 staged Skill result janitor，并测试顺序 |

## 4. 实施策略选择

考虑过三种执行顺序：

1. **Authority-first，按持久化边界横向闭合（采用）**：先 profile/activation、prepared v2，再连续完成三 repository 初始化/恢复、context/Tool choice、hint admission 和 pending transition；随后接 projector、Artifact/CAS，最后切前端。Checkpoint C、D 是同一开发阶段，D 完成前不得发布或切换前端。
2. **Frontend 到后端的纵向最小切片**：先让 picker hint 跑通 informational case，再补 recovery 和大结果。初期演示快，但会产生只能在正常路径工作、崩溃后可能升级为 force 或丢 activation 的在途 Task，不接受。
3. **先修大结果 crash，再恢复 soft binding**：能优先消除 `AgentPayloadError`，但会延后用户最直接的“询问却执行”回归，且 projector 仍需随后与 hint/delegated activation 重接一次，不采用。

所有 checkpoint commit 仅作为开发回滚点；只有 A～H 全部 green、前后端/Sidecar 成对构建并完成 Final Gate 后才形成可发布单位。

## 5. Checkpoint 总览

| Checkpoint | 主题 | 主要完成证据 |
|---|---|---|
| 0 | 基线、红测清单与不变量冻结 | 当前 forced/required/oversize 故障可重复；MCP 与 legacy Artifact 基线通过 |
| A | Public profile 与 canonical activation | 字段级 profile、两层大小边界、无 raw body/内部字段 |
| B | prepared execution v2 双读与 v2-only writer | Python/SQLite/Sidecar/Rust exact keys/domain/cross-version 门禁 |
| C | 三 repository 原子初始化、context 与 Tool choice | user+activation all-or-zero；prepared recovery逐字节一致；hint全入口auto |
| D | API hint admission、PendingSkillContext exact transition 与 delegated | 422/409零submission副作用、transition receipt、HTTP 202闭合、instruction只在调用后出现 |
| E | 唯一纯 `AgentCallResultProjector` | inline/MCP/delegated 投影、双预算、strict JSON、deterministic bytes |
| F | `skill_result` staging、Agent CAS、terminal reconciliation 与 janitor | CAS 前不可见、CAS 后 owner-only、fault/replay/orphan 全闭合 |
| G | Frontend soft-binding 切换 | picker/Slash hint，MCP force 不变，选择一次性清除 |
| H | 审计、文档、全量门禁、真实 smoke 与发布/回滚证据 | 17 项完成条件逐项有证据；`prod` 未变 |

### 5.1 非功能要求与证据合同

| NFR | 固定要求 | 自动证据 |
|---|---|---|
| 模型调用 | 不增加独立 decision LLM、`main_agent.respond`、Replanner 或 DAG 阶段；所有模型调用仍是同一 AgentRun 的普通 sample/retry | fake model recorder 断言 decision-call=0；informational case无Tool时只出现既有Agent sample类别；静态扫描退役入口零生产引用 |
| I/O 与资源 | inline projector 为纯 CPU/内存且零文件/网络 I/O；artifact-backed 每个 call 只持久化一份 canonical raw、一个小型manifest和既有数据库metadata，不复制第二份raw | spy断言 inline零stager调用；28-record/约285 KiB fixture断言raw文件数量=1、SHA/size精确、safe result双预算成立 |
| 安全与隐私 | profile/instruction/raw/model view、绝对路径、storage key、credential 不进入公共Message、普通history、memory、audit正文或日志 | leak scan + cross-owner/guess-key/non-regular-file/size-SHA drift下载测试 |
| 可靠性 | 任一pre-CAS失败必须终态化；CAS结果未知时只能exact-winner确认或startup no-replay收敛；janitor fail-safe保留 | fault matrix、response-lost replay、startup recovery-before-janitor测试 |
| 兼容性 | v1只读、v2-only新写；MCP wire/DTO/raw安全边界、`skill_output`生命周期、三Skill runtime mode均不变 | Python/Rust cross-version vectors、MCP/Skill完整回归、static contract扫描 |
| 可访问性 | picker/Slash文案变化不得破坏键盘选择、Escape关闭、焦点、badge取消按钮aria-label、busy/Interrupt gate | 现有App/Slash a11y与键盘测试保留并改写为hint语义 |
| 可观测性 | 新事件低基数且不含正文；执行与否只由同一Agent Tool call/Node/Skill事实证明 | audit payload exact-key tests、指标closed-label tests、事件数量/幂等测试 |

## 6. Checkpoint 0：基线与红测清单冻结

### 0.1 启动检查

实施前执行只读检查：

```bash
git status --short --branch
git log -5 --oneline --decorate
test -f docker_cmd.md
git check-ignore docker_cmd.md
git ls-files --error-unmatch docker_cmd.md
```

最后一条必须失败，证明 `docker_cmd.md` 未被跟踪；不得读取文件内容。若工作树已有与本计划目标文件重叠的用户修改，先审阅并保留，不能覆盖。

确认：

- 分支为 `main`，目标是本地开发环境，不是 `prod`。
- 外部只读 Skill bundle 能提供 automated fixture；真实 smoke 前再确认 `bioinfo-daily` 和 `germplasm-mcp` 可用。
- SQLite 默认回归可运行；真实 PostgreSQL DSN、Rust toolchain 和 Frontend Node 依赖是否可用单独记录。

### 0.2 先保存当前 green 基线

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_message_submission \
  tests.api.test_submission_admission_recovery \
  tests.api.test_submission_admission_runtime_startup \
  tests.orchestration.test_agent_loop \
  tests.orchestration.test_agent_invocation \
  tests.orchestration.test_agent_context_builder \
  tests.orchestration.test_agent_skill_activation \
  tests.storage.test_agent_storage_conformance \
  tests.storage.test_submission_admission_sqlite \
  tests.integrations.agent_skills.test_public_skill_profile

cd frontend
npm test -- --run src/domain/slashCommands.test.ts src/api/client.test.ts src/App.test.tsx
```

### 0.3 固定旧实现应失败的新断言

每个后续 checkpoint 先写本节对应红测。最低红测清单：

- picker/Slash 期望 `hint`，旧 client 仍发 force。
- hint informational sample 期望 auto/零调用，旧 orchestrator 仍 required。
- prepared v2 exact keys/domain，旧 writer 仍 v1。
- user + hint activation fault injection all-or-zero，旧 writer 只能写 user。
- contract-v2 field/default/constraint 摘要，旧 profile 不含字段。
- 285 KiB duplicate-articles Skill 输出，旧 invoker 在 Agent canonicalize 时失败。
- terminal projection fault 后 Node/result 同步 failed，旧 commit port 会留下 terminal Node + reserved result。

Checkpoint 0 不提交生产代码；若当前基线已经与本表不符，先更新证据地图和本计划，不盲目套用。

## 7. Checkpoint A：PublicSkillProfile 与 canonical activation

### A1. 先写 profile 红测

修改 `tests/integrations/agent_skills/test_public_skill_profile.py`，覆盖：

- 从 pinned contract 引用的每个 input schema 解析 `expose=true` 字段。
- 只投影 `name/title/description/type/required/required_when/aliases/default/enum/const/question/clarification.examples`、允许的 validation 和 file-selection 摘要。
- `expose=false`、source policy、regex/pattern、slot policy、entrypoint mapping、reference path 和内部 handler/runtime 完全不存在。
- schema constraints 只允许规范化 `any_of/one_of/mutually_exclusive/字段依赖`；未知 shape fail closed，不原样复制。
- outputs 按 output ID 排序，投影 `output_id/required_fields/artifacts.extensions/artifacts.mime_types`；不复制 handler 样例或 storage 信息。
- strict-JSON default/enum/const 通过敏感 key/text sanitizer；非 JSON、禁止字段或无法解析的 pinned schema 失败，不回退空摘要。
- 同一 pinned bundle 重复构建逐字节相同；active bundle 切换不改变已经构建的 profile。

### A2. 抽出共享 activation codec

修改：

- `src/integrations/agent_skills/public_profile.py`
- `src/orchestration/agent_loop/skill_activation.py`
- `src/orchestration/agent_loop/__init__.py`
- `tests/orchestration/test_agent_skill_activation.py`

实现原则：

- 在 `skill_activation.py` 增加唯一纯 builder，输入只能是 `binding_mode`、安全 profile、pinned revision 和 expected resolved revision。
- canonical payload exact keys 为 `binding_mode/pinned_bundle_revision/profile/profile_digest`；hint 固定 `binding_mode=hint`。
- `profile_digest` 只对 canonical profile bytes 计算；activation payload 再走 `canonicalize_agent_payload`，完整外壳受 131,072 bytes 限制。
- admission 可在没有 Run ID 时先构建 canonical payload；初始化时再用 run/capability/revision 派生 deterministic AgentItem ID，payload bytes 不重建、不改写。
- 现有 `DelegatedSkillActivationService` 改为复用 builder；不保留第二套 profile sanitizer。
- hint builder 不接受 `SkillManifest.body`，测试断言 raw `SKILL.md` 片段不在 payload。
- 131071/131072/131073 bytes 测试必须计算完整 activation 外壳，而不是只填充 profile body。

### A3. Green 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.agent_skills.test_public_skill_profile \
  tests.orchestration.test_agent_skill_activation

conda run -n multi_agent ruff check \
  src/integrations/agent_skills/public_profile.py \
  src/orchestration/agent_loop/skill_activation.py \
  tests/integrations/agent_skills/test_public_skill_profile.py \
  tests/orchestration/test_agent_skill_activation.py

git diff --check
```

检查点提交建议：

```text
feat(agent): build pinned skill hint activation
```

## 8. Checkpoint B：prepared execution v2 双读与 v2-only writer

### B1. 先写版本合同红测

修改/新增测试：

- `tests/api/test_submission_admission_recovery.py`
- `tests/api/test_submission_admission_runtime_startup.py`
- `tests/api/test_submission_admission_request_builder.py`
- `tests/storage/test_submission_admission_sqlite.py`
- `tests/storage/test_runtime_sidecar_submission_migration.py`
- `tests/api/test_runtime_sidecar_contract.py`
- `tests/storage/test_rust_runtime_sidecar_contract.py`
- `native/crates/maf_runtime_sidecar/tests/runtime_sidecar_kernel.rs`
- `native/crates/maf_runtime_sidecar/tests/runtime_sidecar_sqlite.rs`
- `native/crates/maf_runtime_sidecar/tests/runtime_sidecar_grpc.rs`

红测矩阵：

- v1 只接受当前源码冻结的原 21 个 exact keys、原 v1 domain 和原关系；测试从权威legacy fixture逐项断言key set，禁止只用手写计数替代。新增 `routing_mode/skill_activation` 任一字段都拒绝。
- v2 必须在 v1 keys 基础上准确新增 `routing_mode/skill_activation`，digest domain 固定 `maf.submission.prepared_execution.v2\0`。
- decoder 必须先解析 canonical JSON 的 `schema`，再选 exact keys/domain；未知 schema、v1 bytes + v2 digest、v2 bytes + v1 digest 全部拒绝。
- v2 `hint` 必须是同一 `skill.*` capability、activation exact two-key wrapper、activation identity一致、`initial_required_tool_name=null`。
- v2 `auto` 必须 activation null、required null；普通 public auto 的 requested capability 为 null，只有 server-derived legacy pending continuation 可携带与 `pending_context.capability_id` 完全一致的 requested capability，仍不派生 required Tool。
- v2 `force_capability` 必须 capability 非空、activation null、required Tool 确定性派生。
- MCP binding 仍只允许 `force_capability + mcp.dispatch + activation=null`。
- `interrupt/no_server_intent` 只能改变 prepared kind 和既有可变选择结果，不能改写 routing/activation authority。
- 所有新 auto/hint/force/MCP submission fixture 断言 v2；只有明确命名 `legacy_v1_*` 的预置 fixture 可以写 v1。
- 完整 prepared envelope 131071/131072 bytes 接受、131073 bytes 拒绝。

### B2. 实现 schema-first 双读

修改：

- `src/api/submission_admission.py`
- `src/storage/runtime_sidecar_facade.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`（只调整 opaque bytes 的验证/调用，不改 physical schema）
- `native/crates/maf_runtime_sidecar/src/lib.rs`
- `native/crates/maf_runtime_sidecar/src/sqlite_adapter.rs`（若其本地验证重复了 lib contract）

具体步骤：

1. 分离 v1/v2 schema 常量、exact key set、digest domain 和关系 validator。
2. `_validated_prepared_content` 先做 strict canonical parse 和 schema 选择，再计算对应 domain digest；禁止“先验 v1 digest，再猜版本”。
3. `_build_prepared_snapshot` 统一输出 v2，并显式携带 continuation 中已固定的 `routing_mode` 和 `skill_activation`。
4. `PreparedAgentRecoveryContext` 增加 routing mode、canonical activation payload bytes/text 与 digest；v1 loader 返回 `activation=None`，不补字段、不重写。
5. Sidecar facade 和 Rust validator 使用相同 exact relations；protobuf/数据库仍传 opaque bytes，不加字段。
6. `_initial_required_tool_name` 改为输入 routing mode + requested capability 的纯 helper；只有 force 返回 provider-safe name。
7. 任何 cross-version 或关系错误使用闭合内部 code，不把 profile body、路径或 digest 上下文返回用户。

### B3. Green 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_submission_admission_recovery \
  tests.api.test_submission_admission_runtime_startup \
  tests.api.test_submission_admission_request_builder \
  tests.storage.test_submission_admission_sqlite \
  tests.storage.test_runtime_sidecar_submission_migration \
  tests.api.test_runtime_sidecar_contract \
  tests.storage.test_rust_runtime_sidecar_contract

cd native
cargo fmt --check
cargo test -p maf_runtime_sidecar --tests
```

检查点提交建议：

```text
feat(submission): version prepared agent execution authority
```

## 9. Checkpoint C：原子初始化、恢复、Context 与 Tool Choice

Checkpoint C 只建立内部 durable foundation，测试用受信 prepared fixture 驱动初始化；Checkpoint D 随后接入 public admission。C、D 作为一个不可拆分发布的开发阶段，只有 D 的 HTTP handoff 门禁全部通过后才允许切换前端或形成发布候选。

### C1. 三 repository 原子初始化红测

修改：

- `src/orchestration/agent_loop/models.py`
- `src/orchestration/agent_loop/repository.py`
- `src/storage/sqlite/agent_repository.py`
- `src/storage/postgres/agent_repository.py`
- `src/storage/runtime_sidecar_agent_repository.py`
- `tests/storage/test_agent_storage_conformance.py`
- `tests/storage/test_agent_storage_sqlite.py`
- `tests/storage/test_agent_storage_postgres_integration.py`
- `tests/storage/test_runtime_sidecar_agent_repository.py`

扩展现有 `commit_agent_user_message` 初始化合同，不新建第二个 writer：

- `AgentUserMessageCommit` 增加可选、已 canonicalize 的 hint activation payload/sha；repository 用共享 deterministic helper 在同一事务中派生 activation item，避免 caller wall-clock timestamp 进入 replay identity。result 返回 user item 和可选 activation item。
- 无 hint 仍只提交 sequence 1 user；hint 原子提交 sequence 1 user + sequence 2 activation，并一次推进 revision/next sequence。
- activation 必须与 run/task/capability/revision/digest exact 一致，binding mode 必须是 hint。
- user 与 activation 使用同一 transaction timestamp；item ID 只由 run/capability/pinned revision 派生，不含 wall clock。
- exact replay 必须同时复验 user 和期望的 activation presence/digest，返回相同两项且不增加 sequence/revision；只有 user、只有 activation、payload drift 或 item identity drift 均 fail closed。
- 在 user write 后、activation write 后、run update 前后注入 fault，三 repository 都必须 all-or-zero。
- Runtime Sidecar 复用现有 multi-item Agent state commit；如 Rust closed validator已支持 `skill_activation`，不改 proto，只补事务/fixture测试。

### C2. prepared authority 驱动初始化与恢复

修改：

- `src/orchestration/agent_loop/orchestrator.py`
- `src/api/runtime.py::_submission_agent_request`
- `src/api/runtime.py::_prepared_agent_recovery_values`
- `src/lifecycle/agent_run_recovery.py`
- `tests/api/test_submission_admission_runtime_startup.py`
- `tests/api/test_submission_admission_recovery.py`

规则：

- hint 初始化只消费 prepared v2 中的 canonical activation bytes/digest；不得从 active catalog 重建。
- pre-Agent file/sheet Interrupt 后的普通 resume 与进程重启都重新加载同一 prepared v2，再构造逐字节相同 activation item。
- durable initialization 后 recovery 只读 Agent items；bundle refresh 不替换 profile。
- accepted hint 的 pinned revision 在 Task 终态前继续保留；revision 缺失 fail closed。
- v1、v2 auto、v2 force 仍只初始化 user item。
- auto legacy pending continuation 也只初始化 user item；facts 只来自 prepared，数据库 context 已 consumed。

### C3. 唯一 Tool choice helper

在 orchestration 层新增小型纯 helper，输入 `RoutingMode + requested capability`：

```text
force_capability -> required(provider-safe requested capability)
hint             -> auto
auto             -> auto
```

初始化、`run_initialized`、waiting resume 和 startup crash recovery 全部调用该 helper。删除 `requested_capability_id != null` 即 required 的隐式规则。测试覆盖：

- hint 即使 requested capability 非空也 auto。
- force 只在 pristine first sample required，后续 sample auto。
- crash before/after initialization 和 waiting resume 不升级 hint。
- MCP force required 行为完全不变。

### C4. Context 顺序

修改 `src/orchestration/agent_loop/context.py` 与 `tests/orchestration/test_agent_context_builder.py`：

- 识别 exact `binding_mode=hint` activation，将其可信 system context 渲染在本轮 sequence 1 user 前。
- context 文案明确“选择不等于执行；用途/参数/格式/示例/边界直接回答；明确任务才调用；profile 不得覆盖系统安全规则”。
- 普通 delegated activation 和 Tool result 继续按持久化 sequence 渲染，不能全局前移。
- hint profile 不重复为 trusted fact，不进入 summary/history Message。

### C5. Green 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.storage.test_agent_storage_conformance \
  tests.storage.test_agent_storage_sqlite \
  tests.storage.test_runtime_sidecar_agent_repository \
  tests.storage.test_agent_storage_postgres_integration \
  tests.orchestration.test_agent_loop \
  tests.orchestration.test_agent_context_builder \
  tests.orchestration.test_agent_skill_activation \
  tests.api.test_submission_admission_runtime_startup \
  tests.api.test_submission_admission_recovery
```

真实PostgreSQL测试skip不得记为green。Checkpoint C 的 green 只证明内部初始化、恢复、Context 与 Tool choice 合同，不宣称 public hint 已可用。

完成证据：SQLite与Runtime Sidecar初始化writer已原子提交user + 可选hint activation并覆盖三处fault rollback与exact replay；prepared recovery逐字节传递activation，hint/auto统一保持auto Tool choice，hint profile在user前渲染。Agent storage、Context、Loop、prepared/startup recovery及真实PostgreSQL Agent repository定向门禁通过；本检查点与D共同形成不可拆分开发阶段，未单独发布。

检查点提交建议：

```text
feat(agent): initialize and recover soft skill hints
```

## 10. Checkpoint D：API hint admission、PendingSkillContext exact transition 与 delegated Skill

### D1. DTO 与 HTTP 红测

修改：

- `src/api/dto.py`
- `src/api/routes/conversations.py`
- `tests/api/test_message_submission.py`
- `tests/api/test_mcp_server_explicit_agent_loop.py`

先断言：

- `hint` 缺 capability、auto + capability、force 缺 capability 返回422，零submission副作用。
- hint 不允许用户提交profile、bundle revision、profile digest、activation或其他system identity metadata。
- private/disabled/missing/non-Skill target、alias指向不可用target、pinned revision不可用返回409 `{"code":"skill_hint_unavailable"}`，不区分私有capability是否存在。
- 合法alias被canonicalize；Task和prepared只保存canonical capability，audit可保存安全alias摘要但不作为authority。
- MCP DTO的精确组合、错误码和metadata allowlist原样通过。

### D2. admission、active registry 与零submission副作用

修改 `src/api/runtime.py::submit_message`，顺序固定为：

1. routing shape 由 DTO/Pydantic 在进入 runtime 前闭合；结构错误返回422。
2. conversation owner/幂等身份确认后，沿用现有catalog refresh并读取本次请求看到的active public Capability Registry。
3. 从该registry校验存在/public/enabled/`skill.*`并canonicalize alias；非Skill、private、disabled、missing target统一返回低敏409，随后固定active bundle revision。
4. 从固定bundle取manifest/descriptor/contract/input schema，调用Checkpoint A唯一builder得到canonical activation。
5. 分别验证完整activation与完整prepared v2的131,072-byte边界；hint prepared必须含同一payload/sha且`initial_required_tool_name=null`。
6. 完成附件/Sheet和model option预校验后，才允许Message/Task/附件绑定、pending transition、Agent初始化及submission-scoped audit。

这里的“零submission副作用”精确定义为：失败hint不得新增/修改Message、Task、TaskInputAttachment、PendingSkillContext状态、AgentRun/AgentItem、submission receipt/handoff或`skill.hint_bound`等请求级事件。沿用既有catalog refresh行为不属于本功能的submission authority，不能把它的调整扩入本计划。

定义窄 `SkillHintUnavailableError`，chat-message route只映射为低敏409 code。结构错误仍由Pydantic 422处理，不能回退成当前通用400。

### D3. 无新列的 exact pending transition receipt

修改：

- `src/core/contracts.py` 中PendingSkillContext/Event窄storage port
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- `src/api/runtime.py` submission materialization callback
- `tests/storage/test_sqlite_pending_skill_context.py`
- `tests/storage/test_submission_admission_postgres_integration.py`
- `tests/api/test_pending_skill_context.py`
- `tests/api/test_submission_preparation_callbacks.py`

不增加表或列；在PendingSkillContext同一SQL事务内写一条deterministic audit-only EventRecord作为transition receipt。receipt exact payload固定为：

```json
{
  "schema": "maf.pending_skill_context.transition_receipt.v1",
  "task_id": "task-...",
  "conversation_id": "conv-...",
  "prepared_execution_sha256": "...",
  "context_ids_sha256": "...",
  "target_status": "consumed|superseded",
  "reason": "legacy_pending_continued|new_skill_hint|new_forced_capability|new_mcp_binding",
  "occurred_at": "canonical timestamp",
  "count": 0
}
```

实现规则：

- event ID由domain、Task ID、prepared SHA、target status确定性派生；receipt不含旧用户正文、missing values、capability或raw context ID，只保存排序context ID列表的canonical SHA。
- storage事务先锁conversation owner和候选pending rows，再根据prepared SHA、routing mode和prepared `pending_context`生成目标集合。
- auto continuation要求prepared中有且只有一个context ID；数据库出现多个active context、ID/status不一致或prepared facts不匹配时fail closed。
- hint/force/MCP的prepared `pending_context=null`；事务选择全部active rows。即使count=0也写receipt，证明该Task观察到的闭合空集合。
- 同一事务完成`pending_user_input -> consumed|superseded`和receipt insert；首次提交后再由runtime best-effort发布已持久化audit event，不另写第二条authority。
- exact replay先读取同event ID，重算当前`target_status + occurred_at`行集合digest并逐字段比较receipt；完全一致返回同一count，不一致fail closed。
- 不同Task、prepared SHA、target、reason、时间或context集合不能借相同status/updated_at冒充replay。
- `defer_task_completed_until_pending_skill_context_processed`仅auto pending continuation为true；hint/force/MCP/普通auto均为null。
- production Agent Loop路径不再调用`save_pending_skill_context`；用spy/static regression证明没有新writer。

`skill.hint_bound`只记录capability ID、安全revision引用和profile digest。transition receipt/audit只记录上面的safe identity/count；不记录旧正文。

### D4. delegated Skill 调用后指令激活

修改 runtime 中 `activate_delegated_skill` wiring，或将其收敛到 `skill_activation.py` 的窄 service：

- 调用前读取 run items；capability/revision/profile digest 与既有 hint activation完全一致时复用，不追加第二 activation。
- instruction body 只取 pinned bundle 中已经解析、去 frontmatter 的 `manifest.body`；不读取 active bundle补齐。
- body 必须完整且不超过 20,000 code points；统一 safe result 不超过 80,000 bytes，完整 Tool result 不超过 131,072 bytes。
- 顶层继续使用 `maf.agent.model_result.v1`；`projection_revision=delegated-skill-instruction-v1`、`projection_mode=inline`、`projection_truncated=false`。
- `model_view.schema=maf.agent.delegated_skill_activation.v1`，包含 activation identity、revision、profile digest、instruction body 和 instruction SHA。
- body 缺失、超限或 digest mismatch 提交 `delegated_skill_instruction_invalid`；不截断、不激活半份指令。
- committed Tool result 是恢复唯一 authority；conversation history、memory candidate、summary 和公共 Artifact 都不得出现 body。

### D5. HTTP 202 闭合

为prepared-only、pending transition后/Agent init前、partial Interrupt、Agent init transaction fault加测试。HTTP 202只允许在以下任一closed durable handoff后返回：

- AgentRun已通过Checkpoint C原子提交user + 可选hint activation；
- pre-Agent Interrupt及原v2 prepared authority完整持久化并可恢复；
- no-server intent完整持久化。

合法hint的HTTP成功测试必须同时读取Agent items，证明activation bytes/digest与prepared逐字节一致且首轮Tool choice为auto。任何partial state必须失败或由同一idempotency key恢复，不能先返回accepted。

### D6. Green 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_message_submission \
  tests.api.test_pending_skill_context \
  tests.api.test_submission_preparation_callbacks \
  tests.api.test_mcp_server_explicit_agent_loop \
  tests.api.test_submission_admission_runtime_startup \
  tests.orchestration.test_agent_loop \
  tests.orchestration.test_agent_skill_activation \
  tests.storage.test_sqlite_pending_skill_context \
  tests.storage.test_submission_admission_postgres_integration
```

真实PostgreSQL DSN未提供或测试skip不能记作通过；本次使用一次性隔离PostgreSQL 17数据库完成零skip门禁后才将D记为完成。

完成证据：DTO 422与低敏409在submission副作用前闭合；合法hint在HTTP 202前已持久化prepared v2及user + activation，informational样例零Tool调用。历史pending transition用同一SQL事务写状态与Task/prepared/status/reason/time绑定的deterministic audit receipt，auto consumed一次，hint/force/MCP superseded且空集合也留receipt；delegated Skill复用hint activation，仅实际调用后提交有界pinned instruction model-result。C/D focused Python 216项、一次性隔离PostgreSQL三组15项、Runtime Sidecar Rust 90项及Ruff/diff/fmt通过；Checkpoint E～H仍未完成。

检查点提交建议：

```text
feat(api): admit recoverable soft skill hints
```

## 11. Checkpoint E：唯一纯 `AgentCallResultProjector`

### E1. 新建纯投影模块与红测

新增：

- `src/orchestration/agent_loop/result_projection.py`
- `tests/orchestration/test_agent_result_projection.py`

`AgentCallResultProjector` 必须是无 I/O 的唯一边界。输入至少包含 capability ID、raw `output_payload`、call item ID、现有业务 Artifact refs 和可选 delegated activation facts；输出是 closed projection decision：

- canonical raw JSON bytes、raw SHA、original size；或 typed invalid reason。
- `maf.agent.model_result.v1` safe result。
- projection mode/revision/truncated 标志。
- 若普通 Skill 需要 spill，返回待 stage 的 canonical raw bytes 和 deterministic identity facts；projector 本身不写文件/数据库/event。

### E2. strict JSON 与敏感边界

- raw canonicalization 复用 strict JSON 规则：拒绝 NaN/Infinity、非字符串 key、surrogate、过深/过多节点和不可序列化对象。
- inline 模型视图对敏感 key/text 做 allowlist/sanitizer，不复制内部 Artifact storage ref、Base64、credential 或 Tool arguments。
- 普通 Skill 只有在需要生成“完整 raw” `skill_result` 时，递归禁止闭合集合：credential/password/secret/API-access-refresh token/authorization/internal-source-storage path/key/handler/runtime/config/raw Tool arguments。
- spill raw 命中禁止字段时整个结果 `agent_result_invalid`；不得删除字段后仍称“完整 raw”。
- 普通业务 URL、文献字段和 output contract 字段不能因子串近似误拒绝；为 token 与业务词边界写正反测试。

### E3. 三类 adapter

1. **MCP adapter**
   - 只使用现有 bounded `text`/agent projection、小型状态和 safe Tool metadata。
   - 不复制 `business_result` user view、raw result 或重复 `structured_content`。
   - 不创建 `skill_result`，不改变 MCP Artifact/DTO/下载边界。
2. **普通 Skill adapter**
   - 小结果 sanitizer 后 inline，`projection_truncated=false`。
   - 大结果按固定优先级保留 `answer/response_text/summary/search_summary/status/missing/error/安全文件描述`；bulk arrays/maps 只进入 spill raw。
   - 固定排序和裁剪算法；任何改变 bytes/预览选择/Artifact identity 的修改必须升级 `skill-result-v1` revision。
3. **delegated adapter**
   - 复用 D4 已构建的 instruction model view。
   - 不再生成第二种顶层 schema或第二份 profile。

### E4. 双预算与最终 envelope 预检

- `model_view` 最多 20,000 Unicode code points。
- 完整 safe result canonical JSON 最多 80,000 UTF-8 bytes。
- `projected_size_bytes` 基于最终 safe result canonical bytes，并通过定点计算得到；写入 size 字段后必须再次验证，不接受近似值。
- 20,000/80,000 边界用 ASCII、中文、多字节 emoji 和 escape-heavy JSON 测试。
- projector 最后用 Agent canonical codec 预检完整 Tool result 外壳；不是只测 safe result。
- 无法在预算内形成最小 envelope 时返回 `agent_result_projection_too_large`，不抛到 runner。

### E5. 接入 invoker

修改：

- `src/orchestration/agent_loop/capability_invoker.py`
- `src/orchestration/agent_loop/runner.py`
- `src/api/runtime.py` wiring
- `tests/orchestration/test_agent_invocation.py`
- `tests/orchestration/test_agent_loop.py`

删除唯一生产 `dict(result.output_payload)` safe-result 直通。waiting/remote continuation locator 仍进入 closed model view，但不能把 raw result重新拼入。projector error 转为 `AgentCallExecution(status=FAILED, safe_error_code=...)`，交由正常 Agent outcome CAS 收敛。

### E6. Green 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_result_projection \
  tests.orchestration.test_agent_invocation \
  tests.orchestration.test_agent_loop \
  tests.integrations.mcp.test_result_parsing

rg -n 'safe_result_payload = dict\(|dict\(result\.output_payload\)' src
git diff --check
```

静态扫描在生产 `src/` 必须零命中；历史 design/test failure description 可保留。

检查点提交建议：

```text
feat(agent): bound capability results before persistence
```

## 12. Checkpoint F：Skill result staging、Agent CAS 与 terminal reconciliation

### F1. 独立 result stager

新增：

- `src/orchestration/agent_loop/result_artifacts.py`
- `tests/orchestration/test_agent_result_artifacts.py`

修改 `src/storage/artifact_files.py`，只增加底层 deterministic bytes/file primitive；不调用 `SkillOutputArtifactManager.process*`。

固定 identity：

```text
identity = sha256(
  "maf.agent.skill_result_artifact.v1\0" +
  call_item_id + "\0" + raw_sha256 + "\0" + projection_revision
)
artifact_id = "agent-skill-result:" + identity
filename = "skill_result.json"
```

stager 行为：

- 输入只能是 E 的 canonical raw bytes 和 identity facts。
- canonical raw直接写入现有managed file store的最终deterministic路径`<artifact-root>/<sanitized-artifact-id>/skill_result.json`；CAS前没有Artifact metadata，所以虽位于最终路径仍不可通过API发现。CAS不移动或重命名raw file。
- manifest写入factory显式配置的独立sibling root（例如`<runtime-root>/agent_skill_result_stage_manifests/<artifact-id>.json`），绝不放入artifact目录，也不生成可被`open_path`解析的storage key。
- artifact root、artifact目录、manifest root均0700；raw/manifest均0600。raw file与manifest在返回handle前分别flush + fsync file/dir。
- manifest exact schema 至少包含 artifact/task/conversation/node/call、raw SHA/projection revision/storage key/size/staged_at。
- manifest 不写 owner username、正文、下载 URL 或内部绝对路径；owner authority 只沿 Artifact -> Task -> Conversation 的既有关系校验，不新增 HMAC owner 引用。
- deterministic raw已存在时逐字节size/SHA比对后复用；identity同、内容不同fail closed。
- manifest采用first-writer-wins：已存在且稳定identity字段完全匹配时保留首次`staged_at`并复用，不按replay wall clock重写；任一稳定字段冲突fail closed。双worker创建必须用no-clobber/O_EXCL或等价原子操作。
- stager 只返回 `AgentStagedArtifact`，不调用 `storage.save_artifact`、不发公共 event、不执行 supersede。

`storage_ref` 使用 closed `source_kind=skill_result` metadata，包含 version、retention、Task/conversation/node/call、raw SHA、projection revision、filename/MIME/size/opaque两段storage key；不含 filesystem path。公共 listing/download 必须先由 Artifact 的 `task_id` 复用既有 owner 校验。

### F2. terminal Node 与 result/artifact 同一 CAS

修改：

- `src/orchestration/agent_loop/invocation.py`
- `src/orchestration/agent_loop/task_projection.py`
- `src/orchestration/agent_loop/models.py`
- `src/orchestration/agent_loop/runner.py`
- `src/storage/sqlite/agent_repository.py`
- `src/storage/postgres/agent_repository.py`
- `src/storage/runtime_sidecar_agent_repository.py`
- 对应 Rust Sidecar事务测试

具体规则：

- Agent-owned `commit_completed/commit_failed/commit_route_rejected` 只持久化 Capability 业务 events，返回 terminal candidate；不保存 terminal Node/output refs，不发 `node.completed/failed`。
- waiting/Interrupt/remote waiting authority保持原状并继续先 durable 后 release lease。
- projector/stager/final envelope成功后，现有 `commit_agent_call_outcome` 一次提交 Tool result、terminal Node、output refs、result Artifact metadata、已有业务 Artifact refs 和 Run revision。
- repository 对 `source_kind=skill_result` 做 closed metadata/binding验证；不读取 raw正文、不按业务字段裁剪。
- projector/staging/envelope失败用同一 CAS 把 result 与 Node一起置 failed，safe codes 精确为 `agent_result_invalid`、`agent_result_artifact_persist_failed`、`agent_result_projection_too_large`。
- exact replay 比较逐字节 safe result、Artifact identity/storage ref 和 terminal status；冲突 fail closed。
- CAS loser 不删除 deterministic raw file或manifest。

Agent outcome CAS异常必须分三类闭合：

1. **response lost / revision changed**：重新读取call result、Node和Artifact metadata；若winner的status、safe payload digest、artifact refs/storage ref逐字节一致，按exact success继续并补terminal event。
2. **storage unavailable / commit outcome unknown**：当前进程不得重执行Capability或重新project；保留raw和manifest并退出本轮。startup沿用`AgentRunRecoveryCoordinator`，将仍reserved的非waiting call提交typed `side_effect_unknown_no_replay` aborted outcome，使Node/result同一CAS终态化；随后janitor仍等待Task终态和24小时。
3. **identity/payload conflict**：fail closed并记录低敏冲突；不得覆盖winner、不得删除file。若result仍reserved，startup仍按no-replay路径收敛；若已有非exact committed winner，Run进入一致性故障而不是伪造typed success。

故障测试必须区分“CAS明确未提交”“响应丢失但exact winner已提交”“真实identity conflict”，不能把三者统一当作staging失败。

### F3. terminal lifecycle event reconciliation

- Agent CAS 成功后，用 `call_item_id + committed result digest` 派生 exact `node.completed|node.failed` event ID。
- runner 尽力写 event；CAS 已成功而 event 写失败不回滚 authority。
- startup 在 Agent run/result recovery 后扫描 committed terminal result/Node 并幂等补写缺失 event。
- 测试 crash before CAS、after CAS/before event、event response lost/replay；每个 call 最终只有一条 terminal lifecycle event。

### F4. API、history、删除与 janitor

修改：

- `src/api/artifact_responses.py`
- `src/api/routes/tasks.py`
- `src/api/routes/conversations.py`（history Artifact过滤若需）
- `src/api/runtime.py` startup/deletion wiring
- `tests/api/test_skill_output_artifacts.py`
- `tests/api/test_conversation_messages_artifacts.py`
- `tests/storage/test_artifact_file_store.py`

规则：

- `is_active_managed_output_file` 增加 `skill_result`；原 `is_active_skill_output_file` 语义不变，避免 business supersede误收 result。
- task Artifact listing 和 history 只在 metadata 已由 Agent CAS 发布后返回 file card。
- 复用或最小扩展`LocalArtifactFileStore`的安全路径与哈希校验能力，按Artifact metadata验证opaque storage key、regular file、expected size和SHA；不为本功能另建通用文件传输抽象。
- download 接口 allowlist `skill_output|skill_result`，先取Artifact metadata、再验证Task owner、closed source kind和上述文件完整性，强制attachment disposition返回。MCP result继续404。
- 直接猜 storage key、跨用户、metadata 缺失/不匹配和 CAS 前 stage 全部 404。
- Task/Conversation 删除复用 managed file cleanup；正常 Task 终态不删除 result。
- startup 顺序固定：submission/prepared recovery -> Agent run/result/terminal event recovery -> staged-result janitor -> 其他后台服务。

janitor 只有在以下全部成立时删除 orphan raw + manifest：

- manifest mtime 超过 24 小时；
- 没有完全匹配的 Artifact metadata；
- 对应 Tool result 不再 reserved；
- AgentRun 不可恢复；
- Task 已终态或不存在；
- 所有查询成功且 identity 一致。

若已存在完全匹配 metadata，只删除 private manifest、保留 raw file。任一查询失败、identity drift、Run recoverable、Task nonterminal、result reserved 都保留并记录低敏诊断。

### F5. 故障注入矩阵

最低必须覆盖：

1. raw invalid，零 stage，Node/result 同步 failed。
2. raw 含禁止内部字段，零“删字段后完整”Artifact。
3. file create/write/fsync/manifest fsync任一点失败。
4. raw/manifest首次创建后exact replay复用首次`staged_at`；manifest identity冲突fail closed。
5. stage成功、CAS明确失败或storage unavailable；文件不可发现，startup no-replay终态化后janitor按规则处理。
6. CAS response lost但winner已exact committed；重读识别成功，不提交failed outcome。
7. CAS exact replay，复用同一file/Artifact/safe bytes。
8. 双worker CAS竞争，loser不删除winner文件。
9. CAS成功、terminal event失败，startup补写。
10. registered Artifact + leftover manifest，只清manifest。
11. reserved/recoverable/nonterminal/未满24h orphan全部保留。
12. `skill_output` supersede不影响`skill_result`，反向也不影响。
13. MCP raw result从未进入`skill_result`下载通道。
14. download拒绝非法storage key、non-regular file、错误owner、size/SHA drift和跨用户请求；正常`skill_output`与`skill_result`均可下载。

### F6. Green 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_result_artifacts \
  tests.orchestration.test_agent_result_projection \
  tests.orchestration.test_agent_invocation \
  tests.orchestration.test_agent_loop \
  tests.lifecycle.test_agent_run_recovery \
  tests.storage.test_agent_storage_conformance \
  tests.storage.test_artifact_file_store \
  tests.api.test_skill_output_artifacts \
  tests.api.test_conversation_messages_artifacts
```

检查点提交建议：

```text
feat(agent): publish large skill results with outcome cas
```

## 13. Checkpoint G：Frontend 单消息 hint 切换

### G1. 先改类型和红测

修改：

- `frontend/src/api/types.ts`
- `frontend/src/api/client.ts`
- `frontend/src/api/client.test.ts`
- `frontend/src/domain/slashCommands.ts`
- `frontend/src/domain/slashCommands.test.ts`
- `frontend/src/App.tsx`
- `frontend/src/App.test.tsx`

把 `SubmitMessageInput.routingMode` 设为必填 closed union：`auto | hint | force_capability`。更新所有调用点显式传递，client 不根据 capability ID 或 UI mode推导。

红测：

- 普通 chat、interrupt answer和无选择 follow-up 传 `auto + null`。
- picker badge 与直接 `/skill-name` 都传 `hint + skill.*`。
- MCP `$Server` 继续传 `force_capability + mcp.dispatch + mcp_server_binding`。
- Skill/MCP intent继续互斥；冲突/unknown Slash仍阻止发送。
- API client 若 routing/capability组合不合法，在发 HTTP 前本地抛稳定错误；这只是 developer guard，后端仍是 authority。
- 前端不再提交 `forced_by_slash_command` 或其他名称含 forced 的 Skill metadata；后端不依赖 picker/Slash来源。
- Slash菜单的键盘上下选择、Enter确认、Escape关闭、焦点保持、badge取消按钮`aria-label`、busy gate和Interrupt gate必须保留现有行为；只改routing语义和文案。

### G2. 一次性选择状态

- picker/Slash badge 文案改为“已选择/优先使用”，不得暗示立即执行。
- 发送开始后沿用 busy gate。
- 实际 submit 成功或失败后都清除 selected Skill；下一条消息默认 auto。
- 附件上传在 submit 前失败时保持现有补偿语义；测试明确是否清除由当前既有产品行为决定，不借本任务修改上传生命周期。
- 新 hint（包括同 capability）不携带 pending metadata；server已完成的 supersede不依赖前端清 badge。
- 不新增 force toggle或执行按钮。

### G3. Green 门禁

```bash
cd frontend
npm test -- --run src/domain/slashCommands.test.ts src/api/client.test.ts src/App.test.tsx
npm run typecheck
npm run build
```

检查点提交建议：

```text
feat(frontend): submit selected skills as soft hints
```

## 14. Checkpoint H：审计、文档、全量门禁与真实验收

### H1. 审计与指标闭合

修改 `src/orchestration/agent_loop/observability.py` 或现有 audit wiring，不建立第二套 event bus：

- `skill.hint_bound` exact event只记录 capability/revision/profile digest。
- `agent.result_projected` 只记录 capability、projection mode、original/projected size、raw digest、Artifact count、closed error code。
- 指标至少区分 `inline/artifact_backed/invalid/artifact_persist_failed/projection_too_large`。
- 事件/日志 leak scan 拒绝 profile body、instruction body、raw/model view正文、storage path/key、download URL、用户正文、credential。
- 不恢复旧 `soft_skill_binding.decision/reasoning_delta`；执行事实只由 Agent Tool call/Node/Skill events证明。

### H2. API 和项目文档

更新：

- `docs/api/api-doc.html`
- `docs/api/API更新日志.md`
- `docs/AGENTS.md`
- 根 `CHANGELOG.md`
- 本 design 与 plan 的状态行
- 新建 `docs/superpowers/specs/2026-08-28-unified-agent-loop-skill-soft-binding-implementation-evidence.md` 作为唯一稳定、脱敏的完成证据索引

文档必须明确：

- Skill picker/Slash 是 `hint`，不保证执行；内部/自动化 force 仍存在但普通 UI 不暴露。
- 422 shape、409 `skill_hint_unavailable` 和 MCP组合错误。
- `skill_result` 是 owner-bound完整业务 JSON 下载，不是 MCP raw result公共旁路。
- 20,000 code points、80,000 bytes、131,072 bytes 三层预算。
- prepared v2 新旧版本锁步发布/回滚约束。
- 当前实施证据只针对 `main` 本地开发环境；`prod` 未变。

### H3. 完整后端回归

按根 AGENTS 的分层门禁执行并读取最终输出：

```bash
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/skill_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
```

若本地 Git-ignored `skill/sql-query` 兼容 checkout 存在，再运行其独立测试；不存在时明确记为 not present，不伪造通过。

对所有本次修改 Python 文件执行精确 Ruff，再执行：

```bash
git diff --check
git diff --stat
git status --short
```

### H4. 完整 Frontend 与 Rust 门禁

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build

cd ..
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run
```

prepared v2 修改了 Runtime Sidecar closed contract，因此完整统一 Rust gate 是 release 条件。外部工具缺失或 Linux-only gate skip 必须记录为验证缺口，不能把 `--skip-unavailable` 结果写成全部通过。

### H5. 自动核心业务验收

新增/扩展 deterministic E2E fixture，避免依赖实时网络数量：

1. pinned `bioinfo-daily` profile 含日期范围、`max_results=30/max=100` 与 output contract。
2. informational prompt“你看看这个 Skill 是干什么的”：final completed、Tool call=0、Skill Node=0、network fake call=0；答案事实只来自 profile。
3. execution prompt“检索最近七天的育种文献”：模型调用指定 Skill；fixture 返回 28 篇并故意同时含顶层与 `structured_content` duplicate arrays。
4. 完整 28 篇只在 deterministic `skill_result.json` 一份 raw中出现；safe result `artifact_backed`、无重复数组、<=80,000 bytes。
5. Tool result/Node/Task/final answer/Artifact card 全部完成，无 reserved或execution crash。
6. final answer若只消费 preview，明确提示完整结果在 Artifact，不声称分析未进入 model view 的全部记录。
7. `germplasm-mcp` informational 零 MCP call；execution进入现有 MCP approval/dispatch链，并复用一个 activation。

### H6. 本地成对 UI/API smoke 与证据归档

在 `main` 本地开发环境成对重建 backend、frontend 和 Runtime Sidecar；不得只替换一侧。记录脱敏 evidence：

- build/启动版本与 commit。
- picker和Slash各一次 informational Task ID：Task completed、zero Tool/Node/Skill network event。
- `bioinfo-daily` execution Task ID：实际返回 N 篇时，下载 raw count=N、safe result bounded、Task completed；若实时数据不是28篇，自动 fixture仍提供28篇固定验收，真实 smoke不得伪称28。
- `germplasm-mcp` informational/execution各一条，证明 MCP `$Server`现有显式路径不回归。
- Artifact owner下载成功；另一测试用户得到404；猜 storage key无效。
- restart pre-Agent file/sheet Interrupt和restart staged result各一次，证明 prepared v2与janitor顺序。

任何真实外部网络/Skill不可用都必须报告为外部证据缺口；不能用单元测试替代“真实 smoke 已通过”的声明。

稳定证据写入 `2026-08-28-unified-agent-loop-skill-soft-binding-implementation-evidence.md`，由Checkpoint H执行者维护、最终release reviewer复核。exact sections为：commit/branch、A～H checkpoint commits、定向与全量测试计数、真实PostgreSQL、Rust/Frontend gate、fault matrix、发布/回滚检查结果、smoke Task IDs和result size/SHA摘要、已知gap、`prod_untouched=true`。原始日志只保存在Git-ignored `runtime/evidence/`，不得提交凭据、DSN、绝对路径、profile/instruction/raw正文。

### H7. Final checkpoint 提交建议

```text
docs(agent): close skill soft binding rollout evidence
```

最终状态只在上述证据齐全后改为 `complete`；不要提前把 plan/design/CHANGELOG 写成已实施。

## 15. 设计完成条件追踪

| Design 完成条件 | 主责 checkpoint | 必要证据 |
|---|---|---|
| 1. picker/Slash 使用 hint | G | Slash/client/App tests + UI smoke |
| 2. hint 全入口 auto choice | C、D | orchestrator/recovery/waiting tests + HTTP handoff |
| 3. profile 首次采样前 durable/pinned | A、B、C、D | activation bytes + atomic init + restart |
| 4. 新 submission v2、旧 v1 精确读 | B | Python/SQLite/Rust cross-version matrix |
| 5. hint supersede、auto consume一次 | D | SQLite/PG atomic transition receipt + crash replay |
| 6. informational 零执行 | C、D、H | deterministic E2E + real Task evidence |
| 7. execution/Interrupt/final 不回归 | C、D、H | Agent/Skill/MCP regression + smoke |
| 8. outcome 必经 projector | E | static scan + invoker tests |
| 9. 三层预算和无损 spill | E、F | boundary tests + 28-record fixture |
| 10. staged/CAS/owner/janitor | F | final-path/manifest topology、CAS/recovery fault matrix、verified-download tests |
| 11. failure typed 收敛 | E、F | invalid/staging/envelope injection |
| 12. delegated 不重复 activation | D | item count/digest/replay tests |
| 13. profile字段充分、raw body延迟 | A、D | profile allowlist + delegated instruction tests |
| 14. MCP语义/投影不变 | E、G、H | MCP regression + `$Server` smoke |
| 15. 前后端/Rust/真实 smoke | H | Final Gate、成对build/commit与稳定evidence文档 |
| 16. 文档/索引/CHANGELOG一致 | H | diff review |
| 17. `prod` 未修改 | 0、H | branch/deploy evidence |

## 16. 发布与回滚步骤

### 16.1 发布前

- 确认 A～H 全 green，当前 commit就是成对镜像输入。
- 停止新 submission，等待正在写 prepared handoff 的请求收敛。
- 用现有只读管理查询确认没有prepared-only、partial Interrupt、recoverable v2 Task或orphan reserved result，并把SQL/Runtime Sidecar两侧的脱敏计数写入稳定evidence文档；任一authority不可查询时不发布。
- 同一维护窗口成对替换 backend/frontend/Runtime Sidecar；不允许新前端连旧后端或新 backend连旧 Sidecar validator。
- 启动顺序必须让 submission/Agent recovery先于 result janitor。

### 16.2 回滚

- 回滚前再次停止新 submission。
- 等待所有由新writer创建的v2 submission、pre-Agent Interrupt和Task终态；用现有只读管理查询确认两侧无recoverable/reserved v2 authority后才可继续，旧backend不能读取在途v2。
- backend/frontend/Sidecar成对回滚。
- 不删除 v2 bytes、AgentItem、Task、Message、Artifact、audit 或 raw result file。
- projector回滚只能进入“小结果 bounded inline、超大结果 typed failure”的 safe mode；禁止恢复 raw `dict(output_payload)` 直通。
- 已完成 hint Task 的普通 assistant history继续可读，private activation不公开。

本计划不授权任何 `prod` 发布；上述步骤只是未来获得明确发布授权后的门禁合同。

## 17. 风险与停止条件

| 风险/条件 | 处理 |
|---|---|
| prepared v2 任一路径仍写 v1 | 停止进入 Frontend checkpoint；先修 writer/fixture |
| hint admission 在副作用后才发现 profile/revision失败 | 视为阻断，补 fault test，不用清理补偿掩盖 |
| 三 repository 任一无法原子提交 user+activation | Checkpoint C 不通过；不以“随后补写 activation”降级 |
| PostgreSQL/Rust外部环境缺失 | 明确验证缺口，状态保持未完成 |
| result raw含禁止字段 | typed invalid；不得删字段或 redact 后发布“完整 raw” |
| CAS loser清理 deterministic file | 立即停止，修复 no-loser-delete和janitor判断 |
| terminal Node 与 result再次出现不一致 | 视为数据完整性阻断，不把 reconcile当正常写路径 |
| download无法完成owner、regular-file、size/SHA验证 | 视为安全阻断，不绕过managed file store校验 |
| live Skill返回数量变化 | 自动 fixture继续锁28；真实 smoke记录实际N且不伪造 |
| Frontend/backend/Sidecar无法成对发布 | 不发布，不增加静默兼容推断 |
| 发布/回滚前无法读取任一durable authority或脱敏检查证据未归档 | 不发布/不回滚，不以口头确认替代 |
| 需要新表、proto字段或通用 Artifact读取Tool | 超出 design，停止并回到设计审批 |
| 发现需要修改 `prod` 或 Skill脚本 | 超出授权，停止并请求用户决定 |

## 18. 实施启动边界

实施者开始 Checkpoint 0 前必须重新读取：

- 根 `AGENTS.md`、`docs/AGENTS.md` 和目标源码目录内任何更深层 `AGENTS.md`。
- 本 design 与本 plan 的最新提交。
- 当前工作树、最近提交和所有目标文件的现状。

每个 checkpoint 都遵循“先红测、最小实现、定向 green、diff review、清晰 commit”。不得在本计划交付阶段直接开始业务实现；用户明确要求实施后才从 Checkpoint 0 执行。

License Requirement：复用现有 Python、Rust/Runtime Sidecar、FastAPI/Pydantic、React/TypeScript、Agent Loop、PublicSkillProfile、MCP projection budget、managed Artifact store 与 Skill runtime；不新增第三方依赖或许可类型。
