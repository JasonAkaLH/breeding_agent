# Agent Tool 结果交付与重复调用熔断设计

状态：`approved_design`
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
2. 带业务Artifact的大Skill结果也必须通过private transient路径完整注入模型，同时保留业务
   Artifact。
3. 同一AgentRun内，已经成功完成的`capability_id + canonical arguments_json`不得再次进入
   Executor；新的provider call必须复用既有模型结果。失败、waiting、明确可重试结果、不同参数
   和不同AgentRun不熔断。

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

新AgentRun已持久化合法`AgentContextBudget`时，普通completed Skill统一采用：

1. 完整结果能通过128 KiB Tool-result preflight时直接inline。
2. 完整结果超限时使用`full_inline_then_transient`，不再因为存在业务Artifact回退到legacy
   `artifact_backed` preview。

本节只取代
`2026-08-28-agent-skill-transient-full-result-context-compaction-design.md`中“带业务Artifact的
超限结果保持legacy”这一条限制；该设计的128 KiB durable边界、90% total-context preflight、
authority复验、recovery、cleanup和janitor合同继续有效。

业务Artifact与result transport是两个独立维度：

- `execution.artifacts`继续按现有CAS和owner-bound规则持久化并出现在Tool result
  `artifact_refs`。
- 完整结构化Skill结果只写入0700/0600 private transient store；durable AgentItem只保存bounded
  receipt，不保存正文、路径或storage key。
- transient resolver校验原有Run/Task/Conversation/Node/call/result/capability/size/SHA authority后，
  在model-only Tool message中注入完整结果，并原样保留已验证的业务Artifact IDs。

Legacy Run没有`AgentContextBudget`时继续使用现有v1行为，避免隐式迁移历史Run。final、terminal、
covered compaction、startup recovery和janitor顺序沿用现有transient lifecycle。

### 4.2 安全边界

- 不把业务Artifact内容自动拼进模型上下文；完整结果来自同一次Skill execution的private raw stage。
- 现有forbidden raw key、secret assignment、canonical JSON、复杂度和total-context preflight保持不变。
- 若private stage写入、authority复验或model preflight失败，提交现有typed failure，不回退为再次执行
  Skill。
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

不同Run永不复用。不同参数允许执行。先前失败或未完成调用允许沿现有策略执行；是否重试继续由
现有retriable/模型决策控制。

### 5.2 执行前熔断与模型结果复用

`AgentCapabilityInvoker`在进入`CapabilityInvocationService/Executor`前查找最新等价成功结果。
命中后：

1. 不创建Capability invocation，不调用Skill/MCP/外部服务，不产生第二份业务Artifact。
2. 为当前provider call提交一个bounded、版本化reuse receipt，绑定当前call item和先前result item。
3. Context Builder渲染当前Tool message时，严格校验source result仍属于同一Run、早于当前call、
   committed/completed且identity等价；随后复用source result的model-effective payload：inline直接
   使用，transient通过现有resolver解析，legacy只使用其既有安全preview。
4. 当前Tool message使用新的provider call ID，满足四角色Tool协议；模型看到的是既有有效结果，
   不是伪造的新执行结果。
5. 记录低敏`agent.tool_call.reused` audit/event与固定低基数metric；只含capability ID和结果类别，
   不记录参数、正文、Artifact ref、Task/Run/call ID或digest。

Reuse receipt本身不得复制private stage ref、raw正文、Artifact storage ref或外部结果。若source
authority或transient payload已不可用，返回typed `agent_reused_tool_result_unavailable`并禁止外部
补偿调用，避免可能重复副作用。

### 5.3 重启、Compaction与清理

- 熔断只依赖durable Agent call/result items，因此进程重启后行为一致。
- 未被compaction覆盖的source result按上述规则完整复用。
- 已覆盖的source result若完整private stage已按现有规则清理，只能复用durable安全preview/summary；
  不允许为获取详情重新执行等价调用。
- Reuse receipt不拥有source Artifact或transient stage，final/cleanup仍由原source result authority执行
  一次，不增加引用计数或清理顺序。

## 6. 错误处理与并发

- 同一AgentRun由现有lease/ownership和atomic writer串行化call/result提交；reuse lookup后仍由当前
  lease提交结果，不新增进程锁。
- 并行同batch中两个完全相同call在batch开始时都可能尚无committed success。Runner必须按现有
  ordinal顺序进行一次winner选择：首个允许执行，后续相同call等待首个terminal；成功则reuse，失败
  则沿普通调用路径。不得并行派发两个等价外部调用。
- response loss、CAS replay或recovery重复进入reuse路径必须得到同一receipt；payload冲突继续
  fail closed。
- cancel在winner完成前发生时，后续duplicate不得执行或reuse为成功；沿现有cancel终态收敛。
- 不放宽MCP no-replay、Tool approval、Skill pinning、owner scope、lease fencing或Artifact authority。

## 7. 测试与验收

### 7.1 MCP

1. Coordinator真实OCR输出经过`AgentCallResultProjector`后，`model_view.text`仍包含OCR sentinel。
2. 普通MCP completed summary进入Outer Agent Tool message。
3. raw/structured/result ref/credential不进入model view，现有大小门禁保持。
4. 端到端fixture证明一次MCP调用后下一次model request包含正文，不生成第二个相同call。

### 7.2 Skill

1. 大型Skill结果同时带业务Artifact时形成transient receipt，而非legacy preview。
2. durable result保留业务Artifact ID但不含raw正文/path/stage ref。
3. 下一次model request同时包含完整结果首尾sentinel和业务Artifact ID。
4. restart、compaction、final cleanup、stage failure与legacy Run兼容测试保持通过。

### 7.3 Reuse guard

1. 相同Run、capability和canonical args的第二次成功调用不进入Executor。
2. 新Tool message复用inline、transient和legacy source result的model-effective payload。
3. 失败、waiting、不同参数和不同Run均不误熔断。
4. 同batch两个等价call只执行ordinal最前的winner。
5. restart/response-loss replay不重复调用，source authority漂移返回typed unavailable。
6. 观测不包含参数、正文、ID、digest或Artifact ref。

验证顺序为聚焦红绿测试、Orchestration/Capabilities/MCP/API相关回归、Backend分层全量、compileall、
Ruff和`git diff --check`。若生产代码未涉及Frontend、Rust或schema，不要求修改这些层；相关公共
合同测试仍必须通过。

## 8. 实施范围与非目标

预期生产修改限定为：

- `src/integrations/mcp/dispatch_coordinator.py`
- `src/orchestration/agent_loop/result_projection.py`
- `src/orchestration/agent_loop/capability_invoker.py`
- `src/orchestration/agent_loop/context.py`
- 必要的Agent model/runner小型复用合同文件

明确不修改：

- 外部Skill仓、MCP Server实现、MCP协议/Selector/Gateway/Result Parser authority；
- 前端、API DTO、数据库schema、Rust/Runtime Sidecar proto、模型配置或`prod`；
- Artifact公开下载策略、用户历史数据、旧失败Task或当前运行Task；
- 通用`artifact.read` Tool、模糊参数相似度、跨Run缓存、TTL缓存或调用结果数据库。

## 9. 文档、回滚与完成声明

- 实施计划按MCP正文、Skill transient+Artifact、reuse guard、端到端证明四个可回滚检查点拆分。
- 实施完成后更新本设计状态、`docs/AGENTS.md`索引与`CHANGELOG.md`验证账本；模块职责或入口未
  变化时不修改其他`AGENTS.md`。
- 回滚按检查点恢复旧字段、旧Skill投影选择和reuse guard；无schema或数据回滚。
- 只有真实Coordinator→projector→Context链包含结果正文，带Artifact大Skill完整注入，以及第二次
  等价成功call零Executor调用三项同时通过，才可声明完成。

License Requirement：复用现有Python、Agent Loop、MCP typed result projection、private transient
Skill result store、Artifact authority和atomic writer；无新增依赖或许可变化。
