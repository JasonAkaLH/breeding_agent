# SQLQuery Query Constraint Contract 阶段性 PRD

- **范围**：后端 / SQLQuery Skill domain engine
- **文档状态**：已评审修订，待代码实现
- **日期**：2026-06-15
- **外部证据日期**：2026-06-15
- **上游基线**：
  - `skill/sql-query/docs/prd/backend/06-SQLQuery-MVP设计.md`
  - `skill/sql-query/docs/prd/backend/07-SQLQuery-LLM增强与真实库验证.md`
  - `skill/sql-query/docs/prd/backend/09-高层DAG规划与SQLQuery宏能力边界.md`
  - `docs/superpowers/specs/2026-06-15-sqlquery-llm-schema-resolution-design.md`
  - `docs/superpowers/specs/2026-06-15-sqlquery-entity-aware-probe-design.md`

## 1. 背景

SQLQuery 当前已作为 `skill.sql_query` platform-service 暴露，外部编排层只看到公开 Skill capability；SQLQuery 自己在 Skill handler 内串联 `intent_route -> schema_resolution -> schema_context_prepare -> sql_generate -> sql_guard -> sql_execute_readonly -> result_filtering` 等领域阶段。

当前实现已经具备以下能力：

- `schema_resolution` 输出 `selected_tables`，后续 SQL 生成和 guard 以它作为表范围权威。
- `prompt_builders` 会把原始 query / resolved query、schema context、entity resolution、guard constraints 拼进 SQL 生成与修复 prompt。
- `sql_generate` 能调用 LLM、校验基础 schema/table/字段引用、在本地 validation failed 时触发 repair 或 fallback。
- `sql_guard` 校验只读、单语句、禁止写模式、禁止系统 schema、禁止超出 `selected_tables`。
- entity-aware probe 已能把名称按 `variety_name`、`applicant`、`breeder`、`approval_num` 等字段做跨作物表探测，并输出 `match_summary`。

但当前 SQLQuery 仍缺少一层通用的“用户语义约束覆盖”机制。用户 query 中的 `2021年`、`适合河南种植`、`作为申请者`、`审定编号`、`前10个` 等过滤条件虽然出现在自然语言 prompt 中，但没有被提升成 runtime 可验证的必达约束。因此 LLM 或 fallback 生成的 SQL 可能安全、合法、可执行，却漏掉用户 query 中的关键 WHERE / ORDER / LIMIT 条件。

## 2. 问题陈述

SQLQuery 需要解决的问题不是单点“年份漏过滤”，而是所有用户查询约束都可能在 SQL 生成中被漏掉：

1. **自然语言 query 不是机器约束**：`prompt_builders.py` 已把 `user_question`、`original_user_query`、`resolved_user_query` 写入 prompt，但 SQL 生成后没有检查这些自然语言条件是否全部被 SQL 覆盖。
2. **SQL guard 只负责安全边界**：`sql_guard.py` 校验只读、单语句和表范围，不负责判断 `WHERE year = 2021`、`suitable_area LIKE '%河南%'` 是否存在。
3. **entity-aware 校验只覆盖 entity 分支**：`sql_generate.py` 现有 `_validate_entity_filter_policy` 只校验 `match_summary` 中的 entity 字段 LIKE 与 `matched_field/match_tier` 来源标记，不覆盖年份、地区、排序、数量、限制条数等通用约束。
4. **fallback compiler 未统一消费 query constraints**：当前 fallback 能基于 entity match summary 生成分支 SQL，但缺少统一约束输入，导致 fallback 也可能漏掉 query 中的其他条件。
5. **历史补全不是本问题唯一来源**：即使第一轮 query 自身包含 `2021年`，只要没有约束覆盖校验，SQL 仍可能漏掉年份条件。follow-up 的 `resolved_user_query` 传递问题是第二道防线，不应替代 query constraint contract。

## 3. 目标

1. 在 SQLQuery Skill 内新增 query constraint contract，把用户 query 中的必达过滤、排序、聚合和限制条件转成机器可校验 IR。
2. SQL 生成 prompt、repair prompt、fallback compiler 都必须消费同一份 `required_constraints` 和 `constraint_groups`。
3. SQL 生成后必须执行 constraint coverage validation：若 required constraints 未覆盖，则本地 validation failed，进入现有 repair / fallback 流程。
4. 第一阶段覆盖审定品种库高频业务约束：`year`、`approval_num`、`variety_name`、`applicant`、`breeder`、`applicant_or_breeder`、`suitable_area`、`crop_name`、`count`、`limit`、`order_by`。
5. 多表 `UNION ALL` 查询中，全局过滤约束必须在每个相关分支中覆盖；`ORDER BY` / `LIMIT` 这类查询级约束必须在最终结果层覆盖；字段角色约束继续保留 `matched_field` / `match_tier` 来源标记。
6. 不为了 SQLQuery 定制 `main_agent` prompt、finalizer 或通用编排层语义；SQLQuery 只消费通用 Skill 输入和自身领域上下文。
7. 用户可见错误保持脱敏；约束校验失败、SQL repair、fallback 等内部细节只进入 audit / artifact，不直接暴露给用户。

## 4. 非目标

- 不开放写操作、DDL、多语句、系统库、跨库访问或任意 SQL 执行。
- 不让 main-agent 理解 SQLQuery 字段、表、SQL 来源标记或约束覆盖规则。
- 不要求 LLM 生成字节级幂等 SQL；本 PRD 关注语义约束覆盖和可验证性。
- 不在第一阶段引入 SQLGlot 或其他新依赖；AST parser 作为后续可选增强。
- 不要求 query constraint extractor 解决所有自然语言歧义；无法高置信解析时应降级为澄清、软约束或 conservative fallback。
- 不改变基因型数据库固定 4 表策略；基因型库约束覆盖可在审定品种库稳定后再扩展。
- 不改变现有 `skill.contract.yaml` 的公开能力边界或 platform-service 注册事实。

## 5. 用户、利益相关方与受影响系统

| 对象 | 影响 |
| --- | --- |
| 业务用户 | 得到更符合原始问题约束的审定品种库查询结果；无结果时能知道系统按哪些条件查过。 |
| SQLQuery Skill runtime | 新增约束抽取、约束透传、coverage validation 和 fallback compiler 消费约束的内部逻辑。 |
| 通用 Skill 编排 / main-agent finalizer | 不增加 SQLQuery-specific 规则；只消费 SQLQuery 结果中已经自然语言化的通用摘要字段。 |
| 测试与运维 | 需要新增约束抽取、SQL 覆盖校验、repair/fallback 和 E2E 回归测试；内部失败细节进入 audit/artifact。 |

## 6. 外部技术参考与设计依据

本 PRD 采用业界 Text-to-SQL 常见的“结构化中间层 + schema linking + SQL 校验 + repair/fallback”模式。以下链接在 2026-06-15 用作设计依据，具体 API 细节以实现时重新核验为准：

- AWS Text2SQL best practices 将 Text-to-SQL 拆为自然语言解析、结构化表示、SQL 生成和数据库查询等阶段，说明不应把自然语言直接视为 SQL 约束完成态。参考：<https://aws.amazon.com/blogs/machine-learning/generating-value-from-enterprise-data-best-practices-for-text2sql-and-generative-ai/>
- LangChain SQL agent 文档建议先取相关表 schema，再生成 query、检查常见错误、执行并根据数据库错误修复。参考：<https://docs.langchain.com/oss/python/langchain/sql-agent>
- LlamaIndex Text-to-SQL 文档支持 query-time table/schema retrieval 与 row/column retriever，用检索增强 schema context。参考：<https://developers.llamaindex.ai/python/examples/index_structs/struct_indices/sqlindexdemo/>
- RAT-SQL 将 schema encoding 和 schema linking 作为 Text-to-SQL 泛化的核心问题。参考：<https://arxiv.org/abs/1911.04942>
- DIN-SQL 通过任务拆解和 self-correction 提升 LLM Text-to-SQL 表现。参考：<https://openreview.net/forum?id=p53QDxSIc5>
- PICARD 使用 constrained decoding 降低非法 SQL 生成；本 PRD 不实现 token-level constrained decoding，但吸收“生成后必须被结构约束”的原则。参考：<https://aclanthology.org/2021.emnlp-main.779/>
- Execution-Guided Decoding 证明执行反馈能排除错误程序；本 PRD 继续复用现有 SQL repair loop，但语义约束覆盖不能只依赖远端执行错误。参考：<https://arxiv.org/abs/1807.03100>

## 7. 现有代码锚点

| 领域 | 当前位置 | 本 PRD 的影响 |
| --- | --- | --- |
| SQLQuery stage 串联 | `skill/sql-query/runtime/sql_query_skill/engine.py` | 不新增公开 stage；repair loop 继续复用。 |
| Skill 输入边界 | `skill/sql-query/runtime/sql_query_skill/platform_handler.py` | 继续只接收通用 `query/user_message/subtask_label/parent_question`，不新增 main-agent 特判。 |
| schema/table 权威 | `skill/sql-query/runtime/sql_query_skill/schema_resolution.py` | entity probe 输出将成为部分 constraints 的来源；`selected_tables` 仍是唯一表范围权威。 |
| schema materialization | `skill/sql-query/runtime/sql_query_skill/schema_context_prepare.py` | 在 selected columns 可见后附加 query constraints，避免新增 stage。 |
| prompt 拼装 | `skill/sql-query/runtime/sql_query_skill/prompt_builders.py` | SQL generation / repair prompt 注入 `required_constraints`、`constraint_groups` 与 coverage 要求。 |
| SQL 生成与本地校验 | `skill/sql-query/runtime/sql_query_skill/sql_generate.py` | 新增 constraint coverage validator，并让 fallback compiler 消费 constraints。 |
| SQL 安全 guard | `skill/sql-query/runtime/sql_query_skill/sql_guard.py` | 保持安全职责；不把业务语义覆盖混入 guard。 |
| 执行与结果筛选 | `skill/sql-query/runtime/sql_query_skill/sql_execute_readonly.py`、`result_filtering.py` | 透传 constraints / coverage summary 到内部 artifact，并在通用摘要字段中自然语言化。 |
| 通用 Skill 编排 | `src/orchestration/skill_workflow_provider.py`、`src/capabilities/skill_tool/executor.py`、`src/capabilities/main_agent/prompt_builder.py` | 不做 SQLQuery 定制；如需调整 dependency allowlist，只能做通用字段扩展，不能写入 SQLQuery 业务规则。 |

## 8. 文档拆分与阅读路径

本阶段性 PRD 只保留背景、目标、边界、外部依据与代码锚点。这里的“两个附属文档”不是两个实施阶段；实际实施阶段定义在 `implementation-test-plan.md` 中，当前为 Phase 0-5，另有 SQLGlot 后续评估项。

详细设计和实施计划已拆分到同目录附属文档：

1. [`technical-design.md`](technical-design.md)
   - Query Constraint Contract 结构。
   - deterministic extractor / LLM structured extractor / entity probe 分工。
   - SQL prompt、repair prompt、coverage validator、fallback compiler、result filtering 与 main-agent 边界。
2. [`implementation-test-plan.md`](implementation-test-plan.md)
   - Phase 0-5 分阶段实施计划。
   - 验收标准、测试计划、风险缓解、已确认决策和 PR 拆分建议。

后续实现评审时，先读本文确认产品目标与边界，再读技术设计确认内部契约，最后读实施与测试计划安排开发顺序。
