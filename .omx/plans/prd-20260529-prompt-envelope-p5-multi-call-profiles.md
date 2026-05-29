# Prompt Envelope P5 多调用场景档案迁移实施计划

- **日期**：2026-05-29
- **模式**：`$plan` direct planning for `$ralph` execution
- **目标 PRD**：`docs/prd/backend/prompt-envelope/06-阶段五-多调用场景档案迁移PRD.md`
- **Ralph context snapshot**：`.omx/context/prompt-envelope-p5-multi-call-profiles-20260529T034955Z.md`
- **配套测试规格**：`.omx/plans/test-spec-20260529-prompt-envelope-p5-multi-call-profiles.md`

## 1. Requirements Summary

本轮必须把主代理回答之外的 LLM prompt 入口纳入 PromptEnvelope profile 管理：

1. Soft Skill decision / answer 分别使用 `soft_skill_decision` 与 `soft_skill_answer` profile；answer 继续走 `main_agent.output_delta` 流式答疑。
2. Planner / repair 使用 `planner` / `planner_repair` profile 或显式 fallback audit；继续保持 JSON-only、public capability-only、repair 一次重试和后端 validator fail-closed。
3. Runtime Replanner 使用 `runtime_replan` profile；输出仍只允许 public DAG，不允许内部 capability / handler / Skill 阶段。
4. Skill input resolver 使用 `skill_input_resolver` profile；只接收 schema、current request / active notes、artifact summaries、answer payload 与少量 clarification，不接收完整 memory，不暴露 entrypoint / script path。
5. Conversation memory resolver / summary 使用 `conversation_memory_resolution` / `conversation_memory_summary` profile 或显式 legacy fallback audit；resolver 只做高置信实体补全，summary 只做忠实摘要。
6. 所有 `string` profile 路径都必须复用 PromptEnvelope 75% final input budget 与 final token preflight：首次失败最多一次历史压缩 retry，二次仍超预算 fail closed；audit 记录 `final_input_token_budget` / `final_input_tokens`。
7. `off` 模式保持旧 prompt 兼容；`shadow` 模式只审计不改变发送 prompt；`string` 模式发送 rendered prompt。

## 2. Brownfield Evidence

- PromptEnvelope core 已提供 75% 输入预算、一次 history compression retry 与 fail-closed preflight：`src/orchestration/prompt_envelope.py:112-166`；audit 字段包含 `final_input_token_budget`、`final_input_tokens`、`preflight_retry_count`、`history_compression_retry`：`src/orchestration/prompt_envelope.py:77-97`。
- 主代理 main prompt 已有 `off|shadow|string` mode、rendered seam 与 audit payload：`src/capabilities/main_agent/prompt_envelope_builder.py:256-346`；但该 helper 当前是 main-agent 专用，不覆盖其他调用场景。
- Soft Skill decision / answer 当前在 executor 内手写字符串 prompt：`src/capabilities/main_agent/executor.py:420-538`、`src/capabilities/main_agent/executor.py:561-609`；answer 流式通过 `_generate_streaming_answer_text` 发布 `main_agent.output_delta`：`src/capabilities/main_agent/executor.py:644-685`。
- Planner / repair prompt 当前在 `planner_contract` 中手写：`src/orchestration/planner_contract.py:47-94`；LLM provider 在 repair 时复用原 prompt 并限制 previous output 2000 chars：`src/orchestration/llm_workflow_provider.py:82-127`。当前 public capability line 会暴露 `source_path`：`src/orchestration/planner_contract.py:197-210`，P5 应收口为 public-only profile。
- Runtime Replanner 仅在触发 replan 时调用 LLM：`src/capabilities/main_agent/runtime_replanner.py:90-134`；prompt 当前手写，包含 public capabilities、current nodes、sanitized outputs 与 schema：`src/capabilities/main_agent/runtime_replanner.py:277-321`。
- Skill input resolver 只有 deterministic 缺参后才走 LLM fallback：`src/integrations/codex_skills/input_resolution.py:132-194`；LLM prompt 当前包含 `entrypoint`：`src/integrations/codex_skills/input_resolution.py:339-381`，与 P5 “只公开用户可见 schema/context”目标冲突。
- Conversation memory summary prompt 与 entity resolution prompt 当前手写：`src/orchestration/conversation_memory.py:683-696`、`src/orchestration/conversation_memory.py:780-910`；resolver 输出之后仍有高置信与 evidence-in-context 校验：`src/orchestration/conversation_memory.py:805-860`。
- 现有 P4 / P2 API 与 executor tests 已覆盖 main prompt envelope audit 与 public profile safety，可复用同一审计字段口径：`tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py:497-655`、`tests/api/test_main_agent_llm.py:118-160`。

## 3. RALPLAN-DR Short Summary

### Principles

1. **单一预算口径**：所有 profile 化最终输入都通过 `render_prompt_envelope`，不复制 75% / preflight 逻辑。
2. **兼容优先、放量显式**：默认 `off` 保持旧路径；`shadow` 收集 audit；`string` 才改变 LLM 输入。
3. **后端校验不让位**：JSON prompt guard 只辅助模型输出，parse / validator / expander / resolver validation 仍是权威。
4. **LLM 最小可见面**：每个场景只传完成任务所需 public contract 和脱敏上下文，不传内部入口、路径、handler、runtime、raw memory。
5. **审计无原文**：audit 只记录 template、token、hash、segment 名称和安全 metadata，不记录 raw prompt / raw candidate。

### Decision Drivers

1. P5 覆盖面横跨 `main_agent`、`orchestration`、`integrations`，需要一个 repo-local 通用 profile helper，避免每条路径各自实现 mode/audit/preflight。
2. 现有 tests 大量直接断言 prompt 文本，必须保持 `off` 兼容并只在 profile mode tests 中新增断言，降低回归风险。
3. Planner / Runtime Replanner 输出关系到 DAG 安全，profile 迁移必须同时验证 public-only 输出 validator 仍生效。

### Viable Options

| Option | Pros | Cons | Decision |
| --- | --- | --- | --- |
| A. 新增通用 `prompt_profiles` helper，各调用点按 profile 构建 PromptEnvelope | 复用 core renderer；mode/audit/preflight 统一；改动可分路径落地 | 需要调整多个调用点和测试 | **选择** |
| B. 每个调用点直接手写 PromptEnvelope + audit | 局部改动少 | 预算/audit/mode 容易分叉，后续维护成本高 | 拒绝 |
| C. 只加 fallback audit，不切换 string prompt | 风险最低 | 不满足 P5 对 profile 化路径与 75% preflight 的核心要求 | 拒绝 |

## 4. Architecture / ADR

### Decision

新增通用 profile 工厂/解析层，建议文件为 `src/orchestration/prompt_profiles.py`：

- `PromptProfileMode = off | shadow | string`，复用 `MAF_PROMPT_ENVELOPE_MODE` 作为统一开关；未知值 fail safe 为 `off`。
- `PromptProfileResolution`：包含 `prompt`、`mode`、`effective_mode`、`rendered`、`audit_payload`、`llm_call_payload`。
- `render_prompt_profile(...)`：接收 `template_id`、`template_version`、`segments`、`trim_max_tokens`、`token_estimator`，内部只调用 `render_prompt_envelope`。
- `resolve_profile_prompt_for_mode(...)`：在 `off` 返回 legacy prompt；`shadow` 生成 audit 但发送 legacy prompt；`string` 发送 rendered prompt；render failure 在 `shadow` 下记录 `render_failed` audit，在 `string` 下抛出 `PromptEnvelopeRenderError` fail closed。
- `prompt_profile_audit_payload(...)`：与 main-agent audit 字段同构，保留 no-raw-content。

### Drivers

- P5 要求 Soft Skill、Planner、repair、Runtime Replanner、Skill resolver、memory resolver/summary 均继承 75% final input budget。
- 当前 main-agent 专用 helper 位于 `src/capabilities/main_agent/`，不适合作为 `orchestration` / `integrations` 的通用依赖来源。
- 默认 `off` 可保护现有行为；profile tests 通过 patch env / explicit mode 覆盖 `shadow|string`。

### Alternatives Considered

- **把 main-agent helper 上移**：会触碰 P2/P4 已稳定路径较多，短期风险高；本轮先新增通用 helper，后续可清理重复 audit code。
- **引入新环境变量 `MAF_PROMPT_PROFILE_MODE`**：会造成两个开关并存；P5 明确“一致 mode 开关”，因此复用 `MAF_PROMPT_ENVELOPE_MODE`。
- **Planner repair 不 profile 化，仅 fallback audit**：PRD 允许 fallback audit，但 repair prompt 同样可能超预算；本轮应 profile 化并保留 previous raw output 长度限制。

### Consequences

- `string` 模式下非 main-agent 调用可能因 required segment 超预算 fail closed；这是 P5 目标行为。
- tests 需要区分 legacy `off` 与 profile `string/shadow`，避免把老 prompt 文案断言误用于 profile prompt。
- 通用 audit helper 与 main-agent 专用 audit 短期并存；后续可做小范围去重。

## 5. Implementation Steps

### CP-0 Planning Gate / State

1. 保存本计划与 test spec 到 `.omx/plans/`。
2. 更新 Ralph state：`planning_status=complete`，记录计划路径。
3. 创建 Codex goal：完成 P5 PRD 的代码、测试、验证、architect 复核、deslop、提交。

### CP-1 通用 Profile Helper（先写测试）

Files:
- `src/orchestration/prompt_profiles.py`（新增）
- `tests/orchestration/test_prompt_profiles.py`（新增）

Steps:
1. 新增 mode 解析、profile resolution、render error audit、safe llm-call payload。
2. 单测覆盖：
   - `off` 返回 legacy prompt 且无 audit。
   - `shadow` 返回 legacy prompt 但 audit 有 template / final token budget。
   - `string` 返回 rendered prompt，`final_input_tokens <= final_input_token_budget`。
   - `string` over-budget 抛 `PromptEnvelopeRenderError`，`shadow` 记录 `render_failed` fallback audit。
   - audit 不含 raw segment content / secret。

### CP-2 Soft Skill Decision / Answer Profile

Files:
- `src/capabilities/main_agent/executor.py`
- `tests/api/test_soft_skill_binding.py`

Steps:
1. 将 `_build_soft_skill_decision_prompt` 与 `_build_soft_skill_answer_prompt` 拆为 legacy prompt + profile segment factory；`off` 下保持现有字符串。
2. `string/shadow` mode 生成 `soft_skill_decision` / `soft_skill_answer` template audit；audit 放入 `soft_skill_binding.decision` 或新增 audit-only profile event，不能记录 raw prompt。
3. `_generate_streaming_answer_text` 仍只在 answer path 发布 transient `main_agent.output_delta`，不持久化 delta。
4. Tests patch `MAF_PROMPT_ENVELOPE_MODE=string|shadow` 验证：
   - decision/answer prompt 或 audit template_id 正确；
   - `main_agent.output_delta` 仍产生；
   - follow-up prompt 仍包含 conversation memory；
   - profile audit 记录 `final_input_token_budget` / `final_input_tokens`。

### CP-3 Planner / Repair Profile

Files:
- `src/orchestration/planner_contract.py`
- `src/orchestration/llm_workflow_provider.py`
- `tests/orchestration/test_planner_contract.py`
- `tests/orchestration/test_llm_workflow_provider.py`

Steps:
1. 新增 `build_planner_profile_resolution(...)` 与 `build_planner_repair_profile_resolution(...)`，保留 `build_planner_prompt` / `build_planner_repair_prompt` legacy API。
2. `build_plan_from_llm_output` 与 `LLMWorkflowProvider` 在 `string/shadow` mode 使用 profile resolution，并通过 `call_text_generator` 可选透传 `prompt_profile` audit 给接受 `**kwargs` 的 generator。
3. Planner public capability segment 不再暴露 `source_path` 给 profile prompt；保留 legacy test 可按 P5 更新为“不暴露路径、不暴露 body”。
4. Repair profile 仍限制 `previous_output[:2000]`，diagnostic 仍限制 500 chars；JSON-only guard 保留。
5. Tests 验证 parse/validate/repair 行为不变、profile audit kwargs 存在、previous raw output 不超过限制、source_path 不进入 profile prompt。

### CP-4 Runtime Replanner Profile

Files:
- `src/capabilities/main_agent/runtime_replanner.py`
- `tests/capabilities/main_agent/test_runtime_replanner.py`
- `tests/api/test_runtime_replanner.py`

Steps:
1. `_build_prompt(context)` 保留 legacy；新增 profile builder with segments：stable replan rules、public capability list、budget/counters、current public nodes、sanitized node outputs、JSON guard、current request。
2. `build_replan` 使用 profile resolution；`_call_text_generator` 透传 `prompt_profile` 给支持 kwargs 的 generator。
3. 输出 parse / validator / expander 流程不变。
4. Tests 验证：profile audit template `runtime_replan`；sanitized observation 仍无 SQL/token/schema/raw rows；public-only validator 仍拒绝内部 nodes。

### CP-5 Skill Input Resolver Profile

Files:
- `src/integrations/codex_skills/input_resolution.py`
- `tests/api/test_skill_input_resolution_runtime.py`
- 必要时新增 `tests/integrations/codex_skills/test_input_resolution.py`

Steps:
1. `_build_llm_slot_prompt` 保留 legacy；新增 resolver profile prompt。
2. profile prompt 的 skill block 只包含 name / description / public parameter schema，不包含 `entrypoint` / script path / handler / runtime。
3. context segment 只包含 query/current/resolved/recent_user_messages/artifact_summaries 和 `_safe_resolved_payload`；artifact 继续 `_safe_artifact_summary`。
4. `resolve_skill_inputs_with_llm` 将 `prompt_profile` audit kwargs 透传给支持 kwargs 的 `text_generator`；无法确定字段仍 missing。
5. Tests 验证 prompt 不含完整 memory、不含 entrypoint、audit no raw evidence，LLM 候选仍需 `_validate_llm_candidate`。

### CP-6 Conversation Memory Resolver / Summary Profile

Files:
- `src/orchestration/conversation_memory.py`
- `tests/orchestration/test_conversation_memory.py`
- `tests/api/test_conversation_memory_runtime.py`

Steps:
1. `_build_resolution_prompt` 与 `_build_summary_prompt` 保留 legacy；新增 profile resolution。
2. resolution profile segments：stable resolver rules、recent resolver turns、history summary、capability summaries、current user message、JSON schema guard。
3. summary profile segments：stable summary rules、existing summary、older turn payload；summary segment 可 `compressible/drop_oldest`，但 stable guard required。
4. 生成器调用支持可选 `prompt_profile` kwargs；若生成器不接受 kwargs，保持兼容。
5. Tests 验证：resolver 仍不回答、不选择 capability；evidence-in-context 校验仍拦截注入式 resolved_user_message；summary prompt audit 有 budget/tokens；`off` legacy 行为不变。

### CP-7 Observability / Changelog / Verification

1. 更新 `CHANGELOG.md` Unreleased，记录 P5 计划和实施落地。
2. 运行 test spec 中的 targeted commands；若目标 tests 暴露跨路径问题，补跑相邻分层。
3. License Requirement：无依赖/许可变更时在最终报告明确“未触发 cargo-deny 风险”。
4. Ralph architect verification + changed-files deslop + post-deslop regression。
5. Ralph completion audit read-back、goal complete、git commit。

## 6. Acceptance Criteria

1. `MAF_PROMPT_ENVELOPE_MODE=off` 下现有 API / planner / resolver 行为兼容。
2. `MAF_PROMPT_ENVELOPE_MODE=shadow` 下非 main-agent profile paths 记录 safe audit，但发送 legacy prompt。
3. `MAF_PROMPT_ENVELOPE_MODE=string` 下 Soft Skill、Planner、repair、Runtime Replanner、Skill input resolver、memory resolver/summary 发送 rendered PromptEnvelope prompt。
4. 每条 profile audit 都包含 template id/version、`final_input_token_budget`、`final_input_tokens`、preflight retry 字段，且不包含 raw prompt / secrets / internal entrypoint。
5. Soft Skill answer 仍流式输出 transient `main_agent.output_delta`，追问仍可消费 conversation memory。
6. Planner / repair / Runtime Replanner 输出仍经 parse + public/internal validator + expander；不能因 prompt guard 绕过后端 fail-closed。
7. Skill resolver prompt 不含完整 memory、不含 artifact raw content、不含 entrypoint/script path；无法高置信抽取的字段保持 missing。
8. Conversation memory resolver/summary 只完成各自职责，不能回答用户问题或选择 capability。

## 7. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| 迁移面大导致一次改动难定位回归 | CP-1 至 CP-6 按调用路径 TDD，小步提交前统一验证 |
| profile prompt 重排影响 planner 输出 | 保留 legacy off，profile tests 使用 fake planner；实际输出仍由 validator/repair 兜底 |
| `PromptEnvelopeRenderError` 在 string 模式下打断任务 | 这是 P5 fail-closed 目标；shadow mode 可先观察 audit |
| Skill resolver 误从历史抽参 | 只暴露 recent user messages / resolved message / artifact summaries；输出仍需 `_validate_llm_candidate` |
| audit 泄漏 raw prompt 或 evidence | 统一只记录 hash/token/segment metadata；新增静态/单测扫描 |

## 8. Verification Steps

Targeted:

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_profiles tests.orchestration.test_planner_contract tests.orchestration.test_llm_workflow_provider tests.orchestration.test_conversation_memory
conda run -n multi_agent python -m unittest tests.api.test_soft_skill_binding tests.api.test_skill_input_resolution_runtime tests.api.test_runtime_replanner tests.api.test_conversation_memory_runtime
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_runtime_replanner tests.capabilities.main_agent.test_main_agent_workflow_and_executor tests.capabilities.main_agent.test_conversation_memory_prompt
```

PRD-required:

```bash
conda run -n multi_agent python -m unittest tests.api.test_soft_skill_binding tests.api.test_skill_input_resolution_runtime tests.api.test_runtime_replanner
conda run -n multi_agent python -m unittest tests.orchestration.test_planner_contract tests.orchestration.test_llm_workflow_provider
```

Optional broader smoke if targeted regression is clean:

```bash
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

License Requirement:

- 本计划不新增依赖、不修改 `native/`、`Cargo.lock` 或 license policy；最终报告记录“无依赖/许可变更，未触发 cargo-deny 风险”。

## 9. Team / Goal Handoff Guidance

- 当前可先由单 owner `$ralph` 实施；若并行化，推荐 `$team` 三条 lane：
  1. **orchestration lane**：`prompt_profiles` + planner/repair + runtime replanner。
  2. **main-agent/integration lane**：Soft Skill + Skill input resolver。
  3. **memory/test lane**：conversation memory profiles + audit no-raw tests + regression matrix。
- Team verification path：每条 lane 返回 changed files、targeted tests、profile audit evidence；leader 汇总后跑 PRD-required commands 与 broader affected tests。
- Goal stop condition：P5 PRD 所列路径均有 profile/audit/preflight 覆盖，targeted tests pass，architect approves，deslop 后回归 pass，git commit 完成。
