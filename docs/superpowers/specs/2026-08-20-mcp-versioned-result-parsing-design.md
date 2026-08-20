# MCP 五版本 Result 解析与业务投影设计

## 状态

- 日期：2026-08-20
- 分支：`main`
- 状态：设计完成，尚未实施
- 决策：采用“版本化 Result Decoder Registry + 统一业务结果模型”
- 范围：Python Client/Gateway 已支持的 `2024-11-05`、`2025-03-26`、
  `2025-06-18`、`2025-11-25`、`2026-07-28` 五版本 Tool Result；覆盖普通调用、
  approval 恢复、2025 Tasks、2026 MRTR/Tasks、remote recovery、Main Agent continuation、
  Task/Conversation Artifact 展示与历史结果读取

## 1. 背景与替代关系

当前 MCP Gateway 把 Adapter 规范化后的完整 Tool Result JSON 写入 durable result，随后将原始 UTF-8
正文作为公共 `text` Artifact 返回给前端。该方式保留了完整结果，但把协议控制字段、元数据、业务内容和
兼容性字段混在同一个用户视图中，并让前端承担隐式解析责任。

本设计替代 `2026-08-20-mcp-result-text-artifact-design.md` 中“公共 API 原样返回完整 JSON 正文”的
展示决定。以下既有保证继续保留：

- durable result 的字节数、SHA-256、content-addressed identity、terminal receipt 和生命周期权威；
- completed Call 后投影、投影失败不回滚业务终态、历史补投、幂等与 no-replay；
- 原始远端结果不进入 event、audit、Node output 或 v2 resume envelope；
- failed、cancelled、unknown Call 不生成成功业务结果 Artifact。

变化是：完整原始 Result 只作为内部协议权威和恢复输入；用户与 Main Agent 只能消费由版本化 Decoder
生成的统一业务结果投影。

## 2. 目标与非目标

### 2.1 目标

1. 每个受支持协议版本独立解析自身合法 Result 结构，不在前端或消费者中散落版本判断。
2. ordinary、approval、remote Task 和 restart recovery 对同一协议结果产生相同规范化业务结果。
3. 用户视图只展示业务内容；Main Agent 只获得有界、脱敏、明确标记为外部数据的业务投影。
4. 原始完整结果继续作为内部 authority，可用于恢复、审计身份校验和重新投影，但不得直接公开。
5. 对必需结构严格校验，对未知扩展字段安全忽略，避免协议漂移造成 raw fallback 或隐式泄漏。
6. `isError=true` 在任何存储形态下都必须先识别为工具业务错误，不能提交 completed Call。

### 2.2 非目标

- 不改变 MCP Transport、JSON-RPC request/response correlation、认证或工具参数协议。
- 不实现 Sampling、Roots、Logging 或新的 MCP 协议版本。
- 不根据 Tool 名称编写 OCR、SQL、CRM 等业务专用解析器。
- 不自动读取 `resource_link` URI，也不把远端 URI 当作可信下载地址。
- 不把未知字段或解析失败结果降级成完整 JSON 展示。
- 不在本设计阶段改变 completed terminal receipt 的 raw result identity。

## 3. 方案选择

### 3.1 未采用：前端或 API 读取时自行解析

优点是改动少、历史结果可直接适配；缺点是 Main Agent、API 和前端会形成多套语义，前端必须理解 MCP
版本，且 terminal commit 前仍无法发现 malformed result 或隐藏在 streamed result 中的 `isError`。

### 3.2 未采用：单一 Parser 内堆叠版本条件

优点是文件少；缺点是 session-era、2025 Tasks 与 2026 `resultType` 联合结果会相互污染，新增协议版本时
容易改变旧版本行为。

### 3.3 采用：版本化 Decoder Registry

每个协议版本拥有独立 Decoder，通过同一 Protocol 输出 `MCPParsedToolResult`。版本差异止于 Decoder；
用户投影、模型投影、Artifact、API 和前端均不感知协议版本。

## 4. 总体架构

```text
Transport response
  -> Protocol Adapter
       - JSON-RPC result/error
       - initialize/discover/session
       - resultType / MRTR / remote Task
       - Task identity and related-task validation
  -> Versioned Result Decoder
       - completed CallToolResult schema
       - content block classification
       - structuredContent/outputSchema
       - isError classification
  -> MCPParsedToolResult
       -> User Result Projector
       -> Main Agent Bounded Projector
       -> Artifact/API projection

Adapter-normalized raw result
  -> durable raw authority
       - terminal identity / recovery / deterministic reprojection only
       - never returned directly to user or Main Agent
```

模块建议：

- `src/integrations/mcp/result_parsing/models.py`：统一模型和闭合枚举；
- `src/integrations/mcp/result_parsing/registry.py`：五版本静态注册表；
- `src/integrations/mcp/result_parsing/decoder_2024_11_05.py`；
- `src/integrations/mcp/result_parsing/decoder_2025_03_26.py`；
- `src/integrations/mcp/result_parsing/decoder_2025_06_18.py`；
- `src/integrations/mcp/result_parsing/decoder_2025_11_25.py`；
- `src/integrations/mcp/result_parsing/decoder_2026_07_28.py`；
- `src/integrations/mcp/result_parsing/projections.py`：用户与 Main Agent 投影；
- `src/integrations/mcp/result_parsing/service.py`：Mapping 与 durable ref 两种输入的统一入口。

不得在 `frontend/`、`artifact_responses.py`、Main Agent prompt builder 或 Coordinator 中新增协议版本分支。

## 5. Decoder 合同

### 5.1 Result Service 输入

```python
MCPResultDecodeRequest(
    protocol_version: str,
    source: MCPResultSource,
    payload: Mapping[str, Any] | MCPDurableResultDescriptor,
    output_schema: Mapping[str, Any] | None,
)
```

`MCPResultSource` 是闭合枚举：

- `tools_call`：普通或 approval 恢复后的 `tools/call`；
- `tasks_result`：2025 `tasks/result`；
- `tasks_get`：2026 `tasks/get` terminal result。

历史 Artifact 或 Main Agent continuation 不是新的 result source。重投影必须从已提交 metadata/authority 恢复
原始 `source`，然后进入同一个 Decoder；不得用 `durable_reprojection` 掩盖 wire 来源。

Result Service 负责读取并复验 `MCPDurableResultDescriptor`，得到 Mapping 后才调用 Registry 中的 Decoder。
单个版本 Decoder 只接收内存中的 Mapping，不负责文件 I/O、生命周期或网络访问。

Task ID、related-task metadata、safe remote task ref 和 request state 的身份验证仍由版本 Adapter/Recovery
Client 完成。Decoder 不建立或持久化远端身份，只解析验证后的 completed Tool Result。

### 5.2 Decoder 输出

Decoder 只能返回 `MCPParsedToolResult`，或抛出闭合的 `MCPResultParseError`。它不得返回原始 Mapping，
不得使用 `{"value": raw}` 兜底，也不得生成用户文案。

### 5.3 结构严格、扩展宽容

- 必需字段缺失或类型错误：解析失败；
- 不合法 `resultType`、非 completed control result 进入 completed decoder：解析失败；
- `outputSchema` 验证失败：解析失败；
- 未知顶层扩展字段：只保留在 raw authority，解析模型忽略；
- 未知 ContentBlock 类型：不进入用户或模型投影，记录闭合诊断码；
- 所有安全内容均为空：返回合法 empty business result，不回退展示 raw JSON。

## 6. 五版本解析规则

### 6.1 `2024-11-05`

- `content` 必须是 ContentBlock 数组；
- 标准内容类型为 `text`、`image`、embedded `resource`；
- `isError` 缺失等价于 `false`，存在时必须为 boolean；
- `structuredContent` 不是该版本标准字段；即使 Server 作为扩展返回，也不得作为可信结构化业务结果；
- `_meta` 不进入统一结果模型。

### 6.2 `2025-03-26`

- 保持 `2024-11-05` 的 `content` 和 `isError` 规则；
- 增加 `audio` ContentBlock；
- `structuredContent` 仍不是标准业务结果字段；
- annotations 只保留闭合且安全的 audience/priority 提示，不作为系统指令。

### 6.3 `2025-06-18`

- `content` 继续为必需数组；
- 增加 `resource_link`；
- `structuredContent` 为可选 JSON object；
- Tool 声明 `outputSchema` 时，存在的 `structuredContent` 必须通过校验；
- 如果 Tool 声明 `outputSchema` 却未返回 `structuredContent`，按协议结果无效处理；
- text block 与 `structuredContent` 的 canonical JSON 完全相同时，标记为 duplicate candidate，实际去重由投影层完成。

### 6.4 `2025-11-25`

普通 `tools/call` 沿用 `2025-06-18` 规则。

Task 路径：

- `tools/call` 返回 CreateTaskResult 时只生成 `task_created` 控制结果，不调用 completed Decoder；
- `tasks/result` 返回结构与原请求结果类型相同；对 Tool Task 来说，它直接是 CallToolResult；
- Adapter 必须先验证 `_meta["io.modelcontextprotocol/related-task"]`，Decoder 随后丢弃 `_meta`；
- ordinary 与 Task 最终结果必须进入同一个 `2025-11-25` completed Decoder。

### 6.5 `2026-07-28`

`tools/call` 首先由 Adapter 按 `resultType` 分流：

- `complete`：进入 completed Decoder；
- `input_required`：进入既有 MRTR/Interrupt 控制流，不产生业务结果；
- `task`：进入 remote Task 控制流，不产生业务结果。

completed Decoder 规则：

- immediate completed payload 必须具有 `resultType="complete"`；
- `content` 必须是 ContentBlock 数组；
- `structuredContent` 可以是任意 JSON 值，包括 object、array、string、number、boolean 和 `null`；
- 必须区分“字段不存在”和“字段存在且为 `null`”；
- Tool 声明 `outputSchema` 时按该 schema 验证任意 JSON 值；
- `tasks/get` 由 Adapter 验证 terminal Task 后，从 `task.result` 取得 CallToolResult；历史兼容允许 Adapter
  接受仓库现有的 root task 形态，但 Decoder 只接收已经解包的 Tool Result；
- `resultType`、Task ID、status、时间戳、TTL、poll interval 和 input requests 都不得进入统一业务结果。

## 7. 统一业务结果模型

### 7.1 JSON 值与 presence

```python
JSONValue = None | bool | int | float | str | tuple["JSONValue", ...] | Mapping[str, "JSONValue"]

MCPStructuredContent(
    present: bool,
    value: JSONValue,
    schema_status: "not_declared" | "valid",
)
```

`present` 不能由 `value is not None` 推导，因为 2026 的显式 `null` 是合法业务结果。

### 7.2 ContentBlock 联合类型

- `MCPTextResultBlock(text, audience, priority)`；
- `MCPImageResultBlock(mime_type, byte_size, sha256, audience, priority)`；
- `MCPAudioResultBlock(mime_type, byte_size, sha256, audience, priority)`；
- `MCPResourceLinkResultBlock(name, title, description, mime_type, uri_scheme)`；
- `MCPEmbeddedTextResourceBlock(uri_scheme, mime_type, text)`；
- `MCPEmbeddedBlobResourceBlock(uri_scheme, mime_type, byte_size, sha256)`。

Binary/Base64 数据不得复制进统一业务结果、Main Agent projection、event 或 API JSON。Decoder 只计算受限
byte size 和 SHA-256 元数据；原始字节继续留在 raw authority。实际公共媒体 Artifact 属于独立后续实施
slice，本轮用户投影只提供类型、MIME 和“存在附件”元数据，不自动拉取或内联二进制正文。

URI 同样不得直接进入 Main Agent；用户视图只可在通过既有 URI policy 后提供安全链接，否则显示名称与 MIME
metadata。

### 7.3 `MCPParsedToolResult`

```python
MCPParsedToolResult(
    protocol_version: str,
    source: MCPResultSource,
    outcome: "succeeded" | "tool_error",
    structured_content: MCPStructuredContent,
    content_blocks: tuple[MCPResultBlock, ...],
    safe_error_code: str | None,
    diagnostics: tuple[MCPResultDiagnostic, ...],
)
```

该模型明确不包含：

- raw result Mapping 或 raw result ref；
- `_meta`；
- `jsonrpc`、request ID、`resultType`；
- Server ID、Task ID、request state、polling 字段；
- credential、header、endpoint 或 transport diagnostics。

raw result ref 继续只存在于 Call/receipt/lifecycle authority，不能因为方便而加入业务结果模型。

`safe_error_code` 只接受既有闭合错误码，或经过长度和字符集限制的远端结构化错误码；无法安全提取时使用
`mcp_tool_error`。

## 8. 投影规则

### 8.1 用户投影

用户投影目标是“展示业务结果，而不是完整 MCP 协议 JSON”。固定优先级：

1. `outcome=tool_error`：展示安全错误码和脱敏后的 text 错误说明；不展示 structured/raw 错误对象。
2. 存在 `structuredContent`：它是 primary business result。
3. 不存在 `structuredContent`：按原顺序拼接 text blocks，作为 primary business result。
4. 非重复的 text block 可作为 structured result 的补充说明。
5. image/audio/resource 只产生安全 metadata 或后续 Artifact，不内联 Base64，不自动读取 URI。
6. 没有安全内容时展示固定文案“工具已完成，但未返回可展示内容”。

去重规则必须确定性：仅当 text 去除外围空白后可解析为 JSON，且其 canonical JSON 与
`structuredContent` 完全一致时才删除该 text block；不得用模糊相似度隐藏真实说明。

公共 API 返回 typed `MCPBusinessResultView`，而不是让前端重新解析 MCP：

```text
schema: maf.mcp.business_result_view.v1
outcome: succeeded | tool_error
primary: structured | text | empty
structured_value?: JSONValue
structured_preview?: string
text?: string
supplemental_texts: string[]
content_metadata: closed safe metadata[]
truncated: boolean
```

API 和事件不得包含 `protocol_version`、raw result/ref 或 `_meta`。协议版本仅用于内部诊断和测试。

为避免再次把 64 MiB result 内联到 Task/Conversation API：

- inline view 使用现有 20,000 字符可信 projection 上限；
- structured canonical JSON 未超限时返回完整 `structured_value`；超限时不返回部分 JSON value，只返回
  UTF-8 安全的 `structured_preview` 文本并设置 `truncated=true`；
- text 超限时按 UTF-8 安全边界截断并设置 `truncated=true`；
- 后续如需要向用户交付完整大结果，应从统一业务模型生成只含业务字段的独立 Artifact，不得恢复 raw JSON
  下载或直接公开内部 raw authority；
- 本设计首个实施 slice 不新增完整大结果下载，因此不会扩大现有权限和文件生命周期范围。

### 8.2 Main Agent 投影

Main Agent 使用相同 `MCPParsedToolResult`，但投影合同更严格：

- 总预算最多 20,000 字符，UTF-8 安全截断；
- 先投影通过 output schema 校验的 structured business result，再加入非重复 text；
- 使用现有 secret key、token pattern 和 URL 策略脱敏；
- image/audio/blob 只说明类型和 MIME，不传 Base64；
- resource link 默认不传 URI；
- 添加固定外部数据声明，明确结果不是系统指令；
- `tool_error` 只传 safe error code 与有界错误说明；
- empty result 传闭合 empty marker，不传 raw。

当前 remote continuation 中的 `metadata["mcp_remote_task_result"]` 不得继续保存完整 Result Mapping；应改为
传递该有界模型投影或其 task-private durable reference。

### 8.3 投影一致性

用户投影和 Main Agent 投影可以有不同预算与 URI/媒体策略，但必须共享：

- outcome；
- structured presence/value 语义；
- content block 分类；
- exact duplicate 判定；
- output schema 验证结果；
- safe error code。

不得在两个 projector 中重新解释协议字段。

## 9. 调用时序与持久化

### 9.1 即时完成

```text
Adapter complete result
  -> Result Service 读取 Mapping 或 streamed result ref
  -> version decoder
  -> isError / schema / structure validation
  -> raw durable result finalize
  -> candidate + receipt terminal commit
  -> parsed user projection
  -> continuation 使用 parsed agent projection
```

解析必须发生在 completed terminal commit 前。解析失败或 `isError=true` 不得产生 completed receipt。

### 9.2 streamed `_mcpResultRef`

`_mcpResultRef` 只是一种内部传输优化，不是完成语义。Result Service 必须：

1. 解析并验证 opaque ref；
2. 从既有 result store 读取受 64 MiB 上限保护的 UTF-8 JSON；
3. 在受限 worker/concurrency gate 中解码；
4. 调用同一版本 Decoder；
5. 先识别 `isError`，再允许 completed terminal commit。

不得因 payload 已落盘而跳过业务解析。这样可以关闭当前 streamed result 隐藏 `isError=true` 的缺口。

### 9.3 2025/2026 remote Task

- Recovery Adapter 完成 Task identity 和 terminal status 校验；
- 2025 从 `tasks/result` 获得 CallToolResult；
- 2026 从 terminal task 的 `result` 获得 CallToolResult；
- remote result persister 前调用对应版本 Decoder；
- 解析成功后 raw result、candidate 和 receipt 沿用现有 authority；
- continuation 重新读取或复用已解析结果，不接收 raw Mapping。

### 9.4 raw authority 生命周期

原始 Result 的定义继续是“Adapter/Gateway 完成 wire normalization 后、与 terminal receipt 绑定的完整字节”，
不是 HTTP headers、SSE frames、credential 或 SDK 内部对象。

公共 Artifact projector 可继续使用既有 internal managed-file copy、identity、CAS 和历史补投机制，但
`source_kind=mcp_result` 文件必须改为 private raw source。Task/Conversation API 不得读取其正文直接返回；API
只能返回已验证的 `MCPBusinessResultView`。

## 10. 历史兼容与迁移

不修改历史 raw bytes，不重放任何 MCP 网络请求。

读取已有 `source_kind=mcp_result` Artifact 时：

1. 通过 lifecycle、Call、receipt 和 Artifact identity 复验 owner/Task/Node/Call/size/SHA；
2. 从 `MCPCallRecord.protocol_version` 选择 Decoder；
3. 2024～2025 completed raw 均按对应 CallToolResult 解析；
4. 2026 payload 含 `resultType="complete"` 时按 immediate result 解析；不含 `resultType` 的历史 remote raw
   必须来自已验证的 remote Task binding/receipt 后才按 inner CallToolResult 解析；
5. 缺协议版本、身份不完整、结构冲突或解析失败时 fail closed，只返回“历史 MCP 结果无法安全解析”，不得回退
   raw text；
6. 新写入的 Artifact private metadata 增加 closed `protocol_version`、`result_source` 和
   `projection_schema=maf.mcp.parsed_tool_result.v1`，但仍以 Call/receipt/lifecycle 为 authority。

现有前端 `mcp-result-artifact:v1:` raw card 改为消费 typed business view；历史 Artifact ID 不变，因此无需数据库
migration 或重新调用 Server。

## 11. 错误与可观测性

闭合解析错误码：

- `mcp_result_json_invalid`；
- `mcp_result_shape_invalid`；
- `mcp_result_content_invalid`；
- `mcp_result_structured_content_invalid`；
- `mcp_result_output_schema_invalid`；
- `mcp_result_type_invalid`；
- `mcp_result_source_mismatch`；
- `mcp_result_projection_unavailable`。

规则：

- JSON-RPC `error` 继续由 Client 映射，不进入 Result Decoder；
- Tool `isError=true` 映射为 `mcp_tool_error` 或安全远端码；
- malformed completed result 映射为非重试协议错误，不重放有副作用 Tool；
- durable ref 读取/identity 失败沿用 storage/lifecycle fail-closed 语义；
- 用户事件只记录 safe Call ref、状态、闭合 reason 和是否 truncated；
- 指标按 protocol version、source、result kind、parse outcome、projection truncation 聚合，不记录 Tool 输出或
  任意远端字段值。

## 12. 安全边界

- MCP Server 输出始终是不可信外部数据；
- structuredContent 通过 schema 只证明形状，不提升为系统指令；
- `_meta`、unknown extensions 和 annotations 不得进入系统 prompt；
- secret pattern、敏感 key、URL 和 URI policy 在用户与 Agent 投影分别执行；
- raw result、raw result ref、internal file path、Server endpoint、credential 和 request state 不得进入公共 DTO；
- 解析失败绝不以 raw fallback 提高可见性；
- 历史重投影只读本地 authority，MCP 网络调用增量必须为 0。

## 13. 测试设计

### 13.1 Decoder 单元测试

每个版本至少覆盖：

- 最小合法 text result；
- `isError=true`；
- 缺失或错误类型的 `content`；
- 各版本合法 ContentBlock；
- unknown block 安全忽略；
- unknown 顶层字段安全忽略；
- 旧版本 `structuredContent` 不被信任；
- 2025-06/11 structured object 和 outputSchema pass/fail；
- 2026 object/array/string/number/boolean/null；
- 2026 absent 与 explicit null 区分；
- exact JSON duplicate 去重候选。

### 13.2 路径等价测试

- ordinary 与 approval 恢复产生相同 parsed result；
- 2025 ordinary 与 `tasks/result` 产生相同 parsed result；
- 2026 immediate complete 与 `tasks/get -> task.result` 产生相同 parsed result；
- live terminal 与 restart recovery 产生相同 parsed result；
- Mapping input 与 `_mcpResultRef` input 产生相同 parsed result；
- streamed `isError=true` 不得提交 completed Call/receipt/Artifact。

### 13.3 投影与泄漏测试

- 用户 API 不含 `jsonrpc`、ID、`resultType`、`_meta`、Task/Server/ref/control 字段；
- Main Agent projection 不含 Base64、raw URI、secret、raw result/ref；
- structured/text exact duplicate 只展示一次；
- 20,000 字符边界 UTF-8 安全并设置 truncated；
- empty/unknown-only result 显示固定闭合文案，不显示 raw；
- tool error 只显示安全错误码和脱敏文本；
- Task/Conversation history 与 live 结果一致；
- 所有历史回填测试网络调用计数为 0。

### 13.4 回归门禁

- 五版本 conformance fixtures；
- Gateway ordinary/approval/remote/recovery tests；
- durable result、terminal candidate/receipt、artifact projection tests；
- Main Agent dependency/continuation context tests；
- API Task/Conversation Artifact tests；
- frontend artifact/history/SSE reducer tests；
- 最终相关后端 suite、前端 test/typecheck/build 和 diff review。

## 14. 实施切片

1. **Parser contract**：模型、Registry、五版本 Decoder、fixtures 与纯单元测试。
2. **Terminal gate**：接入 ordinary、approval、streamed ref、2025/2026 remote Task，关闭 hidden `isError`。
3. **Shared projections**：替换 legacy Executor、Gateway external text 和 remote continuation raw Mapping。
4. **Public result view**：API 只返回 typed business view，前端删除 raw-result parser/card。
5. **Historical reprojection**：按 Call protocol/source 安全读取已有 private raw Artifact，无网络回放。
6. **Rollout**：shadow 比较旧 raw parser 与新 Decoder 的状态/内容分类，确认后 enforce；raw public fallback 永久禁止。

每个切片必须独立可回滚；切片 2 完成前不得把新 parser 作为 terminal authority，切片 4 完成后不得保留旧 raw
公共展示旁路。

## 15. 验收标准

1. 五个受支持协议版本均由独立 Decoder 覆盖，Registry 无默认/猜测 fallback。
2. 所有 completed 路径在 terminal commit 前完成解析和 `isError` 判断。
3. 用户只看到业务结果 typed view，Main Agent 只看到同源有界投影。
4. raw result 继续满足 durable identity/recovery，但不出现在 API、前端、prompt、event 或 audit。
5. 2026 explicit null、2025 Task direct result 和 2026 nested task result 均有回归。
6. 历史结果无需数据库迁移或网络重放；无法确定版本/来源时 fail closed。
7. 当前完整 raw JSON text artifact 展示合同被删除，不存在兼容 raw fallback。
8. 无新增外部依赖；`docker_cmd.md`、`prod` 和部署状态不变。

## 16. AGENTS、CHANGELOG 与依赖影响

本设计新增 Result parsing 模块边界，但尚未修改源码目录。实施时若创建
`src/integrations/mcp/result_parsing/`，必须同步检查根 `AGENTS.md` 的模块索引；前端 Artifact contract
变化时同步 `frontend/AGENTS.md`、API 文档与 `CHANGELOG.md`。

本设计无新增依赖或许可变化。

## 17. 协议参考

- MCP `2024-11-05` Tools：<https://modelcontextprotocol.io/specification/2024-11-05/server/tools>
- MCP `2025-03-26` Tools：<https://modelcontextprotocol.io/specification/2025-03-26/server/tools>
- MCP `2025-06-18` Tools：<https://modelcontextprotocol.io/specification/2025-06-18/server/tools>
- MCP `2025-11-25` Tools：<https://modelcontextprotocol.io/specification/2025-11-25/server/tools>
- MCP `2025-11-25` Tasks：<https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks>
- MCP `2026-07-28` Tools：<https://modelcontextprotocol.io/specification/2026-07-28/server/tools>
