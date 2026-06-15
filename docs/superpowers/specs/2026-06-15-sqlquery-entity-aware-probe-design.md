# SQLQuery Entity-aware Probe Design

Date: 2026-06-15
Status: User-approved design; awaiting user review before implementation planning.

## Problem Statement

SQLQuery 已经把审定品种库的表选择收敛到 `schema_resolution`，但当前无物种场景中的 probe 仍默认把抽出的名称当作 `variety_name`。这会误处理公司、机构、育种者、申请者、转化体所有者或审定编号等查询。例如“某公司所有审定品种”不应只在品种名中查找公司名，而应先判断这个名称在 query 中扮演的 entity 角色，再按对应字段探测五个审定品种表。

本设计在既有 SQLQuery LLM schema/table resolution 基础上加入 entity-aware probe：LLM/规则先抽取名称和实体意图，runtime 再用确定性的字段映射和 probe 模板查找命中表、命中字段与命中等级。SQL 生成阶段仍只能使用 resolution 输出的 `selected_tables`，不得自行扩大表范围。

## Goals

1. 无物种但出现名称时，不再默认按 `variety_name` 探测；先识别名称可能对应的 entity 字段。
2. 支持品种名、申请者、育种者、申请者/育种者混合、审定编号和基础机构/人名查询。
3. 明确字段语义时采用“主字段优先 + 副字段补充”，最终回答分主要命中和附带命中。
4. 不明确公司/人名/机构时，同时查 `applicant` 与 `breeder`，不强行二选一。
5. entity 类型不确定时查核心名称字段集合：`variety_name OR applicant OR breeder`。
6. 多作物表命中时自动查询所有命中的表，不再要求用户选择作物。
7. 无命中时用自然语言说明查过哪些字段和哪些审定品种表，但不暴露 SQL、guard、DB 错误或内部 retry。
8. 最终回答必须说明数据来自哪些作物表、哪些字段命中，以及哪些是主命中/附带命中。

## Non-goals

- 不允许 LLM 直接拼接 probe SQL。
- 不开放写操作、DDL、多语句、系统库、跨库访问或管理类 SQL。
- 不要求把所有审定表字段都纳入 entity probe；首版只覆盖稳定、跨五表一致的名称/编号字段。
- 不新增外部依赖或数据库连接方式。
- 不把内部 probe SQL、完整 prompt、handler、service、数据库连接信息或错误栈暴露给用户。
- 不改变基因型数据库的固定 4 表策略。

## Decisions Confirmed by User

1. 申请者/育种者不强行二选一，默认可以同时查，并在结果中标明命中字段。
2. 公司/人名/机构无物种时，如果多个作物表命中，自动查询所有命中的表。
3. entity 不确定时查核心名称字段集合：`variety_name OR applicant OR breeder`。
4. 如果用户明确说“申请者/育种者”，采用主字段优先，同时查副字段；最终回答分“主要命中”和“附带命中”。
5. probe 完全无命中时，说明查过哪些字段和哪些表，但不暴露 SQL 或内部错误。

## Affected Components

| Component | Change |
| --- | --- |
| `intent_route.py` | 扩展 LLM understanding 输出：从单一 `entity_type` / `variety_name_candidates` 扩展为 `entities[]`。保留旧字段兼容。 |
| `schema_resolution.py` | 将品种名 probe 改为 entity-aware field probe；输出 `probe_summary`、`selected_tables`、`selected_crops`、`matched_fields`、`match_tiers`。 |
| `schema_context_prepare.py` | 继续只 materialize `selected_tables`，同时透传 entity/probe metadata 给 SQL generation。 |
| `prompt_builders.py` | SQL 生成 prompt 纳入 entity/probe metadata，要求 SQL where 条件优先使用命中字段。 |
| `sql_generate.py` | fallback SQL 与 LLM validation 需要支持主字段/副字段字段条件和来源字段投影。 |
| `result_filtering.py` | 输出 `source_summary` 时包含表、作物、命中字段和命中等级。 |
| `main_agent.prompt_builder` | 已有泛化 allowlist 若不足，补充通用 `matched_fields` / `match_summary`，不写 SQLQuery 专属 final prompt。 |
| Frontend | 沿用自然语言 missing_input；无需新增 interrupt 卡片。 |
| Tests | 新增 entity-aware probe、主/附命中、无命中澄清、最终来源说明相关测试。 |

## Entity Understanding Contract

`intent_route` 的 LLM semantic output 增加 `entities`，保留旧字段用于兼容和 fallback。

```json
{
  "route_id": "approval_variety_db",
  "confidence": 0.84,
  "crop": null,
  "entities": [
    {
      "text": "隆平高科",
      "entity_type": "organization",
      "field_intent": "applicant",
      "primary_fields": ["applicant"],
      "secondary_fields": ["breeder"],
      "confidence": 0.82
    }
  ],
  "variety_name_candidates": [],
  "cross_crop_allowed": true,
  "clarification_needed": false,
  "reason": "用户询问机构相关审定品种"
}
```

### Allowed Values

`entity_type` values:

- `variety`
- `organization`
- `person`
- `approval_number`
- `region`
- `year`
- `other`

`field_intent` values:

- `variety_name`
- `applicant`
- `breeder`
- `applicant_or_breeder`
- `approval_num`
- `unknown`

`primary_fields` and `secondary_fields` must be subsets of:

- `variety_name`
- `applicant`
- `breeder`
- `approval_num`

`transgenic_owner` is intentionally deferred from the default probe set because not every company/person query should imply transgenic ownership. It can be added later as an explicit field intent if product usage proves it necessary.

## Runtime Field Mapping

Runtime remains authoritative. LLM suggestions are normalized through deterministic mapping rules.

| User signal / LLM intent | Probe fields | Match tier |
| --- | --- | --- |
| Explicit variety name | `variety_name` | `primary` |
| Explicit approval number | `approval_num` | `primary` |
| Explicit “申请/申请者/申报/申请单位” | `applicant`, plus `breeder` | `applicant=primary`, `breeder=secondary` |
| Explicit “育种/育成/选育/育种者” | `breeder`, plus `applicant` | `breeder=primary`, `applicant=secondary` |
| Company/person/organization without explicit field | `applicant OR breeder` | both `peer` |
| Unknown extracted name | `variety_name OR applicant OR breeder` | all `peer` |

If multiple entities are extracted, runtime probes up to 3 cleaned entities in stable order. Each entity can use its own field set. Dangerous characters are stripped before literal construction. Probe SQL remains template-generated and still passes SQL guard before execution.

## Probe SQL Shape

For each selected entity, field set, crop and table, runtime generates a deterministic read-only probe. The SQL template is runtime-owned and not LLM-generated.

For one field:

```sql
SELECT '<table>' AS source_table,
       '<crop>' AS source_crop,
       '<field>' AS matched_field,
       '<tier>' AS match_tier,
       crop_name,
       variety_name,
       approval_num,
       applicant,
       breeder,
       year
FROM <table>
WHERE <field> LIKE :entity_like
ORDER BY
  CASE WHEN <field> = :entity_exact THEN 0 ELSE 1 END,
  year DESC,
  variety_name ASC,
  approval_num ASC
```

For multiple fields, runtime can either issue one probe per field or issue one query with OR predicates. The preferred implementation is **one probe per field** because it preserves exact `matched_field` and `match_tier` without database-specific CASE complexity. The result aggregator merges hits by table while preserving all field-level evidence.

## Resolution Behavior

### With crop

If crop is present, use crop mapping to select one table. Entity metadata is still passed downstream so SQL generation can filter the selected table using the correct field set.

### No crop + entity hits

Run entity-aware probe across five approval tables.

- If one or more tables hit: select all hit tables.
- If multiple fields hit within a table: preserve all field evidence.
- If fields include primary/secondary tiers: keep both and mark primary hits first in `match_summary`.
- Do not ask the user to choose a crop when the query shape naturally supports cross-crop results.

### No crop + no entity hits

Return natural-language missing input / no-hit clarification. The message must say which fields and which crop tables were searched. Example:

> 我已在玉米、水稻、棉花、小麦、大豆五个审定品种表中，按品种名、申请者、育种者字段查找“隆平高科”，但没有找到匹配记录。请补充作物类型、确认名称，或换一个更准确的机构/品种名称。

### No crop + no entity + broad query

If LLM/deterministic understanding marks `cross_crop_allowed=true`, select all five approval tables as before.

## Resolution Output Contract

`schema_resolution` output should include:

```json
{
  "selected_tables": ["corn_varieties", "rice_varieties"],
  "selected_crops": ["corn", "rice"],
  "resolution_reason": "approval_entity_probe_hits",
  "entities": [
    {
      "text": "隆平高科",
      "entity_type": "organization",
      "field_intent": "applicant",
      "primary_fields": ["applicant"],
      "secondary_fields": ["breeder"]
    }
  ],
  "probe_summary": {
    "searched_tables": ["corn_varieties", "rice_varieties", "cotton_varieties", "wheat_varieties", "soybean_varieties"],
    "searched_fields": ["applicant", "breeder"],
    "table_hits": [
      {
        "table": "corn_varieties",
        "crop": "corn",
        "matched_fields": ["applicant"],
        "match_tiers": ["primary"],
        "sample_rows": []
      }
    ]
  },
  "match_summary": {
    "primary": [{"table": "corn_varieties", "field": "applicant"}],
    "secondary": [{"table": "rice_varieties", "field": "breeder"}],
    "peer": []
  }
}
```

## SQL Generation Requirements

SQL generation LLM receives `entities`, `probe_summary`, `match_summary`, `selected_tables`, `schema_ddl`, `original_user_query`, and `resolved_user_query`.

Rules:

1. SQL must only use `selected_tables`.
2. If `match_summary.primary` exists, SQL must include primary field filters.
3. If secondary hits exist, SQL may include secondary field filters and should preserve source fields for final answer grouping.
4. Cross-table approval SQL must continue projecting `source_table` and `source_crop`.
5. Entity-aware approval SQL should also project a deterministic source marker when possible:
   - `matched_field`
   - `match_tier`
   - or equivalent output derived from the field branch.
6. If a result row can come from multiple field branches, prefer `UNION ALL` branches with explicit literal `matched_field` and `match_tier` rather than an ambiguous OR-only query.

## Final Answer Requirements

The main answer should be based on sanitized skill output, not SQL internals. It must mention:

- which crop tables were queried,
- which fields matched,
- which hits are primary vs attached/secondary when the query had explicit field intent,
- no-hit search effort when no rows are returned.

Example style:

> 主要命中：在玉米审定品种表 `corn_varieties` 的申请者字段中查到 12 条记录。  
> 附带命中：在水稻审定品种表 `rice_varieties` 的育种者字段中也查到 3 条记录。

## Error Handling and Safety

- Probe SQL must be built by runtime templates and pass SQL guard.
- Probe failures should not expose DB errors to the user; public message stays vague.
- No-hit clarification is not a system error and should be natural-language missing input.
- If LLM entity output is invalid, run one understanding repair; if still invalid, fall back to deterministic entity heuristics when safe, otherwise ask for clarification.
- Final user-facing error text must not reveal SQL, guard, MySQL, retry count, stack trace, DSN, table internals beyond approved source table names, or prompt content.

## Testing Plan

### Unit tests

- `intent_route` parses valid `entities[]` and preserves legacy `variety_name_candidates` fallback.
- entity field mapping:
  - “申请的品种” -> primary `applicant`, secondary `breeder`.
  - “育成的品种” -> primary `breeder`, secondary `applicant`.
  - organization/person without field -> peer `applicant`, `breeder`.
  - unknown name -> peer `variety_name`, `applicant`, `breeder`.
- `schema_resolution` probes field-aware templates and selects all hit tables.
- no-hit response mentions searched fields and five crop tables.
- probe result records `matched_field` and `match_tier`.

### Integration tests

- “隆平高科申请的审定品种” returns primary applicant hits plus secondary breeder hits.
- “某育种者育成的品种” returns primary breeder hits plus secondary applicant hits.
- “某公司所有审定品种” auto-selects all hit crop tables without interrupt.
- “不存在机构所有审定品种” returns natural-language no-hit clarification.
- SQLQuery failure remains redacted.

### Frontend tests

- Natural-language no-hit / clarification message is shown as normal assistant text, not an interrupt card.
- User can still answer follow-up clarification through the normal composer.

## Rollout Notes

This should be implemented as a scoped extension to the existing SQLQuery resolution pipeline. It should not reintroduce `SchemaContextBuilder` table selection fallback and should not customize main-agent final prompt for SQLQuery only. Any new output fields exposed to final answer should be added through generic sanitizer allowlist names such as `match_summary`, `matched_fields`, or `source_summary`.
