# 统一 Agent Loop Skill 临时完整结果与全局上下文 Compaction 实施计划

依据：`2026-08-28-agent-skill-transient-full-result-context-compaction-design.md`

设计复审基线：`main@a01a32a2`

计划日期：2026-08-28

状态：`ready_for_implementation`（仅计划完成，业务代码尚未实施）

目标分支：`main`

## 1. 完成声明

本计划只落实已批准设计中的一条闭合链路：普通可执行Skill完成后，完整strict-JSON结果若能放入128 KiB AgentItem则完整inline；否则写入private transient stage，以bounded receipt完成outcome CAS，并在下一次主Agent采样前按Run固定模型窗口90%的total-context预算加载完整raw。只有实际候选请求超限时才compact全局closed history。

只有以下事实同时成立，才可把实施状态改为`complete_local`：

- 新AgentRun按固定`model_edition`解析窗口，并在首条user AgentItem内持久化不可漂移的90% `context_budget` authority。
- 普通可执行Skill不再受20,000 code points / 80,000 bytes模型视图上限；128 KiB仍是单个durable AgentItem硬上限。
- 大结果写`skill-result-v2 transient_staged` receipt和private stage；transient raw不进入`AgentStagedArtifact`、`commit.staged_artifacts`或Artifact表/API。
- 下一次主Agent请求在模型窗口允许时包含完整raw；receipt marker不进入provider request，raw不写回Message、Memory、Event、Artifact metadata或其他AgentItem。
- 每次主模型采样前按实际messages、tools、tool choice、history、current user、continuation和完整raw统一估算total tokens；不超90%时compaction模型调用为0。
- 超限时只compact连续、committed、closed且eligible的历史；尚未被主模型采样消费的最新transient result保持逐字节完整。
- compaction请求自身也必须fit；每次commit后重读Run/Items、重新resolve、重新构建candidate并重新预检，直到FITS或typed failure。
- stage-before-CAS、CAS response loss、receipt-before-sample、compaction response loss和final-before-cleanup均可恢复且不重跑Skill。
- final、covered compaction、failed/cancelled和24小时orphan janitor闭合stage生命周期；cleanup故障不反转已提交的final/compaction authority。
- legacy v1 inline/`artifact_backed`、旧`skill_result`下载、MCP、delegated Skill、waiting/Interrupt、失败结果、Frontend和外部Skill脚本保持现有合同。
- 定向、全量、真实PostgreSQL、Runtime Sidecar和本地真实Skill smoke形成低敏证据；未修改或部署`prod`。

## 2. 固定范围与非目标

### 2.1 允许修改

- `src/orchestration/agent_loop/`中的context budget、result projector、private stage/resolver、Context Builder、total preflight、compaction、Runner、recovery协作、cleanup和observability。
- `src/api/runtime.py`中的model-edition budget factory、Agent wiring、startup recovery/janitor顺序和低敏事件接线。
- SQLite、PostgreSQL、Runtime Sidecar三条Agent repository的首条user payload exact replay测试与必要的Python adapter修改。
- OpenAI-compatible Agent model adapter的closed context-length错误映射；其他Provider错误保持透传。
- 后端定向/E2E/故障注入测试、文档索引、CHANGELOG和实施证据。

### 2.2 明确不做

- 不修改128 KiB AgentItem上限、数据库表/列、physical migration或protobuf。
- 不增加Artifact分块读取、result-specific summary、向量检索、跨Task缓存或通用Artifact读取Tool。
- 不为MCP/delegated/waiting/failure结果开放transient full-result路径。
- 不增加固定Agent轮次上限、参数语义去重或猜测性重复调用拦截。
- 不改Skill manifest、input/output schema、检索策略、外部Skill仓或Frontend wire/UI。
- 不重构旧`skill_output`、MCP result和legacy `skill_result` Artifact生命周期。
- 不新增第三方依赖，不构建/推送发布镜像，不修改`prod`。

## 3. 当前HEAD证据与精确改造缝

实施开始前已在`main@a01a32a2`核对以下事实；执行时重新检查符号和调用关系，不依赖固定行号：

| 当前缝 | 当前行为 | 本计划目标 |
|---|---|---|
| `AgentLoopOrchestrator.initialize_run` | 固定model binding后创建Run，再提交只含`text`的首条user item | 在模型调用前生成90% budget，并与`text`同一user item提交；旧Run缺budget走legacy |
| 三条Agent repository `commit_agent_user_message` | exact replay user text和可选hint activation | exact replay `text + context_budget`；不改表、列、proto |
| `AgentCallResultProjector._skill` | 先受20k/80k约束，超限生成v1 `artifact_backed` preview | completed ordinary Skill先做完整安全校验；128 KiB内完整inline，否则v2 receipt |
| `AgentSkillResultArtifactStager` | 生成可进入`AgentStagedArtifact`并最终发布Artifact的raw | 只保留legacy兼容；新增独立private transient store，绝不返回`AgentStagedArtifact` |
| `AgentCapabilityInvoker` | spill后把result Artifact追加到`staged_artifacts` | transient store只返回closed `stage_ref`；现有业务Artifact仍走原字段，transient raw不追加 |
| `AgentContextBuilder` | committed Tool result直接把durable `safe_result`渲染给模型 | schema-first识别v2 receipt，经resolver完整替换；失败则provider调用为0 |
| `AgentCatalogPreflight` | 只估算catalog/rules/user/minimum suffix，未接生产Runner | 新total preflight按完整`AgentModelRequest`计数；旧catalog helper只保留兼容测试 |
| `AgentCompactionService` | 可compact closed prefix，但prompt一次装入整个prefix且未接Runner | 选择能在90%内完整进入compaction请求的最大closed prefix，commit后repreflight |
| `AgentLoopRunner` | 每次直接`context build -> sample`，无preflight/compaction | 每次sample前统一candidate/preflight；FITS零额外调用，超限compact或typed failure |
| `AgentRunRecoveryCoordinator` | 非waiting reserved result统一`side_effect_unknown_no_replay` | matching transient manifest先恢复同一receipt；无可信stage才沿用原unknown路径 |
| `ApiRuntime.start` | Agent recovery后运行legacy Skill result Artifact janitor | Agent recovery后运行transient janitor，再运行legacy janitor；二者根目录/source kind隔离 |
| `AgentFinalOutputPublisher`/cancel/compaction | 不知道transient stage | authority commit成功后best-effort cleanup；遗留由startup janitor收敛 |

## 4. 实施策略选择

考虑三种顺序：

1. **Authority-first，按可恢复边界横向闭合（采用）**：先budget和receipt/stage，再接resolver/preflight、compaction、recovery/cleanup，最后统一生产接线和真实smoke。每个checkpoint都能独立验证，A～E完成前整体不可发布。
2. **先改Runner做纵向happy path**：能较快看到完整raw，但stage、receipt和recovery尚未闭合时会产生不可恢复的在途v2 Run，不采用。
3. **把现有Artifact stager重构成通用blob平台**：复用代码较多，但会同时改动legacy Artifact生命周期和公共下载边界，超出目标，不采用。

新private store只复用现有文件安全模式和`LocalArtifactFileStore`已验证的canonical size/SHA思路，不把legacy Artifact stager抽象成新平台。所有checkpoint commit只是开发回滚点；只有Final Gate通过后才形成`complete_local`候选。

## 5. Checkpoint总览

| Checkpoint | 主题 | 主要完成证据 |
|---|---|---|
| 0 | 基线与红测冻结 | 当前preview/无preflight行为可重复，既有MCP/delegated/Artifact基线green |
| A | Run级90% context budget authority | user item精确携带budget；三repository exact replay；旧Run legacy |
| B | v2 receipt与private transient store | 128 KiB边界、stage no-clobber、transient raw不生成Artifact |
| C | resolver、完整candidate与total preflight | provider request含完整首尾sentinel；receipt/raw不重复计数；FITS时compact=0 |
| D | 全局compaction与Provider context误差 | 最大fit closed prefix、循环repreflight、一次受控context retry |
| E | crash recovery与生命周期cleanup | stage/CAS/compaction/final fault matrix、startup recovery-before-janitor |
| F | 可观测性、兼容回归与核心E2E | 28条fixture一次执行、完整末条引用、Artifact列表无新result |
| G | 全量门禁、真实smoke与回滚证据 | Python/Frontend/Rust/PG/Sidecar/真实Skill证据，`prod_untouched=true` |

## 6. Checkpoint 0：基线与红测冻结

### 0.1 启动检查

```bash
git status --short --branch
git log -6 --oneline --decorate
test -f docker_cmd.md
git check-ignore docker_cmd.md
git ls-files --error-unmatch docker_cmd.md
```

最后一条必须失败，证明`docker_cmd.md`未被跟踪；不得读取文件内容。确认分支为`main`，保留所有无关用户改动，不切换`prod`。

### 0.2 保存当前green基线

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_result_projection \
  tests.orchestration.test_agent_result_artifacts \
  tests.orchestration.test_agent_context_builder \
  tests.orchestration.test_agent_catalog_preflight \
  tests.orchestration.test_agent_compaction \
  tests.orchestration.test_agent_loop \
  tests.lifecycle.test_agent_run_recovery \
  tests.storage.test_agent_storage_sqlite \
  tests.storage.test_runtime_sidecar_agent_repository \
  tests.api.test_skill_output_artifacts \
  tests.api.test_submission_admission_runtime_startup \
  tests.e2e.test_skill_soft_binding \
  tests.observability.test_agent_metrics
```

真实PostgreSQL环境可用时同时运行`tests.storage.test_agent_storage_postgres_integration`；skip只能记为环境gap，不能记为通过。

### 0.3 后续checkpoint必须先出现的红断言

- 约280 KiB ordinary Skill结果当前得到`artifact_backed` preview，新断言要求`transient_staged` receipt且Task Artifact无新`skill_result`。
- 当前首条user item没有`context_budget`，新断言要求精确90%整数authority。
- 当前Runner不会调用preflight/compaction，新断言要求低于阈值preflight=1、compaction=0。
- 当前Context Builder把receipt直接当Tool内容，新断言要求完整第28条sentinel进入provider request且receipt marker缺席。
- 当前compaction prompt可自身超窗，新断言要求只选择完整fit的最大closed prefix。
- 当前startup对stage-before-CAS一律unknown abort，新断言要求可信manifest提交同一receipt且Skill执行次数不增加。

Checkpoint 0不修改业务代码；若当前HEAD已与上述事实不符，先更新本计划的证据地图，不机械套用。

## 7. Checkpoint A：Run级90% context budget authority

### A1. 新增纯budget合同

新增`src/orchestration/agent_loop/context_budget.py`和`tests/orchestration/test_agent_context_budget.py`：

- `AgentContextBudget` exact schema固定为`maf.agent.total_context_budget.v1`、`compact_threshold_percent=90`、正整数`model_context_window_tokens`和`total_context_limit_tokens`。
- limit使用`floor(window * 90 / 100)`整数运算；缺失、bool、非正整数或关系不一致fail closed。
- runtime按已固定的`AgentModelBinding.model_edition`调用`trim_max_tokens_for_model_edition`；不接受用户metadata覆盖，不新增第二个模型窗口配置源。
- canonical payload和digest确定性；日志、event和Frontend不包含整个budget对象。

### A2. 与首条user item原子提交

修改：

- `src/orchestration/agent_loop/models.py`
- `src/orchestration/agent_loop/orchestrator.py`
- `src/orchestration/agent_loop/context.py`
- `src/storage/sqlite/agent_repository.py`
- `src/storage/postgres/agent_repository.py`
- `src/storage/runtime_sidecar_agent_repository.py`
- `src/api/runtime.py`

规则：

- `AgentUserMessageCommit`增加已验证的可选budget；新生产Run必须提供，legacy fixture/旧Run允许缺失。
- durable user payload exact shape为`{"context_budget": {...}, "text": "..."}`；Context Builder仍只渲染`text`。
- user text、budget和可选hint activation保持原事务/all-or-zero顺序；不新增Item kind、表、列或proto字段。
- exact replay逐字段验证同一budget；同Run不同window/limit/revision一律`AgentStorageConflict`。
- 新Run配置非法在第一次模型调用前失败；user item已提交后，配置刷新不得改变该Run budget。
- 旧Run首条user payload无budget时保持当前legacy projection/无新transient writer；不能按当前配置猜测补写。

### A3. 三repository与恢复测试

扩展：

- `tests/storage/test_agent_storage_sqlite.py`
- `tests/storage/test_agent_storage_postgres_integration.py`
- `tests/storage/test_runtime_sidecar_agent_repository.py`
- `tests/orchestration/test_agent_loop.py`
- `tests/api/test_submission_admission_runtime_startup.py`

覆盖128 KiB整包边界、user/activation/budget fault rollback、response loss replay、配置漂移、旧Run legacy和Runtime Sidecar opaque payload等价。Rust kernel/proto只跑合同回归，不修改源码。

### A4. Green门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_context_budget \
  tests.orchestration.test_agent_context_builder \
  tests.orchestration.test_agent_loop \
  tests.storage.test_agent_storage_sqlite \
  tests.storage.test_runtime_sidecar_agent_repository \
  tests.api.test_submission_admission_runtime_startup
git diff --check
```

检查点提交建议：

```text
feat(agent): pin total context budget per run
```

## 8. Checkpoint B：v2 receipt与private transient store

### B1. 改造普通Skill投影

修改`src/orchestration/agent_loop/result_projection.py`和`tests/orchestration/test_agent_result_projection.py`：

- 仅`completed`、非delegated、ordinary `skill.*`进入新分支；MCP、delegated、waiting和failed继续当前adapter。
- strict JSON、depth 64、node 200,000、Unicode/non-finite和禁止authority扫描在inline/stage分类前一次完成；禁止字段使整个结果typed invalid，不做删字段后宣称完整。
- ordinary Skill完整`model_view`先构建完整Tool result envelope，并只以`canonicalize_agent_payload`的128 KiB整包预检决定inline。
- projector的纯输入增加closed `transient_full_result_enabled`策略位，只能由Invoker在复验当前Run首条user item存在合法`context_budget`后设置；旧Run或缺失budget时固定走现有v1 inline/`artifact_backed` legacy路径。该值不来自用户metadata或Tool参数。
- fit：继续`skill-result-v1 inline`、`projection_truncated=false`，不再检查20k/80k。
- 不fit：构建`skill-result-v2 transient_staged` bounded receipt，durable `projection_truncated=true`，`model_view`只含exact receipt schema和closed `stage_ref`。
- `stage_ref`由domain、call item ID、raw SHA和revision确定；不是路径、storage key、URL或Artifact ID。
- 保留`skill_result_artifact_id`、legacy `artifact_backed` reader和下载合同，但新ordinary writer不再生成它。

### B2. 新增独立private store

新增：

- `src/orchestration/agent_loop/transient_results.py`
- `tests/orchestration/test_agent_transient_results.py`

实现`AgentTransientSkillResultStore`，固定独立根目录和source kind：

- 根目录0700，raw/manifest 0600；O_NOFOLLOW、regular-file、owner UID、link count、realpath containment、fsync和no-clobber沿用现有安全模式。
- manifest只保存schema、stage ref、Run/Task/Conversation/Node/call/result identity、capability ID、raw size/SHA、projection revision、安全Artifact ID列表和`staged_at`；不保存正文、Tool参数、路径、storage key、URL或preview。
- stable identity相同则逐字段、size和SHA复验后exact replay；identity冲突fail closed。
- store返回独立`AgentTransientSkillResultStage`，绝不构造或返回`AgentStagedArtifact`。
- 缺失、symlink、non-regular、越界、size/SHA/owner drift使用closed内部错误，不把路径写入错误正文。

### B3. Invoker与outcome CAS

修改：

- `src/orchestration/agent_loop/capability_invoker.py`
- `src/orchestration/agent_loop/runner.py`
- `src/orchestration/agent_loop/__init__.py`
- `src/api/runtime.py`

规则：

- Invoker在projector返回transient decision后先stage canonical raw，复验返回`stage_ref`，再返回bounded safe result。
- Invoker从durable首条user item解析一次Run budget来选择new/legacy projector策略；配置刷新或请求metadata不得中途启用v2。
- transient stage不追加到`AgentCallExecution.staged_artifacts`；原执行产生的正常业务Artifact继续原路径。
- repository outcome CAS只看到receipt和原业务Artifact；transient raw不会触发`validate_skill_result_staged_artifact`或Artifact row创建。
- stage失败提交`agent_transient_skill_result_stage_failed` typed outcome；不得回退preview、公共Artifact或重跑Skill。
- outcome CAS response loss继续exact reread；同一receipt bytes、stage ref和raw SHA必须一致。

### B4. Green门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_result_projection \
  tests.orchestration.test_agent_transient_results \
  tests.orchestration.test_agent_result_artifacts \
  tests.orchestration.test_agent_invocation \
  tests.storage.test_agent_storage_sqlite \
  tests.storage.test_runtime_sidecar_agent_repository \
  tests.api.test_skill_output_artifacts
git diff --check
```

检查点提交建议：

```text
feat(agent): stage large skill results privately
```

## 9. Checkpoint C：resolver、完整candidate与total preflight

### C1. Schema-first transient resolver

在`transient_results.py`增加resolver，并修改：

- `src/orchestration/agent_loop/context.py`
- `tests/orchestration/test_agent_context_builder.py`
- `tests/orchestration/test_agent_transient_results.py`

规则：

- 只识别exact `maf.agent.model_result.v1 + skill-result-v2 + transient_staged`；未知/混合revision fail closed。
- resolver同时校验receipt、manifest、Run/Task/Conversation/Node/call/result/capability、regular file、size和SHA。
- model-only Tool内容保留原provider call ID和`artifact_refs`，把durable receipt替换为`maf.agent.skill_result_full.v1`及完整strict-JSON `result`。
- receipt marker、stage ref、文件路径和manifest字段不得进入provider request。
- 同一Run/revision/retry重复构建的Tool内容逐字节一致；解析或校验失败时不调用provider。

### C2. 唯一candidate builder与90% total preflight

新增：

- `src/orchestration/agent_loop/context_preflight.py`
- `tests/orchestration/test_agent_context_preflight.py`

保持`AgentContextBuilder`为唯一message renderer；新candidate builder只负责resolve、分类和计数，不复制另一套消息构造逻辑。

计数合同：

- 对最终`AgentModelRequest`的roles/content/tool calls、完整tool descriptors/schema、tool choice和framing生成确定性preflight serialization。
- 使用Run pinned model edition对应的现有token counter；provider tokenizer不可用时使用现有tiktoken fallback。不得调用独立LLM估算。
- stable/safe/final rules、active summary、trusted facts、所有可见AgentItems、current user、continuation、完整raw和Tool catalog各计一次。
- 当前user text始终作为required逐字节保留。durable compaction boundary覆盖其原Item bookkeeping后，Builder从sequence-1 user item重新注入一次，并把它从summary source排除，避免丢失或重复。
- 尚无后继主Agent assistant sample的最新transient result属于required；已有后继sample且call/result/sample关系closed后才可归入history。
- 返回closed decision：`FITS`、`HISTORY_COMPACTION_REQUIRED`、`FATAL_REQUIRED_SEGMENTS_TOO_LARGE`，并给出required/history/transient/tool/total tokens和limit；不包含正文。
- `total <= limit`必须FITS；只有`total > limit`且存在真实eligible closed history才允许compaction。

旧`AgentCatalogPreflight`保留为兼容helper和原测试对象，不接生产Runner，也不作为total-context authority。

### C3. Green门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_context_builder \
  tests.orchestration.test_agent_context_preflight \
  tests.orchestration.test_agent_transient_results \
  tests.orchestration.test_agent_catalog_preflight \
  tests.integrations.test_token_counter
git diff --check
```

检查点提交建议：

```text
feat(agent): preflight complete model context
```

## 10. Checkpoint D：全局compaction与Provider context误差

### D1. Compaction请求自身预算

修改：

- `src/orchestration/agent_loop/compaction.py`
- `tests/orchestration/test_agent_compaction.py`

规则：

- 输入改为total-context preflight结果；每轮重读权威Run/Items。
- 从最大eligible prefix开始，仅在closed边界缩小，直到`compaction system + resolved source + framing <= Run 90% limit`；不得截正文或切开assistant/call/result。
- sequence-1 current user不进入summary source，后续由Context Builder逐字节重新注入；boundary仍按连续prefix提交。
- 未消费transient result永不进入source；已消费result进入source时由resolver加载完整raw，durable receipt SHA继续绑定source digest。
- compaction model固定同一Run binding、no-tools、plain summary；Tool call、空summary、binding drift或非法输出typed失败。
- commit成功后重读Run/Items、resolve required raw、重建candidate、重新估算；相同covered range、revision无进展或required超限确定性失败。
- summary commit成功后才允许把covered stage交给cleanup；cleanup失败不回滚summary。

### D2. Runner生产接线

修改：

- `src/orchestration/agent_loop/runner.py`
- `src/orchestration/agent_loop/orchestrator.py`
- `src/api/runtime.py`
- `tests/orchestration/test_agent_loop.py`

每次模型采样固定顺序：

1. 重读Run/Items并构建catalog。
2. resolve完整candidate并执行total preflight。
3. FITS：直接sample，compaction调用数必须为0。
4. HISTORY_COMPACTION_REQUIRED：调用`compact_until_fit`，使用返回的最新Run/Items/candidate。
5. FATAL：以`agent_context_required_segments_too_large`终态化Run，provider和Skill调用数均不增加。

不在pending capability wave前做无意义preflight；先提交Tool result，再为下一次主Agent sample执行上述流程。A～E未全部完成前不得发布该Runner接线。

### D3. Provider context-length typed retry

修改：

- `src/orchestration/agent_loop/models.py`或`model_port.py`
- `src/integrations/openai_agent_model_adapter.py`
- `tests/integrations/test_agent_model_adapter.py`
- `tests/orchestration/test_agent_loop.py`

规则：

- Adapter只把closed provider code/type与HTTP状态组合映射为`AgentModelContextLengthError`；不得仅凭任意自由文本把鉴权、限流、5xx或timeout误分类。
- 该typed error是估算低报的权威证据。Runner只允许一次受控收敛：不重跑Skill，重读同一Run/Items/raw；存在eligible history时强制一次全局compaction并重建candidate，否则required-too-large。
- retry仍使用同一model binding和逐字节相同required raw；第二次context-length error直接typed失败。
- 其他Provider异常沿用现有行为，不借机compact或重放Skill。

### D4. Green门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_compaction \
  tests.orchestration.test_agent_context_preflight \
  tests.orchestration.test_agent_loop \
  tests.integrations.test_agent_model_adapter
git diff --check
```

检查点提交建议：

```text
feat(agent): compact total context before sampling
```

## 11. Checkpoint E：crash recovery与stage生命周期

### E1. Stage-before-CAS恢复

修改：

- `src/lifecycle/agent_run_recovery.py`
- `src/orchestration/agent_loop/transient_results.py`
- `src/api/runtime.py`
- `tests/lifecycle/test_agent_run_recovery.py`
- `tests/api/test_submission_admission_runtime_startup.py`

在现有reserved-result unknown/no-replay分支前增加窄transient recovery callback：

- matching manifest+raw完整时，复验call/result/Run/capability、raw size/SHA和v2 identity，重建同一bounded receipt并提交outcome CAS；不调用Skill executor。
- manifest记录的普通业务Artifact ID只有在其独立durable authority仍存在且owner/node匹配时才可重新关联；不能从raw猜测或发明Artifact ref。transient raw本身永不进入`staged_artifacts`。
- stage完成但outcome CAS response丢失时，exact reread winner；payload drift fail closed。
- partial/missing/drift stage使用`agent_transient_skill_result_unavailable`或`agent_transient_skill_result_stage_failed`收敛，禁止重跑Skill。
- 没有可信transient manifest的reserved result继续当前`side_effect_unknown_no_replay`，不改变MCP/waiting恢复。

### E2. Final/compaction/terminal cleanup

修改：

- `src/orchestration/agent_loop/final_output.py`
- `src/orchestration/agent_loop/orchestrator.py`
- `src/orchestration/agent_loop/compaction.py`
- `src/orchestration/agent_loop/transient_results.py`
- `tests/orchestration/test_agent_final_output.py`
- `tests/orchestration/test_agent_compaction.py`
- `tests/orchestration/test_agent_transient_results.py`

规则：

- final durable、covered compaction、failed和cancelled authority提交成功后，按receipt exact stage ref执行best-effort删除。
- running/recoverable/waiting/reserved/CAS未决保持；任一owner/identity查询失败fail-safe保留并低敏告警。
- cleanup顺序保证authority先于delete；response loss可由authority reread决定是否重试删除。
- cleanup失败不改写final、summary或terminal状态，由startup janitor收敛。

### E3. 24小时janitor与startup顺序

新增`AgentTransientSkillResultJanitor`，并保持：

```text
submission/MCP recovery
  -> AgentRun recovery（可消费transient stage）
  -> transient janitor
  -> legacy Skill result Artifact janitor
  -> background services
```

janitor只删除满足以下条件的stage：已被committed compaction覆盖、Run已final/failed/cancelled，或满24小时且Task终态/不存在、Run不可恢复。query失败、owner drift、reserved/nonterminal均保留。无manifest的raw orphan只在满24小时且文件名/权限/根目录验证通过后删除。

### E4. Green门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.lifecycle.test_agent_run_recovery \
  tests.orchestration.test_agent_transient_results \
  tests.orchestration.test_agent_compaction \
  tests.orchestration.test_agent_final_output \
  tests.api.test_submission_admission_runtime_startup
git diff --check
```

检查点提交建议：

```text
feat(agent): recover and clean transient skill results
```

## 12. Checkpoint F：可观测性、兼容回归与核心E2E

### F1. Closed observability

修改：

- `src/orchestration/agent_loop/observability.py`
- `src/orchestration/agent_loop/capability_invoker.py`
- `src/orchestration/agent_loop/runner.py`
- `tests/observability/test_agent_metrics.py`

闭合新增：

- result projection mode：`transient_staged`、`transient_stage_failed`；legacy `artifact_backed`仍可观测。
- `agent_context_preflights_total{decision=fits|compaction_required|required_too_large}`。
- `agent_context_compactions_total{outcome=completed|failed|no_progress|required_too_large}`。
- `agent_transient_skill_results_total{outcome=staged|injected|covered|cleaned|failed}`。
- `agent.result_projected`中`projected_size_bytes`只代表durable receipt；消费事件可记录capability ID、raw size/SHA、resolved/total tokens、threshold、compact count和cleanup outcome。

禁止动态Task/Run/call/stage ref、路径、正文、Tool参数或error正文成为metric label或日志字段。

### F2. 兼容回归

扩展：

- `tests/orchestration/test_agent_result_artifacts.py`
- `tests/api/test_skill_output_artifacts.py`
- `tests/e2e/test_mcp_server_explicit_agent_loop.py`
- `tests/orchestration/test_agent_skill_activation.py`
- `tests/orchestration/test_agent_continuation.py`

证明：

- old v1 inline/`artifact_backed`历史仍可构建上下文、枚举和下载；不迁移、不删除。
- 新v2 Task Artifact列表不出现raw `skill_result`；已有正常`skill_output`仍按原规则存在。
- MCP result projection/raw安全、delegated instruction、waiting/Interrupt、failed result和informational hint zero-call合同不变。
- Frontend DTO/组件零diff；不新增stage卡片、下载按钮或状态文案。

### F3. 固定28条核心E2E

修改`tests/e2e/test_skill_soft_binding.py`，使用确定性large fixture：

1. Skill executor只调用一次，写一份private stage。
2. 下一次provider request包含28条完整记录和首尾sentinel，resolved `result`重新canonicalize后的digest与staged raw一致；receipt marker缺席。
3. 模型final准确引用第28条唯一sentinel，不为了重读相同结果再次调用Skill。
4. total低于90%时compaction=0；注入足够closed历史后发生全局compaction且latest raw不变。
5. final后stage/manifest不存在，Task Artifact无新`skill_result`。

该fixture只验证完整结果可见性，不建立通用Tool调用次数上限或语义去重规则。

### F4. Green门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_loop \
  tests.orchestration.test_agent_result_artifacts \
  tests.orchestration.test_agent_skill_activation \
  tests.orchestration.test_agent_continuation \
  tests.api.test_skill_output_artifacts \
  tests.e2e.test_skill_soft_binding \
  tests.e2e.test_mcp_server_explicit_agent_loop \
  tests.observability.test_agent_metrics
git diff --check
```

检查点提交建议：

```text
test(agent): prove complete transient skill context
```

## 13. Checkpoint G：全量门禁、真实smoke与交付证据

### G1. Python全量

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

真实PostgreSQL Agent repository测试必须使用隔离数据库并零skip；环境不可用时状态只能是`implementation_complete_with_pg_gap`，不能宣称`complete_local`。

### G2. Frontend与Rust/Runtime Sidecar回归

虽无Frontend和proto业务改动，仍做兼容证明：

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build

cd ..
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run
```

Rust门禁覆盖fmt、clippy、test/nextest、audit、deny、coverage/provenance/fuzz manifest和Sidecar合同；若平台门禁明确skip，逐项记录，不能写成通过。

### G3. 本地真实`bioinfo-daily` smoke

只在`main`本地开发环境执行新Task，不复活旧失败Task，不发布镜像：

- 记录实际result count、raw bytes/SHA、estimated required/history/transient/tool/total tokens、pinned window、90% limit、preflight decision和compaction次数。
- 记录实际Tool call次数、每个call ID、Node ID和final状态；不预设实时结果一定为28。
- 验证下一次主Agent确实引用结果末端信息，不只看到preview/receipt。
- 验证Task Artifact列表没有新raw `skill_result`；正常业务Artifact与final output保持现有行为。
- 验证final后private stage和manifest清理；audit/log/Message/Memory/Frontend响应无raw、路径和stage ref泄漏。
- 再构造一条超90%的受控本地fixture证明global compaction；真实外部Skill不为制造超限重复调用。

### G4. 发布与回滚证据

新增稳定低敏证据文档：

`docs/superpowers/specs/2026-08-28-agent-skill-transient-full-result-context-compaction-implementation-evidence.md`

记录checkpoint commits、测试数量/skip、PG/Sidecar、fixture/真实smoke的size/token/count/final摘要、stage cleanup、Artifact列表、日志泄漏扫描、rollback检查和`prod_untouched=true`；不记录用户正文、完整raw、路径、credential或Tool参数。

回滚演练固定为：

1. 停止新submission。
2. 统计并等待所有v2 Run final、terminal或被compaction覆盖。
3. 用新版本恢复/清理在途v2；旧版本不得接管v2 receipt。
4. 确认无in-flight v2后回滚backend/Sidecar成对版本。
5. 回滚后新大Skill结果恢复旧`artifact_backed` safe mode；历史v1继续可读，v2 AgentItem/stage不做破坏性清理。

### G5. Final checkpoint

同步本计划状态、设计状态、`docs/AGENTS.md`和`CHANGELOG.md`，运行：

```bash
git diff --check
git status --short --branch
bash scripts/check_docker_cmd_policy.sh
```

检查点提交建议：

```text
docs(agent): close transient result context rollout
```

## 14. 故障注入矩阵

| 故障点 | 自动断言 |
|---|---|
| budget解析失败 | Run不进入模型采样；无猜测fallback |
| user/budget/activation事务中断 | 三repository all-or-zero；exact retry同一identity |
| raw写入或manifest fsync失败 | typed stage failure；无receipt/Artifact；不重跑Skill |
| stage完成、outcome CAS前崩溃 | startup复验stage并提交同一receipt；Skill call count不增加 |
| outcome CAS response loss | exact reread同一Tool result；不写第二stage |
| receipt提交、model sample前崩溃 | recovery重新resolve/preflight；完整raw SHA不变 |
| stage缺失、symlink、size/SHA漂移 | provider调用=0；typed unavailable；不重跑Skill |
| compaction model成功、commit前崩溃 | 旧boundary权威；保留stage；同covered range/digest可重试 |
| compaction commit、cleanup前崩溃 | summary/boundary权威；startup删除covered stage |
| final commit、cleanup前崩溃 | final权威；startup删除stage；答案不回滚 |
| cleanup I/O失败 | final/summary/terminal保持；janitor低敏重试 |
| estimator低报且Provider context error | 一次受控global compact/retry；第二次typed failure |
| auth/429/5xx/timeout | 不误分类context error；不compact、不重跑Skill |
| no eligible history且required超限 | `agent_context_required_segments_too_large`；provider/Skill调用不增加 |
| 相同compaction range无进展 | `agent_context_compaction_no_progress`；停止循环 |

## 15. 设计完成条件追踪

| Design完成条件 | 主责checkpoint |
|---|---|
| ordinary Skill移除20k/80k提前截断 | B |
| 128 KiB与三repository合同保持 | A、B、G |
| private stage + receipt，无新Artifact | B、F |
| 下一主Agent sample看到完整raw | C、F |
| 只有total context超90%才compact | C、D |
| global eligible history且latest raw受保护 | D |
| compact/repreflight可恢复、无进展失败 | D、E |
| required超限/stage损坏/provider误差typed收敛 | D、E |
| final/covered/terminal/janitor cleanup | E |
| v1/MCP/delegated/waiting/failure/Frontend不回归 | F、G |
| 自动、PG、Sidecar、真实Skill证据 | G |
| `prod`未变 | 0、G |

## 16. 每个checkpoint的固定执行纪律

每个A～G checkpoint都按以下顺序执行：

1. 重读本checkpoint目标文件和相关`AGENTS.md`。
2. 先写能在旧实现上失败的最小红测，并读取失败原因。
3. 只实现本checkpoint所需的最少代码，不提前做下一checkpoint。
4. 跑focused green、Ruff/格式和`git diff --check`。
5. 检查没有MCP/delegated/Frontend/外部Skill/`prod`越界改动。
6. 审阅完整diff，确认`docker_cmd.md`仍存在、ignored、untracked。
7. 创建范围单一的checkpoint commit，再进入下一步。

A～E任一未完成时不得启动本地真实用户Task或把部分实现作为可发布版本。发现设计外的新产品决策时停止并回到用户；普通实现细节、测试补齐和现有合同一致性修复可在本计划范围内闭合。

License Requirement：复用现有Python、SQLAlchemy、SQLite/PostgreSQL、Rust/Runtime Sidecar opaque AgentItem、tiktoken/provider tokenization、Agent result projector/context/compaction、LocalArtifactFileStore安全模式和兼容Artifact reader；不新增第三方依赖或许可类型。
