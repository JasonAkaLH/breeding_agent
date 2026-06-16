# SQLQuery Query Constraint Contract 技术设计

- **范围**：后端 / SQLQuery Skill domain engine
- **文档状态**：已评审修订，待代码实现
- **日期**：2026-06-15
- **主文档**：[`README.md`](README.md)

本文承接主 PRD，集中描述 Query Constraint Contract 的内部结构、抽取策略、SQL 生成/修复/校验方案、fallback compiler 以及 main-agent 边界。

## 1. Query Constraint Contract

新增内部 IR，必须由 `skill/sql-query/runtime/sql_query_skill/query_constraints.py` 负责构造、清洗、去重、字段合法性过滤和摘要。该 IR 是 SQLQuery 内部实现契约，不是公开 API；不使用 `v1` 等临时版本命名，后续如确需破坏性迁移再单独设计迁移策略。

### 1.1 顶层结构

```json
{
  "query_constraints": {
    "contract": "sqlquery.constraint_contract",
    "source_question": "隆平高科2021年都审定了什么品种？",
    "resolved_question": "隆平高科2021年都审定了什么品种？",
    "required_constraints": [],
    "soft_constraints": [],
    "constraint_groups": [],
    "constraint_summary": "必须限制 year = 2021，并按 applicant/breeder 包含 隆平高科 查询。",
    "coverage_requirements": "global filters must be present in every UNION branch; query-level order/limit must be present on the final result",
    "extraction_sources": ["deterministic", "structured_llm", "entity_probe"]
  }
}
```

### 1.2 Constraint item

```json
{
  "id": "c_year_2021",
  "kind": "temporal",
  "field": "year",
  "operator": "=",
  "value": 2021,
  "required": true,
  "scope": "global_filter",
  "tables": ["rice_varieties", "wheat_varieties"],
  "group_id": null,
  "match_tier": null,
  "source": "deterministic",
  "source_span": "2021年",
  "confidence": "high"
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定、可审计的约束 ID。 |
| `kind` | `temporal`、`entity`、`region`、`approval_number`、`variety_name`、`crop`、`aggregate`、`limit`、`order`。 |
| `field` | 目标字段，如 `year`、`applicant`、`breeder`、`suitable_area`。 |
| `operator` | `=`、`LIKE`、`BETWEEN`、`IN`、`>=`、`<=`、`COUNT`、`LIMIT`、`ORDER_BY`。 |
| `value` | 标准化值。 |
| `required` | true 时必须被 SQL 覆盖；false 时作为 prompt hint 或 result filtering hint。 |
| `scope` | `global_filter` 表示每个相关 SQL 分支都必须覆盖；`branch_filter` 表示约束指定表/字段分支；`query_level` 表示作用在最终结果层，例如 `ORDER BY` / `LIMIT`；`aggregate` 表示聚合形态要求。 |
| `tables` | 已知表范围；未知时由 `selected_tables` 在后续阶段补齐。 |
| `group_id` | 可选；属于多字段/多分支约束组时填写。 |
| `match_tier` | 可选；entity constraint 的 `primary`、`secondary` 或 `peer`。 |
| `source` | `deterministic`、`structured_llm`、`intent_route_llm`、`entity_probe`、`resolved_context`。 |
| `source_span` | 用户 query 中触发该约束的短文本。 |
| `confidence` | `high`、`medium`、`low`；只有 high/明确 medium 进入 required。 |

### 1.3 Constraint group

多字段语义必须显式建模，不能依赖单个 `field` 或普通 `OR`。例如 `applicant_or_breeder` 必须表示为一个 `branch_union` 组：每个成员在 SQL 中生成独立分支，任一分支命中都算结果命中，但 coverage validator 必须确认每个应搜索成员都被覆盖。

```json
{
  "id": "g_lpkj_applicant_or_breeder",
  "kind": "entity_field_group",
  "mode": "branch_union",
  "required": true,
  "members": ["c_lpkj_applicant", "c_lpkj_breeder"],
  "compile_policy": "compile_each_member_as_union_all_branch",
  "answer_policy": "report primary hits and attached secondary/peer hits separately"
}
```

组模式：

| `mode` | 语义 | SQL 要求 |
| --- | --- | --- |
| `all_of` | 多个约束必须同时满足。 | 同一分支内用 `AND` 覆盖。 |
| `branch_union` | 多个字段都要搜索，任一字段命中即进入结果。 | 每个成员用独立 `UNION ALL` 分支；分支投影 `matched_field` / `match_tier`。 |
| `query_level` | 对最终结果集排序或限制数量。 | 单表可直接加 `ORDER BY` / `LIMIT`；多表 UNION 需要外层包装或等价最终结果层覆盖。 |

### 1.4 第一阶段字段覆盖

| 用户表达 | Constraint |
| --- | --- |
| `2021年` | `year = 2021`，`scope=global_filter`。 |
| `2020到2023年` / `2020-2023年` | `year BETWEEN 2020 AND 2023`，`scope=global_filter`。 |
| `近五年` | `year >= current_year - 4`，按 runtime clock 计算并写入标准值。 |
| `今年` / `去年` | `year = current_year` / `year = current_year - 1`。 |
| `国审稻20210001` | `approval_num LIKE '%国审稻20210001%'`。 |
| `适合河南种植` / `河南适种` | `suitable_area LIKE '%河南%'`。 |
| `隆平高科申请` | applicant 为 primary，breeder 只能作为 secondary 附带命中。 |
| `隆平高科选育` / `育种者是隆平高科` | breeder 为 primary，applicant 只能作为 secondary 附带命中。 |
| `隆平高科都审定了什么品种` 且无明确字段 | `applicant_or_breeder` 形成 `branch_union` 组，applicant/breeder 为 peer 分支。 |
| `龙粳18` / `龙粳系列` | `variety_name LIKE '%龙粳18%'` / `variety_name LIKE '%龙粳%'`。 |
| `有多少` / `数量` | `COUNT(*)`，`scope=aggregate`。 |
| `前10个` / `10条` | `LIMIT 10`，`scope=query_level`，仅用户明确要求时生成。 |
| `最新` / `最近` | `ORDER BY year DESC`，`scope=query_level`；如同时有 limit，则配合 limit。 |

## 2. 约束抽取策略

### 2.1 deterministic extractor

第一优先级使用规则抽取高置信、可解释约束：年份、年份范围、近 N 年、审定编号、省份/地区、数量/limit/order、明显的申请者/育种者关键词。

要求：

- 规则抽出的 high confidence required constraints 是 authoritative；LLM 不得删除。
- 数字年份只在合理范围内识别，例如 `1900 <= year <= 当前年份 + 1`。
- `近N年` 使用可注入、可测试的 runtime clock 计算，将标准化后的 year lower bound 写入 contract；测试必须能 freeze clock，SQL 不依赖数据库方言函数。
- `limit` 只有用户明确要求“前 N 条 / N 个 / N 条记录”时出现；不得因为结果多而自动生成。

### 2.2 LLM structured extractor

第二优先级让 LLM 输出结构化 JSON，只用于补充规则难以判断的语义，不直接生成 SQL：

- “作为申请者” / “参与选育” / “他们” 等角色判断。
- 公司简称、机构名、人名、品种名的语义分类。
- 多实体关系中哪个实体对应哪个字段。

LLM 输出 schema 必须限定为以下安全结构，未知 key、未知字段、未知 operator、无 `source_span` 的条目必须被丢弃：

```json
{
  "entities": [
    {
      "text": "隆平高科",
      "entity_type": "organization",
      "field_intent": "applicant_or_breeder",
      "confidence": "high",
      "source_span": "隆平高科"
    }
  ],
  "suggested_constraints": [
    {
      "kind": "entity",
      "field_intent": "applicant_or_breeder",
      "operator": "LIKE",
      "value": "隆平高科",
      "confidence": "medium",
      "source_span": "隆平高科"
    }
  ],
  "clarification_needed": null
}
```

验证与失败策略：

- JSON parse 或 schema validation 失败时，不触发 SQL 生成；丢弃 LLM 建议并继续使用 deterministic/entity probe 可确认的约束。
- 如果丢弃后仍缺少生成安全 SQL 的必要信息，返回通用 `missing_input` 自然语言澄清。
- LLM 不允许输出表范围权威；表范围仍由 `schema_resolution` 的 `selected_tables` 决定。
- LLM 只能建议 constraints，不能删除 deterministic required constraints。
- 低置信或冲突项进入 `soft_constraints` 或触发自然语言澄清。

### 2.3 entity probe 转 constraint

现有 entity-aware probe 保留，并把 `match_summary` 转成 entity constraints：

- `primary` → required branch constraint；最终回答作为主命中。
- `secondary` → required branch constraint；最终回答作为附带命中，不得伪装成主命中。
- `peer` → required `branch_union` 组成员；最终回答按命中字段分别说明。
- `matched_field` / `match_tier` 继续作为 SELECT 来源标记要求。

`applicant_or_breeder` 的确定规则：

1. 用户明确说“申请者 / 申请单位 / 申报”等 applicant 语义时，applicant 是 primary；breeder 仅可作为 secondary 附带搜索。
2. 用户明确说“育种者 / 选育 / 培育”等 breeder 语义时，breeder 是 primary；applicant 仅可作为 secondary 附带搜索。
3. 用户只给机构或人名、未说明字段时，applicant 与 breeder 都是 peer，必须使用 `branch_union` 搜索并分别标记来源。

### 2.4 冲突处理

| 冲突 | 处理 |
| --- | --- |
| 用户同时说 `2021年` 和 `2022年` 且非范围表达 | 触发自然语言澄清。 |
| 用户说“申请者”但 entity probe 只命中 breeder | 可执行 breeder 作为附带命中，但必须说明没有申请者主命中；不能悄悄把申请者改成育种者。 |
| query 中有地区但 selected tables 缺少 `suitable_area` | 该表分支不能声称覆盖地区；如所有表缺字段则澄清或返回支持范围说明。 |
| LLM 建议字段不在 selected schema | 丢弃该建议并记录 audit，不进入 required constraints。 |

## 3. SQL 生成与修复要求

### 3.1 Prompt 注入

`prompt_builders.py` 的 SQL generation / repair prompt 必须加入：

```text
【必须满足的查询约束】
- c_year_2021: year = 2021，scope=global_filter，来源：2021年
- g_lpkj_applicant_or_breeder: branch_union(applicant LIKE '%隆平高科%', breeder LIKE '%隆平高科%')，来源：隆平高科

要求：
- SQL WHERE / HAVING / ORDER / LIMIT 必须覆盖所有 required_constraints。
- 多表 UNION ALL 时，每个相关分支必须覆盖 scope=global_filter 的 constraints。
- scope=query_level 的 ORDER / LIMIT 必须作用在最终结果集，不能只限制单个 UNION 分支。
- branch_union 组必须使用独立 UNION ALL 分支，不得合并成无法追踪来源的普通 OR。
- entity constraints 必须保留 matched_field / match_tier 来源标记。
```

### 3.2 SQL generation validation

`sql_generate.py` 在现有 schema/table/field validation 后新增：

```text
_validate_constraint_coverage(context, sql, tables_used=tables_used)
```

校验失败时：

- 非 repair 首次失败：返回 `sql_generation_validation_failed`，由 engine 触发本地 repair 一次。
- repair 仍失败：走 deterministic fallback compiler。
- fallback compiler 仍无法覆盖 required constraints：不得执行漏约束 SQL。

用户可见失败策略：

| 场景 | 用户可见行为 | 内部记录 |
| --- | --- | --- |
| 约束冲突或缺少必要信息 | 使用通用 `missing_input` 自然语言澄清，不使用 interrupt 卡片。 | audit 记录 constraint conflict / missing slot。 |
| selected schema 不支持用户字段 | 说明当前可查询范围不支持该条件，或请用户换条件。 | audit 记录 unsupported field。 |
| runtime/compiler/repair 失败 | 返回脱敏的“服务器内部错误，请稍后重试”。 | audit 记录具体 error code、failed SQL、coverage report。 |

### 3.3 Repair prompt

repair prompt 必须携带：

- failed SQL。
- error code / validation reason。
- query constraints 与 constraint groups。
- selected tables/schema。
- entity resolution。

repair 目标不是“修到可执行”即可，而是“修到安全、合法且覆盖 required constraints”。

## 4. Constraint Coverage Validator

### 4.1 第一阶段实现方式

第一阶段不新增依赖，使用保守 SQL 文本解析：

- 按 `UNION ALL` 拆分分支。
- 提取每个分支 `FROM <table>`。
- 在分支文本中检查：
  - `year = 2021`、`year BETWEEN 2020 AND 2023`、`year >= 2022`。
  - `<field> LIKE '%value%'`。
  - `COUNT(*)`。
  - `ORDER BY <field> ASC|DESC`。
  - `LIMIT N`。
- 对 `scope=global_filter` 的 constraints，要求每个相关分支覆盖。
- 对 `branch_filter` / entity constraints，要求存在匹配表、字段、LIKE 和来源 marker。
- 对 `branch_union` 组，要求每个成员都存在独立分支，并且分支投影正确的 `matched_field` / `match_tier`。
- 对 `query_level` 的 `ORDER BY` / `LIMIT`，要求作用在最终结果层；多表 UNION 场景必须用外层 SELECT 包装或其它可验证等价形态。

### 4.2 后续 SQLGlot 评估

当 regex validator 难以维护时，再评估引入 SQLGlot：

- 使用 SQL AST 解析 WHERE / ORDER / LIMIT。
- 使用 schema-aware qualification 解析未限定字段。
- 对复杂布尔表达式、括号、别名和 CTE 做更稳健检查。

SQLGlot 不作为第一阶段依赖，避免扩大变更面。

## 5. Fallback Compiler 改造

fallback SQL 生成必须消费统一 constraint contract，而不是只根据 entity match summary 拼接 WHERE。

输入：

```text
selected_tables
selected_columns
query_constraints
constraint_groups
match_summary
user_question
```

输出要求：

- 所有 required constraints 被编译进 SQL。
- `scope=global_filter` 的 WHERE 条件必须追加到每个相关分支。
- `branch_union` 组必须生成独立 `UNION ALL` 分支，不用 OR 混合，避免丢失命中字段来源。
- 多表审定品种查询使用 `UNION ALL`。
- 每个 entity 分支投影 `source_table`、`source_crop`、`matched_field`、`match_tier`。
- `ORDER BY` / `LIMIT` 作为 query-level 约束时作用在最终结果集。
- 只在用户明确要求时添加 LIMIT。

示例：

```sql
SELECT 'rice_varieties' AS source_table,
       'rice' AS source_crop,
       'applicant' AS matched_field,
       'primary' AS match_tier,
       rice_varieties.year,
       rice_varieties.approval_num,
       rice_varieties.crop_name,
       rice_varieties.variety_name,
       rice_varieties.applicant,
       rice_varieties.breeder
FROM rice_varieties
WHERE rice_varieties.year = 2021
  AND rice_varieties.applicant LIKE '%隆平高科%'
UNION ALL
SELECT 'rice_varieties' AS source_table,
       'rice' AS source_crop,
       'breeder' AS matched_field,
       'secondary' AS match_tier,
       rice_varieties.year,
       rice_varieties.approval_num,
       rice_varieties.crop_name,
       rice_varieties.variety_name,
       rice_varieties.applicant,
       rice_varieties.breeder
FROM rice_varieties
WHERE rice_varieties.year = 2021
  AND rice_varieties.breeder LIKE '%隆平高科%'
```

## 6. Result Filtering 与最终回答

`result_filtering` 不负责补救漏 SQL 约束；它只处理已执行结果中的候选行筛选。漏约束 SQL 必须在执行前被 coverage validator 拦截。

SQLQuery 内部需要透传到 artifact / audit：

- `query_constraints`
- `constraint_coverage_summary`
- `source_scope`
- `match_summary`
- `matched_fields`
- `match_tiers`

给通用 finalizer 的 dependency context 不依赖 SQLQuery 专属字段。SQLQuery 必须把用户可见信息整理进已有通用字段，例如：

- `summary`：说明查询限制了哪些条件，例如“已限制审定年份为 2021 年”。
- `source_summary` / `source_scope`：说明结果来自哪些表。
- `no_result_explanation`：无结果时说明系统做过哪些字段、表、约束范围内的检索努力。
- `rows` / `columns` / `row_count`：提供实际结果预览。

最终回答仍由通用 finalizer 消费 SQLQuery 输出；不得把 SQLQuery 专属规则写进 main-agent prompt。若未来确实需要把新的结构化字段暴露给 finalizer，只能做通用、安全、非 SQLQuery-specific 的 dependency allowlist 扩展，并不得要求 main-agent 理解 SQLQuery 表字段语义。

## 7. Main-agent 与通用 runtime 边界

本 PRD 明确禁止为了 SQLQuery 定制 main-agent：

- 不修改 `main_agent.respond` prompt，让它理解 SQLQuery schema、字段或来源表标记。
- 不在 main-agent final prompt 中硬编码 SQLQuery 来源引用规则。
- 不让 LLM Planner 直接选择 SQLQuery 内部阶段。
- 不把 `query_constraints` 作为通用 runtime 的业务规则。

允许的外层修复只有一类：如果发现所有 Skill 的通用输入没有正确传递 `effective_user_message` / resolved message，可做**通用 Skill 输入传递修复**，且不得包含 SQLQuery-specific 分支。
