# Phase 0：现状基线与 Agent Model Contract PRD

- **日期**：2026-08-22
- **状态**：in_progress（P0-A green checkpoint已完成；P0-B尚未开始）
- **文档审阅**：document-perfectization第二次全量审计100/100通过；实现按批准计划推进
- **父总纲**：`00-统一同模型AgentLoop总纲PRD.md`
- **主责需求**：FR-2、FR-3、FR-19
- **主责NFR**：Provider兼容与同模型
- **直接参与者**：Agent/LLM Runtime维护者、模型配置维护者、MCP Router/Selector维护者、测试与发布审查者
- **目标结果**：建立provider-neutral原生Agent采样合同、公开model edition启动门禁和现状/PRD inventory；不创建AgentRun，不接入真实执行入口。

## 1. 目标与价值

Agent Loop必须把assistant text、零到多个tool calls、流式arguments和usage统一为可持久化的规范化sample。若继续用
当前text generator或自由文本JSON，后续Storage、Invocation和Loop都无法可靠区分协议错误、未知Tool和最终回答。

本阶段给Runtime/Storage提供稳定模型合同，并在启动时排除不支持Agent消息与Tool协议的model edition，避免把失败
延迟到用户任务执行中。

## 2. 进入条件与依赖

- 统一Agent Loop架构设计和PRD总纲已批准；
- 当前`LLMClient`、`SharedLLMRuntime`、model edition配置和相关tests可作为基线；
- 不依赖Agent storage、Invocation Kernel或Frontend改造。

## 3. 范围

### 3.1 范围内

- 锁定当前Skill、MCP、Interrupt、Task、history、LLM text和model edition行为基线；
- provider-neutral Agent request/sample/tool-call/stream/usage/error contract；
- OpenAI-compatible native tool-call adapter；
- tool call delta闭合、provider-safe name规则和unknown tool边界；
- required first-call choice合同；
- 每个公开model edition的启动能力门禁；
- Run-bound model option/value对象，不携带client、key或provider实例；
- active PRD与旧DAG测试inventory。

### 3.2 非范围

- 不新增AgentRun/AgentItem表；
- 不执行Capability；
- 不实现Tool Catalog业务可见性；
- 不实现Agent循环、compaction或final publisher；
- 不改变非Agent标题、内部窄helper等`generate_text()`路径；
- 不切换任何用户请求入口；
- 不连接或部署`prod`。

## 4. 当前证据与受影响系统

| 证据 | 当前事实 | 本阶段要求 |
|---|---|---|
| `src/integrations/llm_client.py` | `generate_text`/messages调用存在，`_messages_payload`会做role fallback | Agent path禁止assistant/tool role fallback；非Agent text兼容保留 |
| `src/integrations/llm_runtime.py` | `SharedLLMRuntime`提供text/stream text能力 | 新增Agent sampling port，不删除现有text接口 |
| `src/integrations/model_editions.py` | 公开模型及能力由配置装配 | 不合格edition不得出现在可选列表或Runtime Ready状态 |
| `tests/integrations/test_llm_client.py` | 覆盖当前LLM wire/text行为 | 保留基线并新增native tools golden vectors |
| `tests/api/test_model_edition_selection.py` | 覆盖model edition选择 | 增加Agent capability gate和同edition断言 |

## 5. 模型合同

至少定义以下provider-neutral类型或等价闭合合同：

```text
AgentModelBinding
  model_edition
  reasoning_effort
  thinking_enabled
  safe option digests

AgentToolDescriptor
  provider_safe_name
  capability_id
  description
  input_schema

AgentModelRequest
  binding
  system/tool rules
  model-visible messages
  tool descriptors
  tool choice constraint
  cancellation token

AgentSample
  sample_id
  visible_text
  ordered tool_calls[]
  usage
  finish metadata
```

`AgentToolCall`至少包含稳定call ID、provider-safe name、规范化JSON arguments和ordinal。业务层不得接收OpenAI SDK
对象、raw stream chunk、client或credential。

## 6. 功能需求与验收

| ID | Requirement | Acceptance |
|---|---|---|
| AL-P0-01 | Adapter必须闭合单个和多个tool calls。 | Golden tests覆盖一轮0/1/N calls及顺序。 |
| AL-P0-02 | 必须组装跨chunk的call ID、name和arguments。 | 分片、交错多call和结束前未闭合场景有确定结果。 |
| AL-P0-03 | 无tool calls且非空text才可表示final candidate。 | Empty sample和tool-call sample不得被标记final。 |
| AL-P0-04 | 缺ID、空/非法name、损坏JSON、重复ID和未闭合delta是protocol violation。 | 进入有界adapter retry；耗尽返回closed fatal code。 |
| AL-P0-05 | 合法name但不在catalog不是wire损坏。 | Adapter保留call，由下游生成`unknown_tool`；不得执行Capability。 |
| AL-P0-06 | required choice只接受目标Tool恰好一次。 | 零个、多个或错误name触发protocol retry，不能退回auto/text。 |
| AL-P0-07 | 每个公开edition必须支持messages、system/user/assistant/tool roles、native tools和required choice。 | 启动门禁失败的edition不公开，默认edition失败则Runtime fail closed。 |
| AL-P0-08 | Agent sample、后续compaction和MCP内部model binding必须可引用同一edition。 | Fake记录binding identity，禁止隐式fallback到其他edition。 |
| AL-P0-09 | text与tool calls并存时text不进入用户history。 | Adapter保留审计metadata，下游只处理tool calls。 |
| AL-P0-10 | 非Agent text调用行为保持。 | 现有title/helper/text tests不退化。 |

## 7. 失败模式

- Provider声明支持但返回非法delta：protocol retry，耗尽后fatal；
- required choice被忽略：protocol retry，不改用文本JSON；
- model edition运行中不可用：按既有transport retry，耗尽后fatal，不换edition；
- usage缺失：允许closed `usage_unavailable` metadata，不伪造token值；
- cancellation：停止读取stream并返回cancelled，不产生半闭合sample；
- Tool schema超过provider限制：在后续Catalog preflight前由启动/adapter合同显式失败，不截断schema。

## 8. NFR与安全

- Agent path不得role fallback或text-JSON fallback；
- 日志/audit不得保存raw prompt、raw arguments、API key或完整assistant observation；
- provider-safe name映射稳定、双向、无冲突；
- AgentModelBinding可序列化为安全配置引用和digest，不序列化client；
- 公开edition gate必须确定性、可在启动测试中复验。

### 8.1 跨阶段NFR协作

| NFR | 本阶段责任 | 后续复验 |
|---|---|---|
| Provider兼容与同模型 | 主责：定义binding、wire和启动门禁 | Phase 2～7验证同一binding贯穿MCP、Loop、resume和final |
| 安全与隐私 | Adapter不记录raw prompt/arguments/key，禁止role/text fallback | Phase 2/3验证Catalog和AgentItems不泄漏 |
| 可维护性 | Provider-neutral contract不暴露SDK对象 | Phase 6静态证明业务层不依赖OpenAI wire/client |

## 9. PRD与测试基线 Inventory

本阶段必须生成`docs/prd/backend/unified-agent-loop/active-prd-inventory.md`，覆盖包含`WorkflowPlan`、
`RuntimeReplanner`、`main_agent.respond`、`CompletionPolicy`、`max_replans`或`max_dynamic_nodes`的active文档。每行必须
包含README定义的document path、matched terms、closed disposition、replacement authority、owner phase、status和
evidence command；扫描发现集与inventory行集必须双向一致。

同时分类旧测试：行为/安全合同必须迁移；只断言DAG实现形状的测试登记为Phase 6候选删除。此阶段只登记，不删除。

## 10. 测试计划

最低现有入口：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.test_llm_client \
  tests.integrations.test_llm_runtime \
  tests.integrations.test_llm_request_options \
  tests.api.test_model_edition_selection
```

新增测试域必须覆盖native wire golden、multi-call deltas、required choice、unknown tool、role门禁、cancellation和同edition
binding。运行`compileall`并记录未运行的真实Provider项；本阶段不新增外部真实Provider smoke门禁。

### 10.1 P0-A 实施证据

- 基线：`main@f4d6425`，tree `d77458ead5d3ed2afd8ec0b781fbed91032f32e9`；正式运行时仍为DAG；
- `active-prd-inventory.md`以closed v1合同登记26份active PRD、54个旧测试和9类start/resume/cancel/recovery入口；
- `tests.scripts.test_unified_agent_loop_evidence_contract` 3项通过；Phase 0 validator返回`status=closed`，未来三份handoff为
  `not_due`；
- LLM/model-edition基线44项最终全量通过；首次组合运行有1项Interrupt等待超时，隔离重跑通过，随后相同44项组合重跑
  全部通过，登记为既有时序观察，不改测试或Runtime；P0-B必须继续复验；
- `conda run -n multi_agent python -m compileall -q src tests scripts`通过；
- 未运行真实Provider smoke，符合本阶段非门禁约束；未新增Agent Model、Agent storage或真实route。

## 11. 风险、假设与开放问题

| 风险 | 缓解/阻断条件 |
|---|---|
| Provider声明能力但stream delta不合规 | 每edition wire golden和protocol retry；不合格edition不公开 |
| Agent adapter修改现有text helper行为 | Agent-only入口隔离并运行现有text/title/helper回归 |
| Model binding意外携带client/key | Contract serialization和leak scan拒绝非安全字段 |
| PRD inventory漏项 | `rg`结果与`active-prd-inventory.md`双向集合校验；漏项阻断Phase 1 |

已确认假设：现有model edition配置可扩展Agent能力声明；非Agent text接口继续保留。开放问题：无。

## 12. Git检查点与回滚

- 本阶段只增加合同、adapter、门禁和测试；不接用户流量；
- 若门禁导致现有公开edition不合格，Runtime必须fail closed，不得自动弱化要求；
- 回滚删除新增Agent-only合同/测试即可恢复旧text路径；
- 不得添加长期feature flag。

## 13. 完成与交接

完成条件：AL-P0-01～10通过；现有text路径回归通过；`active-prd-inventory.md`集合校验完整；无Agent storage或用户
入口变化。

交付Phase 1：`AgentModelPort`、规范化sample/tool-call类型、provider capability gate、AgentModelBinding和测试fake。
Phase 1不得依赖OpenAI wire对象、client实例、API key或role fallback实现。
