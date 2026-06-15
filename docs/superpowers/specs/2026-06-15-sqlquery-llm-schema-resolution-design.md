# SQLQuery LLM Schema Resolution Design

Date: 2026-06-15
Status: Revised after document-perfectization review; awaiting final user review before implementation planning.

## Problem Statement

SQLQuery 当前主要依赖静态规则和 `SchemaContextBuilder` 选择 route、表和 schema context。这个路径可控，但对自然语言语义、无物种品种查询、公司/地区/年份等跨物种查询的表达能力不足。用户希望让 LLM 参与 route 与 schema/table 选择，同时保留确定性边界、安全 guard、只读执行、可审计恢复，以及更自然的澄清交互。

## Goals

1. 让 LLM 主导理解用户 query，并直接输出内部 route id：`approval_variety_db`、`genotype_db` 或 `unsupported`。
2. 审定品种库根据 query 选择安全表范围：有物种选单表；无物种但有品种名先探测；宽泛跨物种查询允许五表范围。
3. 基因型数据库不做表选择，固定把 4 张基因型相关表提供给 SQL 生成 LLM。
4. resolution 阶段输出的 `selected_tables` 是后续 schema materialization、SQL 生成、guard 和执行的唯一权威表范围。
5. SQL 生成、修复、结果筛选等 prompt 都必须携带原始 query，并在有上下文补全时区分原始 query 与 resolved query。
6. 本地 runtime/guard 不合法时允许 LLM 修复一次；远端 SQL 执行异常允许 LLM 修复最多 5 次。
7. 最终回答按实际结果行涉及的表说明来源；无结果时说明系统做过哪些查询努力。
8. SQLQuery 需要用户补充信息时，用户可见交互必须是自然语言消息，不使用 interrupt 卡片 UI。
9. SQLQuery 全链路用户可见失败信息必须脱敏，不向用户暴露具体 SQL、guard、MySQL、retry 或 stack trace 细节。

## Non-goals

- 不开放写操作、DDL、多语句、系统库、跨库访问或管理类 SQL。
- 不让 SQL 生成 LLM 任意决定执行范围；它只能使用 resolution 注入的 schema。
- 不要求 LLM 生成的 SQL 文本字节级幂等。
- 不新增数据库连接方式或新外部依赖。
- 不把内部 handler、service、完整 prompt、数据库连接信息或错误栈暴露给用户。
- 不要求把所有内部恢复状态暴露成用户可见结构；用户侧只需要自然语言澄清和普通回复。

## Users, Stakeholders, and Affected Systems

| Area | Impact |
| --- | --- |
| End users | 更少因为缺少物种而失败；可通过自然语言补充作物或选择命中表；最终回答说明数据来源或检索努力。 |
| SQLQuery skill runtime | 新增 LLM understanding、approval/genotype resolution、schema materialization 权威边界和扩展 repair。 |
| Main agent finalizer / prompt context | 需要拿到 SQLQuery 输出行中的 `source_table/source_crop`，并在最终回答中说明来源；失败信息要脱敏。 |
| Frontend task / failure display | SQLQuery 澄清不得渲染 interrupt 卡片；SQLQuery 失败气泡不得显示具体 SQL/DB/guard 错误。 |
| MySQL readonly service | 继续承担只读执行；新增审定品种库 5 表探测查询，但不得绕过 guard/readonly 安全边界。 |
| Audit/events/artifacts | 可保留内部错误、repair 次数、SQL fingerprint、探测摘要和恢复状态，但不得进入用户可见回答。 |
| Tests | 需要新增 route understanding、resolution、probe、repair、source attribution、自然语言澄清和错误脱敏测试。 |

## Current State and Evidence

| Area | Current behavior |
| --- | --- |
| Skill contract | `skill/sql-query/skill.contract.yaml` 声明 `platform_service`，handler 为 `runtime/sql_query_skill/platform_handler.py`，输入 schema 只有 `query`。 |
| Engine pipeline | `SQLQueryEngine` 固定执行 `intent_route -> schema_context_prepare -> sql_generate -> sql_guard -> sql_execute_readonly -> result_filtering`。 |
| Route selection | `intent_route.py` 先用 `routing_rules.yaml` 和关键词规则，只有规则未命中且 LLM 可用时才做 semantic fallback。 |
| Route IDs | `routing_rules.yaml` 使用内部 route id：`approval_variety_db` 和 `genotype_db`。 |
| Schema selection | `schema_context_builder.py` 用静态 route/profile metadata、crop mapping、表/字段打分选择表和字段。 |
| SQL generation | `sql_generate.py` 使用 LLM 生成 SQL；LLM 不可用或输出不合法时当前会走 fallback generator。 |
| Guard/execution | `sql_guard.py` 做只读、单语句、表白名单和危险 pattern 检查；`sql_execute_readonly.py` 通过 `mysql_readonly` 执行。 |
| Existing repair | 当前执行错误 repair 上限为 1 次，主要面向远端 DB 可分类错误。 |
| Final answer inputs | 主代理 prompt sanitizer 当前允许 rows/columns/summary 等字段；若来源要来自结果行，跨表 SQL 必须在 rows 中输出 `source_table/source_crop`。 |

## Proposed Architecture

将 SQLQuery 内部流水线调整为：

```text
llm_query_understanding
  -> approval_variety_resolution | genotype_schema_resolution
  -> schema_context_materialize
  -> sql_generate
  -> sql_guard
  -> sql_execute_readonly
  -> result_filtering
```

`schema_context_materialize` 是现有 `schema_context_prepare` 的收窄版：它继续生成 `selected_columns`、`selected_column_details`、`schema_ddl`、`join_hints` 和 `context_summary`，但不再自行决定或扩大表范围。

## Stage 1: LLM Query Understanding

LLM understanding 阶段只输出结构化理解，不输出 SQL，也不直接决定可执行表名之外的任意范围。LLM 必须直接输出内部 route id。

输出契约：

```json
{
  "route_id": "approval_variety_db | genotype_db | unsupported",
  "confidence": 0.0,
  "crop": "corn | rice | cotton | wheat | soybean | null",
  "variety_name_candidates": ["..."],
  "entity_type": "variety | company | region | year | gene | qtn | genotype | phenotype | other",
  "cross_crop_allowed": true,
  "clarification_needed": false,
  "clarifying_question": null,
  "reason": "brief internal reason"
}
```

Runtime 校验规则：

- `route_id` 必须是 `approval_variety_db`、`genotype_db` 或 `unsupported`。
- `crop` 必须是 `corn/rice/cotton/wheat/soybean` 或 null。
- `variety_name_candidates` 最多保留 3 个，清洗危险字符，不能作为原始 SQL 片段拼接。
- `cross_crop_allowed` 只对 `approval_variety_db` 生效。
- `clarification_needed=true` 时必须提供一个简短自然语言问题。
- LLM 输出无效或低置信时，必须先调用一次 understanding repair。
- understanding repair 后仍无效时：如果 deterministic 规则能唯一高置信命中 route，则可使用 deterministic route 并记录 audit；否则用自然语言澄清，不执行 SQL。

## Stage 2A: Genotype Schema Resolution

当 `route_id=genotype_db`：

- 固定选择 4 张表：
  - `variety`
  - `variety_genotype`
  - `qtn`
  - `rice_comp`
- 不让 LLM 裁剪或新增表。
- 输出的 `selected_tables` 是后续 schema materialization 和 SQL guard 的唯一表范围。

## Stage 2B: Approval Variety Resolution

当 `route_id=approval_variety_db`，系统根据 LLM understanding 与 deterministic resolver 收敛表范围。resolution 输出的 `selected_tables` 是唯一权威；后续阶段不得重新选表、扩大表范围或用 query 打分覆盖 resolution。

### 有物种

如果 LLM 抽取到 `crop`，使用固定映射：

| crop | table |
| --- | --- |
| `corn` | `corn_varieties` |
| `rice` | `rice_varieties` |
| `cotton` | `cotton_varieties` |
| `wheat` | `wheat_varieties` |
| `soybean` | `soybean_varieties` |

系统直接选择对应表，不再做品种名探测。

### 无物种 + 有品种名候选

LLM 只负责抽取品种名候选。系统用固定只读模板在 5 个审定表中探测：

```sql
SELECT '<table>' AS source_table,
       '<crop>' AS source_crop,
       crop_name,
       variety_name,
       approval_num,
       year
FROM <table>
WHERE variety_name LIKE :candidate
ORDER BY
  CASE WHEN variety_name = :candidate_exact THEN 0 ELSE 1 END,
  year DESC,
  variety_name ASC,
  approval_num ASC
```

实现要求：

- 探测 SQL 由 runtime 模板生成，不由 LLM 生成。
- 候选名使用参数化或安全 literal 处理，不拼接未清洗文本。
- 探测仍经过只读边界；不得绕过安全策略。
- 每个候选、每张审定表都执行探测。
- 每张表不设置返回行数限制。
- 排序必须固定：完全等值优先，其次 LIKE 命中，再 `year DESC`、`variety_name ASC`、`approval_num ASC`。
- 用户可见自然语言澄清只展示每表前若干条摘要，避免消息过长；完整探测结果可保留在内部 artifact/audit。

命中规则：

| Probe result | Behavior |
| --- | --- |
| 0 个表命中 | 用自然语言说明已在五个审定品种表中按候选品种名包含匹配探测但未命中，请用户补充作物类型或更准确品种名。 |
| 1 个表命中 | 自动继续，选择该表。 |
| 多个表命中 | 用自然语言列出命中作物/表和候选摘要，让用户选择；不得展示 interrupt 卡片。 |

内部恢复状态必须包含原始 query、LLM understanding、候选品种、命中表、候选记录摘要和恢复 stage。用户选择后恢复到 `approval_variety_resolution`，不重新随机理解 query。

### 无物种 + 无品种名

根据 LLM 的 `cross_crop_allowed` 决定：

- `true`：注入 5 张审定品种表，适用于某公司所有审定品种、某地区适种品种、近几年审定品种、按申请者/育种者/年份/地区跨作物筛选等问题。
- `false`：用自然语言追问物种，不使用 interrupt 卡片。

## Schema Context Materialization

`schema_context_materialize` 输入：

```json
{
  "route_id": "approval_variety_db | genotype_db",
  "schema_profile_id": "...",
  "selected_tables": ["..."]
}
```

职责：

- 从 `schema_metadata.yaml` 读取 `selected_tables` 的字段定义。
- 过滤 `expose_to_llm=false` 字段，保留必要 join 字段。
- 渲染 `schema_ddl`。
- 生成与 `selected_tables` 内部关系匹配的 `join_hints`。
- 生成 `context_summary`。

禁止事项：

- 不得根据 query 重新挑表。
- 不得扩大 `selected_tables`。
- 不得把 resolution 未选择的表注入 SQL 生成 prompt。

## Natural-Language Clarification UX

SQLQuery 需要用户补充信息或选择候选时，用户可见界面必须是自然语言交流，而不是 interrupt 卡片。

要求：

- 系统可以内部使用 interrupt/state 机制保存恢复状态，但前端不得渲染 SQLQuery 专用 interrupt 卡片。
- 用户看到的是普通 assistant 消息，例如：
  > 我在水稻和玉米审定品种表里都找到了相似品种。你想继续查哪一个作物库？
- 选项可以用自然语言列表展示，但不使用表单卡片。
- 用户回复后，系统从保存的 resolution 状态恢复，不重新随机路由。
- 如果用户回复与原始 query 明显冲突，系统应再次用自然语言确认，而不是静默覆盖。

## Prompt Requirements

所有相关 prompt 都必须携带原始 query：

```json
{
  "original_user_query": "...",
  "resolved_user_query": "...",
  "parent_question": "...",
  "subtask_label": "..."
}
```

规则：

- `original_user_query` 是本轮用户最初问题，必须贯穿 understanding、resolution、SQL generation、SQL repair 和 result filtering。
- `resolved_user_query` 可包含上下文补全；SQL 生成和修复可优先使用它，但不能丢失原始 query。
- repair prompt 必须明确禁止改变 route、schema profile 和 table scope。

跨审定表 SQL prompt 还必须要求：

- 若跨表 `UNION ALL`，每个 SELECT 必须投影 `source_table` 和 `source_crop`。
- 多表字段不一致时，只选共同字段，或对缺失字段使用 `NULL AS <alias>`。
- 仍只能生成单条只读 `SELECT` 或 `WITH ... SELECT`。
- 不自动加 `LIMIT`，除非用户明确要求。
- 按 `variety_name` 查询必须使用 `LIKE` 包含匹配。

## SQL Validation, Repair, and Failure Policy

### Validation order

```text
LLM SQL output
  -> runtime parse/schema/business validation
  -> SQL guard
  -> readonly DB execute
  -> remote DB error classification
```

### Local runtime/guard validation

本地执行前检测包括：

- 是否能抽出单条 SQL。
- 是否是 `SELECT` 或 `WITH ... SELECT`。
- 是否多语句。
- 表是否都在当前 `selected_tables` 内。
- 字段是否在注入 schema 内。
- `variety_name` 过滤是否使用 LIKE。
- 是否包含 DDL/DML、系统库、跨库、`INTO OUTFILE`、lock/admin 操作等危险内容。

本地 validation/guard 不合法时：

```text
本地 validation/guard 不合法
  -> LLM repair 1 次
  -> 仍不合法才 fallback
  -> fallback SQL 也必须重新 validation/guard
  -> 仍失败则对用户返回模糊内部错误
```

高风险安全类 guard 失败不得进入 repair/fallback，直接终止并脱敏。高风险包括写操作、DDL、多语句、系统库、跨库、`INTO OUTFILE`、lock/admin 操作。

### Remote DB execution repair

远端 SQL 执行异常可触发最多 5 次 LLM repair，适用于可修复错误类型，例如：

- syntax error
- unknown column
- ambiguous column
- unknown function
- unknown table
- 可安全归类的类型/表达式错误

每次 remote repair 后必须重新经过 runtime validation、SQL guard 和 readonly execution。

Repair prompt 输入：

```json
{
  "error_source": "runtime_validation | sql_guard | remote_db",
  "error_code": "...",
  "error_message": "...",
  "failed_sql": "...",
  "original_user_query": "...",
  "resolved_user_query": "...",
  "route_id": "...",
  "schema_profile_id": "...",
  "selected_tables": ["..."],
  "schema_ddl": "..."
}
```

### User-facing failure policy

SQLQuery 全链路用户可见失败信息必须脱敏。覆盖范围包括最终回答、自然语言澄清后失败消息、API/task failure bubble 和前端失败气泡。

如果进入 SQL generation、guard、execute 或 repair 后仍失败，用户只看到模糊错误，例如：

> 查询暂时没有完成，服务器内部处理异常。请稍后重试，或换一种更明确的查询条件。

不得向用户展示：

- SQL 原文；
- 表/字段错误细节；
- MySQL error code/message；
- guard 规则名；
- repair 次数；
- stack trace；
- provider 诊断；
- 数据库连接或本机路径。

正常澄清型交互仍可明确说明需要用户补充什么，例如选择作物、补充更准确品种名或在多个命中表中选择。

内部 audit/debug event 可以保留 error source、error code、attempt、max attempts、SQL fingerprint、route 和 selected tables。

## Final Answer Source Attribution and No-result Explanations

最终回答来源说明以实际结果来源为准，不只看初始 resolution 范围。

有结果时：

1. 优先按结果行中的 `source_table` / `source_crop` 统计来源。
2. 如果结果行没有 source 字段，退回 SQL generation 输出的 `tables_used`。
3. 最终回答必须说明实际结果来自哪些表。

无结果时：

- 单表范围无结果：说明已在哪个审定品种表中按当前条件查询，但未找到匹配记录。
- 跨五表范围无结果：说明已在玉米、水稻、棉花、小麦、大豆五个审定品种表中按当前条件查询，但未找到匹配记录。
- 无物种 + 品种名探测 0 命中：说明已先用候选品种名在五个审定品种表中做品种名包含匹配探测，但未找到候选记录，并请求补充作物类型或更准确品种名。

示例：

- 单表结果：
  > 本次结果来自水稻审定品种表（`rice_varieties`）。
- 多表结果：
  > 本次结果来自玉米审定品种表（`corn_varieties`）和水稻审定品种表（`rice_varieties`）。
- 单表无结果：
  > 已在水稻审定品种表（`rice_varieties`）中按当前条件查询，未找到匹配记录。
- 跨表无结果：
  > 已在玉米、水稻、棉花、小麦、大豆五个审定品种表中按当前条件查询，未找到匹配记录。

## Idempotency Targets

同一 `original_user_query`、同一 LLM understanding JSON、同一数据库探测结果和同一配置下：

- route id 应相同；
- selected tables 应相同；
- schema DDL 应相同；
- 自然语言澄清类型应相同；
- `genotype_db` 永远注入固定 4 表；
- `approval_variety_db + crop` 永远映射到固定单表；
- `approval_variety_db + no crop + variety candidate` 的表范围由探测结果决定；
- `approval_variety_db + cross_crop_allowed` 最多注入 5 个审定表。

SQL 文本本身不要求字节级完全一致，但必须满足：

- 只引用 selected table scope；
- 只引用 schema 内字段；
- guard 通过；
- 结果来源说明正确；
- 失败时用户可见错误脱敏。

## Edge Cases and Failure Modes

| Case | Expected behavior |
| --- | --- |
| LLM understanding 输出无效 | 修复一次；仍无效则 deterministic 唯一高置信时 fallback，否则自然语言澄清。 |
| LLM route 低置信 | 按 invalid/low-confidence 策略处理，不直接执行 SQL。 |
| 用户 query 同时像审定库和基因型库 | 自然语言澄清 route，不猜测执行。 |
| 品种名候选多个 | 对每个候选执行五表探测；自然语言展示命中摘要时标明候选来源。 |
| 同一品种名跨多个物种表命中 | 自然语言让用户选择作物/表，不自动猜。 |
| 探测返回大量候选 | 内部保留完整探测结果；用户可见消息只展示摘要和继续选择所需信息。 |
| 单表探测命中 | 自动继续；最终回答说明实际结果来源表。 |
| 跨表查询部分表有结果、部分表无结果 | 有结果时按结果行 `source_table/source_crop` 说明实际来源；不列无结果表为来源。 |
| 跨表查询全部无结果 | 说明已查询五个审定品种表但无匹配。 |
| result rows 缺少 `source_table/source_crop` | 退回 `tables_used`；同时记录 audit，后续测试应覆盖 prompt 要求。 |
| 本地 validation/guard repair 后仍失败 | fallback；fallback 仍失败则用户可见模糊内部错误。 |
| 远端 SQL repair 5 次仍失败 | 用户可见模糊内部错误；内部记录错误与尝试次数。 |
| 用户在自然语言澄清中选择的作物与原始 query 冲突 | 再次自然语言确认，不静默覆盖。 |
| SQLQuery 失败进入 task/frontend failure bubble | 用户可见文本仍为模糊内部错误，不展示具体 DB/guard 错误。 |

## Dependencies and Integration Points

| Dependency | Required change / constraint |
| --- | --- |
| `SQLQueryEngine` | 增加 understanding、resolution、schema materialization 阶段；支持不同 repair budgets。 |
| `routing_rules.yaml` | 继续作为 route/crop/table allowlist 和 deterministic fallback 来源。 |
| `schema_metadata.yaml` | 继续作为字段、表说明、join hints 和 expose policy 来源。 |
| `schema_context_builder.py` / `schema_context_prepare.py` | 收窄为 materialization；必须接受 resolution `selected_tables`，不得重新选表。 |
| `prompt_builders.py` | 所有相关 prompt 带 `original_user_query`；跨审定表 SQL 要求 `source_table/source_crop`。 |
| `sql_generate.py` | 本地 validation fail 先 repair 1 次，再 fallback；fallback 也必须 validation/guard。 |
| `sql_guard.py` | 继续强制只读、单语句和表白名单；高风险 guard fail 不 repair。 |
| `sql_execute_readonly.py` / `MySQLReadonlyAdapter` | 远端可修复错误支持最多 5 次 repair；探测查询不得绕过只读执行边界。 |
| `result_filtering.py` | 保留 source fields；结果筛选不得删除来源说明所需字段。 |
| Main agent finalizer / prompt sanitizer | 能看到结果行中的 `source_table/source_crop` 或 `tables_used`，并按来源规则生成最终回答。 |
| Interrupt/state recovery | 内部可保留恢复状态，但 SQLQuery 用户可见澄清必须是自然语言消息。 |
| Frontend task failure display | SQLQuery 失败气泡脱敏；不显示 SQL/DB/guard 具体原因；不显示 interrupt 卡片。 |

## Risks, Assumptions, and Mitigations

| Type | Item | Mitigation / handling |
| --- | --- | --- |
| Risk | LLM route/slot 漂移 | route/crop allowlist 校验；invalid/low-confidence 先 repair，再 deterministic fallback 或澄清。 |
| Risk | 无行数限制探测可能返回大量候选 | 用户可见只展示摘要；内部结果可进 artifact/audit；必要时后续性能优化再加分页。 |
| Risk | 五表跨表 SQL 复杂且可能慢 | 只在 `cross_crop_allowed=true` 时使用五表范围；guard/readonly deadline 继续生效。 |
| Risk | 远端 repair 最多 5 次增加延迟 | 仅对可修复 DB 错误触发；内部记录 attempt；最终失败脱敏。 |
| Risk | `source_table/source_crop` 缺失导致来源说明不完整 | 跨表 prompt 必须要求 source fields；无 source 时退回 `tables_used` 并记录 audit。 |
| Risk | 前端现有 failure mapping 泄露具体错误 | SQLQuery failure bubble 必须单独走脱敏 copy 或按 capability/source 覆盖映射。 |
| Assumption | 五个审定表均有 `variety_name/crop_name/approval_num/year` | 由 `schema_metadata.yaml` 和测试确认；若某表缺字段，probe/materialization 测试应失败并修正。 |
| Assumption | 用户希望无结果时说明检索努力但不看 SQL | 已由用户确认；文档固定为用户可见行为。 |
| Assumption | 内部 audit/debug 可保留具体错误 | 只限非用户可见通道，并继续做敏感信息清洗。 |

## Testing Strategy

### LLM understanding

- LLM 输出使用内部 route id：`approval_variety_db`、`genotype_db`、`unsupported`。
- route=`genotype_db` 时固定进入 4 表 schema。
- route=`approval_variety_db` 且 crop=`rice` 时选 `rice_varieties`。
- invalid/low-confidence route 先 repair 一次；仍无效时按 deterministic unique fallback 或自然语言澄清。
- 每个 prompt 都包含 `original_user_query`。

### Resolution and schema materialization

- resolution 输出的 `selected_tables` 是后续唯一权威。
- schema materialization 只渲染字段/DDL/join hints，不重新选表、不扩大表范围。
- `genotype_db` 永远 materialize 4 表。
- `approval_variety_db + crop` 只 materialize 对应作物表。

### Approval variety probe

- 无 crop + 品种候选 0 表命中 -> 自然语言说明五表探测努力并请求补充信息。
- 1 表命中 -> 自动继续。
- 多表命中 -> 自然语言选择作物/表，不渲染 interrupt 卡片。
- 探测 SQL 使用安全参数或安全 literal。
- 探测排序固定：等值优先、`year DESC`、`variety_name ASC`、`approval_num ASC`。
- 探测结果不直接替代最终 SQLQuery 结果。

### Cross-crop approval queries

- “某公司所有审定品种”允许 5 表范围。
- 不适合跨物种的问题追问物种。
- 跨表 SQL prompt 要求 `source_table/source_crop`。
- 部分表有结果时最终回答只说明实际有结果的来源表。
- 五表无结果时说明已查询五个审定品种表但无匹配。

### Repair

- 本地 validation/guard 失败最多 repair 1 次。
- 本地 repair 失败后才 fallback；fallback SQL 必须重新 validation/guard。
- 远端 DB 可修复错误最多 repair 5 次。
- repair prompt 带原始 query、schema、failed SQL 和错误来源。
- repair 后仍失败时用户只收到模糊内部错误。
- 高风险 guard failure 不进入 repair/fallback。

### Natural language clarification and failure redaction

- SQLQuery clarification 不显示 interrupt 卡片。
- 用户回复后从保存的 resolution 状态恢复。
- SQLQuery 最终回答、API/task failure bubble 和前端失败气泡均不泄露 SQL、MySQL 错误、guard 规则、retry 次数或 stack trace。

### Final answer source

- 有 `source_table/source_crop` 时按结果行统计来源。
- 无 source 字段时退回 `tables_used`。
- 无结果时说明查询范围和检索努力。
- 最终回答不泄露 SQL、MySQL 错误、guard 规则或 retry 细节。

### Regression

- guard/write denial 不放宽。
- row trim 和 result filtering 行为不回退。
- SQLQuery skill contract 不暴露内部 handler/service。
- 现有 SQLQuery happy path、guard block、resume、observability 测试保持通过或按新契约更新。

## Acceptance Criteria

| ID | Requirement |
| --- | --- |
| AC-1 | LLM understanding 能结构化输出内部 route id、crop、entity、cross-crop intent，非法输出不会进入 SQL 执行。 |
| AC-2 | understanding invalid/low-confidence 时先 repair 一次；仍无效时 deterministic 唯一高置信才 fallback，否则自然语言澄清。 |
| AC-3 | `genotype_db` route 固定注入 `variety`、`variety_genotype`、`qtn`、`rice_comp`。 |
| AC-4 | `approval_variety_db` 有 crop 时按固定 mapping 选择单表。 |
| AC-5 | resolution `selected_tables` 是后续 schema materialization、SQL generation、guard 和 execution 的唯一权威表范围。 |
| AC-6 | `approval_variety_db` 无 crop 有品种名时执行 5 表安全探测，并按 0/1/多表命中规则处理。 |
| AC-7 | 五表探测不限制每表返回行数，但排序必须稳定，用户可见消息只展示摘要。 |
| AC-8 | 宽泛跨物种审定查询可注入 5 表，不适合跨物种时自然语言追问物种。 |
| AC-9 | SQLQuery 所有用户澄清都以自然语言消息呈现，不显示 interrupt 卡片。 |
| AC-10 | SQL generation、repair、result filtering prompt 都携带 `original_user_query`。 |
| AC-11 | 本地 runtime/guard 失败最多 LLM repair 1 次；远端 SQL 执行异常最多 repair 5 次。 |
| AC-12 | fallback SQL 必须重新经过 validation/guard；仍失败则用户可见模糊内部错误。 |
| AC-13 | 最终回答按结果行 source 或 `tables_used` 说明实际来源表；无结果时说明查询范围和检索努力。 |
| AC-14 | SQLQuery 最终失败对所有用户可见表面展示模糊内部错误，不暴露 SQL、MySQL/guard 细节、retry 次数或 stack trace。 |
| AC-15 | 现有只读安全、表白名单、row trim、result filtering 和 skill contract 边界保持有效。 |

## Rollout Notes

建议按小步实现并测试：

1. 新增 LLM understanding 契约、repair 和测试。
2. 新增 approval/genotype resolution 层，并让 selected tables 成为权威表范围。
3. 将 schema context prepare 收窄为 schema materialization。
4. 接入审定库品种名探测、自然语言澄清和恢复状态。
5. 更新 SQL generation/repair prompt，加入原始 query 和跨表 source 字段要求。
6. 扩展 SQL repair 策略：本地 1 次、远端 5 次、fallback 后重校验。
7. 更新最终回答来源说明、无结果检索努力说明和 SQLQuery 用户可见错误脱敏。
8. 更新前端/任务失败展示，确保 SQLQuery 不显示 interrupt 卡片和具体内部错误。
9. 跑 SQLQuery skill 单元/集成测试，再跑相关 API、main-agent finalizer 和 frontend 展示测试。
