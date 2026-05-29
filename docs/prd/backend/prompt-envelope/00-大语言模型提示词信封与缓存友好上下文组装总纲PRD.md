# 大语言模型提示词信封与缓存友好上下文组装总纲 PRD

- **日期**：2026-05-29
- **状态**：总纲 PRD；已完成 Document Perfectization 审查，待分阶段实施
- **设计来源**：`docs/superpowers/specs/2026-05-28-llm-prompt-envelope-cache-aware-design.md`
- **专题索引**：`docs/prd/backend/prompt-envelope/README.md`
- **范围**：主代理回答、Soft Skill 软绑定判断、LLM Planner、Runtime Replanner、Skill input resolver、conversation memory resolver / summary、conversation memory candidate、工具结果 / artifact prompt 注入、LLM runtime 兼容层
- **干系人 / 受影响系统**：业务对话台用户、后端 runtime 维护者、Skill 作者、前端下载 / interrupt UI 维护者、运维排障人员；受影响系统包括 API/SSE、状态事件、主代理、Planner、Skill runtime、conversation memory 与 LLM provider 适配

## 1. 问题陈述

当前主代理、Planner、Soft Skill decision / answer、Runtime Replanner、Skill input resolver 与 conversation memory resolver / summary 仍存在多处手写大字符串 prompt。随着系统引入长历史、Skill 软绑定、文件 artifact、动态 DAG、缺参 interrupt、多模型上下文预算和流式输出，这种拼接方式已经带来长期维护风险：

1. **上下文预算静态化**：conversation memory 当前按 `trim_max_tokens - max(1024, trim_max_tokens / 4)` 计算历史预算；以 `trim_max_tokens=1024000` 为例，历史预算固定为 `768000`，无法根据本次真实 system/tool/user/dependency token 占用动态释放空间。
2. **缓存前缀不稳定**：高变化的历史、artifact、task metadata 较早进入 prompt，会破坏稳定系统规则和工具规则的 KV Cache 复用。
3. **Primacy / Recency 未显式建模**：系统硬约束需要吃 primacy，当前用户请求、active continuity notes 和 final guard 需要吃 recency；当前拼接方式没有把这些效应作为一等设计目标。
4. **工具信息与历史信息混杂**：工具调用规则、公开档案、输入 schema、工具结果、artifact 下载事实属于不同事实层级，不应混入普通 history。
5. **审计困难**：无法清楚解释某次 LLM 调用带入了哪些 segment、哪段被裁剪、cacheable prefix 是否变化、provider role fallback 是否发生。
6. **安全边界不一致**：主代理 Skill match 仍可能拼接 `manifest.body`，有泄漏脚本路径、handler、runtime、配置或内部目录结构的风险。

本 PRD 的目标是建立长期稳健的 Prompt Runtime 基线，而不是只修复一个静态预算常量。

## 2. 目标与非目标

### 2.1 目标

1. 将 LLM 输入从散落的字符串拼接升级为结构化 `PromptEnvelope` / `PromptSegment`。
2. 最终发送给模型的输入 token 预算默认固定为 `floor(trim_max_tokens * 0.75)`；其中 25% 预留给可见输出、thinking / reasoning、provider message overhead 与安全余量。历史上下文预算在该输入预算内按本次实际非历史 token 动态反算，不再把 `trim_max_tokens * 0.75` 当作历史预算。
3. 稳定 system / tool rules 形成可 hash 的 cacheable prefix，提升 KV Cache 命中稳定性。
4. 显式利用 primacy / recency：系统规则靠前，当前用户请求、active continuity notes、final guard 靠后。
5. 工具规则、工具公开档案、工具输入 schema、工具结果与普通 history 分层。
6. 为主代理回答、Soft Skill decision / answer、Planner / repair、Runtime Replanner、Skill input resolver、conversation memory resolver / summary 提供不同 profile。
7. 通过 `off|shadow|string|messages` 模式灰度迁移，先不破坏现有字符串 LLM runtime。
8. 记录 segment-level token、裁剪、prefix hash、role fallback 和预算来源审计，且不记录 raw prompt。

### 2.2 非目标

- 不引入 LangChain、LangGraph、AutoGen 等现成 Agent 框架。
- 不要求第一阶段立即改造所有 provider 为原生 messages 或 tool calling。
- 不把 Skill 内部代码结构暴露给 LLM；只允许披露 public profile。
- 不把结构化 Skill artifact 继承问题交给 prompt 解决；Skill 执行必须继续通过受控 metadata / artifact context 拿到真实结构化数据。
- 不新增数据库 schema；如后续确需独立 prompt audit 表，必须另立迁移 PRD / rollback plan。
- 不改变前端 SSE completion 语义。

## 3. 当前状态与代码依据

| 当前事实 | 代码位置 | PRD 约束 |
| --- | --- | --- |
| 主代理用 `parts` 拼接单字符串 | `src/capabilities/main_agent/prompt_builder.py:23-75` | 需要先迁移为 envelope-to-string，保持对外签名兼容。 |
| memory 在 artifact / dependency / current user 前插入 | `src/capabilities/main_agent/prompt_builder.py:49-74` | 需要调整为 bulk history 中段，active notes 靠后。 |
| Skill match 当前拼接 `match.manifest.body` | `src/capabilities/main_agent/prompt_builder.py:62-71` | 必须改成 public profile，避免暴露内部实现结构。 |
| public Skill profile 已存在并有脱敏测试 | `src/integrations/agent_skills/public_profile.py:90-114`、`tests/integrations/agent_skills/test_public_skill_profile.py` | 必须复用并扩展现有 sanitizer，不重复造第二套。 |
| Soft Skill decision / answer 已手写专用 prompt | `src/capabilities/main_agent/executor.py:512-560` | 需要迁移到 decision / answer profile，并保持流式答疑与历史追问语义。 |
| Planner prompt 是单字符串 | `src/orchestration/planner_contract.py:67-94` | 需要 planner profile，继续保证 JSON-only、public capability-only 与 repair prompt 行为。 |
| Runtime Replanner 也是单字符串 | `src/capabilities/main_agent/runtime_replanner.py:277-321` | 需要纳入 replan profile，避免关键 LLM 调用散落拼接。 |
| Skill input resolver prompt 包含 entrypoint | `src/integrations/agent_skills/input_resolution.py:339-381` | 需要 resolver profile，明确只披露用户可见 schema。 |
| conversation memory resolver / summary 有独立 prompt | `src/orchestration/conversation_memory.py:699-747`、`src/orchestration/conversation_memory.py:520-533` | 需要 memory profile 或受控旧路径 fallback audit。 |
| conversation memory 预算固定扣 1/4 | `src/orchestration/conversation_memory.py:63-67` | memory 从最终预算决策者降级为候选上下文提供者。 |
| LLMClient 只发送单条 user message | `src/integrations/llm_client.py:153-203` | 阶段一至阶段五不改 client；阶段六才扩展 messages。 |
| SharedLLMRuntime 入参是 `prompt: str` | `src/integrations/llm_runtime.py:103-165` | 需要分阶段扩展，避免破坏 thinking / stream path。 |
| interrupt resume 已合并同一任务已接受 answer payload 与上传 artifact metadata | `src/api/runtime.py:1424-1478`、`src/api/runtime.py:2018-2036`、`tests/api/test_pending_skill_context.py` | active continuity notes 必须只消费系统可信、已接受的补充事实，不能把任意用户历史当事实。 |

### 3.1 依赖与集成点矩阵

| 集成点 | 当前依赖 | PromptEnvelope 约束 |
| --- | --- | --- |
| 配置来源 | 启动期 `config.yaml` bootstrap 到环境变量 / runtime config；业务节点不得重复读 YAML。 | `PromptEnvelopeConfig` 只能消费进程环境、runtime config 或测试显式 config。 |
| Token 计数 | `src/integrations/token_counter.py` 与 per-model `trim_max_tokens`。 | `trim_max_tokens` 是 provider/model 总上下文口径；PromptEnvelope 的默认最终输入预算为 `final_input_token_budget = floor(trim_max_tokens * 0.75)`，剩余 25% 预留给模型输出、thinking / reasoning、provider overhead 与安全余量。精确/可信估算默认 margin 为 `max(1024, floor(trim_max_tokens * 0.01))`；fallback 估算默认 margin 为 `max(2048, floor(trim_max_tokens * 0.02))`，且 audit 标记 fallback。 |
| Skill 公开信息 | `build_public_skill_profile` 现有 sanitizer。 | 不新增第二套不一致 sanitizer；Skill match、Soft Skill、resolver profile 复用同一 public profile / schema 投影。 |
| Artifact 下载 | 平台 artifact descriptor 与 `/api/v1/artifacts/{artifact_id}/download`。 | LLM 只能看到脱敏 descriptor 和平台 download_url；不得生成 sandbox、本地路径或 outputs 伪链接。 |
| Audit/事件 | 现有 `EventVisibility.AUDIT_ONLY` / `main_agent.llm_call` 诊断出口。 | renderer 返回 audit，由 caller 写事件；renderer 不依赖 API repository，也不写前端可见 SSE。 |
| LLM runtime | `SharedLLMRuntime` / `LLMClient` 当前以字符串为主。 | P1-P5 保持字符串兼容；P6 才扩展 messages-native，并保留 deterministic fallback。 |

## 4. 功能需求

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-001 | 系统必须用 `PromptEnvelope` / `PromptSegment` 表达 LLM 输入。 | 核心模型有独立单元测试；业务调用点不再自由拼接不可审计的大字符串。 |
| FR-002 | 系统必须把最终输入 token 预算限制在 `trim_max_tokens` 的 75%，并在该输入预算内按本次实际非历史 prompt token 反算 bulk history budget。 | `final_input_token_budget = floor(trim_max_tokens * 0.75)`；`bulk_history_budget = final_input_token_budget - required_non_history_tokens - safety_margin`；精确/可信估算默认 margin 为 `max(1024, floor(trim_max_tokens * 0.01))`，fallback 估算默认 margin 为 `max(2048, floor(trim_max_tokens * 0.02))`；不得继续把 `trim_max_tokens * 0.75` 作为最终历史预算。 |
| FR-003 | 系统必须把工具规则、工具公开档案、输入 schema、工具结果拆成独立 segment。 | 工具结果不进入普通 history；public profile 不暴露内部结构。 |
| FR-004 | 系统必须保留 active continuity notes，并将其放在 current user request 之前的 recency 区。 | active notes 只来自系统可信状态和已接受 answer / artifact / tool result。 |
| FR-005 | 系统必须支持至少八类 profile：main agent answer、soft skill decision、soft skill answer、planner、planner repair、runtime replanner、skill input resolver、conversation memory resolver / summary。 | 每类 profile 有 template_id / template_version 和 render audit。 |
| FR-006 | 系统必须提供 `off|shadow|string|messages` 渐进运行模式。 | `off` 可回滚；`shadow` 只生成 audit；`string` 才发送 envelope string；`messages` 需显式启用。 |
| FR-007 | 主代理 Skill match 必须使用 public Skill profile。 | prompt 不包含脚本路径、handler、runtime、sidecar、内部目录、配置、DSN、token、secret。 |
| FR-008 | Prompt audit 必须能解释输入预算、最终输入 token、裁剪、preflight 重试和缓存前缀。 | audit 包含 `final_input_token_budget`、`final_input_tokens`、`preflight_retry_count`、`history_compression_retry`、segment token、trim reason、cacheable prefix hash、role fallback；不含 raw content。 |
| FR-009 | messages-native runtime 必须保留 string fallback。 | Provider 不支持 role 时 deterministic fallback，并写入 audit。 |
| FR-010 | 新增 audit event 不得影响前端 SSE / completion 语义。 | `main_agent.output_delta`、`main_agent.output_final` 行为保持兼容。 |

## 5. 非功能需求

- **安全**：prompt renderer 与 audit schema 必须默认拒绝 raw prompt、raw artifact、内部路径、DSN、secret、token；Skill public profile 继续复用现有 allowlist sanitizer。
- **可靠性**：最终渲染输入必须通过 final token preflight，`final_input_tokens` 不得超过 `floor(trim_max_tokens * 0.75)`；首次 preflight 失败时只允许执行一次 bulk history 压缩重试，重渲染后再次 preflight；第二次仍失败必须 fail closed。必保 segment 超过该输入预算时必须 fail closed，不得截断系统规则、工具规则或当前用户请求。
- **兼容性**：阶段一至阶段五不得要求 LLM provider 支持 messages-native；所有阶段必须保留 `off` 回滚。
- **可观测性**：所有 profile 渲染均应产生 audit；audit-only event 不进入前端可见事件流。
- **可测试性**：每个阶段均有可独立运行的 targeted tests；测试不依赖真实 LLM provider。
- **可维护性**：PromptEnvelope 核心模块不得依赖 FastAPI、具体 provider、Skill executor 或 storage repository。

## 6. 产品与技术方案

### 6.1 新增核心模块

建议新增：

```text
src/orchestration/prompt_envelope.py
```

职责：

- 定义 `PromptSegment`、`PromptEnvelope`、`PromptSegmentAudit`、`PromptRenderAudit`、`RenderedPrompt`、`RenderedMessages`。
- 提供 segment 排序、token 估算、输入预算分配、裁剪、final token preflight、prefix hash、string/messages 渲染。
- 不依赖 FastAPI、具体 LLM provider 或 Skill executor，保持 orchestration 层可测。

### 6.2 主代理适配层

建议新增或重构：

```text
src/capabilities/main_agent/prompt_envelope_builder.py
```

职责：

- 将主代理当前输入转成 `PromptEnvelope`。
- 构造 stable system contract、stable tool rules、selected public tool profiles、tool schema、bulk history、tool results、active continuity notes、current user request、final guard。
- 保持 `build_main_agent_prompt(...) -> str` 作为兼容函数；内部根据 feature flag 决定旧 builder / shadow / envelope string。

### 6.3 Segment 顺序

主代理回答默认顺序：

```text
A. stable_system_contract
B. stable_tool_rules
C. selected_public_tool_profiles
D. tool_input_schema
E. bulk_conversation_history
F. required_tool_results_and_artifacts
G. active_continuity_notes
H. current_user_request
I. final_recency_guard
```

### 6.4 Conversation memory 候选上下文

`src/orchestration/conversation_memory.py` 需要增加 candidate 输出能力：

- 继续保留 `ConversationMemoryContext.to_prompt_payload()`，避免破坏现有测试和旧路径。
- 新增候选结构，例如 `ConversationMemoryCandidatePayload` 或 `ConversationMemorySegmentCandidate`。
- 每个候选标记 kind、priority、trim_policy、token_estimate。
- 最终输入预算由 PromptAssembler 按 `floor(trim_max_tokens * 0.75)` 建模；bulk history 在该输入预算内按完整 prompt 反算。

### 6.5 Runtime 与 LLM Client 迁移

阶段一至阶段五：

- `SharedLLMRuntime` 和 `LLMClient` 仍只接收 `str`。
- PromptEnvelope 在调用前渲染为字符串。

阶段六：

- 定义 `LLMMessage`。
- `SharedLLMRuntime.generate_text/stream_events` 支持 `str | PromptEnvelope | Sequence[LLMMessage]`。
- `LLMClient` 对 OpenAI-compatible provider 发送 messages。
- 不支持 `developer` / `tool` role 的 provider 使用 deterministic role fallback，并写入 audit。

### 6.6 配置、profile registry 与审计出口

- 配置统一由 `PromptEnvelopeConfig` 或等价对象解析 `MAF_PROMPT_ENVELOPE_MODE`、`trim_max_tokens`、输入预算比例、safety margin 与 provider role 能力；业务执行路径不得重新读取 `config.yaml`。
- 输入预算比例默认规则必须可配置但不可隐式漂移：默认 `input_budget_ratio=0.75`，即 `final_input_token_budget=floor(trim_max_tokens * 0.75)`；25% 保留给输出、thinking / reasoning、provider overhead 与安全余量。safety margin 默认规则同样不可隐式漂移：精确/可信 token 估算使用 `max(1024, floor(trim_max_tokens * 0.01))`，fallback 估算使用 `max(2048, floor(trim_max_tokens * 0.02))`；任何调整必须同步测试和 PRD。
- Profile 统一由 `PromptProfileRegistry` 或等价工厂按调用场景选择。
- 审计由 caller 显式接收 `RenderedPrompt.audit` 并写入现有 `EventVisibility.AUDIT_ONLY` 事件；renderer 本身不直接依赖 FastAPI / repository / SSE。
- Audit event payload 只能包含 `template_id`、`template_version`、mode、`final_input_token_budget`、`final_input_tokens`、`preflight_retry_count`、`history_compression_retry`、segment audit、token、hash、trim/fallback reason、provider role fallback；不得包含 raw prompt、raw artifact、secret、DSN、token 或内部路径。

## 7. 运行模式

```text
MAF_PROMPT_ENVELOPE_MODE=off|shadow|string|messages
```

配置来源必须是进程环境变量、启动期注入的 runtime config 或测试显式 config；不得在节点执行期间重新读取 `config.yaml`。

| 模式 | 行为 | 用途 |
| --- | --- | --- |
| `off` | 完全走旧 prompt builder | 回滚 / 默认安全态 |
| `shadow` | 实际发送旧 prompt，同时生成 envelope audit | 观测差异，零行为变更 |
| `string` | 发送 envelope-to-string prompt | 第一阶段生产候选；涉及 Skill match / `/skill` 软绑定时必须等 P4 public profile 安全门禁通过后才能用于生产流量 |
| `messages` | 发送 messages-native prompt | 第二阶段 provider 适配后启用 |

默认值：阶段一合并时应为 `off` 或 `shadow`，不得直接默认 `string/messages`。

## 8. 分阶段交付 PRD

本总纲 PRD 拆分为八个可独立实施、验收、回滚的阶段 PRD：

| 阶段 | PRD | 目标 |
| --- | --- | --- |
| P0 | `docs/prd/backend/prompt-envelope/01-阶段零-测试基线与旧行为锁定PRD.md` | 锁定旧 prompt 顺序、静态预算、Skill 内部结构风险和旧 runtime 行为。 |
| P1 | `docs/prd/backend/prompt-envelope/02-阶段一-提示词信封核心模型与渲染器PRD.md` | 实现 PromptEnvelope 核心模型、排序、预算、裁剪和 no-raw audit。 |
| P2 | `docs/prd/backend/prompt-envelope/03-阶段二-主代理信封字符串迁移PRD.md` | 主代理接入 envelope-to-string 与 `off|shadow|string`。 |
| P3 | `docs/prd/backend/prompt-envelope/04-阶段三-对话记忆候选上下文化PRD.md` | Conversation memory candidate 化，动态反算历史预算。 |
| P4 | `docs/prd/backend/prompt-envelope/05-阶段四-工具信息分层与能力公开档案安全PRD.md` | 工具信息分层，复用 public profile，禁止内部结构泄漏。 |
| P5 | `docs/prd/backend/prompt-envelope/06-阶段五-多调用场景档案迁移PRD.md` | 迁移 Soft Skill、Planner、Replanner、Resolver、Memory profiles。 |
| P6 | `docs/prd/backend/prompt-envelope/07-阶段六-消息原生运行时扩展PRD.md` | 扩展 messages-native runtime 和 role fallback audit。 |
| P7 | `docs/prd/backend/prompt-envelope/08-阶段七-供应商缓存与观测增强PRD.md` | Provider cache hint、prefix 污染检测与观测增强。 |

## 9. 数据、迁移、安全与可观测性

- **数据库 schema**：默认不需要新增表或迁移。Prompt audit 通过现有 event payload 扩展承载；如实施中发现必须持久化独立 prompt audit 表，必须先补充迁移 PRD / rollback plan。
- **事件兼容**：新增 audit-only event 不得影响前端 SSE completion 语义；现有 `main_agent.output_delta`、`main_agent.output_final`、`main_agent.llm_call` 行为必须保持兼容。
- **安全边界**：prompt renderer 与 audit schema 必须默认拒绝 raw prompt、raw artifact、内部路径、DSN、secret、token；public Skill profile 继续复用现有 allowlist sanitizer。
- **权限与隐私**：PromptEnvelope 不新增权限模型，不绕过现有 artifact download authorization，不把 username / conversation_id / task_id 放入 stable prefix，不把 audit payload 作为用户可见数据返回。
- **回滚策略**：`MAF_PROMPT_ENVELOPE_MODE=off` 是运行时回滚开关；`shadow` 只允许增加 audit，不得改变发送给 LLM 的 prompt。
- **可观测性**：所有 mode 下若生成 audit，必须能回答“本次 final input budget 是多少、final input tokens 是多少、non-history token 占用多少、history 可用预算多少、哪些 segment 被裁剪、是否触发唯一一次 history compression retry、final preflight 是否通过、prefix hash 是否变化、role fallback 是否发生”。

## 10. 验收矩阵

| AC | 对应阶段 | 验证 |
| --- | --- | --- |
| AC-001 75% 最终输入预算与动态历史预算 | P1/P3 | `tests/orchestration/test_prompt_envelope.py` 覆盖 `final_input_token_budget=floor(trim_max_tokens*0.75)`、dynamic budget、final preflight，以及首次 preflight 失败后仅一次 history compression retry case |
| AC-002 segment audit | P1/P2 | audit 不含 raw content，只含 hash/token/trim |
| AC-003 current user + final guard 末尾 | P2 | main agent golden order test |
| AC-004 stable prefix hash | P1/P7 | prefix hash determinism test |
| AC-005 工具结果独立 segment | P2/P4 | segment classification test |
| AC-006 Skill public profile 不暴露内部结构 | P4/P5 | `/skill` prompt safety test + existing public profile sanitizer regression |
| AC-007 长历史接近完整预算 | P3 | long-history budget test |
| AC-008 role fallback audit | P6 | fake provider role fallback test |
| AC-009 token counter fallback | P1/P3 | fallback estimator margin test |
| AC-010 active notes 来源可信 | P3/P4 | interrupt resume active notes test |
| AC-011 shadow 模式不改变输出 | P2/P5 | API shadow regression |
| AC-012 必保超出最终输入预算 fail closed | P1 | over-budget failure test 证明 required segments 超过 `floor(trim_max_tokens*0.75)` 时不发送 LLM；history compression retry 后第二次 preflight 仍失败也不发送 LLM |
| AC-013 audit 事件兼容 | P2/P5/P7 | 新增/扩展 audit-only event 不影响前端 SSE 与 completion |
| AC-014 Runtime Replanner / memory resolver 不成盲区 | P5 | runtime replanner、conversation memory resolver / summary prompt 均有 profile 或显式旧路径 fallback audit |

## 11. 推荐验证命令

阶段性最小验证：

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_main_agent_workflow_and_executor
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_public_skill_profile
conda run -n multi_agent python -m unittest tests.api.test_soft_skill_binding tests.api.test_pending_skill_context tests.api.test_skill_input_resolution_runtime tests.api.test_runtime_replanner
```

阶段收口验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

若改动 `src/integrations/llm_client.py` / `src/integrations/llm_runtime.py`，追加：

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
```

## 12. Rollout 与 Stop Gates

1. P1 完成后必须能独立通过 prompt envelope 单元测试。
2. P2 完成后必须能在 `shadow` 模式不改变现有 API 行为；`string` 模式若涉及 Skill match / `/skill` 软绑定，只能用于开发或小流量 shadow 对比，不能进入生产放量。
3. P3 完成后必须证明 75% 是最终输入预算而不是历史预算；history 只能使用 `floor(trim_max_tokens*0.75) - required_non_history_tokens - safety_margin` 的剩余空间，且 current-task clarification、已接受 interrupt answer 与上传 artifact metadata 不会被旧 history 裁掉。
4. P4 完成后必须证明 `/skill` 和主代理 Skill match prompt 不暴露内部结构；只有此门禁通过后，含 Skill profile 的 `string` 模式才可进入生产灰度。
5. P5 完成后必须证明 planner repair、runtime replanner、memory resolver / summary 没有成为未审计 prompt 盲区。
6. P6 完成前不得默认启用 `messages` 模式。
7. P7 的 provider cache hint 必须先 shadow 观测，再按 provider 小流量启用；cache 命中不得成为 correctness 依赖。

## 13. 风险、缓解与假设

| 风险 | 缓解 |
| --- | --- |
| prompt 顺序变化影响模型输出 | `off/shadow/string/messages` 分阶段，先 shadow 观测，再 string 灰度。 |
| token 估算误差或输出空间不足导致 provider 拒绝 | 默认只允许最终输入使用 `trim_max_tokens` 的 75%；provider tokenization 优先；fallback 使用更大 safety margin；最终发送前执行 final token preflight。 |
| audit 泄漏 prompt 或 secret | audit schema 禁 raw content，只保留 hash、token、segment name、trim reason。 |
| Skill 内部结构继续暴露 | P4 明确替换 `manifest.body`，复用 `build_public_skill_profile`，测试扫描脚本路径/handler/runtime。 |
| messages role provider 不兼容 | P6 role fallback mapping + audit，默认保留 string fallback。 |
| 长工具结果挤占历史或撑爆输入预算 | 工具结果关键事实必保，明细可压缩；bulk history 最后竞争 flexible budget；若必保工具事实仍超过 75% 最终输入预算则 fail closed。 |
| active notes 误把用户文本当事实 | active notes 只来自系统可信状态和已接受 answer / artifact / tool result。 |
| 审计出口不清导致 shadow 无法验证 | P2 明确 rendered prompt 返回 audit，由 caller 写入 audit-only event；renderer 不直接写库。 |
| 现有 prompt 路径遗漏 | P5 将 planner repair、runtime replanner、memory resolver / summary 列入 profile 或 fallback audit 验收。 |

明确假设：本 PRD 范围内不需要数据库 schema 迁移；如后续实现必须新增持久化结构，应暂停实施并补充迁移 PRD。

## 14. License Requirement

本总纲 PRD 仅定义架构与分阶段验收，不要求新增依赖。各阶段实施结束前仍必须按仓库规范报告 License Requirement；若涉及 `native/` / Rust 依赖、`Cargo.lock`、`native/deny.toml` 或供应链策略变更，必须运行 `cd native && cargo deny check`。
