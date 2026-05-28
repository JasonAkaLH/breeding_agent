# LLM 提示词信封与缓存友好上下文组装实施计划

- **状态**：待实施计划，基于已审查设计文档。
- **设计输入**：`docs/superpowers/specs/2026-05-28-llm-prompt-envelope-cache-aware-design.md`
- **目标范围**：主代理回答、Soft Skill 软绑定判断、LLM Planner、Skill input resolver、conversation memory、工具结果 / artifact prompt 注入、LLM runtime 兼容层。
- **非目标**：本计划不直接修复 Skill resume 的结构化 artifact 继承；该问题属于运行时 metadata / artifact handoff。Prompt Envelope 只负责 LLM 输入组装与审计。

## 1. 目标与成功标准

### 1.1 目标

将当前散落的 prompt 字符串拼接升级为可测试、可审计、可渐进迁移的 `PromptEnvelope` 子系统：

1. 使用 segment 表达 LLM 输入，避免各调用点自由拼接不可审计的大字符串。
2. 历史上下文预算按本次实际非历史 token 动态反算，不再固定 `trim_max_tokens * 0.75`。
3. 稳定 system / tool rules 形成 cacheable prefix，提升 KV Cache 命中稳定性。
4. 显式利用 primacy / recency：系统规则靠前，当前用户请求、active continuity notes、final guard 靠后。
5. 工具规则、工具 public profile、工具输入 schema、工具结果与普通 history 分层。
6. 通过 `off|shadow|string|messages` 模式灰度迁移，先不破坏现有字符串 LLM runtime。

### 1.2 成功标准

| ID | 成功标准 |
| --- | --- |
| S-001 | `PromptEnvelope` / `PromptSegment` / `PromptRenderAudit` 有独立单元测试。 |
| S-002 | 主代理 prompt 可在 `shadow` 模式生成 envelope audit，同时实际 LLM 输入仍保持旧字符串。 |
| S-003 | `string` 模式下主代理使用 envelope-to-string prompt，并通过 API / main_agent 回归。 |
| S-004 | history budget 由 `trim_max_tokens - required_non_history_tokens - safety_margin` 计算。 |
| S-005 | audit 中有 cacheable prefix hash、segment token、裁剪状态、role fallback；无 raw prompt。 |
| S-006 | `/skill` soft decision、planner、skill input resolver 逐步迁移到专属 profile，并保留旧路径 fallback。 |
| S-007 | messages-native 支持在 feature flag 下启用；provider 不支持 role 时有 deterministic fallback。 |

## 2. 当前代码依据

| 当前事实 | 代码位置 | 影响 |
| --- | --- | --- |
| 主代理用 `parts` 拼接单字符串 | `src/capabilities/main_agent/prompt_builder.py:23-75` | 需要先把主代理迁移为 envelope-to-string，保持对外签名兼容。 |
| memory 在 artifact / dependency / current user 前插入 | `src/capabilities/main_agent/prompt_builder.py:49-74` | 需要调整为 bulk history 中段，active notes 靠后。 |
| Skill match 当前拼接 `match.manifest.body` | `src/capabilities/main_agent/prompt_builder.py:62-71` | 需要改成 public profile，避免暴露内部实现结构。 |
| conversation memory 预算固定扣 1/4 | `src/orchestration/conversation_memory.py:63-67` | 需要把 memory 从“最终预算决策者”降级为候选上下文提供者。 |
| memory 压缩基于自身 token_budget | `src/orchestration/conversation_memory.py:439-516` | 需要新增 candidate payload，最终裁剪由 PromptAssembler 完成。 |
| LLMClient 只发送单条 user message | `src/integrations/llm_client.py:153-167`、`src/integrations/llm_client.py:189-203` | 阶段 1 不改 client；阶段 2 才扩展 messages。 |
| SharedLLMRuntime 入参是 `prompt: str` | `src/integrations/llm_runtime.py:103-165` | 需要分阶段扩展，避免破坏 thinking / stream path。 |

## 3. 架构拆分

### 3.1 新增模块

建议新增：

```text
src/orchestration/prompt_envelope.py
```

职责：

- 定义 `PromptSegment`、`PromptEnvelope`、`PromptSegmentAudit`、`PromptRenderAudit`、`RenderedPrompt`、`RenderedMessages`。
- 提供 segment 排序、token 估算、预算分配、裁剪、prefix hash、string/messages 渲染。
- 不依赖 FastAPI、具体 LLM provider 或 Skill executor，保持 orchestration 层可测。

### 3.2 主代理适配层

建议新增或重构：

```text
src/capabilities/main_agent/prompt_envelope_builder.py
```

职责：

- 将主代理当前输入转成 `PromptEnvelope`。
- 构造 stable system contract、stable tool rules、selected public tool profiles、tool schema、bulk history、tool results、active continuity notes、current user request、final guard。
- 阶段 1 保持 `build_main_agent_prompt(...) -> str` 作为兼容函数；内部根据 feature flag 决定旧 builder / shadow / envelope string。

### 3.3 Conversation memory 候选上下文

在 `src/orchestration/conversation_memory.py` 增加 candidate 输出能力：

- 继续保留 `ConversationMemoryContext.to_prompt_payload()`，避免破坏现有测试和旧路径。
- 新增候选结构，例如 `ConversationMemoryCandidatePayload` 或 `ConversationMemorySegmentCandidate`。
- 每个候选标记 kind、priority、trim_policy、token_estimate。
- 不再在 memory 层把 `actual_memory_budget` 当最终历史预算；最终预算由 PromptAssembler 按完整 prompt 反算。

### 3.4 Runtime 与 LLM Client 迁移

阶段 1：

- `SharedLLMRuntime` 和 `LLMClient` 仍只接收 `str`。
- PromptEnvelope 在调用前渲染为字符串。

阶段 2：

- 定义 `LLMMessage`。
- `SharedLLMRuntime.generate_text/stream_events` 支持 `str | PromptEnvelope | Sequence[LLMMessage]`。
- `LLMClient` 对 OpenAI-compatible provider 发送 messages。
- 不支持 `developer` / `tool` role 的 provider 使用 deterministic role fallback，并写入 audit。

## 4. 运行模式

新增配置：

```text
MAF_PROMPT_ENVELOPE_MODE=off|shadow|string|messages
```

| 模式 | 行为 | 用途 |
| --- | --- | --- |
| `off` | 完全走旧 prompt builder | 回滚 / 默认安全态 |
| `shadow` | 实际发送旧 prompt，同时生成 envelope audit | 观测差异，零行为变更 |
| `string` | 发送 envelope-to-string prompt | 第一阶段生产候选 |
| `messages` | 发送 messages-native prompt | 第二阶段 provider 适配后启用 |

默认值：阶段 1 合并时应为 `off` 或 `shadow`，不得直接默认 `string/messages`。

## 5. 实施步骤

### P0：测试基线与旧行为锁定

目标：先证明现状和风险点，避免迁移时无意改变行为。

改动：

1. 为 `build_main_agent_prompt` 增加顺序 / 内容快照测试：当前 memory、artifact、dependency、skill、script、user 的相对顺序可复现。
2. 为 `ConversationMemoryConfig.actual_memory_budget` 增加当前静态 75% 行为测试，作为后续要替换的 failing / updated baseline。
3. 为 Skill prompt 安全增加测试：现状可能包含 `manifest.body`，计划中明确迁移后不得包含脚本路径 / handler / runtime 内部结构。

建议测试文件：

```text
tests/capabilities/main_agent/test_prompt_envelope_baseline.py
tests/orchestration/test_prompt_envelope.py
```

验收：测试能描述现状，并为 P1 迁移提供断言基础。

### P1：PromptEnvelope 核心模型与 Renderer

目标：先实现与业务无关的可测核心。

改动：

1. 新增 `src/orchestration/prompt_envelope.py`。
2. 定义数据模型：`PromptSegment`、`PromptEnvelope`、`PromptSegmentAudit`、`PromptRenderAudit`、`RenderedPrompt`。
3. 实现 segment 排序：稳定 prefix -> semi-static profile/schema -> bulk history -> required tool results -> active notes -> current user -> final guard。
4. 实现两遍预算：先估 non-history required tokens，再计算 bulk history budget。
5. 实现裁剪策略：只裁 `compressible/drop_oldest/drop_if_needed`，必保 segment 超限 fail closed。
6. 实现 prefix hash：只覆盖 `cache_affinity=prefix` 且 `mutability=stable` 的 segment。
7. 实现 raw prompt 禁止审计：audit 只保存 hash、token、segment name、trim reason。

测试：

- segment order test。
- dynamic budget test。
- prefix hash determinism test。
- raw content audit prohibition test。
- over-budget fail-closed test。
- token counter fallback test。

### P2：主代理 Envelope-to-string 迁移

目标：主代理先接入 envelope，但保持函数签名和 LLM runtime 不变。

改动：

1. 新增 `src/capabilities/main_agent/prompt_envelope_builder.py`。
2. 将当前 `prompt_builder.py` 的稳定规则拆为：
   - `stable_system_contract`
   - `stable_tool_rules`
   - `final_recency_guard`
3. 将当前 memory 渲染改为 `bulk_conversation_history` segment。
4. 将 dependency / script results / artifact context 改为 `required_tool_results_and_artifacts` segment。
5. 将当前用户问题放入 `current_user_request` segment，并保证接近尾部。
6. `build_main_agent_prompt` 根据 `MAF_PROMPT_ENVELOPE_MODE`：
   - `off`：旧逻辑。
   - `shadow`：返回旧 prompt，旁路生成 envelope audit。
   - `string`：返回 envelope-to-string prompt。
7. 通过 audit-only event 记录 render audit；不得写 raw prompt。

测试：

- `off` 模式旧行为不变。
- `shadow` 模式业务 prompt 不变但有 audit。
- `string` 模式 current user + final guard 在末尾。
- memory 不再早于全部工具结果进入 recency 区。
- 下载硬约束仍存在且位于 stable tool rules。

### P3：Conversation Memory Candidate 化

目标：让 memory 提供候选，而不是最终裁剪决策。

改动：

1. 增加 memory candidate 数据结构。
2. `_compress` 保留旧逻辑给 `off` 模式使用，同时新增 candidate path。
3. candidate path 返回：history summary、recent messages、clarification messages、capability summaries，并附 priority / trim policy。
4. PromptAssembler 根据最终 dynamic budget 装入候选历史。
5. 更新 audit：区分 `candidate_history_tokens`、`bulk_history_budget`、`bulk_history_tokens_used`。

测试：

- 长历史场景 bulk history budget 接近 `trim_max_tokens - non_history - margin`。
- token counter fallback 时 margin 提高。
- current-task clarifications 优先于旧 history。
- active continuity notes 不依赖用户原文直接提升为事实。

### P4：工具信息分层与 Skill public profile 安全

目标：消除工具信息混入普通 history、Skill 内部结构暴露风险。

改动：

1. 建立 public profile 渲染函数，只使用 capability descriptor / public_usage / manifest public metadata。
2. 禁止 `match.manifest.body` 直接进入主代理 prompt。
3. tool schema segment 只包含参数名、类型、是否必填、aliases、accepted formats、missing input 标准。
4. tool result segment 保留 `download_url`、missing、error、diagnostics 等关键事实；大型明细可压缩。
5. Skill input resolver profile 不接收完整 conversation memory，只接收 schema、current request、active notes、artifact summaries、answer payload 和少量 clarification。

测试：

- `/skill` soft decision prompt 不包含脚本路径、handler、runtime、内部目录结构。
- finalizer 只有 tool result segment 存在平台 `download_url` 时才可声称文件可下载。
- artifact raw content 不进入 LLM prompt。
- tool result 不进入 bulk history segment。

### P5：Soft Skill Decision / Planner / Skill Input Resolver Profile 迁移

目标：将非主代理回答路径纳入同一 PromptEnvelope 体系。

改动：

1. Soft Skill decision 使用 decision profile：stable decision rules + selected public profile + minimal schema + small recent history + current user + JSON guard。
2. Planner 使用 planner profile：stable planner rules + capability summary + current user + short memory summary + JSON plan guard。
3. Skill input resolver 使用 resolver profile：tool schema + current user + active notes + artifact summaries + answer payload + small clarification messages。
4. 各 profile 均生成 render audit。
5. 保留旧 prompt fallback，受 `MAF_PROMPT_ENVELOPE_MODE` 控制。

测试：

- soft skill answer / execute 判断不回退为硬执行。
- Planner JSON plan 仍可解析和 validate。
- 缺参解析不被旧历史误触发。
- API 回归覆盖 `/skill` 追问、流式答疑、interrupt 缺参。

### P6：Messages-native Runtime 扩展

目标：在不破坏 string fallback 的基础上支持 messages-native。

改动：

1. 定义 `LLMMessage`。
2. `SharedLLMRuntime.generate_text/stream_events` 支持 `str | PromptEnvelope | Sequence[LLMMessage]`。
3. `LLMClient.generate_text/generate_text_with_thinking/stream_text` 支持 messages。
4. 实现 role fallback mapping：
   - `developer` 不支持时折叠到 system。
   - `tool` 不支持时渲染为 context block。
   - 所有 fallback 进入 audit。
5. 保留 string path，`MAF_PROMPT_ENVELOPE_MODE=messages` 才启用 messages。

测试：

- fake provider 支持 messages 时收到分 role message。
- fake provider 不支持 developer/tool 时产生 deterministic fallback。
- thinking / reasoning_delta streaming 不回归。
- string 与 messages golden prompt 语义等价。

### P7：Provider Cache 与观测增强

目标：为后续 provider-specific prompt cache 做准备，不把实现绑死到某个 vendor。

改动：

1. audit 记录 `cacheable_prefix_hash`、`cacheable_prefix_tokens`、`first_dynamic_segment`。
2. 增加 prefix 动态污染检测：prefix segment 中不得有 task_id、conversation_id、username、current user、artifact、dependency result。
3. 如 provider 支持 prompt cache hint，再通过配置启用；不支持时只保留 hash 观测。
4. 增加 metrics / audit-only event，不记录 raw prompt。

测试：

- 同模板同稳定 segment hash 一致。
- 当前用户变更不改变 stable prefix hash。
- tool result / history 变更不进入 stable prefix hash。
- prompt audit 不含 raw prompt、secret、DSN、token。

## 6. 验收矩阵

| AC | 对应阶段 | 验证 |
| --- | --- | --- |
| AC-001 动态历史预算 | P1/P3 | `tests/orchestration/test_prompt_envelope.py` dynamic budget case |
| AC-002 segment audit | P1/P2 | audit 不含 raw content，只含 hash/token/trim |
| AC-003 current user + final guard 末尾 | P2 | main agent golden order test |
| AC-004 stable prefix hash | P1/P7 | prefix hash determinism test |
| AC-005 工具结果独立 segment | P2/P4 | segment classification test |
| AC-006 Skill public profile 不暴露内部结构 | P4/P5 | `/skill` prompt safety test |
| AC-007 长历史接近完整预算 | P3 | long-history budget test |
| AC-008 role fallback audit | P6 | fake provider role fallback test |
| AC-009 token counter fallback | P1/P3 | fallback estimator margin test |
| AC-010 active notes 来源可信 | P3/P4 | interrupt resume active notes test |
| AC-011 shadow 模式不改变输出 | P2/P5 | API shadow regression |
| AC-012 必保超限 fail closed | P1 | over-budget failure test |

## 7. 推荐验证命令

阶段性最小验证：

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_main_agent_workflow_and_executor
conda run -n multi_agent python -m unittest tests.api.test_soft_skill_binding tests.api.test_pending_skill_context tests.api.test_skill_input_resolution_runtime
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

本计划不涉及 Rust / `native/` / 依赖变更；无需触发 cargo-deny，最终报告仍需记录 License Requirement。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| prompt 顺序变化影响模型输出 | `off/shadow/string/messages` 分阶段，先 shadow 观测，再 string 灰度。 |
| token 估算误差导致 provider 拒绝 | provider tokenization 优先；fallback 使用更大 safety margin。 |
| audit 泄漏 prompt 或 secret | audit schema 禁 raw content，只保留 hash、token、segment name、trim reason。 |
| Skill 内部结构继续暴露 | P4 明确替换 `manifest.body`，测试扫描脚本路径/handler/runtime。 |
| messages role provider 不兼容 | P6 role fallback mapping + audit，默认保留 string fallback。 |
| 长工具结果挤占历史 | 工具结果关键事实必保，明细可压缩；bulk history 最后竞争 flexible budget。 |
| active notes 误把用户文本当事实 | active notes 只来自系统可信状态和已接受 answer / artifact / tool result。 |

## 9. 执行建议

### 9.1 推荐执行模式

推荐用 `$ralph` 或 `$ultragoal` 分阶段推进；P1-P5 涉及多模块联动，适合在需要提速时拉 `$team`。

### 9.2 可并行 Team lanes

| Lane | 角色 | 范围 |
| --- | --- | --- |
| Envelope Core | executor + test-engineer | `src/orchestration/prompt_envelope.py` 与单元测试。 |
| Main Agent Migration | executor | `prompt_builder.py` / `prompt_envelope_builder.py` / main_agent tests。 |
| Memory Candidate | executor + debugger | `conversation_memory.py` candidate path 与长历史测试。 |
| Tool/Profile Safety | executor + verifier | Skill public profile、安全扫描、artifact/tool result segment。 |
| Runtime Messages | executor + integration verifier | `llm_runtime.py` / `llm_client.py` messages 与 fallback。 |

### 9.3 建议 stop gates

- P1 完成后必须能独立通过 prompt envelope 单元测试。
- P2 完成后必须能在 `shadow` 模式不改变现有 API 行为。
- P3 完成后必须证明历史预算不再固定 75%。
- P4 完成后必须证明 `/skill` prompt 不暴露内部结构。
- P6 完成前不得默认启用 `messages` 模式。

## 10. 后续交付物

实施开始前建议按本计划生成更细的任务拆分：

```text
.omx/plans/prd-YYYYMMDD-llm-prompt-envelope.md
.omx/plans/test-spec-YYYYMMDD-llm-prompt-envelope.md
```

如果用户直接要求执行，可用本文件作为 `$ralph` / `$ultragoal` / `$team` 的输入上下文。
