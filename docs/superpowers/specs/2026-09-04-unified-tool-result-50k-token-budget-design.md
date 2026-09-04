# 主 Agent 统一 Tool Result 50k Token 预算设计

状态：用户已批准，限定硬伤复审通过；Backend实现与验证已完成，Frontend后续独立发布

目标分支：`main`；不涉及 `prod` 部署

## 背景

当前 Tool Result 没有单一全局语义预算。MCP Result Parser、Agent carrier、MCP
Selector、API DTO、Frontend、Legacy Skill 和 delegated Skill 分别存在 20,000 字符或
80,000 UTF-8 bytes 限制。开发库中的一次 MCP 调用已经证明：34,148-byte 原始结果完整落盘，
但业务正文先后被 Parser 和 carrier 的 20,000 字符预算截短。

用户最终决定取消字符数和 UTF-8 bytes 业务预算，改为：每个 Tool Result 最多 50,000 tokens，
统一复用项目现有、基于模型 Provider `POST /tokenization` 的 token 计算方法。tokenization 不可用
时必须 fail closed，不允许回退本地 `tiktoken`，并向前端返回明确的模型不可用错误。

本次只影响后续新结果。既有历史结果不补投、不重建、不迁移。

## 已批准目标

- 所有新产生并提供给主 Agent、MCP 业务卡片或 MCP Selector 的 MCP / Skill Tool Result，
  每个业务调用最多保留 50,000 tokens；
- 50,000 tokens 以内完整保留，超过才截断，并沿用明确的 truncation 标记；
- 删除所有 20,000 / 100,000 字符和 80,000-byte 业务裁剪；
- token 计数只使用发起 Tool Call 的 Agent Run 已绑定 model edition 对应 Provider 的
  `POST /tokenization`；
- 每个新 Tool Result 在本业务预算阶段只调用一次 `/tokenization`，不重试、不二分、不复算；
- tokenization 缺少可信配置、请求失败或响应非法时直接 fail closed，不使用 `tiktoken`；
- 一次业务 Tool Call 对应一个逻辑 Tool Result，不拆成多条 AgentItem 或 provider Tool message；
- 同轮多个 Tool Result 各自先应用 50,000-token 上限，再受对应模型的总上下文预算约束；
- 所有阻断当前 Task 的模型侧 API 不可用时统一返回 `model_unavailable`；
- 前端明确提示模型服务暂时不可用；
- 不修改历史数据。

## 非目标

- 不删除 MCP raw result 的 64 MiB 输入安全上限；
- 不删除数据库 AgentItem、临时文件、sandbox、RPC 或进程内存的基础设施安全上限；
- 不公开 MCP 私有 raw result、内部路径、storage ref、凭据或未清洗字段；
- 不修改数据库 schema，不执行历史数据迁移或历史重投影；
- 不把对话标题生成等 best-effort 模型功能失败升级为整个 Task 失败；
- 不修改外部 MCP Server、外部 Skill 源码或 `prod`。

## 方案选择

采用“现有 token counter + Provider-required 模式 + 通用模型不可用错误”方案：

- 扩展现有 token counter 的调用语义，不另写 tokenizer；
- production hot path 使用泛化后的 async 入口；
- MCP、Skill、Selector 和完整模型上下文共享相同 model-edition 配置与远端计数合同；
- 用通用 `model_unavailable` 向前端表达所有必需模型 API 的可用性失败；
- 不新增数据库表或 projection revision。

不采用以下方案：

- 固定字符数：不同语言和内容结构对应的 token 消耗差异过大；
- UTF-8 bytes：字节数不是模型上下文预算；
- 本地 `tiktoken` fallback：无法保证与当前 Provider/model edition 一致；
- endpoint-specific 前端错误码：会让同一种“当前模型服务不可用”产生多套 UI 分支，且暴露不必要
  的 Provider 细节。

## 统一 Token 合同

Backend 定义：

```text
TOOL_RESULT_BUSINESS_MAX_TOKENS = 50_000
MODEL_UNAVAILABLE_ERROR_CODE = "model_unavailable"
```

这里的 50,000 表示发起 Tool Call 的 Agent Run 已绑定 model edition 对应 Provider
`/tokenization` 返回的 `total_tokens`。业务 token 的计数对象是 Tool 完整返回后，经本地协议解析、
schema 校验和脱敏得到的完整安全业务文本：

- 文本结果将正文作为单一 text item 计算；
- structured JSON 使用 `ensure_ascii=False` 的 canonical JSON 文本，JSON key、数值和结构符号均
  计入；
- supplemental text 和可展示 metadata 的文本值计入；
- schema、状态、调用 ID、SHA、size、projection revision、truncation flag 等 closed envelope
  metadata 不计入单个 Result 的 50,000-token 业务额度；
- 完整模型请求的总上下文计算仍包含这些 envelope、消息 framing、Tool schema 和 Tool choice。

Backend 是 token 数值和裁剪语义的唯一权威。Frontend 不复制 tokenizer、不重新计算 token，也不再
执行字符或 UTF-8 byte 预算校验，只验证 typed result schema。

## 复用现有 `/tokenization` 函数

项目继续复用 `src/integrations/token_counter.py`，不新增 tokenizer：

- 泛化 Provider 响应类型，至少返回整数 `total_tokens` 和字符偏移 `offset_mapping`；解析时同时
  校验 `total_tokens == len(token_ids) == len(offset_mapping)`；
- 新增或泛化同步、异步详细 Tokenization 入口；production Tool Result 路径使用 async 入口；
- 详细入口必须显式接收 Agent Run 的 `model_edition`，请求使用
  `{model: model_edition, text: [safe_text]}`；响应中的 model 必须与请求一致；
- 现有 `get_num_of_tokens_*` public function 继续返回 `int`，由内部适配提取 `total_tokens`，保持
  既有调用方兼容；被泛化的内部函数及其所有生产、测试调用点必须同步修改；
- 现有 count-only bounded cache 只服务计数调用；Tool Result 详细路径不得用只有整数的 cache hit
  代替本次完整 Offset 响应；
- 每个 Tool Result 的详细路径恰好发送一次 Provider 请求，不自动重试；
- 有效超时为 `min(configured_timeout, 10 seconds)`，配置不能突破 10 秒硬上限。

Tool Result 预算、主 Agent context preflight 和 MCP Selector 使用 provider-required 模式。该模式下：

- tokenization disabled、API key/base URL/model 缺失时抛出 typed model-unavailable 异常；
- 网络失败、超时、非 2xx、响应非 JSON、`data`/`total_tokens` 合同非法时抛出同一 typed 异常；
- 禁止进入 `_fallback_num_tokens_from_messages()`；
- 错误正文、API key 和 Provider response body 不进入公开 Event 或前端。

## 50,000-token 裁剪

Parser/Skill runtime 先完成 schema、`isError`、敏感内容和 URL 清洗，再把 safe business candidate
交给父进程预算层；隔离 Parser worker 不接收模型凭据，也不直接访问网络。

预算层对完整 candidate 调用一次 `/tokenization`：

1. `total_tokens <= 50,000` 时完整保留；
2. `total_tokens > 50,000` 时取第一个超出预算 Token 的开始字符位置
   `cutoff = offset_mapping[50_000][0]`，保留 `safe_text[:cutoff]`；
3. 不能使用 `offset_mapping[49_999][1]`：emoji 等单个字符可能由多个 Token 共享同一字符区间，
   该写法可能保留完整字符并得到 50,001 Token；
4. 不做二分、重试或第二次 Tokenization 复核；
5. structured result 超限时把截断后的 canonical JSON 文本放入合法 `structured_preview`，不得把
   残缺文本作为 structured JSON value；
6. truncation 提示只写 envelope 字段，不拼入业务正文、不占 50,000-token 业务额度；
7. 任何 tokenization 失败立即 `model_unavailable`，不得改用字符数、bytes 或本地 tokenizer 猜测。

真实 Provider 合同测试已验证：三款已配置 model edition 均返回 HTTP 200，且每项
`total_tokens`、`token_ids`、`offset_mapping` 数量一致；中文、emoji 和组合字符的 offset 是可直接
用于 Python 字符串切片的字符索引。三模型 72,000+ Token 大文本均按上述切点复核为 50,000
Token；构造的共享 emoji offset 边界证明旧的结束位置公式会得到 50,001 Token。

若安全 candidate 因基础设施容量无法交给父进程或 Provider tokenization endpoint，本次结果 fail
closed；不得通过隐藏字符上限或 byte 截断伪装成功。

## 基础设施安全上限

基础设施 byte 上限只负责拒绝异常载荷或决定 inline / reference 承载方式，不得裁剪业务正文：

- MCP raw result 继续使用现有 64 MiB 上限；
- AgentItem 继续使用现有 128 KiB 行载荷上限；
- 临时结果文件、RPC 和 sandbox 继续有容量门禁；Projection 层取消现有 192 KiB 内容接收限制，
  不得用另一个字符或 KB 上限替代。

实现必须证明正常 50,000-token 结果能经 inline 或 reference 路径承载。若 AgentItem 无法 inline，
则写入 identity-bound receipt，模型请求时从既有 private store 复验并恢复同一个逻辑结果。MCP
优先复用 Projection Store；普通/delegated Skill 优先复用现有 transient result store。必要的 store
容量调整属于基础设施容量，不是第二套业务预算；容量不足必须 fail closed，不能再次按 bytes 或字符
截短正文。

private payload 必须先完整 stage/publish，再提交引用它的 AgentItem。发布失败不得提交 receipt；
AgentItem 已提交后，重试必须复用同一 identity-bound 内容。resolver 复验 owner、Task、Run、Call、
result item、revision 和 SHA；任一不符都在 Provider 调用前 fail closed，不回退 raw，也不重放 Tool。

## Capability 行为

### MCP

Result Parser worker 继续先完成协议解码、`isError`、output schema、敏感内容清洗和 raw SHA
校验，但只向父进程交付一份完整安全业务 candidate 和必要元数据，不在 worker 内生成两份已裁剪
Projection，也不接收模型凭据或访问网络。父进程使用 Agent Run 绑定的 model edition 调用权威
`/tokenization`，裁剪后再生成 user view 与 agent projection。`source_truncated` 只表示安全业务
投影超过 50,000 tokens。

前端 MCP 业务卡片不再有独立字符/byte 限制，直接展示 Backend 已验证的 token-bounded typed view。
同一个 `mcp.dispatch` 内的每个 completed MCP business Call 保持各自一条 result entry 和各自
50,000-token 上限；顶层仍是与该 Agent Tool Call 配对的一条 closed bundle。

### Skill

普通 Skill、Legacy Skill 和 delegated Skill 都使用同一 50,000-token policy。现有
`AgentCallResultProjector.project(...)` 直接异步化：Skill 完整返回并完成本地校验、脱敏后，在该函数
内 await 统一详细 Tokenization 入口，再生成持久化投影；所有生产和测试调用点必须改为 await，不
增加 prepare/finalize 中间抽象，也不使用同步阻塞包装。能放入 AgentItem 的结果继续 inline；不能
放入时使用既有 Artifact / transient receipt 路径。各 resolver 不得把未预算 raw 绕过统一入口送给
模型。

### MCP Selector

Selector 不再使用独立的 20,000 字符 / 80,000-byte 历史结果总预算。部署后新写入并在后续轮次
恢复的 MCP result entry 已按统一 50,000-token policy 持久化；部署前旧 projection 继续按原内容只读，
不为本次功能重新预算或改写。Runtime 把 Selector 实际使用的 model edition、context window 和同一个
Provider-required token counter 注入 Selector candidate builder；完整 Selector candidate 的总上下文
计数仍覆盖新旧 entry。Selector 继续零网络恢复 Tool 数据，只允许 token counter 访问已配置的模型
Provider。

## 同轮多结果与总上下文

主 Agent 沿用 AgentRun 固定模型窗口的 90% total-context budget，并复用既有 context preflight 与
compaction：

1. 当前 Tool wave 中每个 Result 独立应用一次 50,000 business tokens 上限；
2. 每个业务 Call 始终对应一个逻辑 Result，裁剪不会拆分 AgentItem 或 provider Tool message；
3. 构建包含当前 Tool wave 的真实完整模型 candidate；
4. 按既有 closed 规则压缩旧历史，不压缩当前 user 正文；
5. 完整 candidate 仍超过 AgentRun 的 90% 总预算时，使用既有 context-too-large 失败语义，不在本
   设计中新增循环 Tokenization、公平分配或再次改写 durable Tool Result。

## 通用模型不可用错误

Backend 使用 typed `ModelUnavailableError`（具体类名可遵循现有模块命名），公开错误码固定为
`model_unavailable`。适用范围是阻断当前 Task 的模型侧 API：

- Provider `/tokenization`；
- 主 Agent model sampling；
- MCP Selector 及其他 Task 必需的模型调用。

Provider transport、timeout、认证/配置、限流、服务端失败，以及 `/tokenization` 响应合同非法，
均收敛为公开 `model_unavailable`；模型采样已经收到响应后的 Tool Call/消息语义协议违规继续使用
现有协议错误，不得误分类为 model unavailable。本地取消、context-too-large 和代码 invariant 错误
同样不映射。内部 audit/log 保留 endpoint kind、HTTP status class、异常类型和 model edition，但不
记录 API key、响应正文、Tool Result 正文或内部路径。

Tool 完整返回并形成可信 durable execution authority后，才进入 `/tokenization`：MCP 以 raw、成功
Parser checkpoint 和 terminal Call `completed` 为 authority；Skill 以统一 Invocation Service 已提交的
completed TaskNode/result 为 authority。Agent Tool Result projection 在 Tokenization 成功后生成。
tokenization 随后失败时：

- 保留已提交的可信 raw / Artifact / projection candidate 和成功 checkpoint；
- 已完成的 MCP Call 或 Skill Invocation TaskNode 继续保持 `completed`，不得改写为业务失败或状态
  未知；
- AgentRun 与 Task fail closed；
- `agent.run.failed` 和 `task.failed` 的公开 code 均为 `model_unavailable`；
- 不自动重试 Tokenization，不重放 Tool；
- 后续由用户重新发起新 Task，不在旧 Task 内隐式恢复调用。

对话标题生成等 best-effort 模型调用继续静默保留主 Task 成功，只记录低敏内部诊断。

## Frontend

Backend 必须先完成 typed error 到 AgentRun、`task.failed` 和 API/SSE 的持久化传播，可独立先发布；
旧 Frontend 收到未知的 `model_unavailable` 时沿用现有通用失败文案，不得崩溃。Backend-first阶段
Frontend现有业务卡片20,000字符/80,000-byte裁剪也暂时保留，因此只宣称Backend结果链完成，不宣称
端到端UI目标完成。后续 Frontend 发布删除`frontend/src/domain/artifacts.ts`的
`MCP_MAX_CODE_POINTS` / `MCP_MAX_UTF8_BYTES`，并在统一`failureMessage`映射中识别
`model_unavailable`，固定显示：

> 模型服务暂时不可用，无法完成本次请求，请稍后重试。

该文案用于实时 SSE 和刷新后的历史 Task。Frontend 不显示 endpoint、HTTP status、Provider response、
异常类或 tokenization 细节，也不把该错误误写成 MCP/Skill 业务失败。

MCP business view 如因同一次 tokenization 故障未形成可展示 projection，仍保持 safe-hide，不读取 raw
fallback；Task 级 `model_unavailable` 提示负责向用户说明本次失败原因。

## 兼容、发布与回滚

- 不创建新 projection revision；内部生成位置和裁剪阈值不是 Projection 数据合同，现有
  `mcp-result-parser.v2` / `maf.mcp.parsed_result_projection.v2` 结构和字段语义保持不变；
- 旧结果逐字节保持原样，不读取 raw 重建，不修改 Artifact，不调用远端 Tool；
- Backend 可先于 Frontend 发布；Backend rollout 验证新结果不再由字符/byte 预算截断，并验证
  49,999、50,000、50,001-token 边界；Frontend 后续删除业务卡片旧字符/byte裁剪并增加专用错误
  文案；
- 回滚恢复旧 writer/reader；旧版本可能把新的大 view 安全降级为 unavailable，但不得读取 raw 或
  改写历史；
- 不进行数据库 schema/data migration。

## 验证与验收

### Token boundary

- 使用 fake `/tokenization` 精确覆盖 49,999、50,000、50,001，并断言每个 Tool Result 只请求一次；
- ASCII、中文、emoji、组合字符和 structured JSON 都以 Provider `total_tokens` 为准；
- 覆盖多个 Token 共享一个 emoji 字符 offset 的边界，断言使用
  `offset_mapping[50_000][0]`，禁止使用 `offset_mapping[49_999][1]`；
- 业务路径不存在 20,000/100,000 字符或 80,000-byte truncate 判定；
- envelope metadata 不占单个 Result 50,000-token 额度，但计入完整 context；
- 三款已配置 model edition 分别覆盖请求/响应 model 一致、offset 合同和 50,000-token 裁剪。

### Provider required

- tokenization disabled、配置缺失、网络错误、timeout、401/403、429、5xx、非 JSON、item 数量不符和
  非整数 `total_tokens`、数组长度不一致、offset 非法或响应 model 不一致，全部 fail closed 为
  `model_unavailable`；
- 显式配置大于 10 秒仍按 10 秒超时；Tokenization 层零自动重试；
- Provider-required 路径对 `_fallback_num_tokens_from_messages()` 为零调用；
- count-only 调用保持既有 batching/cache 合同；Tool Result 详细路径必须取得完整 offset 响应；
- 本地 invariant、取消、context-too-large 和协议错误不映射为 `model_unavailable`。

### Tool Result

- 新 MCP result 在主 Agent、业务卡片和 Selector 中均遵守统一 50,000-token policy；
- 普通、Legacy、delegated Skill 均遵守统一 policy；
- 大结果通过 inline 或 identity-bound receipt 成为一个逻辑 Tool Result，AgentItem 仍不超过
  128 KiB；
- 同轮多个结果在 Provider token 总预算内确定性收敛，保持顺序且不拆消息；
- tokenization 在 Tool 执行后失败时保留可信结果、Task 失败且 Tool 零重放；
- MCP terminal Call 或 Skill Invocation TaskNode 的 durable `completed` 提交发生在 Tokenization
  之前；Tokenization 等待处于 Agent Lease active phase，MCP Tool heartbeat 在 Tool 返回后停止；
- Projection envelope 超过旧 192 KiB 时仍可落入 Projection Store，AgentItem 超过 128 KiB 时只
  切换为 identity-bound reference/receipt，不截断正文；
- raw、path、storage ref 和凭据泄漏扫描为零。

### Frontend 与历史

- Backend-first 阶段的 `agent.run.failed` / `task.failed` 和刷新历史均稳定携带
  `model_unavailable`；旧 Frontend 安全显示通用失败文案；
- Frontend 后续发布后，实时 SSE 与刷新历史均显示固定中文文案；
- 历史 fixture 逐字节不变；
- 零历史 reproject、零 raw 补读、零 Artifact CAS、零数据库迁移、零 Tool 网络重放；
- Backend token counter、Context preflight、MCP、Skill、API、E2E 与 Frontend task-event 门禁通过。
