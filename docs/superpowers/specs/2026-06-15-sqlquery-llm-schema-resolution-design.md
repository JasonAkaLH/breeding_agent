# SQLQuery LLM Schema Resolution Design

Date: 2026-06-15
Status: Draft written from approved brainstorming; awaiting user review before implementation planning.

## Problem Statement

SQLQuery 当前主要依赖静态规则和 `SchemaContextBuilder` 选择 route、表和 schema context。这个路径可控，但对自然语言语义、无物种品种查询、公司/地区/年份等跨物种查询的表达能力不足。用户希望让 LLM 参与 route 与 schema/table 选择，同时保留确定性边界、安全 guard、只读执行和可审计的中断恢复。

## Goals

1. 让 LLM 主导理解用户 query：判断查询属于审定品种库、基因型数据库或不支持范围。
2. 审定品种库根据 query 选择安全表范围：有物种选单表；无物种但有品种名先探测；宽泛跨物种查询允许五表范围。
3. 基因型数据库不做表选择，固定把 4 张基因型相关表提供给 SQL 生成 LLM。
4. SQL 生成、修复、结果筛选等 prompt 都必须携带原始 query，并在有上下文补全时区分原始 query 与 resolved query。
5. 本地 runtime/guard 不合法时允许 LLM 修复一次；远端 SQL 执行异常允许 LLM 修复最多 5 次。
6. 最终回答按实际结果来源说明涉及哪些表；失败时不向用户暴露具体 SQL、guard、MySQL 或 retry 细节。

## Non-goals

- 不开放写操作、DDL、多语句、系统库、跨库访问或管理类 SQL。
- 不让 SQL 生成 LLM 任意决定执行范围；它只能使用 resolution 注入的 schema。
- 不要求 LLM 生成的 SQL 文本字节级幂等。
- 不新增数据库连接方式或新外部依赖。
- 不把内部 handler、service、完整 prompt、数据库连接信息或错误栈暴露给用户。

## Current State and Evidence

| Area | Current behavior |
| --- | --- |
| Skill contract | `skill/sql-query/skill.contract.yaml` 声明 `platform_service`，handler 为 `runtime/sql_query_skill/platform_handler.py`，输入 schema 只有 `query`。 |
| Engine pipeline | `SQLQueryEngine` 固定执行 `intent_route -> schema_context_prepare -> sql_generate -> sql_guard -> sql_execute_readonly -> result_filtering`。 |
| Route selection | `intent_route.py` 先用 `routing_rules.yaml` 和关键词规则，只有规则未命中且 LLM 可用时才做 semantic fallback。 |
| Schema selection | `schema_context_builder.py` 用静态 route/profile metadata、crop mapping、表/字段打分选择表和字段。 |
| SQL generation | `sql_generate.py` 使用 LLM 生成 SQL；LLM 不可用或输出不合法时走 fallback generator。 |
| Guard/execution | `sql_guard.py` 做只读、单语句、表白名单和危险 pattern 检查；`sql_execute_readonly.py` 通过 `mysql_readonly` 执行。 |
| Existing repair | 当前执行错误 repair 上限为 1 次，主要面向远端 DB 可分类错误。 |

## Proposed Architecture

将 SQLQuery 内部流水线调整为：

```text
llm_query_understanding
  -> approval_variety_resolution | genotype_schema_resolution
  -> schema_context_prepare
  -> sql_generate
  -> sql_guard
  -> sql_execute_readonly
  -> result_filtering
```

### Stage 1: `llm_query_understanding`

新增 LLM 主导的 query 理解阶段。LLM 只输出结构化理解，不输出 SQL，也不直接决定可执行表名。

输出契约：

```json
{
  "route": "approval_variety | genotype | unsupported",
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

- `route` 必须在 allowlist 内。
- `crop` 必须在 allowlist 内或为 null。
- `variety_name_candidates` 最多保留 3 个，清洗危险字符，不能作为原始 SQL 片段拼接。
- `cross_crop_allowed` 只对 `approval_variety` 生效。
- `clarification_needed=true` 时必须提供一个简短澄清问题。
- LLM 输出不合法时不执行 SQL；可以走保守 fallback 或澄清。

### Stage 2A: `genotype_schema_resolution`

当 `route=genotype`：

- 固定选择 4 张表：
  - `variety`
  - `variety_genotype`
  - `qtn`
  - `rice_comp`
- 不让 LLM 裁剪或新增表。
- 后续 schema renderer 按 metadata 注入这些表中 `expose_to_llm=true` 的字段和 join hints。

### Stage 2B: `approval_variety_resolution`

当 `route=approval_variety`，系统根据 LLM 理解结果和 deterministic resolver 收敛表范围。

#### 有物种

如果 LLM 抽取到 `crop`，使用固定映射：

| crop | table |
| --- | --- |
| `corn` | `corn_varieties` |
| `rice` | `rice_varieties` |
| `cotton` | `cotton_varieties` |
| `wheat` | `wheat_varieties` |
| `soybean` | `soybean_varieties` |

系统直接选择对应表，不再做品种名探测。

#### 无物种 + 有品种名候选

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
```

实现要求：

- 探测 SQL 由 runtime 模板生成，不由 LLM 生成。
- 候选名使用参数化或安全 literal 处理，不拼接未清洗文本。
- 探测仍经过只读边界；不得绕过安全策略。

命中规则：

| Probe result | Behavior |
| --- | --- |
| 0 个表命中 | 中断追问：请补充作物类型或更准确品种名。 |
| 1 个表命中 | 自动继续，选择该表。 |
| 多个表命中 | 中断让用户选择作物/表，并展示简短候选。 |

多表命中 interrupt payload 必须包含原始 query、LLM understanding、候选品种、命中表、候选记录摘要和恢复 stage。用户选择后恢复到 `approval_variety_resolution`，不重新随机理解 query。

#### 无物种 + 无品种名

根据 LLM 的 `cross_crop_allowed` 决定：

- `true`：注入 5 张审定品种表，适用于某公司所有审定品种、某地区适种品种、近几年审定品种、按申请者/育种者/年份/地区跨作物筛选等问题。
- `false`：中断追问物种。

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

本地不合法时最多触发 1 次 LLM repair。高风险 guard 失败不 repair，直接终止为内部错误或安全拒绝。

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
  "route": "...",
  "schema_profile_id": "...",
  "selected_tables": ["..."],
  "schema_ddl": "..."
}
```

### User-facing failure policy

如果 repair 后仍失败，用户只看到模糊错误，例如：

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

内部 audit/debug event 可以保留 error source、error code、attempt、max attempts、SQL fingerprint、route 和 selected tables。

## Final Answer Source Attribution

最终回答来源说明以实际结果来源为准，不只看初始 resolution 范围。

优先级：

1. 有结果时，按结果行中的 `source_table` / `source_crop` 统计来源。
2. 如果结果行没有 source 字段，退回 SQL generation 输出的 `tables_used`。
3. 如果无结果，说明实际检索范围，即 `tables_used` 或 selected scope。

示例：

- 单表结果：
  > 本次结果来自水稻审定品种表（`rice_varieties`）。
- 多表结果：
  > 本次结果来自玉米审定品种表（`corn_varieties`）和水稻审定品种表（`rice_varieties`）。
- 跨表无结果：
  > 已在玉米、水稻、棉花、小麦、大豆审定品种表中查询，未找到匹配记录。

## Idempotency Targets

同一 `original_user_query`、同一 LLM understanding JSON、同一数据库探测结果和同一配置下：

- route 应相同；
- selected tables 应相同；
- schema DDL 应相同；
- interrupt 类型应相同；
- `genotype` 永远注入固定 4 表；
- `approval_variety + crop` 永远映射到固定单表；
- `approval_variety + no crop + variety candidate` 的表范围由探测结果决定；
- `approval_variety + cross_crop_allowed` 最多注入 5 个审定表。

SQL 文本本身不要求字节级完全一致，但必须满足：

- 只引用 selected table scope；
- 只引用 schema 内字段；
- guard 通过；
- 结果来源说明正确；
- 失败时用户可见错误脱敏。

## State and Interrupt Recovery

新增或调整两类 interrupt：

1. 多物种表探测命中：
   - 要求用户选择作物/表。
   - 保存原始 query、understanding、probe candidates、allowed options、stage 和 schema selection state。
2. 探测 0 命中或信息不足：
   - 要求用户补充物种或更准确品种名。
   - 恢复时合并新消息与原始 query，并保留 `original_user_query`。

恢复后应从 resolution 阶段继续，而不是完整重跑并重新随机理解 query。

## Testing Strategy

### LLM understanding

- route=`genotype` 时固定进入 4 表 schema。
- route=`approval_variety` 且 crop=`rice` 时选 `rice_varieties`。
- invalid route/crop 被拒绝或澄清。
- 每个 prompt 都包含 `original_user_query`。

### Approval variety probe

- 无 crop + 品种候选 0 表命中 -> interrupt 补充信息。
- 1 表命中 -> 自动继续。
- 多表命中 -> interrupt 选择作物/表。
- 探测 SQL 使用安全参数或安全 literal。
- 探测结果不直接替代最终 SQLQuery 结果。

### Cross-crop approval queries

- “某公司所有审定品种”允许 5 表范围。
- 不适合跨物种的问题追问物种。
- 跨表 SQL prompt 要求 `source_table/source_crop`。

### Repair

- 本地 validation/guard 失败最多 repair 1 次。
- 远端 DB 可修复错误最多 repair 5 次。
- repair prompt 带原始 query、schema、failed SQL 和错误来源。
- repair 后仍失败时用户只收到模糊内部错误。

### Final answer source

- 有 `source_table/source_crop` 时按结果行统计来源。
- 无 source 字段时退回 `tables_used`。
- 无结果时说明查询范围。
- 最终回答不泄露 SQL、MySQL 错误、guard 规则或 retry 细节。

### Regression

- guard/write denial 不放宽。
- row trim 和 result filtering 行为不回退。
- SQLQuery skill contract 不暴露内部 handler/service。
- 现有 SQLQuery happy path、guard block、interrupt resume、observability 测试保持通过或按新契约更新。

## Acceptance Criteria

| ID | Requirement |
| --- | --- |
| AC-1 | LLM understanding 能结构化输出 route/crop/entity/cross-crop intent，非法输出不会进入 SQL 执行。 |
| AC-2 | `genotype` route 固定注入 `variety`、`variety_genotype`、`qtn`、`rice_comp`。 |
| AC-3 | `approval_variety` 有 crop 时按固定 mapping 选择单表。 |
| AC-4 | `approval_variety` 无 crop 有品种名时执行 5 表安全探测，并按 0/1/多表命中规则处理。 |
| AC-5 | 宽泛跨物种审定查询可注入 5 表，不适合跨物种时追问物种。 |
| AC-6 | SQL generation、repair、result filtering prompt 都携带 `original_user_query`。 |
| AC-7 | 本地 runtime/guard 失败最多 LLM repair 1 次；远端 SQL 执行异常最多 repair 5 次。 |
| AC-8 | 最终回答按结果行 source 或 `tables_used` 说明实际来源表；无结果时说明检索范围。 |
| AC-9 | 最终失败对用户展示模糊内部错误，不暴露 SQL、MySQL/guard 细节、retry 次数或 stack trace。 |
| AC-10 | 现有只读安全、表白名单、row trim、result filtering 和 skill contract 边界保持有效。 |

## Rollout Notes

建议按小步实现并测试：

1. 新增 LLM understanding 契约和测试。
2. 新增 approval/genotype resolution 层，先不改 SQL guard。
3. 接入审定库品种名探测和中断恢复。
4. 扩展 SQL repair 策略。
5. 更新 prompt、来源说明和失败脱敏。
6. 跑 SQLQuery skill 单元/集成测试，再跑相关 API 和 frontend 最终回答展示测试。

