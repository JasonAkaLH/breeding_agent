# Phase 5.5：SQLQuery LLM 增强专题

> 状态：首轮实现完成（2026-04-24）  
> 定位：在一期 Phase 0 ~ Phase 7 已验收通过之后，围绕 SQLQuery capability 的 LLM 主路径升级进行专题规划与后续实施沉淀。  
> 讨论入口：后续关于“本项目如何接入 LLM”的阶段性结论，优先沉淀到本文档。

## 1. 专题命名与定位

本专题正式命名为：

> **Phase 5.5：SQLQuery LLM 增强专题**

这里的 “5.5” 表示它是 **Phase 5 SQLQuery MVP 能力链路的后续增强专题**，而不是把已经完成的 Phase 6 / Phase 7 重新打开。

当前一期已经完成：
- Phase 5：SQLQuery MVP 能力链路；
- Phase 6：FastAPI / SSE 对外接口；
- Phase 7：一期验收与第二阶段评估。

因此 Phase 5.5 的启动前提是：

> **在一期非 LLM 主导版本已稳定收口之后，针对 SQLQuery 内部生成与总结能力做 LLM 化升级。**

## 2. 当前仓库事实

当前仓库已经具备：

- `src/capabilities/sql_query/`：SQLQuery capability 六节点链路；
- `src/capabilities/sql_query/workflow.py`：标准 SQLQuery workflow；
- `src/capabilities/sql_query/executor.py`：`sql_generator` / `summarizer` / `llm_text_generator` 注入位；
- `src/capabilities/sql_query/sql_guard.py`：只读 SQL 安全校验；
- `src/integrations/mysql_readonly.py`：MySQL 只读执行适配；
- `src/integrations/llm_client.py`：异步 OpenAI-compatible client seam；
- `src/capabilities/sql_query/prompt_builders.py`：SQLQuery LLM prompt 组装；
- `src/capabilities/sql_query/llm_utils.py`：LLM 输出 JSON 解析与 rows preview 序列化工具；
- `docs/SQLQuery-LLM版本改造方案.md`：已有 LLM 版本改造提案；
- `docs/LLM接入阶段建议.md`：已有 LLM 接入阶段建议。

Phase 5.5 首轮后已落地的部分：

- `sql_query.sql_generate` 支持显式注入 `llm_text_generator` 作为 LLM 主路径，未注入或 LLM 失败时回退启发式 SQL 生成；
- `sql_query.result_summarize` 支持显式注入 `llm_text_generator` 作为 LLM 摘要主路径，未注入或 LLM 失败时回退确定性模板摘要；
- `src/integrations/llm_client.py` 的 `generate_text()` 默认使用非 streaming completion，适合作为 SQLQuery 结构化 JSON 生成 seam；`stream_text()` 提供非 thinking + streaming 文本输出模式，预留给主代理流式回传用户输出；`generate_text_with_thinking()` 保留 thinking / reasoning chunk 输出，供需要逐步展示思考与回答的场景使用；
- `sql_query.result_summarize` 已额外依赖 `sql_query.sql_generate` 输出，避免 DB 执行节点透传摘要层上下文；
- 自动化测试全部使用 fake LLM / fake stream，不访问真实 provider。

Phase 8 首轮后已补齐的部分：

- 主代理已通过 `main_agent.respond` 接入非 thinking streaming LLM seam，`capability_id=None` 的普通消息默认进入主代理；

当前仍未落地的部分：

- 通用子代理未接入 LLM；
- 真实 provider 的生产 runtime 绑定与手工 smoke 验证尚未补齐。

## 3. 专题目标

Phase 5.5 的目标是：

1. 把 `sql_query.sql_generate` 从启发式生成升级为 **LLM 主路径 + 确定性 fallback**。
2. 把 `sql_query.result_summarize` 从模板摘要升级为 **LLM 主路径 + 结构化 fallback**。
3. 沉淀一个后续可复用的 LLM 接入 seam，但不提前泛化成复杂框架。
4. 保持 Phase 4 / Phase 5 已确定的编排边界不变：
   - orchestration 不理解 SQL prompt；
   - orchestration 不直接接触 schema 细节；
   - LLM 输出不绕过 `sql_guard`；
   - SQL 执行仍必须走 readonly adapter。
5. 保证所有 LLM 行为可测试、可审计、可降级。

## 4. 非目标

本专题不做：

- 不重写主代理 orchestration 内核；
- 不做主代理任务理解的 LLM 化；
- 不做通用子代理 / worker 型能力的 LLM 化；
- 不做多 capability 路由竞争；
- 不做写入型 SQL、管理员豁免或绕过 guard 的执行路径；
- 不引入 LangChain、LangGraph、AutoGen 等现成 Agent 框架；
- 不把 prompt、schema 和 SQL 细节反向塞入主框架通用层；
- 不把真实 LLM 调用混入默认单元测试和常规 e2e 回归。

## 5. 初始设计原则

### 5.1 LLM 不拥有执行权

LLM 可以生成 SQL 草案、解释查询结果，但不能决定是否执行 SQL。

执行权继续由：

```text
sql_generate -> sql_guard -> sql_execute_readonly
```

这条链路控制。

### 5.2 安全性不交给 LLM 保证

prompt 中可以声明只读、单语句、白名单、LIMIT 等要求，但最终安全判定必须由 `sql_guard` 执行。

### 5.3 LLM 失败必须可降级

以下情况必须进入 fallback：

- provider 调用失败；
- provider 超时；
- 输出为空；
- 输出结构不符合预期；
- 输出无法提取 SQL 或摘要；
- 输出被 guard 阻断后需要保留可解释失败信息。

### 5.4 LLM 输出必须结构化

`sql_generate` 不应直接信任自由文本输出。建议结构至少包含：

```json
{
  "mode": "answer | clarify | reject",
  "sql": "SELECT ... LIMIT ...",
  "reason": "生成依据或拒绝原因",
  "supported_scope_hint": "拒答时的支持范围提示",
  "clarification_question": "需要澄清时的问题"
}
```

`result_summarize` 也应避免把原始模型输出无约束透传给前端，至少需要有摘要文本、行数上下文与 fallback 标记。

### 5.5 默认测试不依赖真实 LLM

默认测试只使用 fake provider / stub generator / stub summarizer，真实 provider 调用只进入单独的手工验证或显式集成测试。

## 6. 建议实施切面

### Step 1：收口 LLM client seam

讨论点：

- `src/integrations/llm_client.py` 已作为当前 LLM client 入口；
- 是否定义最小 `LLMTextGenerationPort`；
- provider 配置从哪里读取；
- timeout / retry / max tokens / model name 如何表达；
- 审计中是否记录 provider、模型、耗时、fallback reason，但不记录敏感 prompt 全文。

#### Step 1 初步结论：LLM seam 放在 integrations 层，能力层只依赖最小文本生成接口

当前讨论先形成以下倾向性结论：

1. **不把 LLM client 留在仓库根目录作为长期入口**。当前 LLM client 已迁入 `src/integrations/llm_client.py`，与既有 `mysql_readonly.py` 一样作为外部系统适配。
2. **不把 provider 细节写入 `src/core/`**。`core` 只承载跨 capability 稳定语义；LLM 的 provider、model、prompt、token、retry 等仍属于外部适配 / capability 使用细节，不应污染核心契约。
3. **SQLQuery capability 不直接依赖 OpenAI SDK**。`sql_generate` / `result_summarize` 应通过一个最小文本生成接口使用 LLM，测试时注入 fake，真实运行时注入 OpenAI-compatible adapter。
4. **先保持接口小而窄**。Phase 5.5 只需要文本生成能力，不需要提前抽象 tool calling、embedding、RAG、多模型路由或 agent worker 协议。
5. **审计记录 metadata，不默认记录完整 prompt**。建议记录 provider、model、latency、是否 fallback、fallback reason、输出结构校验状态；完整 prompt 可能包含业务 schema 或用户问题，默认不进普通审计日志。

建议的实现形态是：

```text
src/integrations/
  llm_client.py          # OpenAI-compatible concrete adapter
  llm_types.py           # LLM request/response dataclass 或轻量类型

src/capabilities/sql_query/
  prompt_builders.py     # SQLQuery 专属 prompt 组装
  sql_generate.py        # 调用注入的 text generator，失败回退启发式
  result_summarize.py    # 调用注入的 text generator，失败回退模板摘要
```

其中 `prompt_builders.py` 放在 `src/capabilities/sql_query/`，是因为 SQLQuery prompt 直接依赖 route、schema context、SQL policy 和业务字段裁剪规则，不应放进通用 integration 层。

### Step 2：改造 `sql_query.sql_generate`

讨论点：

- prompt 输入如何复用 `schema_context_prepare` 的输出；
- 如何只注入 `expose_to_llm: true` 的字段；
- LLM 输出结构如何校验；
- 何时 fallback 到当前启发式生成；
- guard blocked 时如何把原因反馈给任务状态、artifact 与 audit。

#### Step 2 初步结论：`sql_generate` 采用结构化 JSON 契约，`answer` 进 guard，`clarify` 进 interrupt，`reject` 进非重试失败

`sql_generate` 的 LLM 化不改变现有 workflow 拓扑。它仍位于：

```text
intent_route -> schema_context_prepare -> sql_generate -> sql_guard -> sql_execute_readonly
```

本节点的输入以 `schema_context_prepare` 的输出为主，因为该输出已经汇总了 route、schema、allowed tables、SQL policy profile 与用户问题。Prompt builder 只在此基础上补充任务元信息和输出格式要求，不重新读取全库 schema。

**建议输入契约**：

```json
{
  "task_meta": {
    "node_name": "sql_query.sql_generate",
    "conversation_id": "...",
    "task_id": "..."
  },
  "route_context": {
    "route_id": "...",
    "schema_profile_id": "...",
    "allowed_tables": ["..."],
    "sql_policy_profile": "strict_readonly_mysql"
  },
  "schema_context": {
    "selected_tables": ["..."],
    "selected_columns": {"table": ["column"]},
    "join_hints": [
      {"left_table": "...", "left_column": "...", "right_table": "...", "right_column": "...", "reason": "..."}
    ],
    "context_summary": "..."
  },
  "guard_constraints": {
    "readonly_only": true,
    "single_statement_only": true,
    "require_limit_for_non_aggregate_query": true,
    "max_limit": 200,
    "allowed_statement_types": ["SELECT", "WITH_SELECT"]
  },
  "user_question": "..."
}
```

输入约束：

- `selected_columns` 必须继续只来自 `schema_context_prepare` 裁剪后的结果，不在 LLM 节点重新扩表或扩字段。
- `allowed_tables` 是硬白名单；LLM 输出引用的表必须是其子集。
- `join_hints` 是唯一允许的多表连接依据；模型不得自行发明 join。
- SQL guard 规则只注入执行必要摘要，不把 `configs/sql_query/sql_guard_rules.yaml` 原文整块灌入 prompt。
- prompt 中允许包含用户问题和裁剪后的业务 schema，但默认不进入普通审计日志。

**建议输出契约**：

```json
{
  "mode": "answer | clarify | reject",
  "route_id": "...",
  "schema_profile_id": "...",
  "sql": "SELECT ... LIMIT ...",
  "tables_used": ["..."],
  "columns_used": ["table.column"],
  "join_hints_used": ["left_table.left_column = right_table.right_column"],
  "missing_info": null,
  "clarifying_question": null,
  "reject_reason": null,
  "supported_scope_hint": null
}
```

节点处理规则：

1. `mode = answer`：
   - 要求 `sql` 非空；
   - 要求 `route_id` / `schema_profile_id` 与上游上下文一致；
   - 要求 `tables_used` 是 `allowed_tables` / `selected_tables` 子集；
   - 通过本节点结构校验后，输出 `sql` 给下游 `sql_guard`；
   - 即使结构校验通过，SQL 仍必须经过 `sql_guard`，不能在本节点直接执行。
2. `mode = clarify`：
   - 本节点返回 `Interrupt`，只问一个最关键问题；
   - 不生成 SQL，不进入 `sql_guard`。
3. `mode = reject`：
   - 本节点返回非重试型 `CapabilityExecutionError`；
   - `sql` 必须为空；
   - 不进入 `sql_guard`。
4. provider 调用失败、超时、空输出、非法 JSON、缺少必填字段、`answer` 结构校验失败：
   - 回退到当前启发式 SQL 生成逻辑；
   - output payload 标记 `generation_source = "fallback"` 与 `fallback_reason`；
   - fallback 生成的 SQL 仍必须进入 `sql_guard`。
5. `sql_guard` 阻断不在 Phase 5.5 最小版里自动二次 fallback：
   - guard blocked 是安全失败，不应被静默掩盖；
   - 可在后续增强里考虑“guard 失败后带 block reason 重新生成一次”，但这不作为 Phase 5.5 的第一版要求。

建议 `sql_generate` artifact 至少保留：

- `sql`；
- `generation_source`: `llm | fallback`；
- `llm_mode`: `answer | clarify | reject | parse_failed | provider_failed`；
- `fallback_used`;
- `fallback_reason`;
- `tables_used` / `columns_used`;
- `route_id` / `schema_profile_id`。

### Step 3：改造 `sql_query.result_summarize`

讨论点：

- 模型输入是否只包含 columns、row_count、有限 rows preview；
- 大结果集如何截断；
- 摘要是否需要结构化字段；
- fallback 摘要如何保持当前确定性行为；
- 前端 artifact 如何展示 LLM 摘要与 fallback 状态。

#### Step 3 初步结论：`result_summarize` 只解释已执行结果，输入必须限量，输出以 `summary` 为稳定主字段

`result_summarize` 的 LLM 化不改变执行权边界。它只能消费 `sql_execute_readonly` 已经返回的结果，不能重新生成 SQL、修改 SQL、补查数据库或推断结果集中不存在的事实。

当前 `sql_execute_readonly` 的输出只有：

```json
{
  "sql": "...",
  "guard_pass_token": "...",
  "columns": ["..."],
  "rows": [{"column": "value"}],
  "row_count": 1
}
```

如果希望 LLM 摘要理解用户原始问题和 route，上游需要补一个输入通道。Phase 5.5 第一版推荐采用以下二选一方案：

1. **优先方案：让 `result_summarize` 额外直接依赖 `sql_generate` 输出**。这样可以拿到 `user_question`、`route_id`、`schema_profile_id`、`generation_source` 等上下文，而不让 `sql_execute_readonly` 承担透传业务上下文职责。
2. **备选方案：由 `sql_execute_readonly` 透传少量 route / question context**。实现更局部，但会让执行节点 payload 混入摘要层关心的信息。

倾向选择优先方案，因为 orchestration 已支持一个节点依赖多个上游输出；这比让 DB 执行节点携带摘要语义更清晰。

**建议输入契约**：

```json
{
  "task_meta": {
    "node_name": "sql_query.result_summarize",
    "conversation_id": "...",
    "task_id": "..."
  },
  "question_context": {
    "user_question": "...",
    "route_id": "...",
    "schema_profile_id": "...",
    "sql": "SELECT ... LIMIT ..."
  },
  "result_context": {
    "columns": ["..."],
    "row_count": 42,
    "rows_preview": [{"column": "value"}],
    "preview_row_count": 20,
    "truncated": true
  },
  "summary_policy": {
    "language": "zh-CN",
    "do_not_fabricate": true,
    "mention_truncation_when_truncated": true,
    "max_summary_chars": 800
  }
}
```

输入约束：

- 默认不把完整 rows 全量发送给 LLM；只发送 `rows_preview`。
- `rows_preview` 的默认上限建议为 20 行；如果后续需要可配置，但不得超过 SQL guard 的 `max_limit`。
- `row_count = 0` 时可以直接走确定性摘要，不必调用 LLM。
- `truncated = true` 时，摘要必须明确“基于前 N 行预览”。
- prompt / 审计默认不记录完整 rows；artifact 可以保留最终 summary 与必要 metadata。
- 模型不得根据 SQL 字段名或业务常识补造未出现在结果集里的结论。

**建议输出契约**：

```json
{
  "summary": "面向用户的中文摘要",
  "highlights": ["可选的关键发现"],
  "row_count": 42,
  "preview_row_count": 20,
  "truncated": true,
  "caveats": ["基于前 20 行预览"],
  "summary_source": "llm"
}
```

节点处理规则：

1. LLM 输出必须是结构化 JSON，且 `summary` 是唯一稳定必填的用户可见主字段。
2. `highlights`、`caveats` 可以作为增强字段，但前端和下游不应依赖它们完成一期功能。
3. provider 调用失败、超时、空输出、非法 JSON、缺少 `summary`、摘要为空或明显超过长度上限时，回退到当前确定性模板摘要。
4. fallback 输出必须保留：
   - `fallback_used = true`；
   - `summary_source = "fallback"`；
   - `fallback_reason`。
5. LLM 摘要只影响最终 artifact / 用户可读解释，不影响 task 是否完成；只要查询执行成功且 fallback 可用，摘要节点应尽量收敛为 completed。
6. 如果 rows 中存在非 JSON 可序列化值，先在 prompt builder 层做字符串化 / 截断，不把序列化异常留给 provider 调用阶段。

建议 `result_summarize` artifact 至少保留：

- `summary`；
- `summary_source`: `llm | fallback`;
- `fallback_used`;
- `fallback_reason`;
- `row_count`;
- `preview_row_count`;
- `truncated`;
- `route_id` / `schema_profile_id`（如果可取得）。

### Step 4：补齐测试与验收

建议测试层次：

- unit：fake LLM 成功 / 超时 / 空输出 / 非法 JSON / reject / clarify；
- capability：`sql_generate` LLM 主路径与 fallback；
- capability：`result_summarize` LLM 主路径与 fallback；
- orchestration：LLM 生成 SQL 仍必须经过 guard；
- e2e：默认 fake LLM 跑通 happy path；
- observability：audit 能看到 LLM call、fallback、guard blocked 等关键事件。

#### Step 4 初步结论：按 seam -> prompt -> capability -> orchestration -> e2e 的顺序 TDD 推进

Phase 5.5 的测试策略必须继续遵循项目 TDD 约束：先补失败测试，再改实现。默认自动化测试不访问真实 LLM provider。

**建议测试矩阵**：

| 层级 | 建议测试位置 | 必测行为 | 是否真实 LLM |
|---|---|---|---|
| integration seam | `tests/integrations/test_llm_client.py`（若新增该目录，需同步 README / AGENTS 测试命令） | config 注入、`config.yaml` 路径、fake stream 聚合、reasoning / answer chunk 提取 | 否 |
| prompt builder | `tests/capabilities/sql_query/test_prompt_builders.py` | SQL 生成 prompt 只包含裁剪后的 schema；摘要 prompt 只包含 rows preview；不输出完整 rows 到审计 payload | 否 |
| sql_generate capability | `tests/capabilities/sql_query/test_sql_generate_llm.py` | LLM `answer` 主路径、`clarify` interrupt、`reject` 非重试失败、非法 JSON fallback、provider error fallback | 否 |
| result_summarize capability | `tests/capabilities/sql_query/test_result_summarize_llm.py` | LLM 摘要主路径、0 行确定性摘要、rows preview 截断、非法 JSON fallback、provider error fallback | 否 |
| orchestration | `tests/capabilities/sql_query/test_orchestration_flow.py` 或新增同目录测试 | LLM SQL 草案仍进入 `sql_guard`；guard blocked 不被静默 fallback；summary 可额外依赖 `sql_generate` 输出 | 否 |
| e2e | `tests/e2e/` | fake LLM happy path、fake LLM fallback path、clarify path 可恢复 | 否 |
| observability | `tests/observability/` | 记录 LLM metadata、fallback reason、guard blocked；不默认记录完整 prompt / 完整 rows | 否 |
| manual smoke | 手工命令或后续单独文档 | 使用真实 `config.yaml` 调 provider 验证连通性 | 是，显式手工 |

**最小实施顺序**：

1. **冻结 LLM seam 测试**
   - 为 `src/integrations/llm_client.py` 补 fake stream / config 注入测试。
   - 如果新增 `tests/integrations/`，同步更新 `README.md` 与 `AGENTS.md` 的最小测试命令。

2. **新增 prompt builder 测试与实现**
   - 新增 `src/capabilities/sql_query/prompt_builders.py`。
   - 先覆盖 SQL prompt：只读约束、route/schema 裁剪、JSON 输出要求。
   - 再覆盖 summary prompt：rows preview 截断、truncated 标记、不要编造。

3. **改造 `sql_generate`**
   - 先让 capability 支持注入 async text generator / fake LLM。
   - 实现 LLM response parser 与结构校验。
   - 实现 `answer` / `clarify` / `reject` / fallback 四类路径。
   - 保留当前启发式 `_generate_sql` 作为 fallback。

4. **改造 workflow 依赖与 `result_summarize`**
   - 让 `result_summarize` 同时依赖 `sql_execute_readonly` 与 `sql_generate`。
   - 支持注入 async text generator / fake LLM。
   - 实现 rows preview、summary parser 与 fallback。

5. **补齐 observability**
   - 记录 `llm.call` / `llm.fallback` / `llm.parse_failed` 等 audit-only 事件或等价 metadata。
   - 默认不写完整 prompt、完整 rows、API key。

6. **回归一期既有链路**
   - 跑 SQLQuery capability 测试、API 测试、e2e 测试、observability 测试。
   - 确认 Phase 5.5 没有破坏一期验收结论。

**阶段门槛**：

- 不允许把真实 provider 调用放进默认 unittest。
- 不允许为了 LLM 摘要改弱 `sql_guard`。
- 不允许 `result_summarize` 失败导致已成功查询的任务整体失败，除非 fallback 也不可用。
- 不允许新增 LangChain / LangGraph / AutoGen。
- 新增测试目录或测试命令时，必须同步更新 `README.md` 与 `AGENTS.md`。

## 7. 初始验收口径

Phase 5.5 通过的最低标准：

- [x] `sql_generate` 支持 LLM 主路径；
- [x] `sql_generate` 在 LLM 失败时回退到当前启发式生成；
- [x] `result_summarize` 支持 LLM 主路径；
- [x] `result_summarize` 在 LLM 失败时回退到当前模板摘要；
- [x] LLM 生成的 SQL 无论如何都必须经过 `sql_guard`；
- [x] 默认自动化测试不依赖真实 LLM provider；
- [x] 关键 LLM 调用、fallback、guard blocked 有可观测记录；
- [x] README / AGENTS 中的测试命令在需要时同步更新；
- [x] 不引入 LangChain / LangGraph / AutoGen。

## 8. 与已有文档关系

- `docs/LLM接入阶段建议.md`：回答 LLM 接入先后顺序，本文档承接其中的 SQLQuery 内部 LLM 增强建议。
- `docs/SQLQuery-LLM版本改造方案.md`：已有较完整改造方案，本文档作为 Phase 5.5 的开发过程与讨论沉淀入口。
- `docs/SQLQuery提示词输入模板.md`：可作为 `sql_generate` prompt 设计输入。
- `docs/dev_processes/Phase-5-接入SQLQuery-MVP能力链路.md`：Phase 5.5 必须复用并保护 Phase 5 已完成的 capability 边界。
- `docs/一期验收报告.md`：Phase 5.5 不修改一期验收结论，而是在一期之后作为增强专题推进。

## 9. 讨论记录

### 2026-04-24：专题命名确认

- 确认后续关于“本项目如何接入 LLM”的近期讨论，优先落到 `Phase 5.5：SQLQuery LLM 增强专题`。
- 确认 Phase 5.5 先聚焦 SQLQuery 内部 LLM 化，不直接扩展到主代理 / 通用子代理 LLM 化。
- 确认本文档作为后续讨论沉淀入口。

### 2026-04-24：LLM seam 初步放置结论

- 执行层面已将根目录 `llm_client.py` 移动为 `src/integrations/llm_client.py`，并通过 `src/integrations/__init__.py` 导出 `LLMClient`、`ReasoningEffort` 与 `load_config`。
- 已将当前 LLM client 入口放在 `src/integrations/llm_client.py`，根目录不再保留长期入口。
- 倾向不把 provider / model / prompt / token 等细节放进 `src/core/`，避免核心契约提前被 LLM 专题污染。
- 倾向让 SQLQuery 通过最小文本生成接口调用 LLM，并把 SQLQuery prompt 组装保留在 `src/capabilities/sql_query/` 内部。
- 倾向默认审计 LLM 调用 metadata 和 fallback reason，不默认记录完整 prompt。

### 2026-04-24：`sql_generate` LLM 输入 / 输出契约初步结论

- 确认 `sql_generate` 的 LLM 输入以 `schema_context_prepare` 输出为主，只补充任务元信息、guard 摘要与输出格式要求，不重新扩表或读取全库 schema。
- 确认 LLM 输出采用 `mode = answer | clarify | reject` 的结构化 JSON 契约，与 `docs/SQLQuery提示词输入模板.md` 保持一致。
- 确认 `answer` 只表示生成 SQL 草案成功，仍必须进入 `sql_guard`；`clarify` 返回 interrupt；`reject` 返回非重试失败。
- 确认 provider / parse / schema 校验失败时 fallback 到当前启发式生成，但 guard blocked 不在第一版里静默 fallback。

### 2026-04-24：`result_summarize` LLM 输入 / 输出契约初步结论

- 确认 `result_summarize` 只解释 `sql_execute_readonly` 已执行结果，不重新生成 SQL、不补查数据库、不改变任务完成判定。
- 确认摘要 LLM 输入默认只发送 columns、row_count 和有限 `rows_preview`，不把完整结果集和完整 prompt 写入普通审计日志。
- 确认如果摘要需要用户问题 / route 上下文，优先让 `result_summarize` 额外依赖 `sql_generate` 输出，而不是让 DB 执行节点承担业务上下文透传职责。
- 确认 LLM 输出以结构化 JSON 为目标，`summary` 是稳定主字段；provider / parse / 空摘要等失败时回退当前确定性模板摘要。

### 2026-04-24：Phase 5.5 测试矩阵与最小实施顺序初步结论

- 确认 Phase 5.5 按 seam -> prompt builder -> `sql_generate` -> `result_summarize` -> observability -> 回归验收的顺序推进。
- 确认默认自动化测试全部使用 fake LLM / fake stream，不访问真实 provider；真实 provider 只做显式手工 smoke。
- 确认若新增 `tests/integrations/` 或新的测试命令，必须同步更新 `README.md` 与 `AGENTS.md`。
- 确认 observability 测试需要覆盖 LLM metadata 与 fallback reason，同时验证默认不记录完整 prompt / 完整 rows / API key。

### 2026-04-24：Phase 5.5 首轮 TDD 实施完成

- 新增 `tests/integrations/`，用 fake stream 覆盖 `src/integrations/llm_client.py` 的配置注入、异步 streaming 聚合、`reasoning_content` / `delta.content` 提取与 `reasoning_effort` 透传。
- 明确 SQLQuery 结构化 SQL / 摘要生成应走非 streaming 文本接口：`LLMClient.generate_text()` 已改为 `stream=False` 的 chat completion；另新增 `stream_text()` 作为非 thinking + streaming 模式，保留给后续主代理选择。
- 新增 `src/capabilities/sql_query/prompt_builders.py` 与 `llm_utils.py`，把 SQL 生成 prompt、结果摘要 prompt、JSON 提取、异步 / 同步文本生成器兼容与 JSON-safe rows preview 收口到 capability 内部。
- `sql_query.sql_generate` 已支持注入 `llm_text_generator`：`answer` 产出 SQL 草案，`clarify` 返回 interrupt，`reject` 返回非重试错误，provider / parse / validation 失败回退当前启发式生成。
- `sql_query.result_summarize` 已支持注入 `llm_text_generator`：LLM 摘要成功时输出 `summary_source=llm`，0 行结果直接走确定性摘要，provider / parse / validation 失败回退模板摘要。
- `sql_query.result_summarize` 已额外依赖 `sql_query.sql_generate` 输出，获取用户问题、route、schema profile 与 SQL 生成来源；`sql_execute_readonly` 不承担摘要层上下文透传职责。
- API runtime / 测试 runtime 增加 `llm_text_generator` 注入 seam；默认运行不自动访问真实 LLM provider，真实 provider 仍需显式接入。
- LLM 调用与 fallback 通过 `sql_query.llm_call` / `sql_query.llm_fallback` audit-only event 记录 metadata，默认不记录完整 prompt、完整 rows 或 API key。
- 已补充 fake LLM happy path e2e、LLM fallback observability、LLM SQL 草案仍进入 `sql_guard` 的 orchestration 测试。
