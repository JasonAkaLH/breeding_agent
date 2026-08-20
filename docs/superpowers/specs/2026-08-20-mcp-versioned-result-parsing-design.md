# MCP 五版本 Result 解析与业务投影设计

## 状态

- 日期：2026-08-20
- 分支：`main`
- 状态：经 document-perfectization 循环加固；八检查点仓库实现完成，生产 rollout 未执行
- 决策：采用“版本化 Result Decoder Registry + 统一业务结果模型”
- 范围：Python Client/Gateway 已支持的 `2024-11-05`、`2025-03-26`、
  `2025-06-18`、`2025-11-25`、`2026-07-28` 五版本 Tool Result；覆盖普通调用、
  approval 恢复、2025 Tasks、2026 MRTR/Tasks、remote recovery、Main Agent continuation、
  Task/Conversation Artifact 展示与历史结果读取
- 信心门：最终完整评分见第 23 节；仓库实现与Linux/source-deleted门禁已完成，真实PostgreSQL和production evidence不属于已完成状态

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
5. 对必需结构和ContentBlock联合类型严格校验，只安全忽略协议允许的未知顶层扩展字段，避免协议漂移造成
   raw fallback、静默丢结果或隐式泄漏。
6. `isError=true` 在任何存储形态下都必须先识别为工具业务错误，不能提交 completed Call。

### 2.2 非目标

- 不改变 MCP Transport、JSON-RPC request/response correlation、认证或工具参数协议。
- 不实现 Sampling、Roots、Logging 或新的 MCP 协议版本。
- 不根据 Tool 名称编写 OCR、SQL、CRM 等业务专用解析器。
- 不自动读取 `resource_link` URI，也不把远端 URI 当作可信下载地址。
- 不把未知字段或解析失败结果降级成完整 JSON 展示。
- 不在本设计阶段改变 completed terminal receipt 的 raw result identity。

### 2.3 用户、干系人与受影响系统

| 角色或系统 | 目标与影响 |
|---|---|
| 最终用户 | 看到 Tool 的业务结果和安全错误状态，不再看到 MCP envelope、`_meta`、Task 控制字段或完整 raw JSON |
| Main Agent / Selector | 消费与用户视图同源、20,000 字符有界、脱敏且明确标记为外部数据的结果投影 |
| MCP Server 集成方 | 合法结果按 negotiated protocol version 和声明的 `outputSchema` 校验；非法结果收到稳定非重试协议错误 |
| Gateway / Adapter / recovery worker | 在 terminal commit 前完成版本分流、Task 解包、结果解析和 `isError` 判定，并保持 no-replay |
| Storage / lifecycle / Artifact | raw result 继续承担 identity/recovery；新增结果来源和 output schema 快照权威；公共读取只返回业务视图 |
| API / frontend | 消费闭合 typed DTO，不再识别 MCP wire 字段或把 `storage_ref` 当 raw result 解析 |
| 运维与发布 | 通过 shadow、历史本地重投影和 safe-hide rollback 放量；任何回滚均不得重新公开 raw JSON |

### 2.4 当前状态与代码证据

| 现状 | 仓库证据 | 本设计要求 |
|---|---|---|
| Gateway 对普通 Mapping 只检查顶层 `isError`，遇到 `_mcpResultRef` 直接记 completed | `src/integrations/mcp/gateway.py::_normalize_outcome` | streamed ref 必须回读并通过同一 Decoder，先判定 `isError` 再允许 terminal commit |
| streaming parser 会先 finalize result，再以 `_mcpResultRef` 替换 response result | `src/integrations/mcp/streaming_response.py::IncrementalJSONRPCResultParser.finish` | streamed 路径允许 raw 文件先 finalize，但 terminal authority 必须等待解析；失败时精确删除 data+manifest |
| Call 只持久化 `input_schema_sha256`，没有 output schema 快照或 terminal result source | `src/core/models.py::MCPCallRecord` | 新 Call 必须持久化有界 output schema 快照/digest，并在 terminal commit 固化结果来源 |
| remote binding 只保存协议版本和 continuation plan | `src/core/models.py::MCPRemoteTaskBinding` | recovery worker 通过 `call_ref` 读取 Call 上的 output schema/result parser authority，不依赖临时 catalog |
| remote continuation 当前把完整 Result Mapping 放入 orchestration metadata | `src/api/runtime.py::_consume_mcp_continuation_command` | 改为 task-private bounded projection ref，不把 raw Mapping 放入 request metadata |
| OCR job workflow内部`start/get/ack`直接读取`structuredContent`和`isError` | `src/integrations/mcp/job_workflows.py` | 每个内部tools/call使用对应版本Decoder；仍只由最终polled业务结果生成一个Call terminal/Artifact |
| Artifact API 当前读取内部文件并把完整 UTF-8 raw body 填入 `storage_ref` | `src/api/artifact_responses.py::artifact_response` | 返回 response-only `artifact_type=mcp_result` 与 typed business view，`storage_ref` 固定为空 |
| 前端以 Artifact ID 前缀识别 raw text 并在 `<pre>` 展示 | `frontend/src/domain/artifacts.ts::parseMCPResultTextDisplays` | 改为解析后端闭合 DTO；删除 raw MCP JSON parser/card |
| 2026 remote fixture 的内层 CallToolResult 缺少必需 `resultType` | `tests/integrations/mcp/test_2026_07_28_adapter.py` | 新结果必须严格要求；仅对设计实施前、authority 完整的历史 remote raw 允许 closed legacy compatibility |

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
       -> task-private parsed projection ref

Adapter-normalized raw result
  -> identity-bound staged raw
       - terminal commit 后才成为 durable raw authority
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

Result Service 必须接收 `MCPCallRecord` 上的 authoritative `protocol_version`、`output_schema` 和
`output_schema_sha256`。live terminal 前的 `source` 是 Adapter 按当前已验证分支产生的 candidate，并由 terminal
writer 与 receipt 原子固化为 `terminal_result_source`；历史/重投影只能读取已固化字段，或使用第10节的 closed
旧 binding proof。调用方传入的临时 catalog、前端 Artifact metadata 或 raw payload 中的自述版本/来源不得覆盖
这些权威字段。

解析完成后生成两个非权威、可确定性重建的派生物：

- `user_view`：最多 20,000 Unicode code points、最多 80,000 UTF-8 bytes 的闭合 DTO；
- `agent_projection`：最多 20,000 Unicode code points、最多 80,000 UTF-8 bytes 的外部数据投影。

两者以 `maf.mcp.parsed_result_projection.v1` task-private content-addressed projection 保存，Call/remote
continuation 只持久化 opaque projection ref 和 SHA-256。projection ref 不是 terminal authority；缺失时可从仍受
identity 校验的 raw authority 重建，重建不得访问 MCP 网络。

## 5. Decoder 合同

### 5.1 Result Service 输入

```python
MCPResultDecodeRequest(
    protocol_version: str,
    source: MCPResultSource,
    payload: Mapping[str, Any] | MCPDurableResultDescriptor,
    output_schema: Mapping[str, Any] | None,
    output_schema_sha256: str | None,
    historical_compatibility: bool = False,
)
```

`MCPResultSource` 是闭合枚举：

- `tools_call`：普通或 approval 恢复后的 `tools/call`；
- `tasks_result`：2025 `tasks/result`；
- `tasks_get`：2026 `tasks/get` terminal result。

历史 Artifact 或 Main Agent continuation 不是新的 result source。重投影必须从已提交 metadata/authority 恢复
原始 `source`，然后进入同一个 Decoder；不得用 `durable_reprojection` 掩盖 wire 来源。

Result Service 在调用 Registry 前把两种输入都收敛为 identity-bound staged raw descriptor：streamed 路径复用
Transport 已生成但尚未取得业务终态的 descriptor；Mapping 路径由隔离 materializer 子进程把 strict JSON 直接
写入父进程预创建的 O_EXCL/no-follow result sink，父进程只接收 size/SHA/descriptor，不接收序列化后的大正文。
staged raw 的 finalize 只表示字节与 manifest 可复验，不表示 Tool 业务完成。Decoder 子进程从只读 descriptor
得到内存 Mapping 后调用 Registry；单个版本 Decoder 不负责生命周期、terminal commit 或网络访问。

Mapping 只允许用于 Transport 已按原始 response bytes 测量为不超过64 KiB的小结果；超过64 KiB时，Transport
必须在构造大 Python Mapping 前把 result 写入受64 MiB上限保护的 result sink并只交付 descriptor。没有原始长度
证据的 SDK Mapping 不得进入 live enforce 路径。这样父进程fair queue最多只保留8个小 Mapping/descriptor handle，
不会因“解析已隔离”而暗中排队多个64 MiB Python对象。

materializer 与 parser 共用容量为1、按现有owner/request公平队列语义调度的隔离worker gate；所有输入形态都在
隔离子进程执行strict JSON序列化/解析、递归校验、canonicalization、Base64 size/SHA计算、jsonschema validation
和两个projector。子进程wall timeout固定10秒，
address-space hard cap 512 MiB；超时、OOM、signal或非法输出时父进程必须terminate并重建worker，返回闭合
`mcp_result_parser_worker_failed`。等待和运行期间必须保持dispatch/remote claim续租；取消必须可从队列移除或
终止当前job，且不得在迟到的子进程结果后提交terminal。

gate只排队identity-bound descriptor或已证明≤64 KiB的小Mapping，固定每实例最多8个queued job、每owner最多2个，最长等待30秒；超限不得
继续增长内存或staged storage。由于Tool可能已经产生副作用，排队超限、等待超时和worker基础设施失败必须走既有
`unknown_no_replay`收敛并精确discard staged对象，不能伪装成Server协议错误或重放Tool。相反，worker已成功判定的
malformed result是确定性非重试协议失败。上述queue/owner/deadline常量属于本schema revision，不做运行时任意配置。

父进程不得接收完整 `MCPParsedToolResult`、序列化后的 raw 正文或任意64 MiB业务树。子进程在 Decoder 完成后
先返回最多4 KiB的`maf.mcp.validated_result_checkpoint.v1`，绑定parser revision、protocol/source、raw/schema SHA、
`succeeded | tool_error | malformed`闭合outcome、统一模型digest和闭合diagnostics；父进程复验checkpoint后即可
作terminal判定。随后同一子进程从仍在子进程内的统一模型生成最多192 KiB的
`maf.mcp.parsed_result_projection.v1`闭合envelope。小型job workflow控制结果使用同一envelope中的闭合
`workflow_control`分支，hard cap 64 KiB，不开放通用raw/structured passthrough。

worker在validated checkpoint前失败属于结果未知，进入`unknown_no_replay`；`malformed`checkpoint进入确定性协议
失败；`tool_error`checkpoint进入既有Tool失败。`succeeded`checkpoint一经父进程复验，后续projector exception、
timeout、worker crash、projection store/CAS失败都只能导致safe unavailable和本地补偿，不得阻止或回滚completed
terminal，也不得重放Tool。checkpoint本身不是公共结果或历史重投影输入，不得写入API/event/prompt。

`historical_compatibility` 只能由“Call 创建时间早于本设计 enforce cutover、Call/receipt/lifecycle identity 完整、
且本地 raw authority 通过 size/SHA 复验”共同派生。API、前端、Server 返回字段和普通调用方不得自行打开。

Task ID、related-task metadata、safe remote task ref 和 request state 的身份验证仍由版本 Adapter/Recovery
Client 完成。Decoder 不建立或持久化远端身份，只解析验证后的 completed Tool Result。

### 5.2 Decoder 输出

Decoder 只能返回 `MCPParsedToolResult`，或抛出闭合的 `MCPResultParseError`。它不得返回原始 Mapping，
不得使用 `{"value": raw}` 兜底，也不得生成用户文案。

### 5.3 结构严格、扩展宽容

- 必需字段缺失或类型错误：解析失败；
- 不合法 `resultType`、非 completed control result 进入 completed decoder：解析失败；
- `isError` 必须在 output schema 前分类；`isError=true` 仍校验 CallToolResult/content 结构，但不得要求错误
  `structuredContent` 符合成功结果的 output schema；
- succeeded result 的 `outputSchema` 验证失败：解析失败；
- 未知顶层扩展字段：只保留在 raw authority，解析模型忽略；
- negotiated version 未定义的 ContentBlock 类型：协议结构失败；不得忽略后提交 empty completed result；
- 所有安全内容均为空：返回合法 empty business result，不回退展示 raw JSON。

Mapping 与 streamed JSON 都必须通过同一 JSON 值规范化：拒绝 duplicate object keys、NaN/Infinity、lone
surrogate、非字符串 object key 和非 JSON Python 类型。Mapping 已由 Transport 解析而无法再观察 duplicate key 时，
仍必须递归验证可序列化类型；streamed raw 使用 `object_pairs_hook` 在边界拒绝 duplicate key。

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
- text-bearing block（text 或 embedded text resource）与 `structuredContent` 的 canonical JSON 完全相同时，
  标记为 duplicate candidate，实际去重由投影层完成。

`outputSchema` 在 catalog freeze 时就必须校验并规范化：无 `$schema` 时使用 JSON Schema 2020-12；显式
draft-07 时使用 Draft 7；其他或无效 dialect 排除该 Tool 并记录安全诊断。Result Decoder 只使用 Call 上冻结的
schema snapshot，不读取 Server 当前 catalog。该规则同时适用于 `2025-11-25` 和 `2026-07-28`。

schema必须自包含：`$ref`只允许当前document内的`#` fragment；任何网络、file、relative external或其他文档
reference都在catalog freeze排除该Tool，validator不得配置可访问网络/文件的resolver。`pattern`等可能高CPU的
关键字只在第5.1节隔离子进程中执行，并受10秒/512 MiB硬门禁。

### 6.4 `2025-11-25`

普通 `tools/call` 沿用 `2025-06-18` 规则。

Task 路径：

- `tools/call` 返回 CreateTaskResult 时只生成 `task_created` 控制结果，不调用 completed Decoder；
- `tasks/result` 返回结构与原请求结果类型相同；对 Tool Task 来说，它直接是 CallToolResult；
- 2025 Tool Task因底层`isError=true`可以处于`failed`状态，但Adapter仍必须读取`tasks/result`；如果它是成功
  JSON-RPC response中的`CallToolResult(isError=true)`，交给Decoder形成Tool失败，不得提前误映射成transport错误；
  `tasks/result`自身返回JSON-RPC error时才沿用Client协议错误路径；
- Adapter 必须先验证 `_meta["io.modelcontextprotocol/related-task"]`，Decoder 随后丢弃 `_meta`；
- ordinary 与 Task 最终结果必须进入同一个 `2025-11-25` completed Decoder。

本文只修改最终 `tasks/result -> CallToolResult` 的业务解析，不借机重写 2025 experimental Tasks 控制协议。
仓库现有 CreateTaskResult fixture 与官方最终 2025-11-25 schema 的嵌套 `task` 形态存在历史差异；该差异必须
作为独立 conformance debt 记录，不能用 Result Decoder 的通过掩盖，也不阻止最终 CallToolResult parser 独立交付。

### 6.5 `2026-07-28`

`tools/call` 首先由 Adapter 按 `resultType` 分流：

- `complete`：进入 completed Decoder；
- `input_required`：进入既有 MRTR/Interrupt 控制流，不产生业务结果；
- `task`：进入 remote Task 控制流，不产生业务结果。

completed Decoder 规则：

- `tools_call` 与 `tasks_get` 解包后的 CallToolResult 都必须具有 `resultType="complete"`；
- `content` 必须是 ContentBlock 数组；
- `structuredContent` 可以是任意 JSON 值，包括 object、array、string、number、boolean 和 `null`；
- 必须区分“字段不存在”和“字段存在且为 `null`”；
- Tool 声明 `outputSchema` 时按该 schema 验证任意 JSON 值；
- `tasks/get` 由 Adapter 验证 terminal Task 后，从 `task.result` 取得 CallToolResult；历史兼容允许 Adapter
  接受仓库现有的 root task 形态，但 Decoder 只接收已经解包的 Tool Result；设计 enforce cutover 后的内层
  CallToolResult 缺 `resultType` 必须失败；
- 只有`status="completed"`的2026 Task才解包`task.result`；`failed/cancelled`没有成功业务结果，按Task状态进入
  既有失败/取消路径，不调用completed Decoder；
- `resultType`、Task ID、status、时间戳、TTL、poll interval 和 input requests 都不得进入统一业务结果。

仅对 cutover 前持久化、identity 完整的 2026 remote raw，`historical_compatibility=true` 时允许内层
CallToolResult 缺少 `resultType`，并记录 `legacy_missing_result_type` 诊断；任何 live、ordinary 或新 remote
结果均不得使用这项兼容。

## 7. 统一业务结果模型

### 7.1 JSON 值与 presence

```python
JSONValue = None | bool | int | float | str | tuple["JSONValue", ...] | Mapping[str, "JSONValue"]

MCPStructuredContent(
    present: bool,
    value: JSONValue,
    schema_status: "not_supported_by_version" | "not_declared" | "valid" | "unavailable_legacy",
)
```

`present` 不能由 `value is not None` 推导，因为 2026 的显式 `null` 是合法业务结果。

- `not_supported_by_version`：2024-11-05 / 2025-03-26；
- `not_declared`：支持 structured output，但 Tool 没有 output schema；
- `valid`：按冻结 schema snapshot 验证通过；
- `unavailable_legacy`：仅用于 cutover 前且 output schema snapshot 不可恢复的历史 Call，不得用于 live result。

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

image/audio/blob 必须使用strict Base64解码后计算byte size/SHA；无效Base64或ContentBlock必需字段错误
均使结果解析失败。image/audio缺MIME失败；embedded blob的可选MIME缺失时使用
`application/octet-stream`。MIME最多255个ASCII字符并规范化为`type/subtype`；不合法可选MIME只投影
`application/octet-stream`，但不得把原值写入日志。annotations只接受`user|assistant` audience、0到1的finite
priority；未知annotation key忽略，已知key类型错误使该ContentBlock失败。

URI 不进入统一业务结果或 Main Agent。首个实施 slice 的用户视图只显示 URI scheme、名称与 MIME metadata，
不提供可点击链接；后续若需要资源访问，必须从 raw authority 重新读取 URI、通过独立授权和 endpoint policy，
不得仅凭本模型的 metadata 发起请求。

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

`safe_error_code` 只接受平台既有闭合映射。workflow明确使用的远端码（当前仅`RESULT_NOT_READY`、
`QUEUE_FULL`）可先映射到平台闭合码；其他任意远端结构化码不得直接成为事件、API、指标或用户文案，统一使用
`mcp_tool_error`。

`diagnostics` 是闭合、低基数、内部枚举，只允许：`legacy_output_schema_unavailable`、
`legacy_missing_result_type`、`structured_text_duplicate`、`user_projection_truncated`、
`agent_projection_truncated`。不得把未知字段名、Tool 输出、URI、MIME 原值或异常消息塞入 diagnostics。

structured/text-bearing duplicate 必须通过确定性 canonical bytes 判断：严格解析正文 JSON 后，以 UTF-8、
`sort_keys=true`、`separators=(",", ":")`、`allow_nan=false` 重新序列化，并按 bytes 比较；不得使用 Python
宽松相等（例如 `true == 1`）或模糊相似度。该 canonicalizer 独立于不接受 float 的 CP7 identity
canonicalizer，不得误用 `cp7_artifacts.canonical_json_bytes()`。

## 8. 投影规则

### 8.1 用户投影

用户投影目标是“展示业务结果，而不是完整 MCP 协议 JSON”。固定优先级：

1. `outcome=tool_error`：Gateway 进入既有 failed Call 路径，只通过安全事件/错误状态展示 safe code 和有界、
   脱敏 text 说明；不得创建成功 Result Artifact，也不得展示 structured/raw 错误对象。
2. 存在 `structuredContent`：它是 primary business result。
3. 不存在 `structuredContent`：按 ContentBlock 原顺序拼接 text block 与已经内联返回的 embedded text resource
   正文，作为 primary business result；这不包括重新读取 resource URI。
4. 存在 structured result 时，非重复 text 与 embedded text resource 正文可作为补充说明。
5. image/audio/embedded blob/resource link 只产生安全 metadata 或后续 Artifact，不内联 Base64，不自动读取 URI。
6. 没有安全内容时展示固定文案“工具已完成，但未返回可展示内容”。

去重规则必须确定性：仅当 text-bearing block 正文去除外围空白后可解析为 JSON，且其 canonical JSON 与
`structuredContent` 完全一致时才删除该正文；不得用模糊相似度隐藏真实说明。

成功 Call 的公共 API 返回 typed `MCPBusinessResultView`，而不是让前端重新解析 MCP：

```text
schema: maf.mcp.business_result_view.v1
availability: ready | unavailable
outcome: succeeded
primary?:
  {kind: structured, value: JSONValue, truncated: false}
  | {kind: structured_preview, preview: string, truncated: true}
  | {kind: text, text: string, truncated: boolean}
  | {kind: empty, message: string, truncated: false}
unavailable_reason?: safe_hide | projection_missing | historical_authority_invalid | projection_invalid
supplemental_texts?: string[]
content_metadata?:
  - {kind: image | audio | embedded_blob_resource, mime_type: string, byte_size: int, sha256: string}
  - {kind: resource_link, name: string, title?: string, description?: string, mime_type?: string, uri_scheme: string}
  - {kind: embedded_text_resource, mime_type?: string, uri_scheme: string}
projection_truncated: boolean
```

`availability=ready` 必须有且只能有 `primary`，不得有 `unavailable_reason`，并携带
`projection_truncated`；`availability=unavailable` 必须有闭合 reason、`projection_truncated=false`，不得有
primary/supplemental/content metadata。`empty` 表示已成功解析且业务内容为空，不能用来表示解析失败、
safe-hide或历史authority不完整。

API response 对 MCP Result Artifact 使用 response-only `artifact_type="mcp_result"`、`storage_ref=""`，并在
新增可选字段 `mcp_business_result` 中返回上述 strict DTO。非 MCP Artifact 的 DTO 行为保持不变。API 和事件的
投影 envelope 不得复制 wire `protocol_version`、raw result/ref、外层 `_meta` 或外层 `resultType`；
`structuredContent` 内部恰好同名的业务 key 不得仅按名称误删，仍需经过通用 sensitive-key sanitizer。

后端 `MCPBusinessResultView`、四种 primary variant 和三种 content metadata variant 必须 `extra="forbid"` 并以
`kind` discriminator校验；`byte_size`必须是非负整数，`sha256`必须匹配`sha256:`加64位小写hex，URI只允许
规范化scheme且不含其余URI。前端 parser 必须检查schema、closed outcome、primary字段组合、metadata union、
数组长度和总字符预算。DTO未知schema或非法组合只显示安全不可用状态，不能读取`storage_ref`补偿。

为避免再次把 64 MiB result 内联到 Task/Conversation API：

- inline view 使用现有 20,000 字符可信 projection 上限；
- structured canonical JSON 未超限时返回完整 `primary.value`；超限时不返回部分 JSON value，只返回
  UTF-8 安全的 `structured_preview` 文本并设置 `truncated=true`；
- text 超限时按 UTF-8 安全边界截断并设置 `truncated=true`；
- 20,000 code points/80,000 bytes 是整个 user view 的序列化预算，不只是 primary；primary 优先，supplemental
  和 metadata 按 wire order 加入；primary截断或其余项被丢弃时设置 `projection_truncated=true`，不得突破
  envelope 上限；
- 后续如需要向用户交付完整大结果，应从统一业务模型生成只含业务字段的独立 Artifact，不得恢复 raw JSON
  下载或直接公开内部 raw authority；
- 本设计首个实施 slice 不新增完整大结果下载，因此不会扩大现有权限和文件生命周期范围。

所有 text、structured、supplemental 和 content metadata 字符串在组成 view 前执行现有递归 sensitive-key、
secret pattern、URL 策略和 UTF-8 校验；name/title/description各自最多1,024 code points，URI scheme必须匹配
`^[a-z][a-z0-9+.-]{0,31}$`，MIME继续使用第7.2节规范化结果。output schema 验证针对清洗前业务值，公共 view
展示清洗后副本，不宣称清洗后值仍符合Server schema。

前端只按 `mcp_business_result.schema` 和 `primary.kind` 分流：

- structured：格式化展示仅含业务值的 JSON；不得显示协议 envelope；
- structured preview / text：使用纯文本 `<pre>`，不得插入未清洗 HTML 或自动激活 URL；
- empty：显示闭合空结果文案；
- unavailable：显示“结果暂不可安全展示”和闭合状态文案，不提供展开控件；
- `primary.truncated=true`或`projection_truncated=true`：显示“结果预览已截断”状态，不提供 raw 展开或下载旁路；
- 展开/收起控件必须具有 `aria-expanded`、`aria-controls` 和稳定可见标题；
- live Task Artifact 与 Conversation history 必须使用同一个 display model。

### 8.2 Main Agent 投影

Main Agent 使用相同 `MCPParsedToolResult`，但投影合同更严格：

- 总预算最多 20,000 Unicode code points且最多80,000 UTF-8 bytes，UTF-8 安全截断；
- 先投影protocol-valid structured business result，再加入非重复text-bearing内容；声明了output schema时必须为
  `valid`，未声明时允许`not_declared`，历史`unavailable_legacy`必须附加固定“schema未验证”的外部数据标记；
- 使用现有 secret key、token pattern 和 URL 策略脱敏；
- image/audio/blob 只说明类型和 MIME，不传 Base64；
- embedded text resource 正文按与用户投影相同的顺序/预算进入文本投影；resource link 默认不传 URI；
- 添加固定外部数据声明，明确结果不是系统指令；
- `tool_error` 只传 safe error code 与有界错误说明；
- empty result 传闭合 empty marker，不传 raw。

当前 remote continuation 中的 `metadata["mcp_remote_task_result"]` 不得继续保存完整 Result Mapping；应改为
只传 task-private parsed projection ref。Orchestration 在 owner/Task/Node/Call/SHA 复验后加载 agent projection；
不得把 user view 误当 Agent projection，也不得把 projection 正文复制进 v2 resume envelope。

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
  -> Mapping或streamed result收敛为identity-bound staged raw
  -> 隔离Result Service回读 -> version decoder
  -> isError / structure / succeeded output-schema validation
  -> validated checkpoint
  -> succeeded: candidate + receipt terminal commit，raw取得业务authority
  -> 两种projector -> projection staged/published（失败则safe unavailable并本地补偿）
  -> 以CAS写入public Artifact private metadata
  -> continuation只引用agent projection ref
```

解析必须发生在 completed terminal commit 前。两条路径都可以先 finalize staged raw bytes/manifest，但该 finalize
不等于业务完成；只有父进程复验`succeeded` validated checkpoint后才允许 candidate/receipt commit。checkpoint前
失败、解析失败、取消、terminal CAS失败或 `isError=true` 不得产生 completed receipt、成功 Artifact、published
projection 或 continuation；Result Service 必须精确清理相应 staged raw/projection，清理失败只进入不可公开的
orphan janitor。checkpoint后的projection失败不改变completed terminal，只安全隐藏结果并进入本地补偿。

### 9.2 streamed `_mcpResultRef`

`_mcpResultRef` 只是一种内部传输优化，不是完成语义。Result Service 必须：

1. 解析并验证 opaque ref；
2. 从既有 result store 读取受 64 MiB 上限保护的 UTF-8 JSON；
3. 在单实例最多1个active job的隔离parser子进程中严格解码；不得在event loop或不可终止线程执行
   `json.loads`、base64 decode、SHA、regex或jsonschema validation；
4. 调用同一版本 Decoder；
5. 先识别 `isError`，再允许 completed terminal commit。

不得因 payload 已落盘而跳过业务解析。这样可以关闭当前 streamed result 隐藏 `isError=true` 的缺口。
若解析失败或 `isError=true`，Result Service 必须对 exact staged ref 调用增强后的 discard：同时删除 data 与
`.manifest.json`、fsync parent、从内存索引移除并记录低基数 cleanup outcome；删除失败进入 orphan janitor，
但仍不得提交 completed authority。

### 9.3 2025/2026 remote Task

- Recovery Adapter 完成 Task identity 和 terminal status 校验；
- 2025 从completed或failed Tool Task的`tasks/result`获得CallToolResult，并由`isError`最终分类；
- 2026 只从`status=completed` task的`result`获得CallToolResult，failed/cancelled不进入Decoder；
- remote result persister 前调用对应版本 Decoder；
- 解析成功后 raw result、candidate 和 receipt 沿用现有 authority；
- continuation 重新读取或复用已解析结果，不接收 raw Mapping。

### 9.4 受控 job workflow

`run_ocr_async_job_workflow` 不再调用私有 `_successful_content` 作为协议解析器。Gateway向workflow注入一个窄的
`decode_internal_tool_result(tool_name, raw_result)` callable，它只能从当前冻结 catalog 查找该Tool descriptor并
调用同一Result Service：

- `start_parse_job`、每次`get_parse_job`和best-effort `ack_parse_job`都先完成版本/shape/isError/schema解析；
- workflow只读取Result Service返回的≤64 KiB闭合`workflow_control`分支；`MCPParsedToolResult`和raw Mapping都不
  离开隔离子进程；
- `RESULT_NOT_READY/QUEUE_FULL`只通过闭合error mapping驱动既有poll/retry；
- 内部步骤不创建平台`MCPCallRecord`、terminal receipt或Artifact；
- start、非最终poll和ack的staged raw在闭合`workflow_control`提取后立即exact discard，不进入projection store；
- 最终成功的`get_parse_job` parsed result才成为外层业务Call的raw/parsed terminal结果；
- ack失败继续best effort且不得改写已取得的业务结果，但必须记录闭合内部步骤指标。

### 9.5 output schema 与 result source 权威

`MCPCallRecord` 新增以下 nullable 字段，并在 SQLite/PostgreSQL 同步迁移：

| 字段 | 写入时机 | 规则 |
|---|---|---|
| `output_schema` | Call reserve/admission | catalog freeze 后的 canonical JSON object；没有声明时为 null；UTF-8 hard cap 256 KiB |
| `output_schema_sha256` | Call reserve/admission | 对 canonical schema bytes 的 `sha256:` digest；schema null 时必须为 null |
| `terminal_result_source` | terminal commit | `tools_call / tasks_result / tasks_get`；nonterminal 必须为 null |

`MCPToolDescriptor` 同步增加 output schema digest。Catalog freeze 必须先按版本 dialect 校验 output schema，
再生成 snapshot；Call reserve 把 snapshot 与 digest写入同一 Call authority。remote worker 通过 binding.call_ref
读取 Call snapshot，禁止重新 discovery 或使用 Server 当前 schema。terminal writer 在同一事务内写
`terminal_result_source`、receipt 和 result ref，避免历史重投影猜测来源。

`MCPValidatedTerminalResultCandidate`与receipt同步增加`result_parser_revision`、`validated_checkpoint_sha256`和
`parsed_model_sha256`。terminal writer必须复验checkpoint中的Call/protocol/source/raw/output-schema identity与当前
candidate逐项一致，并把三个审计字段与receipt原子固化；checkpoint正文不进入公共Artifact，也不取代raw authority。
这些字段只证明当时terminal gate使用的解析结论，历史业务投影仍必须从raw/schema/source authority重算。

旧 Call 的三个字段均可为 null。只有第 10 节的 closed historical compatibility 可以接受缺失；新 Call 缺少
相应 authority 必须在网络调用前或 terminal commit 前失败关闭。

### 9.6 raw authority 与派生 projection 生命周期

原始 Result 的定义继续是“Adapter/Gateway 完成 wire normalization 后、与 terminal receipt 绑定的完整字节”，
不是 HTTP headers、SSE frames、credential 或 SDK 内部对象。

公共 Artifact projector 可继续使用既有 internal managed-file copy、identity、CAS 和历史补投机制，但
`source_kind=mcp_result` 文件必须改为 private raw source。Task/Conversation API 不得读取其正文直接返回；API
只能返回已验证的 `MCPBusinessResultView`。

Artifact private metadata 增加：`visibility=internal_raw`、`protocol_version`、`terminal_result_source`、
`output_schema_sha256`、`projection_schema`、`projection_ref`、`projection_sha256`。公共 response builder 必须
显式识别 `source_kind=mcp_result`，复验 projection ref 后只返回 user view；缺 projection 时返回安全 unavailable
状态，不得把 internal file 退化为普通 File/Text Artifact。

`MCPRawResultAuthorityResolver` 统一解析来源：优先 held durable result；源已因 `artifact_owned` 回收时读取已校验的
internal managed-file copy。它只服务历史/恢复重投影，不对 API 暴露 bytes。parsed projection 与 internal raw
Artifact 同生命周期保留；conversation 强删除沿用现有 Artifact 清理。

task-private projection store 必须使用 `0700`目录、`0600`文件、O_EXCL/no-clobber、file+directory fsync、
O_NOFOLLOW、单链接/owner/mode/inode复验，并在 manifest 绑定 owner/Task/Node/Call、parser revision、raw SHA、
protocol/source/output-schema SHA和projection SHA。API/continuation读取前必须复验完整identity，不能只信opaque ref。

projection 写入分为 `staged` 与 `published` 两态。projector可在terminal前后运行，但staged ref不得写入公共
Artifact、continuation或任何可由API解析的metadata；terminal commit成功后，publisher才能把同一
content-addressed object标记published，并以
revision CAS 绑定 Artifact/continuation metadata。publish/CAS 失败不得回滚或重放已经完成的 Tool，只返回
`availability=unavailable` 并由本地补偿任务从 raw authority 重建。terminal CAS失败、取消或超过24小时仍未绑定的
staged projection 由 janitor 在完整 identity/SHA 复验后精确删除；janitor 不得删除 published 或仍被 active claim
引用的对象。

## 10. 历史兼容与迁移

不修改历史 raw bytes，不重放任何 MCP 网络请求。

读取已有 `source_kind=mcp_result` Artifact 时：

1. 通过 lifecycle、Call、receipt 和 Artifact identity 复验 owner/Task/Node/Call/size/SHA；
2. 从 `MCPCallRecord.protocol_version` 选择 Decoder；Registry 不允许默认版本；
3. 2024～2025 completed raw 均按对应 CallToolResult 解析；
4. terminal source 优先读取新 `terminal_result_source`；旧 Call 只能由已验证的 remote Task binding + receipt
   证明 `tasks_result/tasks_get`，否则不得从 payload 形状猜测；
5. 2026 inner payload 含 `resultType="complete"` 时正常解析；不含 `resultType` 的历史 remote raw 只有在上述
   authority 完整时才以 `historical_compatibility=true` 解析并记录诊断；
6. output schema snapshot 存在时必须验证 digest 和结果；cutover 前 snapshot 不可恢复时使用
   `schema_status=unavailable_legacy`，仍执行结构校验、脱敏和有界投影，但不声称 schema validated；
7. 缺协议版本、来源 authority、身份、结构或 projection digest 时 fail closed，只返回“历史 MCP 结果无法安全解析”，不得回退
   raw text；
8. 历史 reconciler 生成/补齐 task-private projection 和 Artifact metadata 后使用 revision CAS；失败不修改业务
   terminal 状态、不删除仍在既有保留期内的 raw authority、不产生网络请求。

现有前端 `mcp-result-artifact:v1:` raw card 改为消费 typed business view；历史 Artifact ID 不变。Call 表新增
nullable additive columns需要 SQLite/PostgreSQL schema migration，但不重写历史 raw result、Artifact ID 或 receipt，
也不重新调用 Server。

## 11. 错误与可观测性

闭合解析错误码：

- `mcp_result_json_invalid`；
- `mcp_result_shape_invalid`；
- `mcp_result_content_invalid`；
- `mcp_result_structured_content_invalid`；
- `mcp_result_output_schema_invalid`；
- `mcp_result_type_invalid`；
- `mcp_result_source_mismatch`；
- `mcp_result_projection_unavailable`；
- `mcp_result_protocol_version_unsupported`；
- `mcp_result_limit_exceeded`；
- `mcp_result_historical_authority_invalid`；
- `mcp_result_parser_worker_failed`；
- `mcp_result_parser_capacity_unavailable`；
- `mcp_result_raw_discard_failed`。

规则：

- JSON-RPC `error` 继续由 Client 映射，不进入 Result Decoder；
- Tool `isError=true` 在 CallToolResult/content 结构校验后、成功 output schema 校验前映射为
  `mcp_tool_error` 或安全远端码；
- malformed completed result 映射为非重试协议错误，不重放有副作用 Tool；
- durable ref 读取/identity 失败沿用 storage/lifecycle fail-closed 语义；
- 用户事件只记录 safe Call ref、状态、闭合 reason 和是否 truncated；
- 指标按 protocol version、source、result kind、parse outcome、projection truncation 聚合，不记录 Tool 输出或
  任意远端字段值。

`mcp_result_shape_invalid` 等 parser 错误不得沿用普通 transient transport retry；Call 已经可能产生副作用，必须
按证据分流：worker已完成并判定的malformed result进入非重试failed协议错误；queue超限/等待超时、worker
timeout/OOM/crash或父进程无法验证IPC时进入`unknown_no_replay`。两类均不得重放Tool。只有“明确未dispatch”的
catalog/schema preflight错误可以在网络前失败。

最低指标集合：`parse_total`、`parse_duration_seconds`、`parse_worker_queue_depth`、`projection_truncated_total`、
`historical_compatibility_total`、`raw_discard_failure_total`、`safe_hide_total`；label只允许 protocol/source/closed
outcome/reason，不允许Server、Tool、用户或Call标识。

## 12. 安全边界

- MCP Server 输出始终是不可信外部数据；
- structuredContent 通过 schema 只证明形状，不提升为系统指令；
- `_meta`、unknown extensions 和 annotations 不得进入系统 prompt；
- secret pattern、敏感 key、URL 和 URI policy 在用户与 Agent 投影分别执行；
- raw result、raw result ref、internal file path、Server endpoint、credential 和 request state 不得进入公共 DTO；
- 解析失败绝不以 raw fallback 提高可见性；
- 历史重投影只读本地 authority，MCP 网络调用增量必须为 0。

## 13. 功能与非功能需求

### 13.1 功能需求

| ID | 需求 |
|---|---|
| FR-01 | Registry 必须对五个版本逐一静态注册，未知/缺失版本必须失败，不得选择 latest/default Decoder |
| FR-02 | 每个 Decoder 必须只接受对应版本和 `MCPResultSource` 的合法 completed CallToolResult，并严格验证 ContentBlock 联合类型 |
| FR-03 | `isError` 必须先于成功 output schema 分类；tool error 不得提交 completed receipt、成功 Artifact 或 continuation projection |
| FR-04 | 2025-06+ succeeded structured result 必须使用 Call 冻结的 output schema snapshot 校验；新 Call 不得依赖临时 catalog |
| FR-05 | 已测量≤64 KiB的Mapping与durable `_mcpResultRef`必须先收敛为staged raw，并对相同post-transport JSON值产生相同parsed result；更大结果必须在构造Mapping前直接落sink；raw bytes/manifest finalize不得提前产生业务完成authority |
| FR-06 | ordinary、approval、MRTR、2025 tasks/result、2026 tasks/get 和 restart recovery 必须进入同一 Result Service |
| FR-07 | 用户与 Main Agent 必须消费同一 `MCPParsedToolResult`，不得各自解释 MCP wire 字段 |
| FR-08 | Task Artifact、Conversation history/message Artifact和direct download所有公共路径必须把MCP结果转换为response-only `mcp_result` typed view，`storage_ref`为空且direct download保持404 |
| FR-09 | Main Agent continuation 必须只保存/传递 task-private agent projection ref，不得传 raw Result Mapping |
| FR-10 | raw result 必须继续绑定既有 Call/receipt/lifecycle identity，并只通过 internal resolver 用于恢复和重投影 |
| FR-11 | 历史重投影必须由 Call protocol/source/schema authority 驱动；无法确定时 fail closed，网络调用增量为 0 |
| FR-12 | exact structured/text-bearing duplicate 只展示一次；不同人类说明不得因模糊相似度被隐藏 |
| FR-13 | image/audio/blob 首个 slice 只返回 MIME/size/SHA metadata；resource link 只返回 scheme/name/MIME，不自动读取或激活 URI |
| FR-14 | empty result 必须返回固定 typed empty view；任何解析或 projection 失败均不得 raw fallback |
| FR-15 | terminal writer 必须复验validated checkpoint，并原子固化`terminal_result_source`、parser/checkpoint/model digest审计字段；Call reserve必须固化output schema snapshot/digest |
| FR-16 | cutover 前历史 remote 兼容必须由 closed authority 和 `historical_compatibility` 控制；live result 永不使用 legacy 宽容规则 |
| FR-17 | 受控job workflow内部的start/poll/ack结果必须按各自Tool descriptor经过版本Decoder；内部步骤不得各自提交平台Call/Artifact，最终业务结果仍只提交一次 |

### 13.2 非功能需求

| ID | 需求 |
|---|---|
| NFR-01 安全 | raw Result、raw ref、Base64、outer `_meta/resultType`、Task/Server/control authority 不得进入公共 DTO、prompt、event 或 audit |
| NFR-02 资源 | 单raw result hard cap继续为64 MiB、父进程Mapping输入≤64 KiB；materialize/decode共享每实例容量1的公平隔离子进程gate；queue每实例≤8、每owner≤2、等待≤30秒、worker wall≤10秒；serialize/parse/validation/full-payload rehash/regex不得运行在event loop或不可终止线程 |
| NFR-03 内存/复杂度 | parser子进程address-space hard cap 512 MiB；JSON深度≤64、总节点≤100,000、ContentBlock≤1,024、单key≤1,024 code points；job后退出或彻底释放状态，父进程RSS不得随迭代增长 |
| NFR-04 响应 | user/agent projection各最多20,000 Unicode code points且最多80,000 UTF-8 bytes；task-private projection envelope hard cap 192 KiB；只含refs/digests的Artifact private metadata hard cap 16 KiB |
| NFR-05 Schema | 单output schema canonical UTF-8 hard cap 256 KiB；无`$schema`使用2020-12，显式draft-07可用，其他dialect fail closed；`$ref`仅允许本document `#` fragment且validator零网络/文件resolver |
| NFR-06 确定性 | 相同 raw bytes + protocol/source/schema snapshot + parser revision 必须产生逐字节相同 projection 和 SHA |
| NFR-07 可靠性 | completed只依赖已复验`succeeded` checkpoint；checkpoint后projection失败不回滚Tool业务终态，checkpoint前失败不得误提交completed；所有重试均不得重放Tool |
| NFR-08 兼容 | Python Gateway 五版本 ordinary tools 保持；Rust Sidecar 不得宣称超出已验证 `2025-11-25` 路径 |
| NFR-09 可观测 | 指标只使用闭合 protocol/source/outcome/reason/truncated 标签；禁止 Tool 输出和任意远端值进入 label/log |
| NFR-10 可访问 | typed result 卡的展开控件必须键盘可操作并设置 `aria-expanded/aria-controls`；截断和不可用状态必须有文字提示 |
| NFR-11 隐私 | user/agent view 分别执行 sensitive-key、secret pattern、URL/URI 策略；schema validation 不提升外部数据可信级别 |
| NFR-12 回滚 | rollback 只能进入 typed view 或 safe-hide；任何版本均不得重新启用公共 raw JSON text/download |

## 14. 测试设计

### 14.1 Decoder 单元测试

每个版本至少覆盖：

- 最小合法 text result；
- `isError=true`；
- 缺失或错误类型的 `content`；
- 各版本合法 ContentBlock；
- negotiated version 的 unknown block 失败关闭；
- unknown 顶层字段安全忽略；
- 旧版本 `structuredContent` 不被信任；
- 2025-06/11 structured object 和 outputSchema pass/fail；
- 2026 object/array/string/number/boolean/null；
- 2026 absent 与 explicit null 区分；
- text/embedded-text exact JSON duplicate 去重候选。

### 14.2 路径等价测试

- ordinary 与 approval 恢复产生相同 parsed result；
- 2025 ordinary 与 `tasks/result` 产生相同 parsed result；
- 2025 failed Task的`tasks/result -> CallToolResult(isError=true)`进入Tool失败，JSON-RPC error进入Client协议错误；
- 2026 immediate complete 与 `tasks/get -> task.result` 产生相同 parsed result；
- 2026 failed/cancelled Task不读取不存在的result，也不调用completed Decoder；
- live terminal 与 restart recovery 产生相同 parsed result；
- 对相同post-transport JSON值，≤64 KiB Mapping input与durable ref input产生相同parsed result；64 KiB首个超限
  response在构造Mapping前自动走sink/descriptor，64 MiB首个超限在Transport失败关闭；
- streamed `isError=true` 不得提交 completed Call/receipt/Artifact。
- Mapping materializer超限、取消或crash只留下可由exact discard/janitor清理的staged对象，不产生terminal authority。
- 2026 new remote inner result 缺 `resultType` 失败；只有 cutover 前 closed historical fixture 可带
  `legacy_missing_result_type` 通过。
- remote restart 从 Call snapshot 读取 output schema；Server catalog 修改或不可用不影响已派发 Call 解析。
- parser子进程gate排队/运行期间dispatch/remote claim持续续租；取消、10秒timeout、OOM、crash和迟到结果后均不
  提交terminal/projection，下一job可由重建worker正常完成。
- checkpoint前worker失败进入unknown no-replay；succeeded checkpoint后projection timeout/crash仍提交terminal、
  public safe unavailable且本地补偿；checkpoint identity/digest篡改不得提交terminal。
- 第9个instance job、第3个owner job和30秒queue timeout均以`unknown_no_replay`收敛且不重放Tool；worker明确
  判定的malformed fixture才进入非重试protocol failure。
- OCR workflow start/poll/ack分别覆盖success/tool-error/schema-invalid；最终仍只有一个业务Call、receipt和Artifact。

### 14.3 投影与泄漏测试

- 用户 API 不含 `jsonrpc`、ID、`resultType`、`_meta`、Task/Server/ref/control 字段；
- Main Agent projection 不含 Base64、raw URI、secret、raw result/ref；
- structured/text-bearing exact duplicate 只展示一次；
- 20,000 字符边界 UTF-8 安全并设置 truncated；
- empty/unknown-only result 显示固定闭合文案，不显示 raw；
- tool error 只显示安全错误码和脱敏文本；
- Task/Conversation history 与 live 结果一致；
- 所有历史回填测试网络调用计数为 0。
- outer `_meta/resultType` 不出现在 DTO；structured business value 中同名 key 不被误删。
- 64 MiB parser在隔离子进程单航班运行，event-loop heartbeat持续推进，512 MiB address-space/10秒timeout生效；
  恶意regex/deep/wide fixture被终止且父进程/下一job保持健康。
- JSON depth/node/key和1,024 ContentBlock边界分别覆盖最大合法值与首个超限值。
- output schema 256 KiB 边界、unsupported dialect、digest drift 和 Call/source authority drift 均失败关闭。
- output schema本地fragment通过；HTTP/file/relative external `$ref`均在catalog freeze排除且网络调用计数为0。
- terminal成功后projection publish/CAS失败返回safe unavailable，本地补偿可重建且Tool调用计数不增加；terminal
  CAS失败和24小时staged orphan可精确清理，published/active对象不被误删。

### 14.4 回归门禁

- 五版本 conformance fixtures；
- Gateway ordinary/approval/remote/recovery tests；
- durable result、terminal candidate/receipt、artifact projection tests；
- Main Agent dependency/continuation context tests；
- API Task/Conversation Artifact tests；
- frontend artifact/history/SSE reducer tests；
- 最终相关后端 suite、前端 test/typecheck/build 和 diff review。

## 15. 实施切片

1. **Safe-hide rollback floor**：先让所有公共 Artifact path 对 `source_kind=mcp_result` 停止返回 raw body；parser
   未 ready 时只返回安全 unavailable，不改变 terminal authority。
2. **Schema/source authority**：校验 output schema dialect/size，扩展 descriptor 与 Call nullable columns，完成
   SQLite/PostgreSQL additive migration和Repository双读写测试。
3. **Parser contract**：统一模型、Registry、五版本 Decoder、strict JSON、fixtures 与纯单元测试。
4. **Terminal gate**：接入 ordinary、approval、streamed ref、2025/2026 remote Task，关闭 hidden `isError`，
   terminal writer 固化 result source。
5. **Shared projections**：生成 task-private user/agent projection，替换 legacy Executor、Gateway external text 和
   remote continuation raw Mapping；OCR job workflow内部start/get/ack也改用同一Decoder但不新增业务Call。
6. **Public result view**：API 返回 response-only `mcp_result` typed view，前端删除 raw-result parser/card并补
   accessibility。
7. **Historical reprojection**：按 Call protocol/source/schema authority 安全读取已有 private raw Artifact，
   revision CAS 补齐 projection，无网络回放。
8. **Rollout**：shadow 比较后进入 enforce；rollback 只能回 safe-hide，raw public fallback 永久禁止。

每个切片必须独立可回滚；切片4及其前置门禁完成前不得把新parser作为terminal authority；切片1部署后即不得
保留旧raw公共展示旁路，后续任何切片回滚都只能落到safe-hide。

## 16. 验收标准

| AC | 可验证结果 | 需求 |
|---|---|---|
| AC-01 | 五版本独立 Decoder 与 Registry exact-version/unknown-version 测试全部通过 | FR-01, FR-02 |
| AC-02 | ordinary/approval/MRTR/2025 Task/2026 Task/restart 等价矩阵逐字节得到相同 projection | FR-06, FR-07, NFR-06 |
| AC-03 | ≤64 KiB Mapping与descriptor对相同post-transport JSON值等价；更大response不构造Mapping；streamed `isError=true`无completed receipt/Artifact且data+manifest被清理 | FR-03, FR-05 |
| AC-04 | 2025-06+ live succeeded result按冻结schema验证；schema漂移和checkpoint Call/protocol/source/raw/schema identity或digest漂移全部fail closed | FR-04, FR-15, NFR-05 |
| AC-05 | Task/Conversation/message API只返回`artifact_type=mcp_result` typed view、空`storage_ref`、无download/raw字段，direct download为404 | FR-08, NFR-01 |
| AC-06 | Main Agent只加载 owner-bound agent projection ref；metadata/v2 envelope不含raw正文或user view | FR-09, NFR-01 |
| AC-07 | 2026 explicit null、array/scalar、inner `resultType` required及closed historical compatibility均有回归 | FR-02, FR-16 |
| AC-08 | exact duplicate去重、非重复说明保留、empty固定文案、sensitive key/secret/URL清洗通过 | FR-12, FR-14, NFR-11 |
| AC-09 | image/audio/resource metadata合同通过，Base64/raw URI在API/prompt/event泄漏扫描为0 | FR-13, NFR-01 |
| AC-10 | 64 MiB隔离parser测试满足event-loop、512 MiB、10秒terminate/restart、8/2/30 queue门禁及no-replay分流；user/agent/API metadata预算边界通过 | NFR-02, NFR-03, NFR-04 |
| AC-11 | 历史补投在协议/来源/schema缺失时按closed规则处理，MCP网络调用计数为0，CAS竞态幂等 | FR-10, FR-11, FR-16 |
| AC-12 | safe-hide、shadow、enforce、rollback测试证明任何模式均不返回raw JSON | NFR-12 |
| AC-13 | 相关后端套件、前端test/typecheck/build、API文档和最终diff gate通过 | NFR-07, NFR-08, NFR-10 |
| AC-14 | 无新增外部依赖；`docker_cmd.md`、`prod`和部署状态未改变 | 非目标/依赖边界 |
| AC-15 | OCR job workflow内部start/get/ack全部走版本Decoder，内部staged raw被清理，错误语义正确且最终仍只有一个业务Call/Artifact | FR-17 |
| AC-16 | 解析指标只含闭合低基数labels，snapshot测试和日志扫描证明任意Tool/Server/Call值均未进入label或正文 | NFR-09 |

## 17. 依赖与实现可行性

| 依赖/集成点 | 当前能力 | 本设计变化 |
|---|---|---|
| `jsonschema` | 已支持 Draft 7/2020-12；Gateway/Adapter已有使用 | 统一 dialect selector，同时校验 input/output schema；无新依赖 |
| MCP catalog/Gateway | `MCPToolDescriptor.output_schema` 仅内存存在 | 增加 canonical digest和256 KiB限制，并写入 Call snapshot |
| SQLite/PostgreSQL Call schema | 有 protocol version，无 output schema/result source | 三个nullable additive列、双后端migration/Repository映射/约束测试 |
| temporary/durable result store | 64 MiB、content-addressed、iter_bytes、data+manifest | 增强exact discard同时删除data+manifest；向隔离parser提供identity-bound只读输入 |
| MCP result-producing transports | streamed路径已有result sink，普通小结果返回Mapping | 先按raw response bytes分流：≤64 KiB可返回Mapping，更大result直接写sink并返回descriptor；live enforce拒绝无长度证据的SDK Mapping |
| Python multiprocessing/resource | stdlib可启动spawn子进程、terminate并在Linux设置address-space limit | 新增容量1公平gate、materializer/parser worker、10秒deadline、512 MiB cap、closed IPC envelope；无第三方依赖 |
| terminal candidate/receipt | raw result identity和no-replay已存在 | raw identity不变；terminal事务额外固化result source及parser/checkpoint/model digests；projection ref/digest在commit后写入派生Artifact/outbox metadata，缺失可补偿 |
| result Artifact projector | raw复制、CAS、历史reconciler已存在 | raw副本转internal-only；private metadata绑定typed projection ref |
| API `ArtifactResponse` | MCP raw作为text写入`storage_ref` | 新增strict `MCPBusinessResultView`和可选`mcp_business_result`字段；MCP response `storage_ref=""` |
| frontend Artifact display | ID前缀识别`mcp_result_text` | 改为DTO discriminator和四种primary view，删除raw card |
| rollout/observability | 已有用户级MCP rollout与低基数指标 | 增加独立result parser closed mode/metric，不改变Server路由authority |

实现不需要新第三方依赖。Call nullable schema migration、API additive字段和前端新union可以按切片部署；safe-hide
先行保证旧前端遇到新 `artifact_type=mcp_result` 时只是不展示，不会回退显示 raw。

## 18. Rollout 与回滚

独立配置 `MAF_USER_MCP_RESULT_PARSER_MODE` 的闭合模式为 `safe_hide | shadow | enforce`，缺失或未知值必须
按 `safe_hide` 处理，不得存在 `legacy_raw`：

| 模式 | Terminal行为 | 公共行为 |
|---|---|---|
| `safe_hide` | 沿用旧terminal语义，不以新Decoder阻断 | MCP result只显示安全不可用状态，不返回raw |
| `shadow` | 新Decoder旁路运行，不改变terminal；记录闭合分类差异 | 仍safe-hide，不返回新旧正文 |
| `enforce` | 新Decoder在terminal commit前强制执行 | 返回typed business view；失败safe-hide |

固定顺序：

1. 部署 safe-hide rollback floor、nullable schema和观测；
2. 五版本fixtures、历史副本dry-run、streamed error、API泄漏与64 MiB资源门禁全部通过；
3. 内部实例进入shadow，只比较 outcome、block kind、structured presence、projection digest和truncated，不记录正文；
4. 已配置的真实Server逐协议执行ordinary smoke；只有实际启用2025/2026 Tasks的Server才执行对应remote smoke；
5. shadow差异必须全部归入闭合allowlist：`legacy_streamed_error_would_fail`或
   `legacy_raw_projection_replaced`；其他outcome/source/structured-presence/projection-digest差异必须为0，
   raw泄漏与unknown parser version必须为0，才可由operator切enforce；本设计不自动修改生产路由；
6. enforce后运行历史reconciler，按每页1,000条既有keyset/CAS节奏补投，不阻塞Ready；
7. 出现parser crash、资源门禁或API兼容问题时切回safe-hide；不得回滚到公开raw的旧backend版本。

`prod`部署、外部观察窗和operator批准仍受现有 MCP rollout runbook约束；本设计文档和仓库测试不构成
production evidence。

## 19. 风险、已定决策与残余假设

| 类型 | 内容 | 处理 |
|---|---|---|
| 风险 | 64 MiB JSON、恶意regex或jsonschema验证可能放大内存/CPU并卡死线程 | 容量1隔离子进程、512 MiB/10秒硬门禁、terminate/restart和event-loop测试；异常fail closed |
| 风险 | 旧Call没有output schema和result source | 只对authority完整历史启用closed compatibility并标记`unavailable_legacy`；否则safe unavailable |
| 风险 | `tests/fixtures/mcp/messages/create_task_result.json`为top-level task字段，而官方2025-11-25 `CreateTaskResult`为`result.task` | 明确不以本Result PRD宣称修复或通过2025 Task控制面conformance；单独登记债务 |
| 风险 | structured result清洗后不再符合原output schema | schema只验证清洗前Server结果；公共DTO不声明清洗后schema conformance |
| 风险 | typed DTO cutover导致旧前端不识别 | safe-hide先行；backend先返回新type且无raw，旧前端安全忽略，随后发布新前端 |
| 风险 | `RLIMIT_AS`等hard cap在非Linux开发机的语义与生产容器不同 | production gate只在Linux容器判定通过；非Linux测试仍验证deadline/terminate/restart和父进程RSS，不以本地软模拟替代生产证据 |
| 决策 | inline业务预览上限20,000字符/80,000 bytes，不提供完整大结果下载 | 防止重新公开64 MiB raw；完整业务文件属于后续独立授权范围 |
| 决策 | 首个slice不公开media/blob和resource URI正文 | 只展示MIME/size/SHA/scheme metadata；后续访问必须独立授权和policy |
| 决策 | rollback只允许safe-hide | 安全性优先于结果可见性，禁止恢复raw公共展示 |
| 已验证假设 | 现有internal managed-file copy可在raw durable源回收后继续作为重投影输入 | source-deleted history测试已按size/SHA/owner/Task/Node/Call复验并以网络调用计数0通过 |

没有未决业务问题。source-deleted测试已通过；后续任何回归失败仍必须停在safe-hide并修复internal resolver，不能降低authority校验或恢复raw展示。

## 20. AGENTS、CHANGELOG 与依赖影响

本设计已新增`src/integrations/mcp/result_parsing/`模块边界，并同步更新受影响的模块索引、`frontend/AGENTS.md`、API文档与`CHANGELOG.md`。根`AGENTS.md`存在用户先前修改，本实施未覆盖或暂存该文件。

本设计无新增依赖或许可变化。

## 21. 协议参考

- MCP `2024-11-05` Tools：<https://modelcontextprotocol.io/specification/2024-11-05/server/tools>
- MCP `2025-03-26` Tools：<https://modelcontextprotocol.io/specification/2025-03-26/server/tools>
- MCP `2025-06-18` Tools：<https://modelcontextprotocol.io/specification/2025-06-18/server/tools>
- MCP `2025-11-25` Tools：<https://modelcontextprotocol.io/specification/2025-11-25/server/tools>
- MCP `2025-11-25` Tasks：<https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks>
- MCP `2026-07-28` Tools：<https://modelcontextprotocol.io/specification/2026-07-28/server/tools>
- MCP `2026-07-28` Tasks extension：<https://tasks.extensions.modelcontextprotocol.io/specification/draft/tasks>

## 22. 信心标准与循环审查记录

本文按 goal/scope/users/value、功能需求、非功能需求、验收与可测试性、边界与失败模式、依赖与可行性、
测试/rollout/migration/rollback、风险/假设/追踪/一致性八类完整门禁审查。通过条件为总分至少95/100，
且不存在 Blocking 或 Major finding；所有扣分必须是有证据、不会阻止安全实施与验证的 Minor finding。

| 循环 | 审查结果 | 已处理范围 |
|---|---|---|
| 1 | 0 Blocking、7 Major、4 Minor，不通过 | 补齐Call级schema/source authority、`isError`顺序、streamed清理、2026历史兼容、typed DTO、资源上限、safe-hide rollback、干系人/追踪/可访问性 |
| 2 | 0 Blocking、3 Major、2 Minor，不通过 | 纳入OCR内部workflow；用可终止隔离子进程约束恶意regex/schema；统一Mapping/streamed staged raw时序；禁止external `$ref`；补齐projection发布/孤儿清理 |
| 3 | 0 Blocking、4 Major、1 Minor，不通过 | 区分live source candidate与历史source authority；闭合content metadata DTO；保留embedded text业务正文；增加bounded parser queue/no-replay分流；补齐workflow staged raw清理和observability验收 |
| 4 | 0 Blocking、5 Major、2 Minor，不通过 | 限制父进程Mapping上限；补齐metadata脱敏与无schema structured投影；用validated checkpoint解除terminal对projection的错误依赖并固化审计digest；纠正workflow跨进程合同 |
| 5 | 0 Blocking、1 Major、0 Minor，不通过 | 对照官方Tasks规范，区分2025 failed Task仍经`tasks/result/isError`分类与2026仅completed Task解包`task.result` |
| 6 | 0 Blocking、0 Major、2 Minor，通过 | 逐条复核FR/NFR/AC、authority、失败路径、迁移、回滚和术语一致性；剩余两项均转为显式实施期验证门禁 |

## 23. 最终信心评分

| 类别 | 满分 | 得分 | 扣分依据 |
|---|---:|---:|---|
| Goal、scope、users 与 stakeholder value | 15 | 15 | 无 |
| Functional requirements | 20 | 20 | 无 |
| Non-functional requirements | 10 | 10 | 无 |
| Acceptance criteria 与 testability | 15 | 15 | 无 |
| Edge cases 与 failure modes | 10 | 10 | 无 |
| Dependencies 与 implementation feasibility | 10 | 9 | Minor：Linux生产容器中的512 MiB address-space hard cap尚未由实现/运行门禁证明；影响是实施可能需要按容器runtime调整resource设置，不改变产品语义；跟进为AC-10的Linux gate |
| Test、rollout、migration 与 rollback | 10 | 10 | 无 |
| Risks、assumptions、traceability 与 consistency | 10 | 9 | Minor：durable source回收后internal managed-file copy仍可重投影是现有架构支持但尚未由source-deleted history test证明；影响限定为历史结果safe unavailable，不会触发raw fallback或网络重放；跟进为第10、19节的实施阻断测试 |
| **总计** | **100** | **98** | **2个有界Minor；0 Blocking、0 Major** |

最终结论：**Pass with recorded assumptions**。98/100是实施前的设计信心评分；当前八检查点仓库实现已完成，但不得把仓库测试表述为真实PostgreSQL门禁、生产部署或production evidence已经完成。
