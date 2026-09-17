# Agent Tool 结果交付与重复调用熔断实施计划

依据：`2026-08-29-agent-tool-result-delivery-and-repeat-guard-design.md`
设计提交：`7c097cf0`；审查加固提交：`c03c14f2`
状态：`implemented_automated`；document-perfectization第二轮`100/100 Pass`
目标分支：`main`

## 1. 完成声明

本计划只修复三个已经由真实本地会话证明的硬伤：

1. MCP安全正文进入Outer Agent下一轮Tool message；
2. 带业务Artifact的大Skill结果通过现有owner-bound result Artifact完整进入模型上下文；
3. 同一AgentRun内精确等价的已成功Tool call在Executor前复用，并保证同batch同键最多一个外部调用并发。

完成必须同时满足：Coordinator→Projector→Context正文链通过、artifact-backed Skill完整结果注入通过、
第二次精确等价成功call的Executor调用数为0。不同canonical参数仍允许执行；不增加语义去重、通用
`artifact.read`、跨Run缓存、任意调用上限、schema、API/前端/Rust改造或外部Skill/MCP修改。

每个Checkpoint均按“聚焦红测→最小生产修改→相关回归→diff审查→独立commit”推进。前一Checkpoint
未green时不得进入后一Checkpoint。

## 2. Checkpoint A：MCP model-safe `text`

### 2.1 先写红测

修改：

- `tests/integrations/mcp/test_dispatch_coordinator.py`
- `tests/orchestration/test_agent_result_projection.py`
- `tests/e2e/test_mcp_server_explicit_agent_loop.py`

红测锁定：

1. 现有OCR workflow完成结果必须把首尾sentinel放在`output_payload["text"]`，不再依赖`content`；
2. Coordinator输出经过`AgentCallResultProjector.project()`后，`model_view.text`仍含同一sentinel；
3. 在现有显式MCP Agent Loop E2E中捕获下一次model request，证明committed Tool result的
   `model_view.text`进入当前provider call对应的Tool message，且fake Gateway实际Tool调用数为1；
4. ordinary MCP completed branch把Selector生成的`safe_summary`放入`text`；
5. raw result、structured result、storage ref、projection path和credential不进入model view；
6. 现有20,000 code points、80,000 bytes与128 KiB Tool-result门禁不变。

### 2.2 最小实现

生产修改限定为：

- `src/integrations/mcp/dispatch_coordinator.py`

具体步骤：

1. OCR固定workflow把已验证的`MCPCallOutcome.external_text`写入canonical外层`text`；
2. ordinary completed branch把现有Selector `safe_summary`写入`text`；
3. 保留现有`mcp_status`、`mcp_tool`、`output_size_bytes`和external-content notice；
4. 不从raw Artifact补读正文，不改变Gateway、Selector、Result Parser或MCP durable authority。

`src/orchestration/agent_loop/result_projection.py`预期只增加/调整合同测试；当前`_mcp()`已允许`text`，
除非红测证明现有closed allowlist确有缺口，否则不修改生产逻辑。

### 2.3 门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_dispatch_coordinator \
  tests.orchestration.test_agent_result_projection \
  tests.e2e.test_mcp_server_explicit_agent_loop
conda run -n multi_agent ruff check \
  src/integrations/mcp/dispatch_coordinator.py \
  tests/integrations/mcp/test_dispatch_coordinator.py \
  tests/orchestration/test_agent_result_projection.py \
  tests/e2e/test_mcp_server_explicit_agent_loop.py
git diff --check
```

Implementation commit：`fix(mcp): deliver model-safe result text`

回滚该commit只恢复旧MCP外层字段；不涉及数据或schema回滚。

## 3. Checkpoint B：Skill Result Artifact model-only注入

### 3.1 先写红测

修改现有测试，不新增测试框架：

- `tests/orchestration/test_agent_result_artifacts.py`
- `tests/orchestration/test_agent_context_builder.py`
- `tests/orchestration/test_agent_context_preflight.py`
- `tests/orchestration/test_agent_loop.py`
- `tests/orchestration/test_agent_result_projection.py`
- `tests/api/test_skill_output_artifacts.py`
- `tests/e2e/test_skill_soft_binding.py`

红测必须覆盖：

1. 新Run的大Skill结果同时带业务Artifact时仍走`artifact_backed`，不改成transient；
2. durable Tool result仍只保存preview、业务Artifact ID和deterministic result Artifact ID，不保存raw正文、
   文件路径或storage ref；
3. 下一次provider request把preview替换为`maf.agent.skill_result_full.v1`完整结果，同时保留原Artifact IDs；
4. 完整结果首尾sentinel、canonical JSON和SHA一致；最新未消费结果在preflight中作为required context只计一次；
5. missing/inactive、跨Run/Task/Conversation/Node/call、错误deterministic ID、closed metadata漂移、
   非regular/private文件、link/mode/owner、size/SHA及JSON漂移均在provider前返回
   `agent_skill_result_artifact_unavailable`；Executor/Skill调用数保持0；
6. legacy Run无`AgentContextBudget`时继续只给preview；inline、transient、restart、compaction、final cleanup和
   janitor现有行为不变；
7. Candidate Builder只查询当前候选引用的deterministic result Artifact ID，不做Task/Conversation级scan，
   不读取普通业务Artifact。

### 3.2 严格resolver

生产修改：

- `src/orchestration/agent_loop/result_artifacts.py`
- `src/orchestration/agent_loop/context.py`
- `src/orchestration/agent_loop/context_preflight.py`
- `src/orchestration/agent_loop/runner.py`
- `src/api/runtime.py`中的现有Storage/file-store装配

具体步骤：

1. 在`result_artifacts.py`增加单一model-only resolver，复用现有
   `parse_skill_result_storage_ref()`、`skill_result_artifact_id()`和`LocalArtifactFileStore`；
2. resolver接收当前Run、call/result item与已预加载Artifact记录，逐项验证owner identity、metadata、
   retention、deterministic ID、文件安全属性、size/SHA及canonical JSON；
3. Artifact authority不确定时统一抛出typed `agent_skill_result_artifact_unavailable`，不得返回preview后
   继续采样，也不得重新执行Skill；
4. 不修改`AgentSkillResultArtifactStager`发布格式、Artifact类型、公开下载合同或janitor顺序。

### 3.3 Candidate/Context/Preflight接入

1. `AgentContextCandidateBuilder`从当前candidate items识别直接artifact-backed结果，只对其中精确引用的
   deterministic result Artifact ID调用现有Storage `get_artifact()`；查询结果形成一次build内的有界map；
   Checkpoint B不得解析尚未定义的reuse receipt；
2. `AgentContextBuilder.build()`只在provider-bound Tool message中调用resolver，把合法preview替换为full
   envelope并保留Artifact IDs；durable AgentItem和API/history投影保持原样；
3. `AgentContextCandidateBuilder`以同一份已解析request做token preflight，避免resolver二次读取和重复计数；
4. `context_preflight`把最新未被后续assistant sample消费的full artifact-backed结果按现有transient
   required-context规则计入一次；legacy preview不进入该新规则；
5. Runner在model sample前收敛resolver的typed unavailable错误，以同一safe error失败当前Run；不得调用
   provider、不得回到pending call执行，也不得把错误误报成context-too-large；
6. `src/api/runtime.py`只把现有`storage.get_artifact`和`LocalArtifactFileStore`注入resolver/Candidate
   Builder，不新增runtime、配置或后台任务。

### 3.4 门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_result_artifacts \
  tests.orchestration.test_agent_context_builder \
  tests.orchestration.test_agent_context_preflight \
  tests.orchestration.test_agent_loop \
  tests.orchestration.test_agent_result_projection \
  tests.api.test_skill_output_artifacts \
  tests.e2e.test_skill_soft_binding
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_transient_results \
  tests.orchestration.test_agent_compaction
conda run -n multi_agent ruff check \
  src/orchestration/agent_loop/result_artifacts.py \
  src/orchestration/agent_loop/context.py \
  src/orchestration/agent_loop/context_preflight.py \
  src/orchestration/agent_loop/runner.py \
  src/api/runtime.py \
  tests/orchestration/test_agent_result_artifacts.py \
  tests/orchestration/test_agent_context_builder.py \
  tests/orchestration/test_agent_context_preflight.py
git diff --check
```

Implementation commit：`fix(agent): inject validated skill result artifacts`

回滚该commit关闭model-only resolver并恢复preview；既有Artifact和durable结果仍可读，无数据回滚。

## 4. Checkpoint C：精确成功结果复用与同batch leader/follower

### 4.1 先写红测

修改：

- `tests/orchestration/test_agent_invocation.py`
- `tests/orchestration/test_agent_context_builder.py`
- `tests/orchestration/test_agent_context_preflight.py`
- `tests/orchestration/test_agent_loop.py`
- `tests/orchestration/test_agent_result_projection.py`
- `tests/observability/`中现有Agent观测测试

红测矩阵：

1. 同Run、同`capability_id + NUL + canonical arguments_json`的第二次成功call零Executor调用；
2. 不同参数、不同Run、先前failed/waiting/reserved/aborted结果仍不复用；
3. 当前call已经处于waiting并由`resume()`继续时，即使更早存在等价成功result，也必须继续当前
   continuation authority且不得生成reuse receipt；
4. receipt exact keys只有`schema/source_result_item_id/source_result_payload_sha256`，不包含参数、正文、
   Artifact ref、storage ref或private stage ref；
5. inline、active artifact-backed、live transient和legacy source均生成正确的当前provider call Tool message；
   active artifact-backed root无论visible或已被compaction覆盖都必须完整解析。model-only消息保留source
   Artifact IDs，当前durable receipt的`artifact_refs`仍为空；
6. covered且已清理transient source只返回bounded
   `duplicate_call_suppressed/context_summary_only`，不触发外部补偿调用；
7. reuse source的Artifact authority失效时返回`agent_reused_tool_result_unavailable`，而直接
   artifact-backed结果失效仍返回`agent_skill_result_artifact_unavailable`；两者均发生在provider前；
8. 连续三个等价call的两个receipt都直接指向root executed result；循环、forward ref、receipt chain和
   payload SHA漂移fail closed；
9. receipt提交后的response loss exact replay；receipt提交前crash继续沿现有reserved non-waiting
   no-replay abort，Executor调用数为0；不改Recovery Coordinator；
10. 当前duplicate call的pending TaskNode由现有outcome CAS完成，`assigned_instance_id/started_at`为空，
   terminal event仍唯一；
11. 运行时观测只增加现有`agent.result_projected`的closed `projection_mode=reused`；已有可选
    `AgentMetricsRecorder`被配置时验证`outcome=duplicate` closed label。两者均不写参数、正文、业务ID、
    Artifact ref或digest，不新增生产metric sink或wiring。

### 4.2 Invoker执行前复用

生产修改：

- `src/orchestration/agent_loop/result_projection.py`
- `src/orchestration/agent_loop/capability_invoker.py`
- `src/orchestration/agent_loop/context.py`
- `src/orchestration/agent_loop/context_preflight.py`
- `src/orchestration/agent_loop/observability.py`

具体步骤：

1. 在现有结果合同模块集中定义repeat key、receipt exact schema和严格parser；不新建通用缓存/registry；
2. repeat lookup只允许Runner的新鲜reserved non-waiting call路径；`AgentCapabilityInvoker.resume()`必须
   显式绕过lookup并继续当前waiting/continuation authority。实现使用一个明确的内部fresh-call开关即可，
   不新增第二套Invoker或恢复协议；
3. fresh `AgentCapabilityInvoker.invoke()`在delegated activation与
   `CapabilityInvocationService/Executor`之前，从同Run已加载items查找更早的committed completed无
   safe error候选；
4. 若候选是reuse receipt，严格解引用到更早root executed result；新receipt始终直接保存root result item ID
   和root payload SHA；authority冲突返回`agent_reused_tool_result_unavailable`且零Executor；
5. 命中时返回普通`AgentCallExecution(COMPLETED)`承载bounded receipt，让现有
   `commit_agent_call_outcome()`完成当前result和TaskNode；不调用Invocation Service/Executor，不复制业务
   Artifact，也不改atomic writer；
6. reuse receipt合同就绪后，`AgentContextCandidateBuilder`才识别当前visible receipt并严格解引用到完整有序
   Run items中的root；只对root精确引用的deterministic result Artifact ID预加载，仍不得扫描Task或Run外
   authority；
7. Context Builder把`visible_items`仅用于决定输出消息，把完整`ordered_items`作为source/root authority；
   验证当前call与root identity后复用root model-effective payload：inline直接使用、artifact-backed调用
   Checkpoint B resolver、transient调用现有resolver、已清理transient使用summary-only。当前model-only
   Tool message保留source `artifact_refs`，当前durable receipt不复制这些refs；
8. reuse路径捕获Checkpoint B resolver/source authority失败并转换为
   `agent_reused_tool_result_unavailable`；直接artifact-backed路径继续使用原错误码；
9. reuse结果是否为required context沿当前未消费Tool result规则计算一次，不新增独立compaction语义；
10. `observability.py`只扩展closed枚举`reused -> error_code None`；reused observation的raw digest为空。
    现有observer继续生成runtime audit event；仅在调用方已配置`AgentMetricsRecorder`时记录closed
    `outcome=duplicate`，不修改`src/api/runtime.py`增加生产metric sink或wiring。

### 4.3 Runner同batch两阶段调度

生产修改：

- `src/orchestration/agent_loop/runner.py`

具体步骤：

1. 每个现有deterministic wave按repeat key分组，ordinal最小项为leader；
2. 所有不同键leader继续使用一次`asyncio.gather`并行执行；同键followers不进入该gather；
3. leaders按原wave ordinal顺序逐个提交durable outcome与terminal event；
4. leader completed后，followers按ordinal依次调用Invoker并只产生reuse receipt；
5. leader明确FAILED时，followers才按ordinal依次走普通调用路径；每个follower提交后再处理下一个；
   若某个follower成功，剩余followers必须通过Invoker观察该已提交root并转为reuse；
6. leader ABORTED时followers提交typed `duplicate_call_leader_aborted`，leader WAITING时提交typed
   `duplicate_call_leader_waiting`；两者均零Executor，且只有leader保留waiting authority；
7. 每次follower处理前重新读取当前Run/items，保证只观察已提交authority；cancel和lease fencing继续沿
   现有路径；
8. 不修改`deterministic_invocation_waves()`、Capability invocation kernel、Recovery Coordinator或
   MCP no-replay合同。

### 4.4 门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_invocation \
  tests.orchestration.test_agent_context_builder \
  tests.orchestration.test_agent_context_preflight \
  tests.orchestration.test_agent_loop \
  tests.orchestration.test_agent_result_projection
conda run -n multi_agent python -m unittest discover \
  -s tests/observability -p 'test_*.py'
conda run -n multi_agent ruff check \
  src/orchestration/agent_loop/result_projection.py \
  src/orchestration/agent_loop/capability_invoker.py \
  src/orchestration/agent_loop/context.py \
  src/orchestration/agent_loop/context_preflight.py \
  src/orchestration/agent_loop/runner.py \
  src/orchestration/agent_loop/observability.py \
  tests/orchestration/test_agent_invocation.py \
  tests/orchestration/test_agent_loop.py
git diff --check
```

Implementation commit：`fix(agent): reuse exact completed tool results`

回滚该commit恢复逐call执行；Checkpoint A/B仍可独立保留。无schema、数据或外部服务回滚。

## 5. Checkpoint D：集成回归、真实本地验收与文档闭合

### 5.1 自动回归

先运行三个Checkpoint全部聚焦测试，再执行Backend分层门禁：

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
conda run -n multi_agent ruff check \
  src/integrations/mcp/dispatch_coordinator.py \
  src/orchestration/agent_loop/result_projection.py \
  src/orchestration/agent_loop/result_artifacts.py \
  src/orchestration/agent_loop/capability_invoker.py \
  src/orchestration/agent_loop/context.py \
  src/orchestration/agent_loop/context_preflight.py \
  src/orchestration/agent_loop/runner.py \
  src/orchestration/agent_loop/observability.py \
  src/api/runtime.py
git diff --check
```

验收Backend公共合同未变化时，不修改Frontend、Rust、API DTO或schema；最终diff必须显式确认这些范围
无生产变更。任何环境性skip按现有门禁规则单独记录，不得宣称通过未运行的检查。

### 5.2 新Task真实本地smoke

只有当前本地运行Task全部进入终态后，才按现有开发流程重建/重启backend。不得复活、编辑、删除或
重跑旧Task，不部署`prod`。

创建两个全新Task：

1. OCR MCP Task：使用现有真实附件和显式Server route；检查Coordinator结果`text`、下一model request
   Tool message与最终回答都含脱敏sentinel，MCP durable call/external call计数均为1；
2. 文献Skill Task：产生超过128 KiB且带业务Artifact的真实结果；检查durable result仍是
   `artifact_backed`，下一model request含完整结果首尾sentinel与Artifact IDs，Skill实际执行计数为1。

如果Skill在正文已完整进入上下文后仍以不同canonical参数重复调用，停止实施完成声明并回到设计评审；
不得现场增加语义相似度、调用次数上限或prompt workaround。

### 5.3 文档闭合

真实smoke和全部自动门禁通过后：

1. 把设计状态更新为`implemented_local`，本计划更新为`complete_local`；
2. 同步`docs/AGENTS.md`与`CHANGELOG.md`，记录各Checkpoint commit、测试计数、skip和真实调用计数；
3. 检查本次修改没有改变模块职责或目录入口；若没有，不修改其他`AGENTS.md`；
4. 最终文档commit：`docs(agent): close tool result delivery guard`。

若真实smoke因用户附件、外部Server或本地环境不可用而未运行，只可标记`implemented_automated`，必须
明确保留验证缺口，不得标记complete。

### 5.4 实施结果（2026-08-29）

- Checkpoint A、B、C分别由`f165e303`、`381ad371`、`132e9f82`完成；实现范围与本计划一致。
- 聚焦验证分别通过47项、76项和82项；Backend分层回归共2,310项通过、15项环境性skip，compileall、
  Ruff和`git diff --check`通过。
- 本地`breeding-agent-a7-local`仅重建backend并恢复healthy；frontend、Runtime Sidecar、旧Task和
  `prod`均未修改。
- 两个全新真实Task尚未发送：浏览器中的新消息会触发真实LLM、MCP或Skill调用，等待用户在执行当下
  确认。当前状态因此保持`implemented_automated`，不得宣称`complete_local`。

## 6. 回滚顺序

按commit逆序回滚：

1. 回滚Checkpoint C，关闭reuse与leader/follower，恢复逐call执行；
2. 回滚Checkpoint B，关闭model-only Artifact resolver，恢复artifact-backed preview；
3. 回滚Checkpoint A，恢复旧MCP外层字段。

回滚不删除Artifact、transient stage、AgentItem、Task或旧会话，不需要数据库迁移；现有durable authority
继续按原生命周期清理。任何回滚后都需重跑对应聚焦测试并只重建backend。

## 7. 范围审计

允许的生产文件只有：

- `src/integrations/mcp/dispatch_coordinator.py`
- `src/orchestration/agent_loop/result_projection.py`
- `src/orchestration/agent_loop/result_artifacts.py`
- `src/orchestration/agent_loop/capability_invoker.py`
- `src/orchestration/agent_loop/context.py`
- `src/orchestration/agent_loop/context_preflight.py`
- `src/orchestration/agent_loop/runner.py`
- `src/orchestration/agent_loop/observability.py`
- `src/api/runtime.py`中的现有装配

不允许修改外部Skill仓、MCP Server、Gateway/Selector/Result Parser authority、数据库schema、API DTO、
Frontend、Rust/Runtime Sidecar、模型配置、用户历史、旧Task或`prod`；不新增依赖。

License Requirement：复用现有Python、Agent Loop、MCP typed projection、Skill Result Artifact、
private transient store、Artifact authority、Context Builder和atomic writer；无新增依赖或许可变化。
