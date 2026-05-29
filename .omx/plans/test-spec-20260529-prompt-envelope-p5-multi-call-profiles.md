# Prompt Envelope P5 多调用场景档案迁移测试规格

- **日期**：2026-05-29
- **目标 PRD**：`docs/prd/backend/prompt-envelope/06-阶段五-多调用场景档案迁移PRD.md`
- **实施计划**：`.omx/plans/prd-20260529-prompt-envelope-p5-multi-call-profiles.md`

## 1. Test Strategy

采用 TDD：先补 profile helper 与各调用路径失败测试，再实现。所有 profile tests 应显式设置 `MAF_PROMPT_ENVELOPE_MODE=shadow|string`；legacy 兼容 tests 使用默认 `off`。

## 2. Unit Tests

### UT-1 通用 profile helper

File: `tests/orchestration/test_prompt_profiles.py`

Cases:
1. `off`：返回 legacy prompt，`audit_payload is None`。
2. `shadow`：返回 legacy prompt，但 audit status 为 `rendered`，包含 `template_id`、`template_version`、`final_input_token_budget`、`final_input_tokens`。
3. `string`：返回 rendered prompt，`final_input_tokens <= final_input_token_budget`。
4. `string` over-budget：抛 `PromptEnvelopeRenderError`；`shadow` over-budget：返回 `render_failed` audit。
5. audit 文本不含 raw segment content、secret、用户原文敏感片段。

### UT-2 Planner / repair

Files: `tests/orchestration/test_planner_contract.py`, `tests/orchestration/test_llm_workflow_provider.py`

Cases:
1. `string` mode planner generator 接收到 profile prompt，`prompt_profile.template_id == "planner"`。
2. Planner profile prompt 包含 public capability id/name/description 和 allowed payload fields，但不包含 `source_path` / Skill body / handler / runtime。
3. `build_plan_from_llm_output` 仍可 parse JSON public DAG。
4. Repair path 第二次 generator 调用接收 `planner_repair` profile，previous raw output 只保留截断片段，不超过现有 2000 chars 口径。
5. invalid JSON / internal capability 仍触发一次 repair；repair 失败仍 fail closed。

### UT-3 Runtime Replanner

File: `tests/capabilities/main_agent/test_runtime_replanner.py`

Cases:
1. `string` mode replan generator 接收 `runtime_replan` profile audit。
2. Prompt 保留 sanitized node outputs：有 `row_sample`，无 SQL、guard token、schema DDL、raw oversized rows。
3. 内部 capability 输出仍被 validator 拒绝 / 返回 None。
4. satisfied output / unresolved interrupt / max replan reached 仍不调用 LLM。

### UT-4 Skill input resolver

Files: `tests/api/test_skill_input_resolution_runtime.py`, optional `tests/integrations/codex_skills/test_input_resolution.py`

Cases:
1. `string` mode LLM slot prompt 使用 `skill_input_resolver` profile。
2. Prompt 不含完整 memory keys：`conversation_memory`、`memory_context`、`history_summary`、完整 `recent_messages`。
3. Prompt 不含 `entrypoint`、script path、handler、runtime、source_path。
4. Artifact 只出现 `_safe_artifact_summary` allowlist 字段，无 raw content。
5. LLM 输出 unknown parameter / invalid value / unsupported source 仍被拒绝并进入 diagnostics 或 missing。

### UT-5 Conversation memory resolver / summary

File: `tests/orchestration/test_conversation_memory.py`

Cases:
1. `string` mode resolution generator 接收 `conversation_memory_resolution` profile audit。
2. Resolver output 仍需 high confidence、entity、evidence-in-context；注入式 `resolved_user_message` 只影响 metadata，不覆盖 safe composed message。
3. Ambiguous / low confidence / no evidence 仍不 resolve。
4. `string` mode summary generator 接收 `conversation_memory_summary` profile audit，summary prompt 不允许回答用户问题或引入新事实。
5. `off` mode 原有 deterministic / LLM resolution tests 保持通过。

## 3. API / Integration Tests

### IT-1 Soft Skill binding

File: `tests/api/test_soft_skill_binding.py`

Cases:
1. `string` mode decision audit 包含 `soft_skill_decision` template，answer/execute API 语义不变。
2. `string` mode answer audit 包含 `soft_skill_answer` template；答疑路径仍产生 transient `main_agent.output_delta`。
3. Follow-up soft skill answer prompt/profile 仍消费 prior turn conversation memory。
4. profile audit 不含 raw prompt、manifest body、entrypoint、内部路径。

### IT-2 Skill input resolution runtime

File: `tests/api/test_skill_input_resolution_runtime.py`

Cases:
1. Main-agent LLM slot resolver 在 profile mode 下仍能补出 scalar 参数。
2. `skill.input_resolved` audit 只记录 resolved_fields/source，不记录 LLM evidence 或历史原文。
3. Skill subprocess payload 不含 raw memory。

### IT-3 Runtime replanner API

File: `tests/api/test_runtime_replanner.py`

Cases:
1. 默认 runtime replanner 行为兼容。
2. 如 API 层能捕获 generator kwargs，则验证 `runtime_replan` profile audit 可达。

### IT-4 Conversation memory runtime

File: `tests/api/test_conversation_memory_runtime.py`

Cases:
1. resolution generator 在 planning 前运行，profile mode 不改变 effective question 注入 planner 的语义。
2. Memory builder failure 仍不阻断主流程，fallback audit/metadata 不含 raw prompt。

## 4. Security / Audit Assertions

对以下关键字做 prompt/audit 双向扫描：

- 不应进入 audit：raw prompt、大段 user content、`SECRET_TOKEN_SHOULD_NOT_LEAK`、`manifest.body`、`entrypoint`、`handler`、`runtime`、`script_path`、`source_path`、SQL/schema DDL。
- 允许进入 prompt 的 public 字段：capability id/name/description、public parameter schema、safe artifact summary、sanitized row sample、current user request、resolved/effective question。
- 必须进入 audit：`template_id`、`template_version`、`final_input_token_budget`、`final_input_tokens`、`preflight_retry_count`、`history_compression_retry`。

## 5. Required Commands

PRD-required:

```bash
conda run -n multi_agent python -m unittest tests.api.test_soft_skill_binding tests.api.test_skill_input_resolution_runtime tests.api.test_runtime_replanner
conda run -n multi_agent python -m unittest tests.orchestration.test_planner_contract tests.orchestration.test_llm_workflow_provider
```

Targeted expanded:

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_profiles tests.orchestration.test_conversation_memory
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_runtime_replanner tests.capabilities.main_agent.test_main_agent_workflow_and_executor tests.capabilities.main_agent.test_conversation_memory_prompt
conda run -n multi_agent python -m unittest tests.api.test_conversation_memory_runtime
```

Optional broad affected suites after targeted pass:

```bash
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

## 6. Exit Criteria

1. 所有 required commands 通过。
2. Targeted expanded commands 通过，或任何跳过/无法运行项有明确原因与替代验证。
3. 每条 P5 调用路径在 `string` profile mode 下都有 audit/preflight evidence。
4. `off` mode 兼容测试未破坏。
5. License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。
