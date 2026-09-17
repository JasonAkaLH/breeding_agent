# Phase 0：现状基线与 Agent Model Contract PRD

- **日期**：2026-08-22
- **终态复验**：2026-08-23
- **状态**：proof_complete（P0-A、P0-B green checkpoint均已完成）
- **文档审阅**：document-perfectization第二次全量审计100/100通过；Phase 0实现已完成并被Phase 1～7消费
- **父总纲**：`00-统一同模型AgentLoop总纲PRD.md`
- **主责需求**：FR-2、FR-3、FR-19
- **主责NFR**：Provider兼容与同模型
- **直接参与者**：Agent/LLM Runtime维护者、模型配置维护者、MCP Router/Selector维护者、测试与发布审查者
- **Phase 0目标结果**：建立provider-neutral原生Agent采样合同、公开model edition启动门禁和现状/PRD inventory；不创建AgentRun，不接入真实执行入口。

> **阶段语境与当前终态**：本文同时保留Phase 0的前瞻要求和检查点证据。第2～4节的进入条件、非范围和基线事实，以
> `f4d6425`到`5d3c82d`的pre-cutover实施边界为语境，不描述当前HEAD。当前`main`已完成Phase 6全入口cutover和Phase 7 physical
> schema/proto删除；不得据本阶段的历史非范围、入口基线或回滚条款重新引入DAG控制面。当前authority、验证和回滚口径以本目录`README.md`及
> Phase 6/7的closed证据为准。

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

## 4. Phase 0进入证据与受影响系统

| 证据 | Phase 0基线事实 | 本阶段要求 |
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

公开edition的启动配置合同为：

```yaml
agent_protocol_max_retries: 1
model_editions:
  options:
    - value: example-edition
      agent_capabilities:
        supports_messages: true
        roles: [system, user, assistant, tool]
        supports_native_tools: true
        supports_required_tool_choice: true
        supports_streamed_tool_calls: true
        # 仅在不支持streamed tool delta时可改为显式true：
        supports_non_stream_agent_sample: false
```

`agent_protocol_max_retries`必须为非负整数，默认1，只控制provider contract violation重试，不覆盖transport
`max_retries`，也不接受请求级覆盖。每个公开edition必须逐项声明closed capability profile；默认edition不合格时启动失败，
非默认不合格edition不出现在公开API中。

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

上述“只登记，不删除”是Phase 0实施边界。当前inventory已在Phase 6按cutover结果更新为`rewritten`/
`superseded`/`removed`，不得为重现Phase 0扫描集而恢复已删除的DAG测试。

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
- `active-prd-inventory.md`以closed v1合同登记26份active PRD、55个旧测试和9类start/resume/cancel/recovery入口；
- `tests.scripts.test_unified_agent_loop_evidence_contract` 3项通过；Phase 0 validator返回`status=closed`，未来三份handoff为
  `not_due`；
- LLM/model-edition基线44项最终全量通过；首次组合运行有1项Interrupt等待超时，隔离重跑通过，随后相同44项组合重跑
  全部通过，登记为既有时序观察，不改测试或Runtime；P0-B必须继续复验；
- `conda run -n multi_agent python -m compileall -q src tests scripts`通过；
- 未运行真实Provider smoke，符合本阶段非门禁约束；未新增Agent Model、Agent storage或真实route。

### 10.2 P0-B 实施证据

- 新增provider-neutral `AgentModelBinding`、message/tool/request/sample/usage/finish/closed error合同和
  `AgentModelPort`；binding安全序列化只包含edition、thinking/reasoning选项与digest；
- OpenAI-compatible adapter使用原生messages/tools/named required choice，闭合0/1/N calls、交错且跨chunk的
  call ID/name/arguments、canonical JSON、usage缺失、text+calls、unknown tool、取消及stream/non-stream sample；
- 协议违规只按独立`agent_protocol_max_retries`重试，默认总尝试2次；transport错误不进入协议重试，耗尽返回closed code；
- API Runtime在启动时对默认edition fail closed，过滤非默认不合格edition；`SharedLLMRuntime.sample_agent()`固定使用
  request binding edition并拒绝client改变binding；旧`generate_text()`路径保留；
- P0-B canonical 67项回归通过，`compileall`、`git diff --check`、Phase 0 evidence validator通过；未运行外部真实
  Provider smoke，符合本阶段门禁；未新增Agent storage、Capability执行或用户route；
- 扩大运行六个非canonical API模块时，70项通过、3项失败：1项等待本地配置指向的真实`/tokenization`超过fixture
  5秒，2项为既有动态Skill fixture未进入临时catalog/同类等待；P0-B未修改这些执行路径，不把该扩展运行记为green。

### 10.3 Phase 7终态复验与命令口径

- 2026-08-23在`main@a7fc467`的相关Agent Model/inventory/API聚焦套件共71项通过，`compileall`和`git diff --check`通过；
- 当前post-cutover工作树的inventory closed命令为
  `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed`，整组最终证据也可以
  `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 7 --require-closed`复验；两者均返回`closed`；
- `--phase 0 --require-closed`只用于Phase 0 pre-cutover发现集或对应历史检查点。Phase 6删除旧DAG shape测试后，不得在当前HEAD
  上将Phase 0参数的inventory mismatch解释为Agent Model回归失败。

## 11. 风险、假设与开放问题

| 风险 | 缓解/阻断条件 |
|---|---|
| Provider声明能力但stream delta不合规 | 每edition wire golden和protocol retry；不合格edition不公开 |
| Agent adapter修改现有text helper行为 | Agent-only入口隔离并运行现有text/title/helper回归 |
| Model binding意外携带client/key | Contract serialization和leak scan拒绝非安全字段 |
| PRD inventory漏项 | `rg`结果与`active-prd-inventory.md`双向集合校验；漏项阻断Phase 1 |

已确认假设：现有model edition配置可扩展Agent能力声明；非Agent text接口继续保留。开放问题：无。

## 12. Git检查点与回滚

以下是仅在Phase 0完成、Phase 1尚未开始时有效的历史检查点回滚边界：

- 本阶段只增加合同、adapter、门禁和测试；不接用户流量；
- 若门禁导致现有公开edition不合格，Runtime必须fail closed，不得自动弱化要求；
- 回滚删除新增Agent-only合同/测试即可恢复旧text路径；
- 不得添加长期feature flag。

当前`main`的Phase 1～7已依赖该合同且旧DAG physical contract已删除，因此禁止单独删除Phase 0合同或尝试恢复旧text/DAG控制面。
当前回滚必须遵守目录`README.md`和`destructive-migration-evidence.md`：成对恢复Phase 7前代码与数据备份，或forward fix。

## 13. 完成与交接

完成条件：AL-P0-01～10通过；现有text路径回归通过；`active-prd-inventory.md`集合校验完整；无Agent storage或用户
入口变化。

交付Phase 1：`AgentModelPort`、规范化sample/tool-call类型、provider capability gate、AgentModelBinding和测试fake。
Phase 1不得依赖OpenAI wire对象、client实例、API key或role fallback实现。
