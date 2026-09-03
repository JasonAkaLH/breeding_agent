# MCP Dispatch 实际 Tool Result 向主 Agent 交付设计

状态：`approved_clean_cutover_hard_defects_resolved`；v2-only修订经限定硬伤复审为0 Blocking / 0 Major；未评Minor，不宣称完整95分信心门
日期：2026-09-03
目标分支：`main`

## 1. 问题与目标

会话 `conv-web-f4f128a8440328` 的只读排查证明，普通 MCP 调用已经成功取得并解析业务结果，
但统一 Agent Loop 中的主 Agent 没有收到该结果：

- 第二个 Task 正确选择目标 Server，并成功调用该 Server 内的项目统计 Tool；Gateway 收到
  34,148 bytes 返回，Result Parser 和 validated checkpoint 均成功。
- 同一轮内部 Selector 能从 `completed_result_projections` 看到经过 authority 校验的结果；
  Selector 随后返回 `finish`。
- Coordinator 在 `finish` 时只把 `finish.reason` 或通用文案写入
  `output_payload["text"]`，主 Agent 最终只看到“已完成、结果可由安全引用获取”的摘要，
  看不到实际业务结果，因此生成了无法查询数据的兜底答复。

这不是 Server 选择错误，也不是 MCP Tool 没有返回结果，而是内部 Selector Loop 与外层
`mcp.dispatch` Tool Result 之间的结果承载合同断裂。

本设计只完成一个目标：保持现有 Capability、Selector、Gateway、审批、恢复和 Artifact 框架
不变，把已经生成的 validated agent projection 作为实际 `mcp.dispatch` Tool Result 交给主
Agent。只要某个会返回主 Agent 的终态已经积累成功结果，该结果就必须随终态 Tool Result 返回，
不能只在 Selector 选择 `finish` 时返回。

本设计仅替代
`2026-08-29-agent-tool-result-delivery-and-repeat-guard-design.md` 第 3.1 节中“普通 MCP branch
把 Selector safe summary 写入 `text`”的结果承载规则；该文档的 OCR、Skill、结果复用和其余安全
边界继续有效。

用户已明确决定退役现有 `mcp-result-parser.v1` / `maf.mcp.parsed_result_projection.v1`：新运行时不恢复、
不读取、不重投影旧 projection。旧 receipt、projection文件和 Artifact metadata 原样保留，不删除、
不清空、不改写。

## 2. 已批准的流程

```text
全局 Capability / Tool Catalog
  └─ mcp.dispatch
       └─ server_id 枚举 + 所有安全 Server Profile
            ↓ 主 Agent 选择 server
单 Server 内部
  └─ Selector 选择具体 tool + arguments
            ↓
       实际 MCP Tool 执行
            ↓
       Selector 判断继续调用还是 finish
            ↓
       实际 Tool Result 返回主 Agent
```

职责边界固定如下：

1. 全局 Capability Pool 只向主 Agent 暴露一个 `mcp.dispatch`；动态 schema 中的
   `server_id` 枚举和安全 Server Profile 用于主 Agent 选择 Server。
2. 一旦进入一次 Tool 选择步骤，当前 Server 已经固定。内部 Selector 只能在该 Server 的已发现
   Tool 目录中选择 `tool_name + arguments`，并决定继续调用、结束或安全停止；它不产生
   `server_id`。现有 automatic binding 的 `route_another_server` 只是交给独立 Server Router 的
   控制信号，不代表 Selector 选择 Server，本设计也不修改该兼容路径。
3. Gateway 执行实际 Tool，Result Parser 生成 typed、model-safe projection，现有 receipt、
   checkpoint 和 projection authority 继续证明结果身份。
4. Selector 的 `finish` 是控制面决策，不是业务结果。结束后，外层同一个 `mcp.dispatch`
   Tool call 必须把 validated projection 作为 Tool Result 返回主 Agent。

## 3. 设计决策

### 3.1 唯一结果权威

普通 Selector 路径和终态 carrier 共享同一个 durable completed-result projection authority；
`MCPSelectorContext.completed_result_projections` 只是该 authority 的 Selector 输入投影，不是终态
结果权威或可复用缓存：

- 只收集状态为 `completed` 的 Call；
- 每个结果必须具有匹配 intent、owner、Task、Node、Call、Server 版本、result ref、内容摘要、
  parser revision 和 validated checkpoint 的 terminal receipt；
- 通过 `MCPPublishedAgentProjectionAuthority` 读取发布后的模型安全投影及其可信完整性元数据；
- 每项至少保留 `call_sequence`、projection 正文和可信 `source_truncated: bool`；
- 继续使用现有 20,000 code points、80,000 bytes 上限和 Call sequence 顺序，但预算必须覆盖最终
  carrier 及其元数据，而不是先截断正文、再额外增加未计入预算的包装。

不得从 raw result、内部文件、公共 Artifact preview、`result_ref` 或 Selector 自由文本重新构造
业务答案，也不得为补齐结果重新调用 Server。

### 3.2 Canonical 结果载荷

`agent_projection` 继续使用现有 allowlist key，但值改为 closed、结构化对象，禁止用自由文本分隔符
拼接多个不可信结果：

```json
{
  "schema": "maf.mcp.agent_result_bundle.v1",
  "result_count": 2,
  "included_count": 2,
  "omitted_count": 0,
  "truncated": false,
  "results": [
    {
      "call_sequence": 1,
      "content": "validated model-safe projection",
      "source_truncated": false,
      "carrier_truncated": false
    },
    {
      "call_sequence": 2,
      "content": "second validated model-safe projection",
      "source_truncated": false,
      "carrier_truncated": false
    }
  ]
}
```

字段合同：

- `result_count` 是该 branch 中通过 receipt/checkpoint authority 的 completed Call 总数；
- `included_count` 是本载荷实际包含的结果数，必须等于 `results` 长度；
- `omitted_count = result_count - included_count`；
- `results` 必须按 `call_sequence` 升序且 sequence 唯一；跨 automatic route 的 Call 仍使用 branch
  内唯一 sequence，不引入第二套 Server 顺序；
- `source_truncated` 由可信 Result Parser projection 元数据产生，不从不可信 Tool 正文猜测；
- `carrier_truncated` 表示该项在聚合或外层模型预算中被进一步截断；
- `truncated` 在任一 source/carrier truncation 或 `omitted_count > 0` 时必须为 `true`。

`source_truncated` 不允许 `null`。只有 parser v2 / projection v2 能进入 bundle；发现明确v1或
`result_parser_revision=null` 的pre-v2结果时返回 `mcp_result_projection_revision_retired`，不得把旧
结果伪装成完整或unknown-compatible结果。其他未知非空revision/schema组合返回typed unsupported/
authority错误，同样不读取内容。

普通单结果也使用同一 bundle schema，避免单/多结果出现两套模型合同。Coordinator 同时把 bundle
的 `truncated` 镜像到 `MCPDispatchOutcome.output_payload["truncated"]`，使现有外层
`projection_truncated` 语义可用。

### 3.3 预算与截断规则

结果 carrier 必须在最终结构化形态上执行一次确定性预算：

1. 预算计入 schema、计数、sequence、布尔字段、JSON 转义、外层 `model_view` 固定字段和 Tool
   Result envelope 开销；不得把已经达到 20,000 code points 的正文直接加包装后交给外层减半。
2. 继续沿用现有“优先保留最新结果、最终按 sequence 恢复顺序”的选择策略，但任何整项省略都增加
   `omitted_count` 并设置 `truncated=true`。
3. 单项正文需要收缩时，按 UTF-8 边界截断，保留明确的可信截断标记，并设置该项
   `carrier_truncated=true`；不得截断到半个结构化 envelope。
4. `AgentCallResultProjector` 对该 closed bundle 只能完整接收，或调用 bundle-aware 收缩逻辑并同步
   更新计数与 truncation 字段；禁止沿用普通字符串反复减半后仍报告未截断。
5. 若连 schema、计数和一项有界结果元数据都无法通过既有 80,000-byte model-result / 128 KiB
   Tool-result preflight，则返回既有 `agent_result_projection_too_large`，不得降级为通用成功摘要。

Result Parser revision 必须升级为 `mcp-result-parser.v2`，新 projection schema 必须升级为
`maf.mcp.parsed_result_projection.v2` 并持久化可信 `agent_projection_truncated`。checkpoint schema
字段集合不变，但 checkpoint 中的 revision值必须为 v2。相同 raw/protocol/source/schema snapshot 与
parser revision仍必须产生逐字节相同 projection和SHA。

唯一 shared projection-envelope validator 必须在 Result Service 接收 worker输出时，以及 Projection
Store `stage/load` 的持久化边界上 exact-validate v2字段集合和类型；只比较顶层 schema不足以把
`agent_projection_truncated` 当成可信 authority。该变化不改变 raw result、user view、业务 Artifact
或 output schema authority。

### 3.4 所有会返回主 Agent 的终态

Coordinator 在形成终态 Tool Result 时统一应用 carrier，而不是只在 FINISH 分支临时拼装：

| 终态 | 已有 validated completed results | 主 Agent 可见结果 |
| --- | --- | --- |
| Selector `finish` | 有 | `mcp_status=completed` + canonical `agent_projection` bundle |
| Selector `finish` / discover-only | 无 | 保留现有安全 `text` 摘要，不伪造 bundle |
| Selector `stop`、automatic route 无下一 Server、Server 后续不可用 | 有 | 保留 stopped/error 状态，同时返回已有 bundle，允许主 Agent说明部分结果与停止原因 |
| 后续 Tool 被拒绝或发生不会重放的终态失败 | 有 | 保留原 safe error/status，同时返回先前已完成 bundle |
| waiting / input-required / approval interrupt | 任意 | 不是最终 Agent Tool Result；等待 continuation，最终终态再统一承载 |
| cancelled 且不会继续采样主 Agent | 任意 | 保持现有取消收敛；不得为交付结果重新唤醒 Agent |
| completed Call 的 projection authority 冲突 | 有 | typed authority error fail closed，不返回未经验证内容，不网络重放 |
| completed Call 仍绑定 retired parser/projection v1或null revision | 有 | `mcp_result_projection_revision_retired`，不读取、不重投影、不修改旧 Artifact |

“会返回主 Agent 的终态”以 committed Agent Tool result 为准。所有此类 stopped/failed 结果必须保留
原 `mcp_status` 和 `safe_error_code`，不得因为携带先前成功 projection 而伪装成 completed。
终态 carrier 必须在终态形成时从 branch 的 durable Call、receipt 和 published projection authority
重新构建；不得直接复用上一 Selector step 的 context，因为它可能不包含刚完成的 Call。该重建只读
现有持久化 authority，不访问 Gateway、不重新执行 Tool，也不改变 no-replay 规则。

只要存在 `agent_projection`，终态 branch 就不得再向模型投放通用成功 `text`。否则模型可能优先
采信“结果可由安全引用获取”而忽略业务数据。`finish.reason`、stop reason 和安全错误摘要仍可进入
branch 的 `safe_summary`，用于审计、恢复和 UI 安全摘要，但不能充当业务 payload。

### 3.5 外层 Agent Tool Result 合同

结果交付链固定为：

```text
MCP Server CallToolResult
  -> versioned Result Parser
  -> validated agent projection + terminal receipt
  -> durable completed-result projection authority
       ├─ bounded Selector context projection -> Selector 决定下一控制动作
       └─ terminal carrier rebuild -> 执行链形成终态
  -> MCPDispatchOutcome.output_payload["agent_projection"]
  -> AgentCallResultProjector MCP allowlist
  -> 同一个 mcp.dispatch call 对应的 committed Tool result
  -> 下一次 AgentModelRequest 的 tool message
  -> 主 Agent 基于实际结果生成最终回答
```

这里的“实际 Tool Result”指经过现有 parser、大小和安全策略处理后的 canonical
`agent_projection` bundle，不是未经验证的 upstream raw payload。它仍然属于主 Agent 发起的同一次
`mcp.dispatch` Tool call，并继续与该 provider call ID 配对；不会伪装成 user message、system
message、Selector answer 或单独的 Artifact 读取结果。

现有 `AgentCallResultProjector._mcp()` 已 allowlist `agent_projection`，因此本设计不放宽主 Agent
模型视图的 key 集合。以下字段继续不可见：

- raw / structured upstream result；
- 内部 result storage ref、projection path、receipt 和 checkpoint；
- credential、header、session、Server 配置和内部 authority；
- branch 内部 `safe_summary` 与 `result_ref`。

### 3.6 Selector 合同保持不变

Selector 输入继续包含安全 Server Profile、当前 Server Tool catalog、上游事实、失败/拒绝指纹、
剩余调用预算和已完成 projections。Selector 输出仍只有：

- `call_tool(tool_name, arguments, reason?)`；
- `finish(reason?)`；
- `route_another_server(reason?)`，仅在现有 automatic binding 允许时作为控制信号，由独立
  Server Router 从全局剩余候选中决定下一个 Server；
- `stop(reason?)`。

不要求 Selector 复制、摘要或转写业务结果，也不把 `finish.reason` 改为必填。这样既避免大结果
在模型生成的 reason 中被截断或遗漏，也防止不可信 Tool 内容借 Selector 自由文本进入新的权威层。

## 4. 兼容性与特殊路径

- OCR 固定工作流继续使用现有 `external_text` / `text` 交付，不并入普通 Selector 改动。
- Tool approval、Interrupt、remote Task、resume outbox、no-replay、claim、cancel 和 aggregate
  终态保持原行为；只有全部 completed result均为v2的恢复任务才能形成新 carrier。任何pre-v2在途
  任务返回revision-retired typed错误，不自动终止、不重放。
- `result_ref`、公共 Artifact 和 raw durable authority 继续用于审计、下载或历史恢复，不要求主 Agent
  再调用 Artifact reader。
- discover-only FINISH 没有业务 projection 时保留现有安全摘要，避免把正常目录发现误判为结果缺失。
- `stop`、failed 或 rejected 没有任何已完成 projection 时不伪造 `agent_projection`；已有结果时按
  第 3.4 节携带结果并保留真实状态。cancelled 和 waiting 不为交付结果额外唤醒主 Agent。
- automatic binding 的既有 `route_another_server` / Server Router 兼容路径不变；每次 Tool 选择仍只
  面向当时已固定的一个 Server，最终终态从同一 durable authority 重建已验证 completed
  projections。
- 历史 Run 已提交的 Tool Result 不原地改写。旧v1 projection及其Artifact metadata保留但不再由新
  Selector、terminal carrier或historical reprojector消费。

## 5. 预期修改面

实施阶段预计只触及：

- `src/integrations/mcp/dispatch_coordinator.py`：在统一终态边界承载 canonical bundle，有结果时禁止
  通用 `text` 替代业务数据；
- `src/integrations/mcp/selector_context.py` 及对应内部 model：在不改变 Selector Server/Tool 决策的
  前提下，保留 sequence、source truncation 和聚合省略元数据；
- `src/integrations/mcp/result_parsing/` 的 projection producer/validator/reader：为新 projection 记录
  可信 agent truncation状态，升级parser/projection v2，并在服务和持久化边界共用exact validator；
- `src/integrations/mcp/result_parsing/historical_reprojection.py`与
  `src/integrations/mcp/durable_result_lifecycle.py`：识别retired pre-v2并向startup reconcile summary
  传递closed计数，禁止load、reprojection、metadata CAS或raw authority读取；
- `src/orchestration/agent_loop/result_projection.py`：让现有 `agent_projection` allowlist 接受 closed
  bundle，并保证外层收缩会同步更新 bundle/top-level truncation；
- 现有 MCP coordinator、Result Parser、Agent projection 和 E2E 测试：补充真实 result handoff、
  多终态与大小边界回归。

这些修改仍只属于结果载荷及其安全预算。不得借机修改 `selector.py` 的 action 集合、全局 Server
路由、Gateway 调用、approval/recovery authority、数据库 schema、公开 API 或 Artifact 生命周期。

## 6. 测试与验收

### 6.1 聚焦回归

1. 构造一次普通 MCP Call，发布带首尾 sentinel 的 validated projection，Selector 随后以空
   `reason` 返回 `finish`；断言 outcome 含 `agent_projection`，且不含通用成功 `text`。
2. Selector 提供非空 `finish.reason` 时，断言 reason 只写入 branch `safe_summary`，不会覆盖或
   替代 `agent_projection`。
3. 两个成功 Call 的 projections 以 closed bundle 按 Call sequence 稳定进入同一 Tool Result；结果
   正文伪造分隔符或 schema 文本不能改变 bundle 边界。
4. source truncation、聚合整项省略、聚合单项截断和外层预算收缩分别正确设置
   `source_truncated`、`carrier_truncated`、`omitted_count`、bundle/top-level `truncated`；达到
   20,000-code-point / 80,000-byte 边界时不得发生无标记减半。
5. 已有一个成功 Call 后分别走 Selector STOP、automatic route 无下一 Server、Server 不可用、后续
   Tool denied 和终态 failure；断言每条会提交 Agent Tool result 的路径都保留先前 bundle及原
   status/error，Gateway 不重放成功 Call。
6. discover-only FINISH 继续返回现有安全 `text`；waiting、input-required、approval 和 OCR 固定流程
   保持原行为。
7. receipt、owner、Call、Server version、digest 或 checkpoint 冲突时，在主 Agent 再采样前返回
   现有 typed authority error，且 Gateway 调用次数不增加。
8. parser.v2/projection.v2组合及 truncation元数据可验证；v1、交叉版本和未知版本返回
   revision-retired或typed authority错误，不读取raw、不迁移、不网络重放。

### 6.2 Agent Loop 回归

1. `AgentCallResultProjector` 后的 `model_view.agent_projection` 是 closed bundle，结果项仍包含首尾
   sentinel、准确 sequence 和 truncation字段。
2. 下一次 `AgentModelRequest` 中，对应 provider call ID 的 tool message 包含 sentinel；不出现
   raw、path、result ref、credential 或内部 receipt。
3. 端到端 fixture 证明主 Agent 在一次 MCP 外部调用后依据 projection 回答，不因看不到结果再次
   调用相同 Tool。
4. 大小门禁覆盖单结果、多结果、JSON 转义和 UTF-8 边界；外层 Tool Result 仍通过 128 KiB
   preflight，任何收缩都可由主 Agent识别。

### 6.3 现场验收

在自动门禁通过后，使用开发环境创建全新会话，执行与原问题等价但不复用历史失败 Task 的请求：

- 主 Agent 选择正确 `server_id`；
- Selector 在该 Server 内选择正确 Tool；
- MCP Call 数为预期值且无补偿重放；
- committed Tool Result 的模型安全视图包含业务 sentinel / 统计字段；
- bundle 的计数、顺序和 truncation字段与 durable completed Call / projection metadata一致；
- 最终回答引用实际项目统计，不再输出“无法直接查询”的兜底文案；
- 日志、Event、AgentItem 和公开响应均不泄露 credential、raw storage path 或内部 authority。

## 7. 明确排除项

以下问题来自同次排查，但不属于本设计：

- 第一个 Task 在切换 Server 后产生的 `unknown_tool`；
- 历史 projection 因缺少 `output_schema` / schema hash 导致的
  `historical_authority_invalid` 补投问题；
- 删除或重构现有 automatic binding 的 `route_another_server` / Server Router 兼容路径，或让
  Selector 直接选择一个或多个 `server_id`；
- 新增 `artifact.read`、结果查询 API、数据库 schema、Frontend、Rust 或外部 MCP Server 改造；
- 修改历史 Task、自动重跑外部 Tool、放宽 raw result 可见性；
- 为旧 projection 回填、重新生成正文、迁移到v2或恢复模型可读能力；
- 语义去重、通用重试策略或其他 Agent 调度优化。

这些事项如需处理，应分别立项、复现、设计和验收，不能借结果承载修复扩大范围。

## 8. 方案取舍

未采用以下方案：

- **让 Selector 在 `finish.reason` 中复述结果**：reason 是可选的模型生成控制文本，存在遗漏、
  截断、幻觉和提示注入面，不能作为 Tool Result 权威。
- **把 `result_ref` 告诉主 Agent，再让它自行读取**：当前主 Agent没有相应读取能力；新增 reader
  会扩大权限、调用轮次和失败面。
- **直接返回 raw MCP payload**：会绕过已完成的版本解析、authority、安全投影和大小控制。
- **取消 Selector**：Server 内 Tool 数量和多步调用规模要求保留 Selector；问题不在选择机制，
  而在 FINISH 后没有把既有结果继续向外承载。
- **让 Selector 直接负责 Server 选择**：与当前全局 Capability Pool 的初始 `server_id` authority
  及独立 Server Router 重叠，会引入两层 Server ID 生成和恢复歧义。

## 9. 回滚与完成定义

代码尚未写入任何v2 projection前，可整体回滚。任一环境一旦写入v2，禁止回滚到v1-only binary；
允许的cutback下限必须继续保留parser/projection v2 writer、v2 exact reader和旧v1 skip规则，只回退
terminal bundle注入等后续行为，或采用向前修复。该不可逆兼容边界是用户批准的clean cutover，不涉及
删除数据库记录、Artifact文件或外部Server变更。

完成定义：聚焦红测先在旧实现上精确复现“Tool 已成功但主 Agent 只收到通用摘要”，最小实现后
转绿；相关 MCP / Orchestration / API 回归、compileall、Ruff 和 `git diff --check` 通过；最后由
全新开发会话证明一次实际调用的 validated Tool Result 已进入主 Agent 上下文，且 oversized fixture
不会被误报为完整。部署前还必须只读证明：可恢复的waiting/approval/input-required/remote-pending
branch中，不存在已完成且revision非v2的Call；若存在则停止部署，不自动取消。尚未产生completed
result的等待任务不因revision为空被误判为旧projection。只有上述证据闭合后，状态才能从
`approved_clean_cutover_hard_defects_resolved` 更新为
`implemented_verified`。

## 10. 上游依据

- OpenAI Function calling 指南要求应用把实际 function/tool output 与对应 `call_id` 一起送回模型，
  再由模型生成最终答复：<https://developers.openai.com/api/docs/guides/function-calling>。
- MCP 2024-11-05 Tools 规范把 `CallToolResult.content` 定义为 Tool 的实际返回；2025-06-18 继续
  保留 `content`，并增加可选 `structuredContent` / `outputSchema`，没有把调度器摘要定义为业务结果：
  <https://modelcontextprotocol.io/specification/2024-11-05/server/tools>、
  <https://modelcontextprotocol.io/specification/2025-06-18/server/tools>。
- Codex 上游在固定提交 `728cb12fe5794b0c3a8e776fb4994b1650b973a8` 中将完整
  `CallToolResult` 转成对应 function-call output，并在明确大小策略下截断，而不是仅返回“已完成”摘要：
  <https://github.com/openai/codex/blob/728cb12fe5794b0c3a8e776fb4994b1650b973a8/codex-rs/core/src/tools/context.rs#L109-L180>。
- 本地 `cc_agent` 固定提交 `3bb6b5746238c418138eb96d57765d79012edd96` 也把 MCP result 与
  `toolUseID` 绑定并加入下一轮消息；该项目仅作为行为参考，不复制其内部实现或依赖。
