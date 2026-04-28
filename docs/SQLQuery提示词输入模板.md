# SQLQuery Prompt 输入模板

> 适用范围：本模板用于 `sql_query.sql_generate` 阶段的提示词输入拼装，不覆盖路由识别、SQL 执行或 `result_filtering` 候选结果筛选阶段。
>
> 参考来源：
> - `docs/prd/backend/00-主代理框架PRD.md`
> - `docs/prd/backend/06-SQLQuery-MVP设计.md`
> - `docs/prd/backend/07-SQLQuery-LLM增强与真实库验证.md`
> - `configs/sql_query/routing_rules.yaml`
> - `configs/sql_query/schema_metadata.yaml`
> - `configs/sql_query/sql_guard_rules.yaml`
> - `docs/MySQL数据库表结构说明.md`

## 1. Prompt 目标

这个 prompt 的目标不是“让模型自由写 SQL”，而是复刻 legacy `sql_query_agent` 的 SQL 生成方式：在**最小必要上下文**下，把当前 route / table 范围渲染为 MySQL DDL 风格的 `database_schema`，让模型直接输出一条**可被后续 guard 严格校验的只读 SQL 草案**。路由不清、作物缺失等澄清优先在 `intent_route` / `schema_context_prepare` 阶段完成；`sql_generate` 主 prompt 不要求模型输出 JSON。

核心目标如下：

1. **路由感知**：明确当前任务属于哪条 SQLQuery 路线。
2. **最小上下文**：只注入与当前问题直接相关的 schema 子集，避免把整库 schema 原样塞给模型。
3. **只读优先**：生成结果必须服从只读约束，不能尝试写入、DDL、导出、锁表等操作。
4. **可审计**：系统会在模型输出 raw SQL 后反推 `tables_used` / `columns_used` / `column_types_used`，便于后续 guard、执行器和审计日志消费。
5. **前置澄清**：当路由不清晰、范围超出或约束冲突时，优先在 SQL 生成前澄清或拒答，而不是让 SQL prompt 猜测。

---

## 2. 输入块结构

建议按“强约束在前、弱约束在后”的顺序拼装 prompt。推荐块顺序如下：

1. **任务元信息**
   - 节点名：`sql_query.sql_generate`
   - 任务类型：只读 SQL 生成
   - 会话 / 任务标识：`conversation_id`、`task_id`

2. **路由块**
   - `route_id`
   - `display_name`
   - `route_description`
   - `supported_scope`
   - `ambiguity_strategy`
   - `allowed_tables`

3. **Schema Context 块**
   - `schema_profile_id`
   - `selected_tables`
   - `selected_columns`
   - `schema_ddl` / `database_schema`：由 `schema_metadata.yaml` 中当前可见表和字段渲染出的 `CREATE TABLE ... COMMENT ...` 片段
   - `join_hints`
   - `business_constraints`
   - `context_summary`

4. **SQL Guard 块**
   - SQL policy profile
   - 允许 / 禁止的语句类型
   - 单语句与只读约束
   - 表白名单约束
   - 形状限制（limit、join 数量、列数等）

5. **用户问题块**
   - 用户原始问题
   - 可选的意图摘要
   - 可选的结构化槽位结果

6. **输出格式块**
   - 明确要求模型只输出 SQL 查询语句
   - 不输出 JSON、Markdown 或解释文本

### 2.1 推荐模板示意

````text
生成一个SQL查询来回答这个问题：{{user_question}}
当前节点：sql_query.sql_generate
当前SQLQuery路由：{{route_id}}；schema_profile：{{schema_profile_id}}

## 以下是数据库结构
```sql
{{schema_ddl}}
```

## 连接关系
{{join_hints}}

## 限制
- 只能使用当前注入到 database_schema 中的表结构生成 SQL。
- 只能生成单条只读 SELECT 或 WITH...SELECT SQL。
- 不要自动添加 LIMIT；只有用户明确要求前 N 条、限制条数或分页时才生成 LIMIT。
- SELECT / WHERE / ORDER BY 字段由模型根据字段注释自行选择。
- 品种名过滤使用 LIKE 包含关系。

**注意！！你只需要输出SQL语句，不要输出其他任何内容！！！**
以下SQL查询最能回答问题 {{user_question}}：
```sql
````

---

## 3. route / schema context / SQL guard 约束如何注入

### 3.1 route 注入

route 层只做“**业务域定界**”，不直接给模型整库信息。

注入来源：`configs/sql_query/routing_rules.yaml`

建议注入字段：

| 字段 | 作用 | 说明 |
|---|---|---|
| `route_id` | 路由主键 | 例如 `approval_variety_db`、`genotype_db` |
| `display_name` | 人类可读名称 | 例如“审定品种库”“基因型数据库” |
| `description` | 路由语义 | 说明这条路线能查什么 |
| `allowed_tables` | 业务白名单 | 只允许该路线内表进入上下文 |
| `ambiguity_strategy` | 歧义策略 | 指示何时先澄清 |
| `examples` | 典型样例 | 帮助模型理解路由边界 |

注入原则：
- `default_route = clarify` 的含义要保留在 prompt 中；
- 若多个 route 置信度接近，**不要强行生成 SQL**；
- 路由信息是硬约束，不是建议项；
- 路由一旦确定，后续 schema 只能在该 route 范围内裁剪。

### 3.2 schema context 注入

schema 层只做“**路线 / 表范围裁剪**”，不注入全库 schema，也不再根据自然语言问题用规则函数预先挑业务字段。

注入来源：`configs/sql_query/schema_metadata.yaml`

建议注入字段：

| 字段 | 作用 | 说明 |
|---|---|---|
| `schema_profile_id` | 路线对应 schema profile | 例如 `approval_variety_profile`、`genotype_profile` |
| `selected_tables` | 当前问题相关表 | 由 context builder 选出 |
| `selected_columns` | 当前 route / table 范围内可供 LLM 选择的字段 | 只保留 `expose_to_llm: true` 字段及必要 join 字段；具体 SELECT / WHERE / ORDER BY 字段由 LLM 选择 |
| `schema_ddl` / `database_schema` | SQL 生成 prompt 的主 schema 输入 | 由 `schema_metadata.yaml` 只对当前 selected tables / columns 渲染 `CREATE TABLE` + 字段注释，不重新读取数据库 |
| `join_hints` | 显式连接关系 | 仅注入白名单 join，不让模型猜 |
| `business_constraints` | 业务限制 | 例如作物范围、路线范围、只读要求 |
| `context_summary` | 压缩摘要 | 让模型快速理解可用数据 |

注入原则：
- 只保留 `expose_to_llm: true` 的字段；
- 主键、自增 ID 等若对查询无帮助，可不默认暴露；
- 不用规则函数替 LLM 预先决定 SQL 投影字段或过滤字段；例如“适合河南种植”应由 LLM 根据字段描述选择 `suitable_area` 并添加地域过滤条件；
- 多表 join 只注入显式关系，不允许模型自行虚构关联；
- 审定品种库路线优先按已识别作物收敛到单作物表；基因型数据库路线按 legacy agent 逻辑保留 `variety`、`variety_genotype`、`qtn`、`rice_comp` 的完整 gene schema，便于模型自行选择字段和 join；
- 如果 route 未确定或表无法匹配，**先澄清，不生成 SQL**。

### 3.3 SQL guard 注入

SQL guard 层是“**最终硬约束**”，优先级高于用户问题和模型偏好。

注入来源：
- `configs/sql_query/sql_guard_rules.yaml`
- `configs/sql_query/routing_rules.yaml` 中的 `sql_policy_profile`

建议注入字段：

| 字段 | 作用 | 说明 |
|---|---|---|
| `allowed_statement_types` | 允许的语句类型 | 仅 `SELECT`、只读 `WITH ... SELECT` |
| `single_statement_only` | 单语句约束 | 禁止多语句 |
| `readonly_only` | 只读约束 | 禁止 DML / DDL / 锁表 / 导入导出 |
| `deny_system_schemas` | 系统库屏蔽 | 禁止 `mysql`、`information_schema` 等 |
| `limit_policy` | LIMIT 策略 | 不自动添加 LIMIT；只有用户明确要求前 N 条、限制条数或分页时才生成 LIMIT |
| `max_joins` | 复杂度上限 | 当前为 6 |
| `deny_patterns` / `deny_functions` | 风险拦截 | 如 `OUTFILE`、`LOAD DATA`、`sleep()` 等 |
| `execution_contract` | 执行前提 | 必须有 route context、schema profile、guard pass token |

注入原则：
- guard 规则要写成“必须遵守”的语句，而不是建议；
- 如果 guard 规则和用户需求冲突，**guard 胜出**；
- 如果模型无法确定某条 SQL 是否安全，应该避免输出不确定 SQL；系统侧后续仍会通过 SQL Guard fail-closed；
- 不要把 guard YAML 原文整块灌给模型，只注入执行所需子集。

---

## 4. 结果输出格式要求

这个阶段的主输出应当是**一条 SQL 查询语句**，而不是自然语言长段解释或 JSON。`sql_generate` 会兼容历史 JSON 输出，但 prompt 主约束按 raw SQL 设计。

### 4.1 推荐 raw SQL 输出

```sql
SELECT
  rice_varieties.year AS '年份',
  rice_varieties.variety_name AS '品种名称',
  rice_varieties.suitable_area AS '适种区域'
FROM rice_varieties
WHERE rice_varieties.suitable_area LIKE '%河南%'
```

### 4.2 输出约束

- SQL 必须是**单条只读 SQL**；
- 可以输出裸 SQL 或单个 ```sql 代码块，系统会提取 SQL 正文；
- 不要输出额外解释文本；
- 不要自动添加 LIMIT，除非用户明确要求；
- 输出引用的表必须来自当前注入的 `database_schema`；
- 输出引用的字段必须来自当前 route-scoped LLM-visible schema；
- 品种名过滤必须使用 `LIKE` 包含关系，不得使用 `variety_name = ...`。


## 5. 澄清与拒答策略

### 5.1 何时澄清

以下情况优先澄清，不直接生成 SQL：

1. **路由歧义**
   - 审定品种库与基因型数据库都可能匹配；
   - 两条 route 置信度接近。

2. **审定品种库作物缺失**
   - 已判断为 `approval_variety_db`；
   - 但问题里没有明确玉米 / 水稻 / 棉花 / 小麦 / 大豆中的任一种。

3. **基因型路线的目标对象不清**
   - 品种名、QTN 位点、基因名三者之一缺失；
   - 无法确定应优先查哪张表或怎么 join。

4. **schema 信息不足**
   - 目标表能确定，但字段选择还缺关键上下文；
   - 允许一次补充上下文后再生成。

澄清时的要求：
- 只问一个问题；
- 问题要尽量可直接回答；
- 不要把“为什么要问”写得太长；
- 不要同时问多个维度。

### 5.2 何时拒答

以下情况应直接拒答，不进入 SQL 生成：

- 请求超出当前支持范围；
- 请求写入、更新、删除、建表、改表、导出文件；
- 请求访问系统 schema 或 route 白名单外表；
- 请求绕过 guard、关闭校验、放宽只读限制；
- 用户要求“无视限制直接执行”；
- 模型无法确认安全性，且澄清也无法消除风险。

拒答时的要求：
- 明确说明当前 SQLQuery 只支持只读查询；
- 给出支持范围提示；
- 不输出任何可执行 SQL。

---

## 6. 审定品种库与基因型数据库两条路线示例

### 6.1 审定品种库路线示例

**用户问题**

> 近五年水稻审定品种有哪些？

**建议注入**

- `route_id`: `approval_variety_db`
- `schema_profile_id`: `approval_variety_profile`
- `allowed_tables`: `rice_varieties`
- `route_description`: 用于查询审定品种、品种公告、申请审定、品种特征、产量表现、适种区域等信息
- `business_constraints`:
  - 当前只支持五种作物：玉米、水稻、棉花、小麦、大豆
  - 作物已明确，可直接进入水稻子域
- `selected_columns`（示例）：
  - `year`
  - `crop_name`
  - `variety_name`
  - `approval_num`
  - `applicant`
  - `breeder`
  - `suitable_area`

**模板关注点**

- 不要把五张审定表都一起塞给模型；
- 既然用户明确说了“水稻”，优先只用 `rice_varieties`；
- 如果用户只说“审定品种有哪些”但没给作物，应先澄清“你想查哪一类作物的审定品种？”。

**期望输出**

- raw SQL 为单表只读查询；
- 若需时间过滤，必须显式体现“近五年”的年份条件；
- 不要为系统默认限制自动添加 `LIMIT`，允许全量返回匹配的只读数据。

### 6.2 基因型数据库路线示例

**用户问题**

> 查询品种 XX 在 QTN12 位点上的基因型。

**建议注入**

- `route_id`: `genotype_db`
- `schema_profile_id`: `genotype_profile`
- `allowed_tables`: `variety`, `variety_genotype`, `qtn`, `rice_comp`
- `route_description`: 用于查询品种的基因型、QTN、变异位点、籼粳成分等信息
- `business_constraints`:
  - 只允许查与品种、QTN、基因型、籼粳成分相关的数据
  - join 关系必须使用白名单显式关系
- `selected_tables`（示例）：
  - `variety`
  - `variety_genotype`
  - `qtn`
  - `rice_comp`
- `selected_columns`（示例）：
  - `variety.variety_name`
  - `qtn.qtn_seq`
  - `qtn.gene_name`
  - `variety_genotype.genotype`
  - `variety_genotype.phenotype`
- `join_hints`：
  - `variety_genotype.variety_id = variety.variety_id`
  - `variety_genotype.qtn_id = qtn.qtn_id`
  - `rice_comp.variety_id = variety.variety_id`

**模板关注点**

- 基因型数据库 prompt 按 legacy agent 方式保留完整 gene schema；是否实际 join / select `rice_comp` 由模型根据用户问题自行判断，并由字段白名单与 SQL Guard 校验；
- 如果“XX”不是已知品种名，先澄清品种名称；
- 如果用户没有明确 QTN 还是基因名，也不要盲目猜 join；
- 只要问题明确到“某品种在某 QTN 位点上的基因型”，优先组合 `variety + variety_genotype + qtn`，不要因为 `rice_comp` 已注入就无关 join。

**期望输出**

- raw SQL 为多表只读查询，但仍必须满足单语句、只读、无系统库访问；
- 结果字段应优先包含品种名、位点标识、基因名、基因型信息；
- 不要为非聚合明细查询自动补 `LIMIT`；仅当用户明确要求前 N 条、限制条数或分页时才生成 LIMIT。

---

## 7. 结论

这份输入模板的核心原则只有三条：

1. **先路由，再裁剪 schema，再注入 guard**；
2. **只读、单语句、白名单优先，不能靠模型自觉**；
3. **信息不足就澄清，风险不明就拒答**。

只要 prompt 按这个顺序拼装，后续的 SQL 生成、guard 校验和执行层就能各司其职，不会把职责混在一起。
