from __future__ import annotations

from dataclasses import asdict
import inspect
import unittest

from src.orchestration.conversation_memory import ConversationMemoryConfig
import src.orchestration.prompt_envelope as prompt_envelope_module
from src.orchestration.prompt_envelope import (
    LLMMessage,
    PromptEnvelope,
    PromptEnvelopeRenderError,
    PromptRenderAudit,
    PromptSegment,
    PromptSegmentAudit,
    RenderedMessages,
    RenderedPrompt,
    prompt_render_metrics_from_audit,
    render_prompt_envelope,
    render_prompt_envelope_messages,
)


def _word_tokens(text: str) -> int:
    return len(str(text).split())


def _words(prefix: str, count: int) -> str:
    return " ".join(f"{prefix}{index}" for index in range(count))


def _segment(
    name: str,
    content: str,
    *,
    security_role: str,
    trim_policy: str = "required",
    priority: int = 0,
    mutability: str = "dynamic",
    cache_affinity: str = "no_cache",
    role: str = "context",
    metadata: dict[str, object] | None = None,
) -> PromptSegment:
    return PromptSegment(
        name=name,
        role=role,
        content=content,
        priority=priority,
        mutability=mutability,
        cache_affinity=cache_affinity,
        trim_policy=trim_policy,
        security_role=security_role,
        metadata=metadata or {},
    )


def _envelope(*segments: PromptSegment, trim_max_tokens: int = 2_000) -> PromptEnvelope:
    return PromptEnvelope(
        template_id="test.prompt-envelope",
        template_version="v1",
        model_edition="fake-model",
        trim_max_tokens=trim_max_tokens,
        segments=segments,
    )


def _audit_text(audit: PromptRenderAudit) -> str:
    values: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(asdict(audit))
    return "\n".join(values)


class PromptEnvelopePhaseZeroBaselineTest(unittest.TestCase):
    def test_phase_zero_locks_static_conversation_memory_budget_before_dynamic_prompt_envelope_budgeting(self) -> None:
        self.assertEqual(ConversationMemoryConfig(max_tokens=1_024_000).actual_memory_budget, 768_000)

    def test_phase_zero_documents_current_reserved_token_formula(self) -> None:
        self.assertEqual(ConversationMemoryConfig(max_tokens=8_000).actual_memory_budget, 6_000)
        self.assertEqual(ConversationMemoryConfig(max_tokens=10_000, reserved_tokens=2_000).actual_memory_budget, 8_000)


class PromptEnvelopeCoreRendererTest(unittest.TestCase):
    def test_core_models_are_importable(self) -> None:
        self.assertEqual(PromptSegment.__name__, "PromptSegment")
        self.assertEqual(PromptEnvelope.__name__, "PromptEnvelope")
        self.assertEqual(PromptSegmentAudit.__name__, "PromptSegmentAudit")
        self.assertEqual(PromptRenderAudit.__name__, "PromptRenderAudit")
        self.assertEqual(RenderedPrompt.__name__, "RenderedPrompt")
        self.assertEqual(RenderedMessages.__name__, "RenderedMessages")
        self.assertEqual(LLMMessage.__name__, "LLMMessage")

    def test_core_module_has_no_runtime_or_provider_dependencies(self) -> None:
        source = inspect.getsource(prompt_envelope_module)
        for forbidden in ("fastapi", "src.storage", "LLMClient", "httpx", "token_counter", "SkillExecutor"):
            self.assertNotIn(forbidden, source)

    def test_segment_order_is_deterministic_independent_of_input_order(self) -> None:
        stable_system = _segment(
            "stable_system_contract",
            "system contract",
            security_role="instruction",
            role="system",
            mutability="stable",
            cache_affinity="prefix",
        )
        tool_rules = _segment(
            "stable_tool_rules",
            "tool rules",
            security_role="tool_rule",
            role="system",
            mutability="stable",
            cache_affinity="prefix",
        )
        history = _segment(
            "bulk_conversation_history",
            "older context",
            security_role="history",
            trim_policy="drop_oldest",
        )
        current_user = _segment("current_user_request", "user asks", security_role="user_input", role="user")
        final_guard = _segment("final_recency_guard", "final guard", security_role="guard", role="system")

        rendered_a = render_prompt_envelope(
            _envelope(current_user, history, final_guard, tool_rules, stable_system),
            token_estimator=_word_tokens,
        )
        rendered_b = render_prompt_envelope(
            _envelope(stable_system, tool_rules, history, current_user, final_guard),
            token_estimator=_word_tokens,
        )

        self.assertEqual(rendered_a.prompt, rendered_b.prompt)
        self.assertEqual([segment.name for segment in rendered_a.audit.segments], [segment.name for segment in rendered_b.audit.segments])
        self.assertEqual(rendered_a.audit.cacheable_prefix_hash, rendered_b.audit.cacheable_prefix_hash)
        self.assertLess(rendered_a.prompt.index("system contract"), rendered_a.prompt.index("tool rules"))
        self.assertLess(rendered_a.prompt.index("tool rules"), rendered_a.prompt.index("older context"))
        self.assertLess(rendered_a.prompt.index("older context"), rendered_a.prompt.index("user asks"))
        self.assertLess(rendered_a.prompt.index("user asks"), rendered_a.prompt.index("final guard"))

    def test_final_input_budget_and_dynamic_history_budget_use_trusted_margin(self) -> None:
        rendered = render_prompt_envelope(
            _envelope(
                _segment("stable_system_contract", _words("sys", 10), security_role="instruction", role="system"),
                _segment("current_user_request", _words("user", 20), security_role="user_input", role="user"),
                _segment("bulk_conversation_history", _words("hist", 100), security_role="history", trim_policy="drop_oldest"),
                trim_max_tokens=1_024_000,
            ),
            token_estimator=_word_tokens,
        )

        self.assertEqual(rendered.audit.final_input_token_budget, 768_000)
        self.assertEqual(rendered.audit.safety_margin_tokens, 10_240)
        self.assertEqual(rendered.audit.non_history_tokens, 30)
        self.assertEqual(rendered.audit.bulk_history_budget, 757_730)

    def test_fallback_estimator_uses_larger_safety_margin(self) -> None:
        rendered = render_prompt_envelope(
            _envelope(
                _segment("stable_system_contract", _words("sys", 10), security_role="instruction", role="system"),
                _segment("bulk_conversation_history", _words("hist", 100), security_role="history", trim_policy="drop_oldest"),
                trim_max_tokens=1_024_000,
            ),
            token_estimator=_word_tokens,
            token_estimator_is_fallback=True,
        )

        self.assertEqual(rendered.audit.final_input_token_budget, 768_000)
        self.assertEqual(rendered.audit.safety_margin_tokens, 20_480)
        self.assertEqual(rendered.audit.token_estimator, "fallback")
        self.assertEqual(rendered.audit.bulk_history_budget, 747_510)

    def test_required_segments_over_budget_fail_closed_without_truncation(self) -> None:
        with self.assertRaisesRegex(PromptEnvelopeRenderError, "required_segments_over_budget"):
            render_prompt_envelope(
                _envelope(
                    _segment("stable_system_contract", _words("sys", 7), security_role="instruction", role="system"),
                    trim_max_tokens=8,
                ),
                token_estimator=_word_tokens,
            )

    def test_history_and_optional_segments_are_trimmed_or_dropped_with_audit(self) -> None:
        rendered = render_prompt_envelope(
            _envelope(
                _segment("stable_system_contract", _words("sys", 1), security_role="instruction", role="system"),
                _segment("bulk_conversation_history", _words("hist", 500), security_role="history", trim_policy="drop_oldest"),
                _segment(
                    "optional_profile_examples",
                    _words("optional", 2_000),
                    security_role="tool_profile",
                    trim_policy="drop_if_needed",
                ),
                trim_max_tokens=2_000,
            ),
            token_estimator=_word_tokens,
        )

        segment_audits = {segment.name: segment for segment in rendered.audit.segments}
        self.assertNotIn("optional0", rendered.prompt)
        self.assertTrue(segment_audits["optional_profile_examples"].trimmed)
        self.assertEqual(segment_audits["optional_profile_examples"].tokens_after, 0)
        self.assertTrue(segment_audits["bulk_conversation_history"].trimmed)
        self.assertNotIn("hist0", rendered.prompt)
        self.assertIn("hist499", rendered.prompt)
        self.assertEqual(segment_audits["bulk_conversation_history"].trim_reason, "drop_oldest_to_bulk_history_budget")

    def test_compressible_segment_keeps_prefix_that_fits_available_budget(self) -> None:
        rendered = render_prompt_envelope(
            _envelope(
                _segment("stable_system_contract", _words("sys", 1_480), security_role="instruction", role="system"),
                _segment("tool_result_detail", _words("detail", 40), security_role="tool_result", trim_policy="compressible"),
                trim_max_tokens=2_000,
            ),
            token_estimator=_word_tokens,
        )

        segment_audits = {segment.name: segment for segment in rendered.audit.segments}
        self.assertTrue(segment_audits["tool_result_detail"].trimmed)
        self.assertIn("detail0", rendered.prompt)
        self.assertNotIn("detail39", rendered.prompt)
        self.assertEqual(segment_audits["tool_result_detail"].trim_reason, "compressible_to_available_budget")

    def test_cacheable_prefix_hash_ignores_dynamic_or_non_prefix_segments(self) -> None:
        stable_prefix = _segment(
            "stable_system_contract",
            "stable prefix",
            security_role="instruction",
            role="system",
            mutability="stable",
            cache_affinity="prefix",
        )
        dynamic_history_a = _segment("bulk_conversation_history", "history a", security_role="history", trim_policy="drop_oldest")
        dynamic_history_b = _segment("bulk_conversation_history", "history b changed", security_role="history", trim_policy="drop_oldest")
        dynamic_prefix = _segment(
            "selected_public_tool_profiles",
            "dynamic profile",
            security_role="tool_profile",
            mutability="dynamic",
            cache_affinity="prefix",
            trim_policy="drop_if_needed",
        )

        hash_a = render_prompt_envelope(
            _envelope(stable_prefix, dynamic_history_a, dynamic_prefix),
            token_estimator=_word_tokens,
        ).audit.cacheable_prefix_hash
        hash_b = render_prompt_envelope(
            _envelope(stable_prefix, dynamic_history_b, dynamic_prefix),
            token_estimator=_word_tokens,
        ).audit.cacheable_prefix_hash
        hash_c = render_prompt_envelope(
            _envelope(
                _segment(
                    "stable_system_contract",
                    "stable prefix changed",
                    security_role="instruction",
                    role="system",
                    mutability="stable",
                    cache_affinity="prefix",
                ),
                dynamic_history_a,
                dynamic_prefix,
            ),
            token_estimator=_word_tokens,
        ).audit.cacheable_prefix_hash

        self.assertEqual(hash_a, hash_b)
        self.assertNotEqual(hash_a, hash_c)

    def test_cacheable_prefix_hash_is_stable_when_dynamic_user_history_or_tool_result_changes(self) -> None:
        stable_prefix = _segment(
            "stable_system_contract",
            "stable prefix",
            security_role="instruction",
            role="system",
            mutability="stable",
            cache_affinity="prefix",
        )
        stable_tool_rules = _segment(
            "stable_tool_rules",
            "stable tool rules",
            security_role="tool_rule",
            role="system",
            mutability="stable",
            cache_affinity="prefix",
        )

        rendered_a = render_prompt_envelope(
            _envelope(
                stable_prefix,
                stable_tool_rules,
                _segment("bulk_conversation_history", "history a", security_role="history", trim_policy="drop_oldest"),
                _segment("required_tool_results_and_artifacts", "tool result a", security_role="tool_result"),
                _segment("current_user_request", "user asks a", security_role="user_input", role="user"),
            ),
            token_estimator=_word_tokens,
        )
        rendered_b = render_prompt_envelope(
            _envelope(
                stable_prefix,
                stable_tool_rules,
                _segment("bulk_conversation_history", "history b changed", security_role="history", trim_policy="drop_oldest"),
                _segment("required_tool_results_and_artifacts", "tool result b changed", security_role="tool_result"),
                _segment("current_user_request", "user asks b changed", security_role="user_input", role="user"),
            ),
            token_estimator=_word_tokens,
        )

        self.assertEqual(rendered_a.audit.cacheable_prefix_hash, rendered_b.audit.cacheable_prefix_hash)
        self.assertEqual(rendered_a.audit.cacheable_prefix_tokens, rendered_b.audit.cacheable_prefix_tokens)
        self.assertFalse(rendered_a.audit.prefix_dynamic_pollution_detected)

    def test_stable_prefix_dynamic_metadata_pollution_fails_closed_without_raw_value(self) -> None:
        with self.assertRaises(PromptEnvelopeRenderError) as captured:
            render_prompt_envelope(
                _envelope(
                    _segment(
                        "stable_system_contract",
                        "stable prefix",
                        security_role="instruction",
                        role="system",
                        mutability="stable",
                        cache_affinity="prefix",
                        metadata={"task_id": "task-secret-raw-value"},
                    ),
                ),
                token_estimator=_word_tokens,
            )

        self.assertEqual(captured.exception.reason, "stable_prefix_dynamic_pollution")
        self.assertEqual(captured.exception.details["segment_name"], "stable_system_contract")
        self.assertEqual(captured.exception.details["source"], "metadata_key")
        self.assertEqual(captured.exception.details["marker"], "task_id")
        self.assertNotIn("task-secret-raw-value", str(captured.exception.details))

    def test_stable_prefix_dynamic_content_marker_pollution_fails_closed_without_raw_prompt(self) -> None:
        raw_prompt_marker = "task_id: task-secret-raw-value"
        with self.assertRaises(PromptEnvelopeRenderError) as captured:
            render_prompt_envelope(
                _envelope(
                    _segment(
                        "stable_system_contract",
                        f"stable prefix\n{raw_prompt_marker}",
                        security_role="instruction",
                        role="system",
                        mutability="stable",
                        cache_affinity="prefix",
                    ),
                ),
                token_estimator=_word_tokens,
            )

        self.assertEqual(captured.exception.reason, "stable_prefix_dynamic_pollution")
        self.assertEqual(captured.exception.details["source"], "content_marker")
        self.assertEqual(captured.exception.details["marker"], "task_id")
        self.assertNotIn(raw_prompt_marker, str(captured.exception.details))

    def test_prompt_render_metrics_are_safe_and_complete(self) -> None:
        rendered = render_prompt_envelope(
            _envelope(
                _segment(
                    "stable_system_contract",
                    "stable prefix SECRET_STABLE_SHOULD_NOT_LEAK",
                    security_role="instruction",
                    role="system",
                    mutability="stable",
                    cache_affinity="prefix",
                ),
                _segment(
                    "bulk_conversation_history",
                    _words("hist", 500),
                    security_role="history",
                    trim_policy="drop_oldest",
                    metadata={"candidate_history_tokens": 500, "memory_candidate_count": 2},
                ),
                trim_max_tokens=600,
            ),
            token_estimator=_word_tokens,
        )

        metrics = prompt_render_metrics_from_audit(rendered.audit, mode="string", effective_mode="string")

        self.assertEqual(metrics["mode"], "string")
        self.assertEqual(metrics["template_version"], "v1")
        self.assertEqual(metrics["cacheable_prefix_hash"], rendered.audit.cacheable_prefix_hash)
        self.assertEqual(metrics["cacheable_prefix_tokens"], rendered.audit.cacheable_prefix_tokens)
        self.assertEqual(metrics["final_input_token_budget"], 450)
        self.assertEqual(metrics["final_input_tokens"], rendered.audit.final_input_tokens)
        self.assertEqual(metrics["bulk_history_budget"], rendered.audit.bulk_history_budget)
        self.assertEqual(metrics["bulk_history_tokens_used"], rendered.audit.bulk_history_tokens_used)
        self.assertEqual(metrics["preflight_retry_count"], rendered.audit.preflight_retry_count)
        self.assertEqual(metrics["history_compression_retry"], rendered.audit.history_compression_retry)
        self.assertGreaterEqual(metrics["trimmed_segment_count"], 1)
        self.assertIn("drop_oldest_to_bulk_history_budget", metrics["trim_reasons"])
        self.assertEqual(metrics["role_fallback_count"], 0)
        self.assertFalse(metrics["prefix_dynamic_pollution_detected"])
        self.assertNotIn("SECRET_STABLE_SHOULD_NOT_LEAK", str(metrics))

    def test_audit_does_not_contain_raw_segment_content_or_internal_values(self) -> None:
        secret_content = "SECRET_TOKEN_ABC postgresql://user:pass@example/db scripts/internal_demo.py artifact_raw_body"
        rendered = render_prompt_envelope(
            _envelope(_segment("tool_result", secret_content, security_role="tool_result", trim_policy="drop_if_needed")),
            token_estimator=_word_tokens,
        )

        audit_text = _audit_text(rendered.audit)
        for forbidden in ("SECRET_TOKEN_ABC", "postgresql://user:pass@example/db", "scripts/internal_demo.py", "artifact_raw_body"):
            self.assertIn(forbidden, rendered.prompt)
            self.assertNotIn(forbidden, audit_text)

    def test_history_candidate_audit_uses_safe_metadata_without_raw_candidate_content(self) -> None:
        rendered = render_prompt_envelope(
            _envelope(
                _segment("stable_system_contract", _words("sys", 10), security_role="instruction", role="system"),
                _segment(
                    "bulk_conversation_history",
                    "RAW_CANDIDATE_CONTENT_SHOULD_NOT_BE_IN_AUDIT",
                    security_role="history",
                    trim_policy="drop_oldest",
                    metadata={
                        "candidate_history_tokens": 42,
                        "memory_candidate_count": 3,
                        "candidate_kinds": ["history_summary", "clarification_message"],
                        "raw_candidate_content": "RAW_CANDIDATE_CONTENT_SHOULD_NOT_BE_IN_AUDIT",
                    },
                ),
                trim_max_tokens=4_000,
            ),
            token_estimator=_word_tokens,
        )

        self.assertEqual(rendered.audit.candidate_history_tokens, 42)
        self.assertEqual(rendered.audit.memory_candidate_count, 3)
        history_audit = next(segment for segment in rendered.audit.segments if segment.name == "bulk_conversation_history")
        self.assertEqual(history_audit.metadata["candidate_history_tokens"], 42)
        self.assertEqual(history_audit.metadata["memory_candidate_count"], 3)
        self.assertEqual(history_audit.metadata["candidate_kinds"], ("history_summary", "clarification_message"))
        audit_text = _audit_text(rendered.audit)
        self.assertNotIn("RAW_CANDIDATE_CONTENT_SHOULD_NOT_BE_IN_AUDIT", audit_text)

    def test_final_preflight_retries_once_by_compressing_history(self) -> None:
        def estimator(text: str) -> int:
            tokens = _word_tokens(text)
            if "\n\n" in text:
                return tokens + 1_100
            return tokens

        rendered = render_prompt_envelope(
            _envelope(
                _segment("stable_system_contract", _words("sys", 400), security_role="instruction", role="system"),
                _segment("bulk_conversation_history", _words("hist", 70), security_role="history", trim_policy="drop_oldest"),
                trim_max_tokens=2_000,
            ),
            token_estimator=estimator,
        )

        self.assertEqual(rendered.audit.preflight_retry_count, 1)
        self.assertTrue(rendered.audit.history_compression_retry)
        self.assertLessEqual(rendered.audit.final_input_tokens, rendered.audit.final_input_token_budget)
        self.assertEqual(rendered.audit.bulk_history_tokens_used, 0)

    def test_second_preflight_failure_fails_closed(self) -> None:
        def estimator(text: str) -> int:
            tokens = _word_tokens(text)
            if "\n\n" in text:
                return tokens + 1_600
            return tokens

        with self.assertRaisesRegex(PromptEnvelopeRenderError, "final_input_over_budget"):
            render_prompt_envelope(
                _envelope(
                    _segment("stable_system_contract", _words("sys", 400), security_role="instruction", role="system"),
                    _segment("final_recency_guard", "guard", security_role="guard", role="system"),
                    _segment("bulk_conversation_history", _words("hist", 70), security_role="history", trim_policy="drop_oldest"),
                    trim_max_tokens=2_000,
                ),
                token_estimator=estimator,
            )

    def test_messages_renderer_preserves_native_roles_and_audits_deterministic_fallbacks(self) -> None:
        rendered = render_prompt_envelope_messages(
            _envelope(
                _segment(
                    "stable_system_contract",
                    "系统规则 SECRET_SYSTEM_SHOULD_NOT_BE_IN_AUDIT",
                    security_role="instruction",
                    role="system",
                ),
                _segment(
                    "bulk_conversation_history",
                    "历史上下文 SECRET_HISTORY_SHOULD_NOT_BE_IN_AUDIT",
                    security_role="history",
                    trim_policy="drop_oldest",
                    role="context",
                ),
                _segment(
                    "tool_result_detail",
                    "工具结果 SECRET_TOOL_SHOULD_NOT_BE_IN_AUDIT",
                    security_role="tool_result",
                    trim_policy="compressible",
                    role="tool",
                ),
                _segment("current_user_request", "当前问题", security_role="user_input", role="user"),
                _segment("final_recency_guard", "最终 guard", security_role="guard", role="system"),
                trim_max_tokens=4_000,
            ),
            role_capabilities={"roles": ["system", "user"]},
            token_estimator=_word_tokens,
        )

        self.assertIsInstance(rendered.messages[0], LLMMessage)
        self.assertEqual({message.role for message in rendered.messages}, {"system", "user"})
        self.assertIn("当前问题", "\n".join(message.content for message in rendered.messages if message.role == "user"))
        fallback_by_segment = {fallback.segment_name: fallback for fallback in rendered.audit.role_fallbacks}
        self.assertEqual(fallback_by_segment["bulk_conversation_history"].reason, "context_to_user_context")
        self.assertEqual(fallback_by_segment["tool_result_detail"].reason, "tool_to_user_context")
        tool_message = next(message for message in rendered.messages if "SECRET_TOOL_SHOULD_NOT_BE_IN_AUDIT" in message.content)
        self.assertEqual(tool_message.role, "user")
        self.assertIn("不是用户指令", tool_message.content)
        audit_text = _audit_text(rendered.audit)
        for forbidden in (
            "SECRET_SYSTEM_SHOULD_NOT_BE_IN_AUDIT",
            "SECRET_HISTORY_SHOULD_NOT_BE_IN_AUDIT",
            "SECRET_TOOL_SHOULD_NOT_BE_IN_AUDIT",
        ):
            self.assertNotIn(forbidden, audit_text)

    def test_messages_renderer_maps_active_note_to_system_without_rewriting_content(self) -> None:
        envelope = _envelope(
            _segment("stable_system_contract", "系统规则", security_role="instruction", role="system"),
            _segment("active_continuity_notes", "连续性约束", security_role="active_note", role="system"),
            _segment("current_user_request", "当前问题", security_role="user_input", role="user"),
            trim_max_tokens=4_000,
        )

        rendered = render_prompt_envelope_messages(
            envelope,
            role_capabilities={"roles": ["system", "user"]},
            token_estimator=_word_tokens,
        )

        self.assertEqual([message.role for message in rendered.messages], ["system", "system", "user"])
        self.assertEqual(rendered.messages[1].content, "连续性约束")
        self.assertFalse(rendered.audit.role_fallbacks)

    def test_prompt_segment_rejects_developer_role(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported prompt segment role"):
            _segment("legacy", "legacy", security_role="history", role="developer")

    def test_messages_preflight_retries_once_after_wrapper_overhead_then_fails_closed_only_if_still_oversized(self) -> None:
        def estimator(text: str) -> int:
            tokens = _word_tokens(text)
            if "<message" in text:
                return tokens + 1_200
            return tokens

        rendered = render_prompt_envelope_messages(
            _envelope(
                _segment("stable_system_contract", _words("sys", 250), security_role="instruction", role="system"),
                _segment("bulk_conversation_history", _words("hist", 70), security_role="history", trim_policy="drop_oldest"),
                trim_max_tokens=2_000,
            ),
            role_capabilities={"roles": ["system", "user"]},
            token_estimator=estimator,
        )

        self.assertEqual(rendered.audit.preflight_retry_count, 1)
        self.assertTrue(rendered.audit.history_compression_retry)
        self.assertLessEqual(rendered.audit.final_input_tokens, rendered.audit.final_input_token_budget)
        self.assertEqual(rendered.audit.bulk_history_tokens_used, 0)

    def test_messages_second_preflight_failure_fails_closed(self) -> None:
        def estimator(text: str) -> int:
            tokens = _word_tokens(text)
            if "<message" in text:
                return tokens + 1_600
            return tokens

        with self.assertRaisesRegex(PromptEnvelopeRenderError, "final_input_over_budget"):
            render_prompt_envelope_messages(
                _envelope(
                    _segment("stable_system_contract", _words("sys", 400), security_role="instruction", role="system"),
                    _segment("bulk_conversation_history", _words("hist", 70), security_role="history", trim_policy="drop_oldest"),
                    trim_max_tokens=2_000,
                ),
                role_capabilities={"roles": ["system", "user"]},
                token_estimator=estimator,
            )


if __name__ == "__main__":
    unittest.main()
