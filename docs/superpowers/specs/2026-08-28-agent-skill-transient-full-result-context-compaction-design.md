# 统一 Agent Loop Skill 临时完整结果与全局上下文 Compaction 设计

日期：2026-08-28

状态：`complete_local`；Checkpoint A～G仓库实现、真实PostgreSQL/Runtime Sidecar、本地真实`bioinfo-daily` Task与全量门禁已闭合，未构建、推送或部署镜像，不宣称`release_complete`

关联基线：`main@c4c9fc57`

## 1. 背景

统一 Agent Loop Skill soft binding 已把普通 Skill 大结果安全投影为 owner-bound `skill_result` Artifact，并把有界 preview 写入 Tool result。真实 `bioinfo-daily` 对话暴露了这一边界的产品缺口：

- 三条连续消息分别产生 1、2、5 次 `skill.bioinfo_daily` 调用，共 8 个不同 model sample、call ID、Node 和 `skill_result`。
- 每次 Skill 都成功完成，没有 waiting、timeout、恢复重放或空结果；每份 raw 为 280,103 bytes并返回28篇。
- 八份 `articles` canonical SHA 和实际 PubMed query SHA完全相同；模型改变自由文本 `query` 并未改变 Skill最终检索主题。
- 每份 raw 被投影为3,584-byte `artifact_backed` preview；模型只知道完整结果存在，无法读取文章数组。
- Agent Loop没有同一Task的固定轮次上限，模型把“成功但信息不足”误判为需要再次调用，直到自己停止。

问题不是128 KiB持久化限制本身，而是模型消费边界与持久化边界耦合：完整raw不能进入AgentItem后，只剩用户可下载Artifact，没有“下一次主模型采样即时读取完整结果”的路径。

## 2. 已确认产品决策

1. 128 KiB继续作为单个durable AgentItem的硬上限，不修改SQLite、PostgreSQL或Runtime Sidecar physical schema/protobuf。
2. 能放进128 KiB AgentItem的普通可执行Skill完整结果不再受固定20,000 code points / 80,000 bytes限制并继续inline；超限结果再按是否存在普通业务Artifact分流。
3. 放不进AgentItem且本次`execution.artifacts`为空的完整raw写入私有、crash-safe transient stage；不创建Artifact数据库记录、下载卡片或公共storage ref。带普通业务Artifact的超限结果继续走现有v1 `artifact_backed`路径，不进入v2。
4. transient stage只保留到final、terminal failure/cancel或覆盖它的全局compaction成功提交；随后删除。
5. 每次主模型采样前组装实际完整候选请求，只有所有模型输入上下文总量超过当前Run预算时才compact。
6. compact是全局Agent上下文压缩，不是Skill result专用摘要；尚未被主模型消费的最新完整result属于required suffix，不能压缩或截断。
7. total-context compact阈值为AgentRun固定模型窗口的90%，预留10%给输出、reasoning与估算安全余量。
8. 超限时反复compact已闭合历史前缀并重新预检；若required context本身仍超过90%，typed failure，不静默裁剪、不重跑Skill。
9. 只修改普通可执行Skill中可完整inline，或超限且没有普通业务Artifact的completed结果。MCP、delegated Skill、waiting/Interrupt、失败结果、带业务Artifact的超限结果和final publisher保持现有合同。
10. 旧`artifact_backed` AgentItem与已发布`skill_result`继续可读和下载；不迁移、不删除历史Artifact。

## 3. 目标与非目标

### 3.1 目标

- 让主Agent在配置模型窗口允许时，逐字节消费可完整inline，或超限且没有普通业务Artifact的普通Skill strict-JSON结果并优先生成答案。
- 让context preflight和compaction真正接入生产Agent Runner，以全模型输入为唯一超限判断依据。
- 在不持久化大raw AgentItem或公共Artifact metadata的前提下保持crash recovery、no-replay和exact identity。
- 消除“模型为了重新查看同一结果而重复调用Skill”的当前诱因。
- 保持三repository outcome CAS、Node/Run一致性、typed failure和审计低敏边界。

### 3.2 非目标

- 不删除通用128 KiB AgentItem上限，不把AgentItem改成任意大blob。
- 不为MCP raw result开放新的模型或公共读取旁路。
- 不修改delegated Skill instruction边界、MCP `$Server`语义、Tool approval或continuation locator。
- 不修改外部`bioinfo-daily`等Skill脚本、schema、检索策略或业务质量。
- 不新增通用Artifact读取Tool、分块Artifact浏览器、向量检索或跨Task结果缓存。
- 不为带普通业务Artifact的超限Skill结果新增可恢复Artifact描述sidecar；它们继续使用legacy `artifact_backed`。
- 不增加固定Agent轮次上限或对不同参数调用做猜测性去重。
- 不修改Frontend提交DTO、数据库表/列、protobuf、部署或`prod`。

## 4. 当前实现事实

- `AgentCallResultProjector`对所有结果先做strict JSON、深度/节点数和敏感authority校验。
- 普通Skill当前以20,000 code points、80,000 bytes和128 KiB AgentItem三层预算决定inline或`artifact_backed`。
- `AgentCompactionService`已经能按closed AgentItem prefix生成同模型no-tools summary，但只在独立测试中使用。
- 生产`AgentLoopRunner`当前没有context preflight或compaction依赖；它在每个Tool result后直接重建Context并再次采样。
- `trim_max_tokens_for_model_edition`和PromptEnvelope已有按model edition解析窗口的能力；现有PromptEnvelope使用75%输入预算。本设计为Agent Loop单独固定90%total-context阈值。
- 当前Skill result stager已有deterministic identity、0700/0600、fsync、first-writer manifest、CAS/recovery和24小时janitor基础能力，可手术式复用其文件安全机制，但不发布Artifact metadata。

## 5. 总体架构

```text
Skill execution completed
        |
        v
strict/canonical result validation
        |
        +-- full result fits durable AgentItem --> inline full Tool result
        |
        +-- does not fit and has business Artifact --> legacy artifact_backed
        |
        +-- does not fit and no business Artifact --> private transient stage + bounded receipt
                                      |
                                      v
                         build exact next model input
                                      |
                         total tokens <= pinned 90%?
                             /                    \
                           yes                    no
                            |                      |
                    inject full raw       compact eligible closed
                    into main sample      history prefix, then rebuild
                            |                      |
                            +-----------> main Agent sample
                                               |
                              final / later global compaction
                                               |
                                      delete transient stage
```

新增或扩展的职责保持窄边界：

1. **Run context budget authority**：在Run初始化时解析并持久化固定model edition的窗口与90%阈值。
2. **Transient Skill result stager**：写私有raw与manifest，不写Artifact row。
3. **Bounded result receipt projector**：把大结果提交为可恢复、不可公开的有界Tool result receipt。
4. **Transient context resolver**：仅在模型请求组装阶段按receipt加载并复验raw。
5. **Total-context preflight**：对实际messages、tools和必需segments统一估算token。
6. **Production compaction coordinator**：把现有compaction接入Runner，循环到fit或确定性失败。
7. **Recovery-aware cleanup**：在final、covered compaction、terminal失败/取消和startup janitor中闭合文件生命周期。

## 6. Run级模型窗口 authority

### 6.1 固定值

Run初始化时按其固定`model_edition`解析：

```text
model_context_window_tokens = configured window for pinned model edition
total_context_limit_tokens = floor(model_context_window_tokens * 0.90)
```

两个整数及policy revision必须作为private run-scoped authority与首条user AgentItem原子持久化。优先复用现有opaque AgentItem payload，在user item中增加不渲染给模型的closed `context_budget`对象，避免新增表、列、Item kind或proto字段：

```json
{
  "context_budget": {
    "compact_threshold_percent": 90,
    "model_context_window_tokens": 450000,
    "policy_revision": "maf.agent.total_context_budget.v1",
    "total_context_limit_tokens": 405000
  },
  "text": "..."
}
```

这里的数值只是schema示例；实际值必须来自当前model edition配置。Context Builder只把`text`渲染成user message；budget对象仅供runtime preflight读取。SQLite、PostgreSQL和Runtime Sidecar exact replay必须逐字段验证同一authority。

### 6.2 配置漂移

- active/recoverable Run始终使用初始化时持久化的window和threshold。
- 新配置只影响新Run；不得让waiting/startup recovery在中途改变budget。
- 缺失、非正整数、超过平台允许最大值或与90%关系不一致时，Run初始化在模型调用前fail closed。
- Run row已创建但尚无任何AgentItem时属于未完成初始化，不是legacy Run。startup必须从prepared authority恢复并提交同一user+budget后才能进入Runner；无法恢复时fail closed。
- 旧Run没有`context_budget`时继续使用当前legacy fixed projection/reader，不用新路径猜测恢复。

## 7. 普通Skill结果分类

### 7.1 共同校验

所有普通可执行Skill结果继续在repository前完成：

- strict JSON与canonical bytes；
- non-finite、surrogate、非字符串key拒绝；
- 最大depth 64、最大node 200,000；
- credential、secret、storage authority等禁止字段与赋值文本扫描；
- raw size与SHA-256计算。

invalid结果继续提交typed failed outcome，不stage、不进入模型。

### 7.2 Durable inline

不再先用20,000/80,000固定预算判断普通Skill完整结果。Projector直接构建包含完整raw model view的Tool result envelope，并以128 KiB canonical AgentItem预检为唯一durable inline门槛：

- envelope可完整放入：`projection_mode=inline`、`projection_truncated=false`；
- envelope放不入且`execution.artifacts`为空：进入transient stage；
- envelope放不入且存在普通业务Artifact：保持现有v1 `artifact_backed`；
- 不允许为通过inline预检而删字段、截数组或缩短文本。

20,000/80,000限制继续保留给MCP、delegated Skill、legacy reader/writer和带业务Artifact的超限fallback，不作为v2 transient路径的模型可见上限。

### 7.3 Transient staged

只有completed ordinary Skill、完整Tool result envelope超过128 KiB且`execution.artifacts`为空时，才使用新revision：

```text
projection_revision = skill-result-v2
projection_mode = transient_staged
projection_truncated = true
```

`projection_truncated`只描述durable `model_view`：receipt没有持久化完整raw，因此必须为`true`；它不表示下一次provider request仍然缺少结果。resolver只有在size/SHA校验通过并逐字节替换完整raw后才算`injected`，该事实由低敏消费事件和candidate测试证明，不回写或改写durable projection字段。

raw文件和manifest只复用现有stager的底层文件安全机制，但使用独立source kind与根目录，避免被`skill_result` Artifact janitor误处理。transient stage不是`AgentStagedArtifact`；v2 admission已保证没有普通业务Artifact，因此对应`AgentCallOutcomeCommit.staged_artifacts`必须为空。stage identity由call item ID、raw SHA和projection revision确定，不含用户文本或路径。

Tool result AgentItem只保存有界receipt：

```json
{
  "model_view": {
    "complete_result_pending_context_injection": true,
    "schema": "maf.agent.transient_skill_result_receipt.v1",
    "stage_ref": "agent-transient-skill-result:<digest>"
  },
  "original_size_bytes": 280103,
  "projection_mode": "transient_staged",
  "projection_revision": "skill-result-v2",
  "projection_truncated": true,
  "raw_sha256": "<sha256>",
  "schema": "maf.agent.model_result.v1"
}
```

`stage_ref`只是不可猜测的closed identity，不是文件路径、storage key、URL或Artifact ID。Context Builder不得把receipt marker直接交给模型；必须先经resolver加载完整raw或返回typed failure。

## 8. 完整模型请求与90%预检

### 8.1 计数范围

每次模型采样前先生成与provider请求语义等价的candidate，token估算必须覆盖：

- stable/system rules；
-安全Tool规则；
-完整Tool catalog、description和input schema；
-active context summary；
-所有未compact的durable AgentItem；
-当前user message；
-waiting/continuation必需状态；
-通过transient resolver加载的完整Skill raw；
-provider message/tool framing开销。

不得只按Artifact大小、AgentItem大小、history增长量或字符数单独触发compact。默认scope固定为`total`。

### 8.2 Token estimator

- 优先复用当前Agent model adapter对应的token estimator。
- Provider没有精确tokenizer时使用现有保守estimator；10%余量承担估算误差与输出空间。
- 预检结果记录required/history/transient/tool schema/total计数，但不得记录正文。
- 任何segment只允许在candidate builder中计数一次，避免既计receipt又计resolved raw。

### 8.3 决策

```text
total_tokens <= total_context_limit_tokens
    -> FITS：零compact，直接采样

total_tokens > limit 且存在eligible closed history
    -> HISTORY_COMPACTION_REQUIRED

total_tokens > limit 且不存在eligible history
    -> FATAL_REQUIRED_SEGMENTS_TOO_LARGE
```

## 9. 全局上下文 Compaction

### 9.1 生产接线

`AgentLoopRunner`在`context build -> model sample`之间调用统一preflight。只有返回`HISTORY_COMPACTION_REQUIRED`时才调用`AgentCompactionService.compact_until_fit`；FITS路径不得产生额外模型调用。

### 9.2 Eligible prefix

Compaction继续只覆盖连续、committed、closed的历史前缀：

- 不切断assistant sample与其Tool calls；
- 不切断Tool call与Tool result；
- 不覆盖reserved/waiting/current continuation；
- 不覆盖当前user message和minimum suffix；
- 最新transient Skill result在第一次主Agent采样前是required suffix。

如果后续主Agent没有final而是继续调用Tool，已被至少一次主采样消费的旧result可在其call/result/sample关系闭合后进入普通eligible历史。Compaction prompt通过resolver读取该receipt对应的完整raw；只有summary与compaction boundary原子提交后，才可删除covered stage。

### 9.3 Compaction请求自身预算

现有实现一次发送整个eligible prefix，可能让compaction请求自身超窗。新coordinator必须选择“closed且能在90%预算内完整送入compaction model”的最大前缀；不能切开call/result边界，也不能通过截断source来制造fit。

每次compact commit后：

1. 重读Run与Items；
2. 重新resolve仍required的transient raw；
3. 重建完整candidate；
4. 重新估算total tokens；
5. FITS才进入主模型采样，否则继续下一次全局compact。

相同covered range无进展、summary非法或required segments超限均确定性失败。

## 10. 主模型输入语义

transient resolver复验manifest、call identity、owner/Run、regular file、size和SHA后，把receipt替换为临时model-only Tool result：

```json
{
  "schema": "maf.agent.skill_result_full.v1",
  "result": {"...": "完整原始strict-JSON"}
}
```

- 完整raw不写回AgentItem、Message、Memory、Event或Artifact metadata。
- 主模型看到的Tool call/result关联保持原call ID和顺序。
- raw只在当前provider request内物化；构建失败不调用provider。
- 同一次candidate/retry必须生成逐字节相同的model input。
- 主模型得到完整结果后应优先生成答案；本设计不增加猜测性调用去重或固定轮次上限。

## 11. 原子性、恢复与清理

### 11.1 正常提交顺序

1. executor完成，取得raw strict JSON；
2. private transient stager写临时文件与manifest，fsync且no-clobber，返回closed `stage_ref`而不是`AgentStagedArtifact`；
3. outcome CAS以空`staged_artifacts`原子提交bounded Tool result receipt、terminal Node与Run revision；
4. 重建完整candidate，按90%执行preflight/compact；
5. 主模型sample与final按现有CAS提交；
6. final durable后删除stage与manifest。

不写Artifact row，因此Task Artifact API不出现新`skill_result`。

### 11.2 Crash matrix

| 故障点 | 权威状态 | 恢复动作 |
|---|---|---|
| stage前失败 | result仍reserved | typed stage failure，不公开、不猜测重跑 |
| stage完成、outcome CAS前崩溃 | manifest存在、result reserved、Run recoverable | startup复验stage并提交同一receipt |
| outcome CAS响应丢失 | committed receipt可能已存在 | exact reread winner，不重复执行或重写raw |
| receipt提交后、模型采样前崩溃 | receipt+stage完整 | recovery重新resolve、preflight并采样 |
| compaction model成功、commit前崩溃 |旧boundary仍权威 | 保留stage，重放同一covered range/digest |
| compaction commit后、stage删除前崩溃 | boundary/summary权威 | janitor删除被覆盖stage |
| final commit后、stage删除前崩溃 | final receipt权威 | janitor删除stage，不影响答案 |
| stage缺失/size或SHA漂移 | receipt存在但raw不可信 | `agent_transient_skill_result_unavailable`，禁止重跑Skill |

### 11.3 生命周期

- running/recoverable/waiting/reserved/CAS未决：保留。
- final completed：删除所有未被后续authority需要的transient stages。
- terminal failed/cancelled：删除stages；不删除已有历史Artifact。
- covered by committed compaction boundary：删除对应stage。
- orphan满24小时后，只有Run不可恢复且Task终态/不存在时才删除。
- 任一查询失败、identity不一致、owner不明或raw没有manifest时fail-safe保留并低敏告警；不得只凭文件年龄、名称或权限自动删除无manifest raw。

## 12. Provider context-length error

即使preflight认为FITS，Provider仍可能因tokenizer差异返回context-length error。只允许一次受控收敛：

1. 不重跑Skill；
2. 重读同一Run、Items和staged raw；
3. 重新执行total-context preflight；
4. 有eligible history时全局compact后重试模型采样；
5. 无eligible history时提交`agent_context_required_segments_too_large`。

鉴权、限流、5xx、网络timeout或其他Provider错误不能伪装成context error，也不能借此触发Skill重放。

## 13. 安全与隐私

- stage根目录0700，raw与manifest 0600；拒绝symlink、non-regular file与越界realpath。
- manifest不含用户正文、credential、source path、raw preview或download URL。
- `stage_ref`只在private Agent authority中使用，不进入公共Message、Conversation history、Memory或Frontend DTO。
- raw security scan在stage前完成；含禁止authority值的raw typed invalid，不做redact后继续。
- model request构建日志、audit和metrics禁止记录raw、临时路径、Tool参数或用户正文。
- 不为MCP raw复用此路径；`capability_id=mcp.*`和delegated result在projector入口前闭合分流。

## 14. 错误与可观测性

新增closed错误：

- `agent_transient_skill_result_stage_failed`
- `agent_transient_skill_result_unavailable`
- `agent_context_required_segments_too_large`
- `agent_context_compaction_no_progress`

`agent.result_projected`对新大Skill结果记录`projection_mode=transient_staged`，其`projected_size_bytes`表示durable receipt大小，不伪装成完整model input大小。新增低敏消费事件可记录：capability ID、raw size/SHA、resolved input tokens、total tokens、threshold、compact count、outcome和cleanup outcome。

指标至少覆盖：

- `agent_result_projections_total{projection_mode=inline|transient_staged|...}`；
- `agent_context_preflights_total{decision=fits|compaction_required|required_too_large}`；
- `agent_context_compactions_total{outcome}`；
- `agent_transient_skill_results_total{outcome=staged|injected|covered|cleaned|failed}`。

所有label必须低基数，不使用Skill名以外的动态ID、SHA、路径或error正文。

## 15. 兼容、发布与回滚

### 15.1 兼容

- legacy `skill-result-v1 inline`继续正常渲染。
- legacy `artifact_backed`及其`skill_result` Artifact继续列出、下载和按原janitor规则维护。
- 新writer只为completed ordinary Skill中超出128 KiB且`execution.artifacts`为空的结果写`skill-result-v2 transient_staged`；带业务Artifact的超限结果继续写现有v1 `artifact_backed`。
- 旧reader遇到v2必须fail closed，不能把receipt当最终模型内容；新reader schema-first支持v1/v2。
- 不对历史Task补投、转换或删除Artifact。

### 15.2 发布

- 本设计只授权后续在`main`本地开发环境实施；不授权镜像推送、部署或`prod`修改。
- backend和Runtime Sidecar必须成对构建与发布；Frontend wire合同不变，但本地真实验收使用当前成对Frontend。
- startup顺序保持Agent outcome/run recovery先于transient janitor。
- 发布前确认旧版本无prepared-only/partial authority，并记录在途v1/v2结果计数。

### 15.3 回滚

- 回滚前停止新submission并等待所有`transient_staged` Run final、terminal或被compaction覆盖。
- 旧backend不能恢复在途v2 receipt。
- 回滚不删除v2 AgentItem、manifest或stage；先由新版本恢复收敛。
- 回滚后新Skill大结果回到现有`artifact_backed` safe mode；禁止恢复raw无界AgentItem直通。

## 16. 测试与验收

### 16.1 Pure / repository

- 90% threshold整数边界、非法window和Run内配置漂移。
- Run row创建后、首条user+budget提交前崩溃时，startup必须恢复初始化或fail closed，不能把零Item Run当legacy采样。
- 完整result恰好fit/超过128 KiB envelope的inline/stage边界；超限且无业务Artifact写v2，超限且有业务Artifact保持v1 legacy。
- receipt exact keys、size/SHA、禁止路径/正文泄漏。
- SQLite、PostgreSQL、Runtime Sidecar user+budget初始化和outcome receipt exact replay；transient stage不进入`AgentStagedArtifact`或`commit.staged_artifacts`。
- stage no-clobber、response loss、CAS loser、regular file、size/SHA和owner验证。

### 16.2 Context / compaction

- total candidate覆盖rules/tools/history/current/continuation/full raw且无重复计数。
- total低于90%：compact=0，provider request含完整raw首尾sentinel和digest。
- total超过90%：只compactclosed历史，latest raw逐字节保持。
- compaction request自身fit；不能切开sample/call/result。
- 多次compact后repreflight直到FITS。
- 无eligible历史且required超限：typed failure，provider/Skill调用均不增加。
- Provider context-length误差只触发一次受控repreflight，不重跑Skill。

### 16.3 Recovery / cleanup

- stage后每个写点fault、startup恢复、waiting保留、final/failed/cancelled清理。
- compaction覆盖raw后summary commit与stage删除的response-loss matrix。
- 24小时janitor保留recoverable/reserved/nonterminal/identity drift和无manifest raw。
- legacy v1 Artifact与new v2 transient互不清理。

### 16.4 核心业务

固定28条large Skill fixture：

1. 确定性fixture的`execution.artifacts`为空，只执行一次Skill并写一份private stage，不得为了重新读取同一结果而再次执行；此项只验证完整结果可见性，不建立通用调用次数上限或语义去重要求。
2. 下一模型请求包含28条完整记录，末条唯一sentinel可被final answer准确引用。
3. total未超90%时compaction=0；人为扩大历史超过90%时发生全局compaction，raw仍完整。
4. final completed后stage和manifest均不存在。
5. Task Artifact列表不出现新`skill_result`，只保留正常final output。
6. informational hint仍为zero Skill call。
7. MCP/delegated/waiting/failure回归逐项不变。
8. 同尺寸但带普通业务Artifact的fixture继续生成legacy `artifact_backed`，不写v2 stage。

真实`bioinfo-daily` smoke记录实际result count、raw bytes、估算tokens、threshold、compact次数、Tool call次数和final状态；不得把fixture 28伪称实时数量。

## 17. 完成条件

只有同时满足以下条件才可宣称本设计实现完成：

1. 可放入128 KiB的普通Skill完整结果不再被20k/80KB提前截断；超限且无普通业务Artifact的结果写v2 transient，超限且有业务Artifact的结果保持v1 legacy。
2. 128 KiB durable AgentItem合同及三repository验证继续成立。
3. v2大结果只有private transient stage和bounded receipt，对应`commit.staged_artifacts`为空，无新Artifact metadata/卡片。
4. 下一次主Agent采样在总上下文不超过90%时看到完整raw。
5. 只有完整候选模型输入超过固定Run预算时才compact。
6. Compaction覆盖全局eligible history，不压缩尚未消费的latest result。
7. compact/repreflight循环可恢复且无进展确定性失败。
8. required context超限、stage损坏和Provider context误差均typed收敛，不重跑Skill。
9. final/covered compaction/terminal状态与24小时janitor正确清理stage。
10. old v1 `skill_result`、MCP、delegated、waiting、failure和Frontend合同不回归。
11. 自动全量门禁、真实PostgreSQL/Runtime Sidecar和本地真实Skill smoke有稳定低敏证据。
12. 未修改或部署`prod`。

## 18. 备选方案及结论

### 18.1 删除128 KiB并把raw持久化到AgentItem

拒绝。它同时破坏Python/SQLite/PostgreSQL/Runtime Sidecar closed contract，让大blob进入每次history读取和CAS，并仍不能保证provider context fit。

### 18.2 所有大结果先做result-specific compact

拒绝。主Agent无法看到完整raw，每次增加模型调用，并把全局context compaction错误降级为单结果摘要。

### 18.3 通用Artifact分块读取/Map-Reduce

本期不做。它可支持raw本身超过模型窗口，但需要游标、覆盖证明、多阶段summary和独立恢复状态。当前决策是在required context本身超90%时明确失败，后续有真实超窗样本再另立设计。

### 18.4 采用方案

采用“128 KiB durable receipt + artifact-free private transient raw + 90% total-context preflight + global history compaction + final/covered cleanup”。该方案不新增可恢复Artifact描述sidecar；带普通业务Artifact的超限结果保持legacy，以最小化持久化与安全变更，同时让当前目标中的完整Skill结果真正进入主Agent。

License Requirement：复用现有Python、SQLAlchemy、SQLite/PostgreSQL、Rust/Runtime Sidecar opaque AgentItem、Agent result projector/stager、Context Builder、ModelEdition resolver、Catalog preflight、CompactionService与Artifact store兼容reader；不新增第三方依赖或许可类型。
