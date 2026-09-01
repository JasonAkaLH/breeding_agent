# MCP 数字字符串响应 ID 全协议兼容设计

## 状态

`implemented_verified_with_external_smoke_gap`

用户已批准的全协议、单向兼容方案已完成仓库实现与自动验证；两轮有界硬伤复审累计发现的2个Blocking和2个Major均已闭合。真实Legacy factory initialize成功，tools/list受外部Server额外发送的无ID JSON-RPC result envelope阻断；OCR仅有占位凭据，两个外部缺口均未误报通过，详见同日implementation plan。

## 问题与证据

当前 MCP clients 使用递增整数作为 client-originated JSON-RPC request ID，并在 response correlation 时要求服务端响应 ID 与请求 ID 相等。一次经用户授权的真实 2024 Legacy HTTP+SSE 探测发现：

- 历史 QA `/sse` Endpoint 仍返回标准 `endpoint` event；
- initialize 和 `tools/list` 的 POST 均成功；
- 服务端将整数请求 ID 序列化为同值字符串响应 ID，例如请求 `1`、响应 `"1"`；
- 当前 Legacy pending map 因类型不同把响应视为未知 ID，最终 timeout；
- 使用字符串请求 ID 后，同一真实链路成功协商 `2024-11-05` 并发现 9 个 Tool。

Git 历史同时证明该 QA 服务曾作为本地非规范 smoke 配置存在，后续活跃配置改用标准 `/sse` 路径；历史提交没有形成当前生产 client 对数字字符串响应 ID 的兼容合同。

Endpoint、API key、动态 endpoint UUID、Session/connection identity 和原始响应正文不写入仓库、fixture、测试或日志。

## 目标

- 所有当前 MCP 协议 adapter 接受整数请求 ID 对应的同值整数响应或规范十进制字符串响应。
- 保持单向兼容：字符串请求 ID 不新增对整数响应 ID 的反向宽松匹配。
- 保持现有整数请求 ID 生成策略，不要求服务端或外部 Gateway 修改。
- 只规范化已解析 response 的内存副本，不修改原始 transport bytes。
- 覆盖普通 JSON、request-scoped SSE、Legacy persistent SSE、直接 POST response 和大结果 streaming sink。
- 保持未知/不规范 ID、并发关联、协议版本、Transport 和安全边界不变。

## 非目标

- 不允许 `2024-11-05 + streamable_http` 非标准组合；现有 transport/version gate 不变。
- 不自动切换 `/mcp` 与 `/sse` Endpoint，也不改变 transport auto detection。
- 不把所有 response ID 转成字符串或把所有 request ID 改成字符串。
- 不使用 `str(response_id) == str(request_id)` 全局宽松比较。
- 不接受 `"01"`、`"+1"`、`" 1"`、`"1.0"`、空串或其他非规范字符串别名。
- 不规范化 server-to-client request ID、notification、Tool 参数、Task ID、Session ID 或业务字段。
- 不修改 DTO、schema、数据库、Frontend、Rust、外部 MCP Server、镜像、部署或 `prod`。

## 已批准规则

### 单向等价关系

对于 expected request ID：

```text
expected = integer 1

response id = integer 1
  -> 使用既有精确匹配

response id = string "1"
  -> "1" 是 expected 的规范十进制字符串
  -> 创建 response mapping 副本
  -> 副本 id 改为 integer 1
  -> 继续既有严格处理

response id = string "01" / "+1" / " 1" / "1.0"
  -> 不匹配
```

规范别名只在以下条件全部满足时成立：

1. expected request ID 的具体类型是 `int`，不包括 `bool`；
2. raw response ID 的具体类型是 `str`；
3. `raw_response_id == str(expected_request_id)`。

该判定自然支持正数、零和负整数，拒绝前导零、正号、空白、小数、指数、Unicode 数字和其他表示法。

既有“精确匹配”也必须是类型精确匹配：

- expected 为具体 `int` 时，raw 只有具体 `int` 且值相同才是 exact；
- expected 为具体 `str` 时，raw 只有具体 `str` 且值相同才是 exact；
- `bool`、`float`、`null` 和其他类型不得利用 Python 的宽松相等语义匹配整数或字符串 request ID。

### Exact-first 与并发

- 类型精确且值相等的 response ID 先走原路径；只有未直接关联时才检查单向别名。
- Legacy pending 同时存在整数 `1` 与字符串 `"1"` 时，响应 `"1"` 必须优先关联字符串 pending，响应 `1` 关联整数 pending。
- 只有不存在 exact string pending 时，响应 `"1"` 才可作为整数 pending `1` 的别名。
- Legacy exact lookup 不得只依赖 Python dict 的宽松 key 相等语义，避免 `True` 与 `1` 串线。
- 未找到 exact 或规范别名时，保持现有 unknown/mismatch/error/timeout 行为。

## 组件设计

### `src/integrations/mcp/protocol.py`

新增唯一纯 helper，接收已解析的 response mapping 和 expected request ID：

- 类型和值均精确相等时返回原 mapping；
- 满足单向规范别名时返回只替换顶层 `id` 的浅副本；
- 不匹配时返回 `None`；
- 不修改输入 mapping，不解析 response body，不处理非 response message。

所有 adapter 复用该 helper，不复制整数解析或字符串比较规则。

### `src/integrations/mcp/client.py`

初始化式 2024/2025 base client 在两个位置复用 helper：

- `_require_message()` 在抛出 response ID mismatch 前规范化最终 message；
- `_handle_stream_events()` 对 response event 使用同一匹配规则，避免同值字符串在最终 message 检查前被 SSE event gate 拒绝。

server request、notification 和 unsupported client request 路径不变。

### `src/integrations/mcp/adapter_2026.py`

2026 stateless adapter 对 JSON body 与 request-scoped SSE 的最终 response 在以下检查前规范化：

- request ID match；
- JSON body 与 SSE final response 冲突检查；
- remote error/result 解码。

`Mcp-Method`、`Mcp-Name`、MRTR、Tasks、cache hint、schema 和 recovery identity 不变。

### `src/integrations/mcp/adapter_2025_tasks.py`

独立的 `MCP2025TaskRecoveryClient` 不经过 base client，必须在 `tasks/get`、`tasks/result` 和 `tasks/cancel` response envelope 校验时复用同一 helper。只规范化顶层 JSON-RPC response ID；远端 `taskId`、safe ref、recovery binding 和业务 result 不变。

### `src/integrations/mcp/streaming_response.py`

JSON object 成员顺序没有语义。Legacy persistent response 可能在顶层 `id` 之前先发送 `result`，而已匹配的普通 buffered pending 本身也没有 result sink；因此 parser 不得把“没有 sink”解释为“没有匹配”：

- result selector 必须返回显式私有 target：匹配时携带 normalized response ID 和可空 sink，未匹配才返回 `None`；不得用 sink 是否为空表达匹配状态；
- prefix 尚无顶层 `id` 字段时不得调用 selector或提前判为unknown，只记录为unresolved并写入provisional spool，完整envelope解析后再选择一次；
- ID 已在 `result` 前出现且 target 有 sink 时，继续使用现有 sink fast path；
- ID 尚未出现、target 为已匹配 buffered pending或尚未匹配时，result 必须写入 parser-owned、匿名且有界的 provisional spool，不得继续使用无上限 `bytearray`；
- provisional spool 的 hard cap 复用 `MAX_DURABLE_MCP_RESULT_BYTES`（64 MiB），超过时使用既有 `MCPResultTooLargeError`；临时存储失败沿用既有 temporary-storage typed failure；
- 完整 envelope 解析出 response ID 后，先按 shared helper 做 exact/单向规范化并取得显式 target；target 有 sink时把 provisional bytes分块写入该sink，target 为buffered pending时从有界spool解析并恢复原业务result；
- 关联成功后按现有 buffered materialize、control-result/materialize 或 durable-result/finalize 规则完成，不能形成第二套 result authority；
- unknown/mismatch 必须关闭 provisional spool且不得触碰任何未关联 pending；已关联后的 parse/write/finalize failure 必须 abort 目标 sink；reader cancel/close 继续通过既有 fail-pending 路径 abort 受影响 sink；所有分支都不得暴露临时路径；
- provisional spool 不新增数据库、manifest、公开 ref、配置项或后台清理任务。

该规则保留 ID-before-result 且有 sink 的现有流式快路径，同时把所有无 sink result 有界化；已匹配 buffered pending 与 unknown response 的终态必须不同。

### `src/integrations/mcp/transport_legacy_http_sse.py`

Legacy persistent reader 必须在三处复用 shared helper/规则：

1. pending response correlation：exact key 优先，之后才尝试规范整数别名；
2. result target selection：字符串化 ID 必须选中对应整数 pending；匹配结果以“normalized ID + 可空 sink”的显式私有 target表达，使buffered pending不会与unknown混淆；无sink时使用上述唯一有界provisional spool；
3. 返回 response/event：别名命中时构造 normalized message 和 event 副本，使后续 `MCPClient`、`sse_events` 与最终 message 看到同一整数 ID。

Legacy direct POST response 也必须在返回上层前或由 base client 唯一 final gate 完成相同规范化。不得修改原始 SSE bytes、pending key 或 request body。

## 错误行为

| 场景 | 行为 |
|---|---|
| request `1`, response `1` | 原样成功 |
| request `1`, response `"1"` | 规范化副本为 `1` 后成功 |
| request `"1"`, response `"1"` | 原样成功 |
| request `"1"`, response `1` | 不新增兼容，保持 mismatch/unknown |
| request `1`, response `"01"`/`"+1"`/`"1.0"` | 保持 mismatch/unknown |
| request `1`, response `true`/`1.0`/`null` | 保持 mismatch/unknown |
| response ID 无 pending | 保持 unknown count/timeout 或 protocol error |
| response ID 命中 buffered pending | 从有界 spool 恢复业务 result，不按 unknown 丢弃 |
| 同时 pending `1` 与 `"1"`，response `"1"` | exact string pending 胜出 |
| 同时 pending `1` 与 `"1"`，response `1` | integer pending 胜出 |
| server-to-client request ID 为 `"1"` | 不规范化 |

## 测试设计

### 纯 helper

- 整数 exact、字符串 exact和规范字符串别名；
- 正数、零、负数；
- leading zero、plus、whitespace、decimal、exponent、Unicode digit、bool、float、null、未知字符串拒绝；
- 输入 mapping 不变，只有别名命中返回副本。

### 2024 Legacy HTTP+SSE

- initialize、tools/list 和 tools/call 的整数响应保持通过；
- 服务端字符串化整数 ID 时 initialize + list 通过；
- direct POST JSON response 与 persistent SSE response均覆盖；
- streaming result sink 在字符串化 ID 下仍形成正常 result ref；
- ID前置/后置的known buffered response均从有界spool恢复原业务result；
- `result` 位于 `id` 前且超过内存阈值时使用 provisional spool 后写入正确 sink，进程内存不随 result 全量增长；
- late-ID provisional spool 超过 64 MiB、unknown/mismatch、parse failure、cancel 和 close 时 typed failure/cleanup 闭合；
- integer/string 双 pending 乱序响应保持 exact-first，尤其 exact buffered string pending不得串到alias integer streaming sink；
- 非规范/未知 ID 继续 ignored + timeout，不串线。

### 2025 Streamable HTTP

- JSON response 与 POST SSE response分别覆盖整数和字符串化整数 ID；
- initialize、initialized notification、list/call 的现有状态机不变；
- 真正不匹配 ID 继续抛 `MCPProtocolError`。

### 2026 Stateless

- JSON 和 request-scoped SSE final response分别覆盖字符串化整数 ID；
- JSON/SSE双final仅ID类型不同、规范后内容一致时不误报冲突；
- 非规范/未知 ID、多个final和真实内容冲突继续拒绝；
- MRTR/Tasks/recovery相关 ID 不被误改。

### 2025 Tasks recovery

- `tasks/get`、`tasks/result`、`tasks/cancel` 分别覆盖整数和字符串化整数 response ID；
- 非规范字符串、bool、float、null和真实 mismatch 继续拒绝；
- remote task ID、safe ref和recovery identity断言保持不变。

自动测试使用确定性 fake transport/server，不访问外部 Endpoint，不保存真实 Header、key、响应正文或 Tool descriptor。

## 验证范围

- 纯 protocol、base client、2024 legacy、2025 Streamable HTTP、2026 adapter 聚焦回归；
- Gateway、Health、Tasks/recovery 与 auto negotiation 相关回归；
- MCP integrations 全量；
- compileall、变更面 Ruff、package import、敏感内容静态扫描和 `git diff --check`；
- 最终 diff 确认无 transport/version gate、配置、DTO、schema、数据库、Frontend、Rust、镜像、部署或 `prod` 变化。

真实脱敏 smoke：

1. 历史 QA `/sse + legacy_http_sse + auto` 必须协商 `2024-11-05` 并发现预期 9 个 Tool；
2. 已验证 OCR `streamable_http + auto` 继续协商 `2025-11-25`；
3. 两者只执行 initialize、`tools/list` 和 close，不调用业务 Tool；
4. 输出只保留协议版本、adapter、布尔状态、capability keys和Tool数量/名称，不输出 Endpoint、Header、key、动态 endpoint、Session ID或响应正文。

外部 smoke 失败时必须记录精确环境缺口，不得用它替代确定性自动测试。

## 风险与回滚

主要风险是错误地把业务字符串 ID 当成整数别名，导致并发响应串线。规范十进制单向规则、类型精确的 exact-first 和整数 expected 限制共同收窄该风险；非规范字符串保持原失败语义。

第二个风险是无 sink 同时可能表示ID尚不可见、已匹配buffered pending或unknown response。显式私有target、唯一有界provisional spool、64 MiB hard cap、streaming replay/buffered restore分流和全异常清理共同闭合该风险，不新增持久化authority。

回滚时恢复 shared helper 和本文列出的消费点旧严格比较即可；没有数据、schema、缓存、外部服务或部署回滚。

## 参考

- JSON-RPC 2.0 Request/Response ID：<https://www.jsonrpc.org/specification>
- MCP 2024-11-05 HTTP+SSE：<https://modelcontextprotocol.io/specification/2024-11-05/basic/transports>
- MCP 2025-11-25 Streamable HTTP：<https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- MCP 2026-07-28 release：<https://blog.modelcontextprotocol.io/posts/2026-07-28/>

License Requirement：复用现有 Python、MCP protocol helpers、adapters、streaming parser、typed errors 与 unittest；无新增依赖或许可变化。

## 硬伤复审记录

- 审计轮次：2轮发现，2轮批准修订与复审。
- 已修复 Blocking：Legacy `result-before-id` 绕过 sink并进入无界内存 materialization；现将无sink result写入64 MiB有界provisional spool，关联后按streaming replay或buffered restore完成。
- 已修复 Major：独立 `MCP2025TaskRecoveryClient` 未进入全协议消费面；现已纳入组件和测试。
- 已修复 Major：普通 Python 相等会让 `True == 1`、`1.0 == 1` 绕过批准规则；现将 exact 固定为类型和值均相同，并要求Legacy pending type-aware lookup。
- 已修复 Blocking：旧 selector 的 `None` 同时表示known buffered pending和unknown response，原计划会丢弃initialize/tools/list等正常结果；现以显式私有target区分未匹配、buffered匹配与streaming匹配，并补齐exact buffered优先级测试。
- 修订后硬伤结论：0 Blocking，0 Major；Minor 按用户要求未审计。
