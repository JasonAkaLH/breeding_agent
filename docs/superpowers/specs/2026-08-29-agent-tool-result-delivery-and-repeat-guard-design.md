# Agent Tool 结果交付与重复调用熔断设计

状态：`implemented_automated`；document-perfectization第三轮`100/100 Pass`
日期：2026-08-29
目标分支：`main`

## 1. 问题与目标

本地真实会话已经证明，统一 Agent Loop 的 Skill/MCP 重复调用不是前端重复渲染，也不是
recovery replay，而是主模型连续生成了新的 Tool call：

- OCR MCP Task 连续产生 9 个不同 provider call ID 和 9 个 MCP call record；每次调用
  `start_parse_job`，9 份 OCR Markdown 的 SHA-256 完全相同。
- MCP Result Parser 已生成包含 OCR 正文的 20,000-code-point agent projection，原始结果也已
  作为内部 Artifact 持久化；Outer Agent 的 Tool result 却只包含
  `mcp_status/mcp_tool/output_size_bytes`，没有 OCR 正文。
- 文献 Skill Task 连续执行 5 次；5 份完整结果均含相同 28 篇文章，articles canonical
  SHA-256 完全相同。主模型只看到 3,584-byte Artifact preview，并明确表示无法读取 Artifact
  中的论文详情。

根因是结果生产、结果投影和下一轮模型上下文之间的合同断裂：MCP Coordinator 输出
`content`，而 Agent MCP allowlist只接收`text/agent_projection`；带业务Artifact的大Skill结果
固定走legacy Artifact preview，现有 transient full-result resolver不允许同时保留业务
Artifact。Agent Loop同时缺少“同一Run内等价成功调用”的执行前熔断。

本设计完成三个目标：

1. MCP完成结果的模型安全正文必须进入Outer Agent下一轮上下文。
2. 带业务Artifact的大Skill结果必须通过现有owner-bound result Artifact完整注入模型，同时保留
   业务Artifact和既有crash recovery边界。
3. 同一AgentRun内，已经成功完成的`capability_id + canonical arguments_json`不得再次进入
   Executor；新的provider call必须复用既有模型结果。失败、waiting、明确可重试结果、不同参数
   和不同AgentRun不熔断。

### 1.1 受影响对象与成功边界

直接受影响的是使用统一Agent Loop调用Skill/MCP的本地业务用户，以及负责Agent结果投影、上下文
组装、调用执行和Artifact authority的后端模块。用户价值是一次成功调用后即可得到真实结果，避免
重复等待、重复外部成本和重复副作用。

本设计不承诺语义相似参数去重：不同canonical参数仍允许执行。已观察到的MCP循环由精确参数熔断
确定性阻止；已观察到的Skill自然语言改写循环主要由完整结果正确交付消除，并以真实本地smoke验证。

## 2. 方案选择

采用方案A：修复结果边界并增加durable reuse guard。

未采用的方案：

- 只增加prompt规则：模型看不到正文时仍可能继续调用，不能形成确定性安全边界。
- 新增`artifact.read` Tool：扩大Tool权限、调用轮次和错误面，仍不能阻止重复副作用。
- 只增加固定最大调用次数：只能限制损失，不能交付结果，也不能在第二次调用前阻止副作用。

## 3. MCP结果合同

### 3.1 Canonical模型字段

`mcp.dispatch`完成结果统一使用现有Agent allowlist支持的`text`字段：

- OCR固定工作流把`MCPCallOutcome.external_text`写入`text`，不再使用不被projector消费的
  `content`。
- 普通MCP branch完成时，把Selector基于typed completed projections生成的`safe_summary`
  写入`text`。
- `mcp_status`、`mcp_tool`、`output_size_bytes`和external-content notice继续保留。
- raw/structured business result、result storage ref、projection path、credential和内部authority不进入
  Outer Agent Tool result。

`AgentCallResultProjector._mcp()`继续只允许closed model keys，并沿用20,000 code points、80,000
bytes和完整Tool result 128 KiB上限；`text`超限沿现有确定性收缩规则处理。MCP raw result和
published typed projection继续由现有durable Artifact/authority链保存，不复制进AgentItem。

### 3.2 数据流

```text
MCP Gateway validated terminal result
  -> Result Parser typed agent projection
  -> Selector / OCR workflow生成model-safe external text
  -> MCPDispatchOutcome.output_payload["text"]
  -> AgentCallResultProjector MCP allowlist
  -> committed Tool result model_view.text
  -> 下一次AgentModelRequest tool message
```

任一环节无法形成安全正文时，返回现有typed状态/错误或安全摘要；不得读取raw Artifact作为
fallback，也不得重新调用MCP来补偿投影失败。

## 4. 带业务Artifact的Skill完整结果

### 4.1 投影策略

新AgentRun已持久化合法`AgentContextBudget`时，普通completed Skill继续采用现有双路径：

1. 完整结果能通过128 KiB Tool-result preflight时直接inline。
2. 完整结果超限且没有业务Artifact时继续使用现有private transient full-result路径。
3. 完整结果超限且存在业务Artifact时继续使用现有`artifact_backed` durable结果，不改变
   `2026-08-28-agent-skill-transient-full-result-context-compaction-design.md`的crash recovery边界；
   新增严格、model-only的Skill Result Artifact resolver读取完整结果。

业务Artifact与result transport是两个独立维度：

- `execution.artifacts`和deterministic `agent-skill-result` Artifact继续由现有Agent outcome CAS原子
  发布并出现在Tool result `artifact_refs`；不把Artifact storage ref写入AgentItem。
- `AgentContextCandidateBuilder`只为当前候选中引用的deterministic `agent-skill-result` ID，从现有
  Storage预加载owner-bound Artifact记录；不得扫描Task全部Artifact或读取普通业务Artifact。
- model-only resolver严格复验Run/Task/Conversation/Node/call、deterministic Artifact ID、closed
  `skill_result` metadata、active retention、regular-file owner/mode/link、size和SHA，再从现有
  `LocalArtifactFileStore`读取canonical raw JSON。
- Context Builder只在本次model Tool message中把artifact-backed preview替换为
  `maf.agent.skill_result_full.v1`完整结果，并原样保留Tool result中的Artifact IDs；durable AgentItem、
  Artifact metadata和公开API不变。
- context preflight把尚未被后续assistant sample消费的full artifact-backed结果与transient结果同样
  计入required context，保证完整结果在第一次下一轮采样前不被compaction丢弃且只计一次。

Legacy Run没有`AgentContextBudget`时继续只发送现有安全preview，避免隐式改变历史Run上下文规模。
Transient recovery/cleanup/janitor和Skill Result Artifact publish/janitor顺序均保持不变。

### 4.2 安全边界

- 不把普通业务Artifact内容自动拼进模型上下文；resolver只允许读取同一Tool result绑定的
  deterministic `agent-skill-result`结构化结果。
- 现有forbidden raw key、secret assignment、canonical JSON、复杂度和total-context preflight保持不变。
- Artifact缺失、inactive、metadata/文件/size/SHA漂移或model preflight失败时，在provider调用前以
  `agent_skill_result_artifact_unavailable` fail closed，不回退为再次执行Skill，也不读取raw路径。
- 不增加数据库schema、Artifact类型、公开下载权限或外部Skill合同。

## 5. 同一AgentRun重复成功调用熔断

### 5.1 等价身份

熔断键为当前AgentRun内：

```text
capability_id + NUL + canonical arguments_json
```

`AgentToolCall`已在构造时把arguments解析并canonical JSON化，因此不做模糊文本、键顺序、自然
语言或结果内容比较。候选必须同时满足：

- 先前Tool call属于同一Run且sequence更早；
- capability ID和canonical arguments JSON完全一致；
- 对应Tool result为committed；
- outcome为completed且`safe_error_code is None`；
- 不是waiting、input-required、cancelled、failed、unknown-side-effect或reserved结果。

候选若本身是合法reuse receipt，Invoker必须先解引用到更早的root executed result；新receipt始终直接
绑定root，不形成receipt链。root必须是同Run更早的committed completed非reuse结果；循环、后向引用、
payload SHA冲突或receipt-of-receipt未解引用均fail closed。

不同Run永不复用。不同参数允许执行。先前失败或未完成调用允许沿现有策略执行；是否重试继续由
现有retriable/模型决策控制。

### 5.2 执行前熔断与模型结果复用

`AgentCapabilityInvoker`在进入`CapabilityInvocationService/Executor`前查找最新等价成功结果。
该查询只适用于Runner提交的新鲜reserved non-waiting call；`resume()`处理的是已经建立
Interrupt/remote/waiting authority的同一逻辑调用，必须显式绕过reuse lookup并继续原authority，不能被更早
的等价成功结果截断。
命中后：

1. 该路径是Agent Tool call resolution，复用现有delegated activation不进入Capability Invocation的
   边界；不调用Skill/MCP/外部服务，不产生第二份业务Artifact。
2. 当前sample commit已经创建pending TaskNode。为当前provider call提交一个bounded receipt：
   `schema=maf.agent.tool_result_reuse_receipt.v1`、`source_result_item_id`和
   `source_result_payload_sha256`，其中source始终是root executed result，不保存参数或source正文。
3. Context Builder渲染当前Tool message时，严格校验source result仍属于同一Run、早于当前call、
   committed/completed且identity等价；随后复用source result的model-effective payload：inline直接
   使用，active artifact-backed通过第4节resolver解析，未清理transient通过现有resolver解析。Context
   authority lookup使用完整有序Run items而不是仅使用compaction后的visible items；当前model-only消息保留
   source result的Artifact IDs，但当前durable reuse receipt的`artifact_refs`保持为空。
4. 当前Tool message使用新的provider call ID，满足四角色Tool协议；模型看到的是既有有效结果，
   不是伪造的新执行结果。
5. Agent outcome CAS把当前pending TaskNode直接置为completed并产生唯一既有terminal event；因为没有
   Capability execution，`assigned_instance_id`和`started_at`保持空。
6. 运行时观测复用现有`agent.result_projected`，新增closed `projection_mode=reused`，这是本范围内必需的
   runtime证据。已有`AgentMetricsRecorder`被调用方配置时，Tool call metric复用closed
   `outcome=duplicate`；当前API runtime没有配置该recorder，本设计不新增生产metric sink或wiring。
   不增加新事件类型；event payload和可选metric labels不记录参数、正文、Artifact ref、业务ID或digest，
   沿用EventRecord现有Task/Node envelope。

Reuse receipt本身不得复制private stage ref、raw正文、Artifact storage ref或外部结果。若source
authority或transient payload已不可用，返回typed `agent_reused_tool_result_unavailable`并禁止外部
补偿调用，避免可能重复副作用。

### 5.3 重启、Compaction与清理

- 熔断候选只依赖durable Agent call/result items。reuse receipt已提交但响应丢失时，现有outcome CAS
  必须exact replay同一receipt。
- sample/call已提交但reuse receipt提交前崩溃时，不扩展Recovery Coordinator重建新receipt；沿用
  现有reserved non-waiting no-replay路径把当前duplicate call收敛为aborted，且不得调用Executor。
  先前成功结果仍保留在Run上下文。
- 未被compaction覆盖的source result按上述规则完整复用。
- artifact-backed source即使已被compaction覆盖，只要owner-bound Artifact authority仍active，仍可按
  第4节完整解析并重新进入required context。
- 已覆盖且private transient stage已清理的source没有durable preview。此时当前Tool message只返回
  bounded `duplicate_call_suppressed/context_summary_only`，要求模型使用现有Context Summary；不得
  伪称重新注入完整结果，也不允许为获取详情重新执行等价调用。
- Reuse receipt不拥有source Artifact或transient stage，final/cleanup仍由原source result authority执行
  一次，不增加引用计数或清理顺序。

## 6. 错误处理与并发

- 同一AgentRun由现有lease/ownership和atomic writer串行化call/result提交；reuse lookup后仍由当前
  lease提交结果，不新增进程锁。
- Runner在每个deterministic wave内先按熔断键分组。各组ordinal最前的leader与所有不同键leader
  可以继续并行执行；followers本轮不进入`asyncio.gather`。
- leader outcomes按现有ordinal顺序提交后才处理followers。leader成功则followers从已提交result
  生成reuse receipt；只有leader明确failed时followers才按ordinal依次走普通调用路径；leader
  aborted时followers以`duplicate_call_leader_aborted`收敛，leader进入waiting时followers以
  `duplicate_call_leader_waiting`收敛，两者均typed aborted且零Executor调用。Run只保留leader
  waiting/unknown authority。每个后续follower都只能观察已经durable提交的最新状态，不得并行派发
  两个等价外部调用。
- receipt提交后的response loss或CAS replay必须得到同一receipt；提交前crash沿第5.3节现有
  no-replay abort收敛，payload冲突继续fail closed。
- cancel在winner完成前发生时，后续duplicate不得执行或reuse为成功；沿现有cancel终态收敛。
- 不放宽MCP no-replay、Tool approval、Skill pinning、owner scope、lease fencing或Artifact authority。

## 7. 测试与验收

### 7.1 MCP

1. Coordinator真实OCR输出经过`AgentCallResultProjector`后，`model_view.text`仍包含OCR sentinel。
2. 普通MCP completed summary进入Outer Agent Tool message。
3. raw/structured/result ref/credential不进入model view，现有大小门禁保持。
4. 端到端fixture证明一次MCP调用后下一次model request包含正文，不生成第二个相同call。

### 7.2 Skill

1. 大型Skill结果同时带业务Artifact时继续形成artifact-backed preview，不进入transient路径。
2. durable result继续形成artifact-backed preview并保留业务Artifact与result Artifact ID，不含raw
   正文/path/storage ref。
3. 下一次model request通过validated Artifact resolver同时包含完整结果首尾sentinel和Artifact IDs；
   最新结果计入required context且只计一次。
4. Artifact missing/inactive/metadata/file/size/SHA drift在provider前fail closed且不重跑Skill。
5. restart、compaction、final cleanup、Artifact janitor、transient路径和legacy Run兼容测试保持通过。

### 7.3 Reuse guard

1. 相同Run、capability和canonical args的第二次成功调用不进入Executor。
2. 新Tool message复用inline、artifact-backed、transient和legacy source result的model-effective
   payload。
3. 历史failed/waiting结果不是reuse candidate；不同参数和不同Run均不误复用。
4. 同batch两个等价call按leader/follower两阶段运行，同一键最多一个外部调用并发；leader明确失败
   后followers才允许按ordinal顺序进入普通路径。
   leader waiting时followers typed aborted且不创建第二个Interrupt、remote Task或外部调用。
5. receipt提交后的response-loss exact replay；提交前restart沿现有no-replay abort且零Executor调用；
   source authority漂移返回typed unavailable。
6. 新增`agent.result_projected`观测payload不包含参数、正文、业务ID、digest或Artifact ref，并继续使用
   现有EventRecord Task/Node envelope；可选`AgentMetricsRecorder`配置存在时，`outcome=duplicate`只使用
   closed labels且同样不含上述内容。不新增生产metric sink或wiring。
7. 连续三次等价调用的两个reuse receipts都直接绑定同一root executed result；循环、后向引用和
   receipt链被拒绝。

验证顺序为聚焦红绿测试、Orchestration/Capabilities/MCP/API相关回归、Backend分层全量、compileall、
Ruff和`git diff --check`。最终本地真实smoke必须分别创建新的OCR MCP Task和带大结果的文献Skill
Task，证明真实正文进入下一model request、Task最终回答基于正文且每个Task实际外部调用数均为1。
若生产代码未涉及Frontend、Rust或schema，不要求修改这些层；相关公共合同测试仍必须通过。

## 8. 非功能要求、依赖与风险

- 性能：每次reuse lookup只扫描本Run已加载的Agent items一次；Artifact预加载只查询当前候选引用的
  deterministic result Artifact，不扫描Task或Conversation全部Artifact，不增加网络请求。
- 可靠性：任何reuse/Artifact authority不确定性均在Executor/provider前fail closed；不以重复外部调用
  作为fallback。
- 安全与隐私：保持128 KiB durable结果、90% total-context、owner scope、regular-file和SHA门禁；
  model-only正文不得写入event、metric、audit或新的durable payload。
- 兼容性：无`AgentContextBudget`的legacy Run保持preview；数据库schema、Sidecar proto、API和前端
  合同不变。
- 依赖：复用Agent repository/atomic writer、Context Candidate Builder、现有transient resolver、
  `AgentSkillResultArtifactStager` metadata/parser、Storage Artifact查询与`LocalArtifactFileStore`。
- 已确认产品取舍：同Run不同canonical参数允许执行，因此不做自然语言相似度去重；真实Skill防循环
  主要依赖完整结果交付。若模型在真实smoke中仍改写参数重复调用，本设计不得以语义去重或任意调用
  上限扩张补救，必须返回重新评审。

部署不做数据迁移。仅在当前本地Task全部终态后重启backend并创建新Task验证；不复活、改写或迁移
旧Task，不部署`prod`。回滚恢复旧MCP字段、关闭model-only Artifact resolver和reuse guard即可，
既有durable数据继续可读。

## 9. 实施范围与非目标

预期生产修改限定为：

- `src/integrations/mcp/dispatch_coordinator.py`
- `src/orchestration/agent_loop/result_projection.py`
- `src/orchestration/agent_loop/result_artifacts.py`
- `src/orchestration/agent_loop/capability_invoker.py`
- `src/orchestration/agent_loop/context.py`
- `src/orchestration/agent_loop/context_preflight.py`
- `src/orchestration/agent_loop/runner.py`
- `src/orchestration/agent_loop/observability.py`
- `src/api/runtime.py`中的现有resolver/storage装配

明确不修改：

- 外部Skill仓、MCP Server实现、MCP协议/Selector/Gateway/Result Parser authority；
- 前端、API DTO、数据库schema、Rust/Runtime Sidecar proto、模型配置或`prod`；
- Artifact公开下载策略、用户历史数据、旧失败Task或当前运行Task；
- 通用`artifact.read` Tool、模糊参数相似度、跨Run缓存、TTL缓存或调用结果数据库。

## 10. 文档、回滚与完成声明

- 实施计划按MCP正文、Skill Result Artifact model-only resolver、reuse guard、端到端证明四个可回滚
  检查点拆分。
- 实施完成后更新本设计状态、`docs/AGENTS.md`索引与`CHANGELOG.md`验证账本；模块职责或入口未
  变化时不修改其他`AGENTS.md`。
- 回滚按检查点恢复旧MCP字段、关闭Skill Result Artifact resolver和reuse guard；不改变旧Skill投影
  选择，无schema或数据回滚。
- 只有真实Coordinator→projector→Context链包含结果正文，带Artifact大Skill完整注入，以及第二次
  等价成功call零Executor调用三项同时通过，才可声明完成。

License Requirement：复用现有Python、Agent Loop、MCP typed result projection、Skill Result Artifact、
private transient store、Artifact authority和atomic writer；无新增依赖或许可变化。
