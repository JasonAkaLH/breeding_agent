# Prompt Envelope P7 测试规格

- **日期**：2026-05-29
- **对应实施计划**：`.omx/plans/prd-20260529-prompt-envelope-p7-cache-observability.md`

## 1. Unit: `tests/orchestration/test_prompt_envelope.py`

1. `test_cacheable_prefix_hash_is_stable_when_dynamic_user_history_or_tool_result_changes`
   - Given stable prefix segment 不变，user/history/tool result segment 内容变化。
   - Expect `cacheable_prefix_hash` / `cacheable_prefix_tokens` 不变。
   - Also assert stable prefix 内容改变时 hash 改变。
2. `test_stable_prefix_dynamic_metadata_pollution_fails_closed`
   - Given stable prefix segment metadata 含 `task_id` / `conversation_id` / `username` / `artifact_id` 等动态键。
   - Expect `PromptEnvelopeRenderError.reason == "stable_prefix_dynamic_pollution"`。
   - Error details only include safe scalar segment/pollution kind/source，不含 raw value。
3. `test_stable_prefix_dynamic_content_marker_pollution_fails_closed`
   - Given stable prefix content 含机器字段标记 `task_id:` 或 `dependency_result`。
   - Expect fail-closed with safe details。
4. `test_prompt_render_metrics_are_safe_and_complete`
   - Given rendered audit has history trim and/or retry。
   - Expect metrics contains final input budget/tokens, history budget/tokens, prefix hash/tokens, first dynamic segment, trim reason summary, role fallback count, history compression retry。
   - JSON scan excludes raw prompt, secret, DSN, token。

## 2. Unit: `tests/orchestration/test_prompt_profiles.py`

1. `test_profile_audit_includes_prompt_render_metrics_without_raw_content`
   - mode=`shadow|string|messages` 至少覆盖一个 rendered path。
   - Expect `audit_payload["prompt_render_metrics"]` exists and llm_call payload includes it。
2. `test_profile_string_mode_fails_closed_on_stable_prefix_pollution`
   - stable prefix metadata/content 动态污染。
   - Expect non-shadow mode raises `PromptEnvelopeRenderError`。
3. Shadow failure existing behavior remains：shadow returns legacy prompt + `render_failed` safe payload。

## 3. Unit: `tests/integrations/test_llm_client.py`

1. `test_provider_cache_hint_defaults_disabled`
   - config 不声明 cache capability。
   - Expect request options 不含 prompt cache hint；safe metadata reports disabled/no-op safely。
2. `test_provider_cache_hint_unsupported_provider_noops_when_enabled`
   - config enabled=true but supports_prompt_cache=false。
   - Expect request options 不含 cache hint；status unsupported。
3. `test_provider_cache_hint_supported_provider_adds_configured_hint`
   - config supports=true enabled=true + safe hint mapping。
   - Expect request `extra_body` includes configured hint merged with thinking；safe metadata includes supports/enabled/status/hint_keys only。
4. Streaming path reuses same request option logic。

## 4. Unit: `tests/integrations/test_llm_runtime.py`

1. `test_static_metadata_includes_provider_cache_capabilities`
   - Given runtime config includes `provider_cache_capabilities`。
   - Expect static metadata has sanitized supports/enabled/hint_keys/status and no raw provider endpoint/secrets。

## 5. Integration: main-agent/API audit-only

1. `tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py`
   - Existing shadow/string/messages tests assert `prompt_render_metrics` in prompt event and llm_call prompt_envelope payload。
   - messages stream metadata with provider cache capabilities appears as safe `provider_cache_capabilities` in audit-only payload.
2. `tests/api/test_main_agent_llm.py`
   - Existing audit-only frontend invisibility remains; prompt event contains metrics, frontend stream types still exclude `main_agent.prompt_envelope_rendered`。

## 6. Security/no-raw scanner

For all new audit/metrics tests, scan serialized audit payload for:

- raw prompt examples such as `SECRET_*_SHOULD_NOT_LEAK`
- `api_key`, `base_url`, `postgresql://`, `mysql://`, `token`, `password`
- internal script paths / handler names where relevant

## 7. Verification commands

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope tests.orchestration.test_prompt_profiles tests.integrations.test_llm_client tests.integrations.test_llm_runtime tests.capabilities.main_agent.test_main_agent_workflow_and_executor tests.api.test_main_agent_llm
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m compileall src tests
git diff --check
```
