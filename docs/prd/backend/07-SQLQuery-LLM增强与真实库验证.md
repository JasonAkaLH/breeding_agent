# SQLQuery LLM 增强与真实库验证

- **范围**：后端 / SQLQuery capability
- **文档状态**：正式版（补齐 Phase 5.5 及后续实现事实）
- **日期**：2026-04-27
- **上游基线**：`docs/prd/backend/06-SQLQuery-MVP设计.md`
- **对应开发过程**：`docs/dev_processes/backend/Phase-5.5-SQLQuery-LLM增强专题.md`

## 1. 背景

`docs/prd/backend/06-SQLQuery-MVP设计.md` 定义了一期 SQLQuery 六节点只读链路，但实现已经进一步扩展到 LLM 驱动的 SQL 生成、LLM 结果摘要、真实 MySQL 只读适配器与 prompt schema 约束。因此需要把这些已经落地的能力补入正式 PRD，作为后续维护与前端设计的后端契约依据。

## 2. 目标

SQLQuery LLM 增强的目标是：
- 让 `sql_query.sql_generate` 可在裁剪后的 schema 上下文内调用 LLM 生成只读 SQL；
- 让 `sql_query.result_summarize` 可在已执行结果上调用 LLM 生成中文摘要；
- 保留确定性 fallback，确保 provider 不可用、输出非法或未配置时仍有可测试的降级路径；
- 强化 prompt schema，使 LLM 只能使用裁剪后的表、字段、字段类型与 join hints；
- 保持审计可见，同时不记录 API key、完整 prompt、完整 rows 等敏感内容；
- 通过真实 MySQL 只读查询 smoke 验证数据库适配器与 SQLQuery workflow 可用。

## 3. 非目标

- 不把 SQLQuery 内部节点开放为外部可直接请求的 public capability。
- 不支持写入、DDL、多语句或任意 SQL 执行。
- 不在 SQLQuery 内部实现通用 LLM Agent 或自由 ReAct 工具调用。
- 不要求自动化测试访问真实 provider 或真实 MySQL；默认测试必须可用 fake / injected seam 完成。
- 不把完整 prompt、完整行数据或 provider secret 写入事件、日志或 artifact。

## 4. SQL 生成 LLM 契约

`sql_query.sql_generate` 支持注入最小文本生成接口，并按照以下契约处理 LLM 输出：

### 4.1 输入约束

SQL 生成 prompt 只能使用来自 `schema_context_prepare` 的裁剪结果：
- `route_id`
- `schema_profile_id`
- `allowed_tables`
- `selected_tables`
- `selected_columns`
- `selected_column_details`
- `join_hints`
- `user_question`
- 只读 SQL Guard 约束

其中 `selected_column_details` 必须包含裁剪字段的字段名、`sql_type` 与说明，作为 LLM 选择字段和回填字段类型的唯一依据。

### 4.2 输出模式

LLM 必须返回 JSON object，`mode` 只能为：

| mode | 含义 | 必填字段 |
|---|---|---|
| `answer` | 生成可进入 guard 的 SQL 草案 | `route_id`、`schema_profile_id`、`sql`、`tables_used`、`columns_used`、`column_types_used` |
| `clarify` | 当前信息不足，需要用户澄清 | `clarifying_question` |
| `reject` | 超出当前 SQLQuery 支持范围 | `reject_reason`、`supported_scope_hint` |

### 4.3 校验规则

`mode=answer` 时必须校验：
- `route_id` 与 `schema_profile_id` 与上游 context 一致；
- `tables_used` 是允许表集合的子集；
- `columns_used` 必须存在于裁剪后的 schema；
- SQL 文本中引用的字段必须能映射到裁剪后的字段；
- `column_types_used` 必须逐项匹配 schema 中的 `sql_type`；
- 后续仍必须通过 `sql_query.sql_guard`，LLM 自身不能绕过 guard。

### 4.4 Fallback

当未配置 LLM、provider 失败、JSON 解析失败、输出模式非法或字段校验失败时，`sql_generate` 使用当前确定性 SQL 生成逻辑降级，并在输出中标记：
- `generation_source`
- `llm_mode`
- `fallback_used`
- `fallback_reason`

同时写入 audit-only 的 `sql_query.llm_fallback` 事件；成功调用写入 `sql_query.llm_call`。

## 5. 结果摘要 LLM 契约

`sql_query.result_summarize` 只解释已经执行完成的 SQL 结果，不重新生成 SQL，也不要求补查数据库。

### 5.1 输入约束

结果摘要 prompt 只接收：
- 用户问题与 route / schema profile 上下文；
- 已执行 SQL 的摘要上下文；
- 结果列名；
- 有限 `rows_preview`；
- `row_count`、`preview_row_count`、`truncated`。

默认只发送有限行预览，不能把完整结果集默认交给 LLM。

### 5.2 输出约束

LLM 必须返回 JSON object，稳定必填字段为：
- `summary`

可选字段包括：
- `highlights`
- `caveats`
- `row_count`
- `preview_row_count`
- `truncated`

摘要必须使用中文，不得编造结果集中不存在的信息；当 `truncated=true` 时，应明确说明摘要基于预览行。

### 5.3 Fallback

当无 LLM、provider 失败、JSON 非法、`summary` 为空或超出最大长度时，摘要节点回退确定性摘要，并写入 `sql_query.llm_fallback` audit 事件。

0 行结果默认直接走确定性摘要，不必调用 LLM。

## 6. MySQL 只读适配器契约

SQLQuery 的真实数据库执行由 `MySQLReadonlyAdapter` 承担，契约如下：
- 执行入口必须提供 `guard_pass_token`；没有 token 不允许访问数据库；
- 数据库执行通过明确异步边界封装，避免直接阻塞事件循环；
- adapter 支持懒加载并复用 SQLAlchemy engine；
- adapter 支持注入 `runner` 与 `engine_factory`，用于测试和 smoke；
- adapter 提供同步 `close()` 与异步 `aclose()`，用于释放连接池；
- transient 错误可做有限重试，禁止无限重试。

真实 MySQL 账号仍需保持数据库层只读权限；SQL Guard 只是应用层第二层保护。

## 7. 可观测性与安全

SQLQuery LLM 增强必须输出以下审计事件：
- `sql_query.llm_call`
- `sql_query.llm_fallback`

事件原则：
- `visibility=AUDIT_ONLY`；
- 默认不记录完整 prompt；
- 默认不记录完整 rows；
- 不记录 API key、authorization、base_url、password 等 secret；
- fallback diagnostic 可记录有限错误类别 / 原因，但不得泄漏 provider 原始敏感内容。

## 8. 对 API 与前端的影响

后端输出中应稳定保留：
- SQL 生成来源、fallback 状态与原因；
- 摘要来源、fallback 状态与原因；
- 结果行数、预览行数、是否截断；
- clarify / reject 的结构化原因。

前端后续可基于这些字段展示：
- “由 LLM 生成 / 由规则降级生成”；
- “结果仅基于预览行摘要”；
- “需要补充信息”；
- “当前问题超出 SQLQuery 范围”。

## 9. 验收口径

本专题的最小验收包括：
- SQL 生成 LLM 主路径、clarify、reject、provider fallback、输出校验失败 fallback 测试通过；
- 结果摘要 LLM 主路径、rows preview 限制、0 行确定性摘要、provider fallback 测试通过；
- SQLQuery workflow 在 fake LLM 下可完成端到端测试；
- `sql_query.llm_call` / `sql_query.llm_fallback` 不记录完整 prompt、完整 rows 或 secret；
- 真实 MySQL 只读 adapter smoke 能查询到已验证样例（当前样例为“龙粳33”），且仍需 SQL Guard token。
