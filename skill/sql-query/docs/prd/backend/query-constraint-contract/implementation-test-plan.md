# SQLQuery Query Constraint Contract 实施与测试计划

- **范围**：后端 / SQLQuery Skill domain engine
- **文档状态**：已评审修订，待代码实现
- **日期**：2026-06-15
- **主文档**：[`README.md`](README.md)
- **技术设计**：[`technical-design.md`](technical-design.md)

本文承接主 PRD 与技术设计，集中描述分阶段交付、验收标准、测试计划、风险缓解、已确认决策与 PR 拆分建议。

## 1. 分阶段实施计划

### Phase 0：文档与测试基线

- 固化本 PRD。
- 补充复现测试草案：`隆平高科2021年都审定了什么品种？` 生成 SQL 必须含 `year = 2021`。
- 明确不触碰 main-agent 定制逻辑。

验收：PRD 存在；测试用例列表明确；现有 SQLQuery 测试可作为回归基线。

### Phase 1：Query Constraint IR

- 新增 `query_constraints.py`。
- 实现 deterministic extractor：年份、年份范围、近 N 年、审定编号、地区、count、limit、order、明确申请者/育种者信号。
- 实现 `constraint_groups`，其中 `applicant_or_breeder` 必须形成 `branch_union` 组。
- 实现可注入 runtime clock，测试中可 freeze current year。
- 实现 LLM structured extractor 的 JSON schema / Python validator；无效输出丢弃，不进入 SQL 生成权威。
- 在不新增公开 stage 的前提下，将 `query_constraints` 接入 `schema_context_prepare` 输出；`schema_resolution` 继续负责 selected_tables / entity probe。
- 保持输出向后兼容。

验收：`test_query_constraints.py` 覆盖 high-confidence 规则抽取、constraint group、clock、LLM schema validation 和冲突处理。

### Phase 2：Prompt 与 artifact 透传

- `prompt_builders.py` 注入 `required_constraints` 与 `constraint_groups`。
- repair prompt 注入同一 contract。
- `sql_execute_readonly.py` / `result_filtering.py` 透传 constraints 和 coverage summary 到内部 artifact / audit。
- 给 generic finalizer 的用户可见信息必须整理进 `summary`、`source_summary`、`no_result_explanation` 等通用字段，不要求 main-agent 理解 SQLQuery 专属字段。

验收：`test_prompt_builders.py` 断言 SQL generation / repair prompt 包含 constraint block；result payload 的通用摘要字段能表达关键约束和来源表。

### Phase 3：Constraint Coverage Validator

- 在 `sql_generate.py` 新增 `_validate_constraint_coverage`。
- 校验年份、LIKE、范围、count、limit、order。
- 多表 `UNION ALL` 按分支校验 `global_filter` constraints。
- `branch_union` 组必须校验每个成员分支及 `matched_field` / `match_tier`。
- `ORDER BY` / `LIMIT` 作为 `query_level` 约束时必须作用在最终结果层。
- 校验失败进入现有本地 repair 流。

验收：LLM 输出漏年份、漏地区、漏申请者字段、漏 branch_union 成员或把 query-level limit 放错层时，必须 validation failed 并进入 repair/fallback；不能执行漏约束 SQL。

### Phase 4：Fallback Compiler 统一消费 constraints

- 改造 `_generate_entity_approval_variety_sql`、`_generate_cross_approval_variety_sql`、单表 approval fallback。
- 所有 required constraints 编入 WHERE / ORDER / LIMIT / COUNT。
- `global_filter` 追加到每个相关分支。
- `branch_union` 生成独立 `UNION ALL` 分支。
- entity 分支继续保留 `matched_field` / `match_tier`。

验收：无 LLM 或 LLM invalid 时 fallback SQL 仍覆盖 required constraints。

### Phase 5：审定品种库 E2E 回归

覆盖至少以下问题：

1. `隆平高科2021年都审定了什么品种？`
2. `隆平高科作为申请者在2021年审定了什么品种？`
3. `适合河南种植的2021年水稻品种有哪些？`
4. `国审稻20210001是什么品种？`
5. `近五年隆平高科申请审定了哪些品种？`
6. `隆平高科最新审定的10个品种是什么？`

验收：SQL artifact、guard report、query result preview 中均能看到约束被覆盖；最终回答说明实际来源表和关键约束。

## 2. 验收标准

| 类别 | 标准 |
| --- | --- |
| 约束抽取 | `2021年`、年份范围、近 N 年、地区、审定编号、申请者/育种者、limit/count/order 均能形成约束或明确软约束。 |
| Constraint group | `applicant_or_breeder` 等多字段语义必须形成 `branch_union`，不能被实现成不可追踪来源的普通 OR。 |
| Prompt | SQL generation 和 repair prompt 都包含 required constraints 与 constraint groups block。 |
| LLM SQL 校验 | LLM SQL 漏 required constraint 时不得进入 guard/execute。 |
| Fallback | fallback SQL 覆盖所有 required constraints。 |
| 多表查询 | 每个相关 `UNION ALL` 分支覆盖 `global_filter` constraints；query-level order/limit 作用在最终结果层。 |
| 来源标记 | entity-aware 查询保留 `source_table`、`source_crop`、`matched_field`、`match_tier`。 |
| Main-agent 边界 | 不出现 SQLQuery-specific main-agent prompt/runtime 改动；最终回答所需信息通过通用摘要字段提供。 |
| 安全 | SQL guard 只读、单语句和 selected_tables 边界不被削弱。 |
| 用户体验 | 缺失或冲突条件用自然语言说明，不暴露 SQL/DB/guard/retry 内部细节。 |
| 可观测性 | 约束抽取结果、coverage report、repair/fallback 原因进入 audit/artifact，用户可见内容保持脱敏。 |

## 3. 测试计划

### 3.1 Unit tests

- `skill/sql-query/tests/test_query_constraints.py`
  - 年份、范围、近 N 年、今年/去年。
  - freeze runtime clock 后 `近五年` 计算稳定。
  - 审定编号。
  - 地区/适种区域。
  - applicant/breeder/applicant_or_breeder。
  - `applicant_or_breeder` 生成 `branch_union` group。
  - count/limit/order。
  - 冲突年份澄清。
  - LLM structured extractor 无效 JSON / 未知字段 / 低置信建议被丢弃或降级。

- `skill/sql-query/tests/test_sql_generate_llm.py`
  - LLM 漏 `year` → validation failed / repair / fallback。
  - LLM 漏 `suitable_area` → validation failed / repair / fallback。
  - 多分支某一分支漏 global constraint → validation failed。
  - `branch_union` 被错误合并成 OR 或缺成员分支 → validation failed。
  - query-level `ORDER BY` / `LIMIT` 只放在单个 UNION 分支 → validation failed。
  - fallback SQL 覆盖 constraints。

- `skill/sql-query/tests/test_prompt_builders.py`
  - generation prompt 包含 `required_constraints`。
  - generation prompt 包含 `constraint_groups`。
  - repair prompt 包含 `required_constraints` 与 `constraint_groups`。

- `skill/sql-query/tests/test_schema_context_prepare.py`
  - schema materialization 后输出 `query_constraints`。
  - crop 已知时 constraints 绑定 selected table。
  - entity probe 输出可转换为 constraints。

### 3.2 Integration tests

- `skill/sql-query/tests/test_engine.py`
  - SQLQuery engine repair loop 能处理 constraint validation failed。
  - local repair 一次后仍失败时 fallback。
  - fallback 仍无法覆盖时不执行 SQL，并返回脱敏错误或 missing_input。

- `skill/sql-query/tests/test_e2e_llm_flow.py`
  - fake LLM 返回漏年份 SQL，最终执行 SQL 不漏年份。
  - fake LLM 返回普通 OR 的 `applicant_or_breeder` SQL，最终 repair/fallback 后保留来源分支。

### 3.3 Verification commands

```bash
pytest -q skill/sql-query/tests
python -m compileall -q skill/sql-query/runtime/sql_query_skill
```

若触及通用 Skill 输入传递，再补充：

```bash
pytest -q tests/integrations/agent_skills tests/orchestration tests/capabilities/main_agent
```

## 4. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 规则抽取误把普通数字当年份 | 限定年份范围、要求年字或上下文词；低置信进 soft constraints。 |
| 多约束组合导致 SQL 过窄无结果 | 无结果时说明已按哪些表/字段/条件检索；必要时建议用户放宽条件。 |
| LLM structured extractor 与规则冲突 | deterministic high confidence 优先；冲突触发澄清或丢弃低置信 LLM 项。 |
| regex validator 对复杂 SQL 误判 | 第一阶段要求生成简单 SELECT / UNION ALL；复杂场景进入 repair/fallback；后续评估 SQLGlot。 |
| fallback SQL 变复杂 | 把 constraint compiler 拆成小函数：WHERE 编译、projection 编译、branch 编译、query-level 包装、coverage 校验。 |
| main-agent 边界被侵入 | PR 和测试中增加边界检查：不得修改 main-agent prompt 注入 SQLQuery 专属规则。 |
| raw `query_constraints` 未进入 finalizer allowlist | SQLQuery 自己把用户可见约束覆盖信息写入通用 `summary/source_summary/no_result_explanation`；不依赖 finalizer 理解 raw 字段。 |

## 5. 已确认决策与延期评估

1. **LLM structured extractor 放置位置**：不新增公开 stage，不并入 `intent_route` 输出；新增 `query_constraints.py` 内部服务，由 `schema_context_prepare` 在 selected tables/columns materialized 后调用并附加到上下文。
2. **`applicant_or_breeder` 策略**：明确 applicant/breeder 时采用 primary + secondary 附带命中；未明确字段时采用 peer `branch_union`，生成独立分支并在回答中分别说明命中字段。
3. **generic `effective_user_message` 修复**：不纳入本 PRD 主线；只有测试证明所有 Skill 的通用输入传递存在缺陷时，才做独立通用 runtime 小修，不含 SQLQuery 特判。
4. **SQLGlot**：Phase 1-5 不新增依赖；只有 regex validator 维护成本或误判率不可接受时，才启动独立 dependency 评估和迁移计划。

## 6. 实施交付建议

建议按三个 PR/任务交付：

1. **PR-A：Constraint IR + prompt 注入**
   - 新增 `query_constraints.py`。
   - deterministic extractor、LLM schema validator、constraint groups。
   - prompt / artifact 透传。
   - 规则抽取单测。

2. **PR-B：Coverage validator + repair/fallback 接入**
   - `sql_generate.py` coverage validation。
   - fallback compiler 消费 constraints。
   - LLM 漏约束回归测试。

3. **PR-C：E2E hardening + optional generic Skill input fix**
   - 真实链路 / fake LLM e2e。
   - 如确有必要，单独修通用 Skill `effective_user_message` 传递，不含 SQLQuery 特判。
