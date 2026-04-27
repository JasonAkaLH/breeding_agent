# SQLQuery LLM 增强与真实库验证

- **范围**：后端 / SQLQuery capability
- **文档状态**：正式版（补齐 Phase 5.5 及后续实现事实）
- **日期**：2026-04-27
- **上游基线**：`docs/prd/backend/06-SQLQuery-MVP设计.md`
- **对应开发过程**：`docs/dev_processes/backend/Phase-5.5-SQLQuery-LLM增强专题.md`

## 1. 背景

`docs/prd/backend/06-SQLQuery-MVP设计.md` 定义了一期 SQLQuery 六节点只读链路；随后实现扩展到 LLM 驱动的 SQL 生成、LLM 结果筛选、真实 MySQL 只读适配器与 prompt schema 约束。当前默认产品链路保留六节点 DAG，尾节点 `sql_query.result_filtering` 在 SQLQuery 内部对 LIKE 召回的候选行做二次筛选并返回筛选后的表格，同时 `sql_execute_readonly` 保留原始表格 preview；本 PRD 记录这些能力的维护边界。

## 2. 目标

SQLQuery LLM 增强的目标是：
- 让 `sql_query.sql_generate` 可在裁剪后的 schema 上下文内调用 LLM 生成只读 SQL；
- 让 `sql_query.result_filtering` 可在已执行结果上调用 LLM 判断候选行是否符合用户真实需求，并返回筛选后的表格；
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

`sql_query.sql_generate` 支持注入最小文本生成接口；真实 API runtime 默认会在未显式注入 fake generator 时，从 `config.yaml` 创建 `LLMClient.generate_text()` 作为 SQLQuery 内部文本生成器，并按照以下契约处理 LLM 输出：

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
- 当按品种名称 / `variety_name` 过滤时，SQL 必须使用 `LIKE` 通配匹配，不得使用严格等值条件 `variety_name = ...`；
- 后续仍必须通过 `sql_query.sql_guard`，LLM 自身不能绕过 guard。

### 4.4 Runtime 装配

`build_api_runtime()` 为 SQLQuery 提供独立 LLM 装配参数：
- `llm_text_generator`：最高优先级，测试或特殊运行时可直接注入文本生成函数；
- `sql_query_llm_config` / `sql_query_llm_config_path` / `sql_query_llm_client_factory`：用于真实 provider 或 fake client factory；
- `sql_query_reasoning_effort`：传给结构化 SQLQuery 文本生成调用；
- `enable_sql_query_llm`：可在 fake backend / 默认自动化测试中显式关闭真实 provider 访问。

同一个 resolved text generator 会同时传给 `sql_query.sql_generate` 与 `sql_query.result_filtering`，确保 SQL 生成和候选结果筛选都先走 LLM 主路径，再按各自规则降级。

### 4.5 Fallback

当禁用/未配置 LLM、provider 失败、JSON 解析失败、输出模式非法或字段校验失败时，`sql_generate` 使用当前确定性 SQL 生成逻辑降级，并在输出中标记：
- `generation_source`
- `llm_mode`
- `fallback_used`
- `fallback_reason`

同时写入 audit-only 的 `sql_query.llm_fallback` 事件；成功调用写入 `sql_query.llm_call`。

## 5. 结果筛选与表格回传契约

当前默认 SQLQuery workflow 的尾节点是 `sql_query.result_filtering`。SQL 生成阶段使用 `LIKE` 匹配品种名是为了先召回候选行，避免差一个字、简称或后缀导致漏查；`result_filtering` 只负责在已执行结果上判断哪些候选行真正符合用户需求，不重新生成 SQL、不修改 SQL、不补查数据库、不生成自然语言总结。

### 5.1 输入约束

结果筛选 prompt 只接收：
- 用户问题与 route / schema profile 上下文；
- 已执行 SQL 的上下文；
- 结果列名；
- 有限 `candidate_rows`，每行带 0-based `row_index`；
- `source_row_count`、`candidate_row_count`、`truncated`。

默认只发送候选行预览，不能把完整结果集默认交给 LLM。`sql_execute_readonly` 同时保留原始 `query_result_preview` artifact，供排障和降级；前端展示应优先使用 `result_filtering` 生成的 `filtered_query_result` artifact。

### 5.2 输出约束

`result_filtering` 的 LLM 输出只接受 JSON：
- 必填 `keep_row_indexes`：从 `candidate_rows.row_index` 中选择要保留的行；
- 可选 `filter_reason`：简短说明筛选依据。

节点稳定输出筛选后的表格：
- `columns`、`rows`、`row_count`、`preview_row_count`、`truncated`；
- `source_row_count`、`candidate_row_count`、`removed_row_count`、`kept_row_indexes`；
- `filter_source`、`filter_reason`、`fallback_used` / `fallback_reason`；
- `route_id` / `schema_profile_id`。

不得编造候选集中不存在的行；如果候选行明显不是用户要查的品种/实体，应从结果表中移除；如果只是简称、别名、缺字或多字但仍可能对应用户需求，可以保守保留。

对带数字编号的单品种查询采用更严格的业务规则：例如用户查询“龙粳18”时，只保留品种名规范化后等于“龙粳18”或“龙粳18号”的行；“龙粳1836”“龙粳1823”“龙粳1851”等在编号后继续追加数字的名称属于其他品种，必须剔除。该规则应作为 LLM prompt 约束和确定性后过滤同时存在，避免无 LLM 或 LLM 误判时污染结果表。

### 5.3 Fallback

当禁用/未配置 LLM 或 0 行结果时，节点走确定性路径并保留 SQL 返回的候选表格；当 provider 失败、JSON 非法或 `keep_row_indexes` 越界/类型非法时，节点回退为未筛选表格（再叠加确定性后过滤规则），并写入 `sql_query.llm_fallback` audit 事件。成功调用写入 `sql_query.llm_call`。

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
- 结果筛选来源、fallback 状态与原因；
- 结果行数、预览行数、是否截断；
- clarify / reject 的结构化原因。

前端后续可基于这些字段展示：
- “由 LLM 生成 / 由规则降级生成”；
- “结果筛选仅基于候选行预览”；
- “需要补充信息”；
- “当前问题超出 SQLQuery 范围”。

## 9. 验收口径

本专题的最小验收包括：
- SQL 生成 LLM 主路径、clarify、reject、provider fallback、输出校验失败 fallback 测试通过；
- SQLQuery 内部 `result_filtering` 节点可生成稳定 `filtered_query_result` artifact；
- `sql_execute_readonly` 继续生成 `query_result_preview` 表格 artifact；
- 品种名称过滤使用 `LIKE`，LLM 严格等值输出会触发 fallback；
- SQLQuery workflow 在 fake LLM 下可完成端到端测试；
- `sql_query.llm_call` / `sql_query.llm_fallback` 不记录完整 prompt、完整 rows 或 secret；
- 真实 MySQL 只读 adapter smoke 能查询到已验证样例（当前样例为“龙粳33”），且仍需 SQL Guard token。
