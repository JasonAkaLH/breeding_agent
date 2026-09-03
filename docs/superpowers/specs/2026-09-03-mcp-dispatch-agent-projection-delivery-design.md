# MCP Dispatch 实际 Tool Result 向主 Agent 交付设计

状态：`approved`；用户已确认总体流程，尚未生成实施计划或修改生产代码
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

本设计只完成一个目标：保持现有 Capability、Selector、Gateway、解析、审批、恢复和 Artifact
框架不变，把已经生成的 validated agent projection 作为实际 `mcp.dispatch` Tool Result 交给
主 Agent。

本设计仅替代
`2026-08-29-agent-tool-result-delivery-and-repeat-guard-design.md` 第 3.1 节中“普通 MCP branch
把 Selector safe summary 写入 `text`”的结果承载规则；该文档的 OCR、Skill、结果复用和其余安全
边界继续有效。

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

普通 Selector 路径的模型可见业务结果只来自现有
`MCPSelectorContext.completed_result_projections`：

- 只收集状态为 `completed` 的 Call；
- 每个结果必须具有匹配 intent、owner、Task、Node、Call、Server 版本、result ref、内容摘要、
  parser revision 和 validated checkpoint 的 terminal receipt；
- 通过 `MCPPublishedAgentProjectionAuthority.load_agent_projection()` 读取发布后的模型安全投影；
- 继续使用现有 20,000 code points、80,000 bytes 总预算和 Call sequence 顺序。

不得从 raw result、内部文件、公共 Artifact preview、`result_ref` 或 Selector 自由文本重新构造
业务答案，也不得为补齐结果重新调用 Server。

### 3.2 FINISH 时的结果承载

当 Selector 返回 `finish` 时，Coordinator 按以下确定性规则生成 outcome：

| 条件 | 主 Agent 可见的 Tool Result | 控制面记录 |
| --- | --- | --- |
| 存在一个 validated projection | `agent_projection` 为该 projection 原文 | 持久化 `safe_summary`、`result_ref` 和既有审计事件 |
| 存在多个 validated projections | `agent_projection` 按 Call sequence 顺序，以固定、无歧义的 result 分隔符连接 | 同上 |
| 没有已完成 Call，属于 discover-only / 无调用结束 | 保留现有安全 `text` 摘要 | 同上 |
| 存在已完成 Call但 projection authority 不成立 | 沿现有 typed authority error fail closed | 不伪装成成功，不网络重放 |

`agent_projection` 使用现有字段，不新增 DTO、数据库字段、Artifact 类型或第二个 Tool。多结果拼接
不得改变单个 projection 内容，必须继续受现有总预算和外层 Agent Tool Result 128 KiB preflight
约束；一旦外层 projector 需要收缩，继续使用既有确定性收缩行为。本次不新建截断协议，也不把
既有预算策略改造成新的结果存储机制。

只要存在 `agent_projection`，completed branch 就不得再向模型投放通用成功 `text`。否则模型可能
优先采信“结果可由安全引用获取”而忽略同一 Tool Result 中的业务数据。`finish.reason` 仍可进入
branch 的 `safe_summary`，用于审计、恢复和 UI 安全摘要，但不能充当业务 payload。

### 3.3 外层 Agent Tool Result 合同

结果交付链固定为：

```text
MCP Server CallToolResult
  -> versioned Result Parser
  -> validated agent projection + terminal receipt
  -> MCPSelectorContext.completed_result_projections
  -> Selector 返回 finish
  -> MCPDispatchOutcome.output_payload["agent_projection"]
  -> AgentCallResultProjector MCP allowlist
  -> 同一个 mcp.dispatch call 对应的 committed Tool result
  -> 下一次 AgentModelRequest 的 tool message
  -> 主 Agent 基于实际结果生成最终回答
```

这里的“实际 Tool Result”指经过现有 parser、大小和安全策略处理后的完整或有界
`agent_projection`，不是未经验证的 upstream raw payload。它仍然属于主 Agent 发起的同一次
`mcp.dispatch` Tool call，并继续与该 provider call ID 配对；不会伪装成 user message、system
message、Selector answer 或单独的 Artifact 读取结果。

现有 `AgentCallResultProjector._mcp()` 已 allowlist `agent_projection`，因此本设计不放宽主 Agent
模型视图的 key 集合。以下字段继续不可见：

- raw / structured upstream result；
- 内部 result storage ref、projection path、receipt 和 checkpoint；
- credential、header、session、Server 配置和内部 authority；
- branch 内部 `safe_summary` 与 `result_ref`。

### 3.4 Selector 合同保持不变

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
  终态保持原行为；恢复后的 FINISH 必须走同一 projection carrier 规则。
- `result_ref`、公共 Artifact 和 raw durable authority 继续用于审计、下载或历史恢复，不要求主 Agent
  再调用 Artifact reader。
- discover-only FINISH 没有业务 projection 时保留现有安全摘要，避免把正常目录发现误判为结果缺失。
- `stop`、failed、rejected、cancelled 和 waiting 不伪造 `agent_projection`。
- automatic binding 的既有 `route_another_server` / Server Router 兼容路径不变；每次 Tool 选择仍只
  面向当时已固定的一个 Server，最终 FINISH 使用同一组已验证 completed projections。
- 历史 Run 已提交的 Tool Result 不原地改写；本修复只影响新执行或按现有 continuation 重新完成的
  outcome。

## 5. 预期最小修改面

实施阶段预计只触及：

- `src/integrations/mcp/dispatch_coordinator.py`：在普通 Selector FINISH 边界承载
  `completed_result_projections`，并在有 projection 时禁止通用 `text` 进入模型视图；
- 现有 MCP coordinator / Agent Loop 测试：补充真实 result handoff 回归；
- 必要时增加一个私有纯函数，负责单/多 projection 的确定性拼接，不建立新抽象层。

原则上不修改 `selector.py`、`selector_context.py`、`result_projection.py`：它们已经分别提供控制面
决策、validated projection authority 和 `agent_projection` allowlist。只有红测证明既有预算或投影
合同无法复用时，才允许对这些文件做与 carrier 直接相关的最小修改，并在实施计划中明确理由。

## 6. 测试与验收

### 6.1 聚焦回归

1. 构造一次普通 MCP Call，发布带首尾 sentinel 的 validated projection，Selector 随后以空
   `reason` 返回 `finish`；断言 outcome 含 `agent_projection`，且不含通用成功 `text`。
2. Selector 提供非空 `finish.reason` 时，断言 reason 只写入 branch `safe_summary`，不会覆盖或
   替代 `agent_projection`。
3. 两个成功 Call 的 projections 按 Call sequence 有界、稳定地进入同一 Tool Result；不同运行和
   重启恢复得到相同顺序。
4. discover-only FINISH 继续返回现有安全 `text`；failed、stop、waiting、approval 和 OCR 固定流程
   保持原行为。
5. receipt、owner、Call、Server version、digest 或 checkpoint 冲突时，在主 Agent 再采样前返回
   现有 typed authority error，且 Gateway 调用次数不增加。

### 6.2 Agent Loop 回归

1. `AgentCallResultProjector` 后的 `model_view.agent_projection` 仍包含首尾 sentinel。
2. 下一次 `AgentModelRequest` 中，对应 provider call ID 的 tool message 包含 sentinel；不出现
   raw、path、result ref、credential 或内部 receipt。
3. 端到端 fixture 证明主 Agent 在一次 MCP 外部调用后依据 projection 回答，不因看不到结果再次
   调用相同 Tool。
4. 大小门禁覆盖单结果、多结果和 UTF-8 边界；外层 Tool Result 仍通过 128 KiB preflight。

### 6.3 现场验收

在自动门禁通过后，使用开发环境创建全新会话，执行与原问题等价但不复用历史失败 Task 的请求：

- 主 Agent 选择正确 `server_id`；
- Selector 在该 Server 内选择正确 Tool；
- MCP Call 数为预期值且无补偿重放；
- committed Tool Result 的模型安全视图包含业务 sentinel / 统计字段；
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

实现应保持为单一、可逆的 carrier 改动。若发生异常，可回滚 FINISH 分支的
`agent_projection` 注入和对应测试，不涉及数据库回滚、Artifact 迁移或外部 Server 变更。

完成定义：聚焦红测先在旧实现上精确复现“Tool 已成功但主 Agent 只收到通用摘要”，最小实现后
转绿；相关 MCP / Orchestration / API 回归、compileall、Ruff 和 `git diff --check` 通过；最后由
全新开发会话证明一次实际调用的 validated Tool Result 已进入主 Agent 上下文。只有上述证据闭合
后，状态才能从 `approved` 更新为 `implemented_verified`。

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
