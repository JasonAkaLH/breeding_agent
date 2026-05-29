from __future__ import annotations

import json
import unittest

from src.orchestration.prompt_envelope import PromptEnvelopeRenderError, PromptSegment
from src.orchestration.prompt_profiles import resolve_profile_prompt_for_mode


class PromptProfilesTest(unittest.TestCase):
    def _segments(self, *, history: str = "历史上下文") -> tuple[PromptSegment, ...]:
        return (
            PromptSegment(
                name="stable_rules",
                role="system",
                content="稳定规则 SECRET_STABLE_SHOULD_NOT_BE_IN_AUDIT",
                priority=0,
                mutability="stable",
                cache_affinity="prefix",
                trim_policy="required",
                security_role="instruction",
            ),
            PromptSegment(
                name="bulk_history",
                role="context",
                content=history,
                priority=0,
                mutability="dynamic",
                cache_affinity="no_cache",
                trim_policy="drop_oldest",
                security_role="history",
            ),
            PromptSegment(
                name="final_guard",
                role="system",
                content="只返回 JSON。",
                priority=0,
                mutability="stable",
                cache_affinity="no_cache",
                trim_policy="required",
                security_role="guard",
            ),
        )

    def test_off_mode_returns_legacy_prompt_without_audit(self) -> None:
        resolved = resolve_profile_prompt_for_mode(
            legacy_prompt="legacy prompt",
            template_id="unit_profile",
            template_version="v1",
            segments=self._segments(),
            mode="off",
            trim_max_tokens=4000,
        )

        self.assertEqual(resolved.prompt, "legacy prompt")
        self.assertEqual(resolved.effective_mode, "off")
        self.assertIsNone(resolved.audit_payload)
        self.assertIsNone(resolved.llm_call_payload)

    def test_shadow_mode_records_audit_but_keeps_legacy_prompt(self) -> None:
        resolved = resolve_profile_prompt_for_mode(
            legacy_prompt="legacy prompt",
            template_id="unit_profile",
            template_version="v1",
            segments=self._segments(),
            mode="shadow",
            trim_max_tokens=4000,
        )

        self.assertEqual(resolved.prompt, "legacy prompt")
        self.assertEqual(resolved.audit_payload["template_id"], "unit_profile")
        self.assertEqual(resolved.audit_payload["final_input_token_budget"], 3000)
        self.assertLessEqual(
            resolved.audit_payload["final_input_tokens"],
            resolved.audit_payload["final_input_token_budget"],
        )
        audit_text = json.dumps(resolved.audit_payload, ensure_ascii=False)
        self.assertNotIn("SECRET_STABLE_SHOULD_NOT_BE_IN_AUDIT", audit_text)
        self.assertEqual(resolved.llm_call_payload["template_id"], "unit_profile")

    def test_string_mode_uses_rendered_prompt_inside_final_input_budget(self) -> None:
        resolved = resolve_profile_prompt_for_mode(
            legacy_prompt="legacy prompt",
            template_id="unit_profile",
            template_version="v1",
            segments=self._segments(history="历史" * 1000),
            mode="string",
            trim_max_tokens=4000,
        )

        self.assertNotEqual(resolved.prompt, "legacy prompt")
        self.assertIn("稳定规则", resolved.prompt)
        self.assertLessEqual(
            resolved.audit_payload["final_input_tokens"],
            resolved.audit_payload["final_input_token_budget"],
        )

    def test_string_mode_fail_closes_when_required_segments_exceed_budget(self) -> None:
        segments = (
            PromptSegment(
                name="required_too_large",
                role="system",
                content="X" * 1000,
                priority=0,
                mutability="stable",
                cache_affinity="prefix",
                trim_policy="required",
                security_role="instruction",
            ),
        )

        with self.assertRaises(PromptEnvelopeRenderError):
            resolve_profile_prompt_for_mode(
                legacy_prompt="legacy",
                template_id="oversized_profile",
                template_version="v1",
                segments=segments,
                mode="string",
                trim_max_tokens=100,
                token_estimator=len,
            )

    def test_shadow_mode_records_render_failure_audit(self) -> None:
        segments = (
            PromptSegment(
                name="required_too_large",
                role="system",
                content="X" * 1000,
                priority=0,
                mutability="stable",
                cache_affinity="prefix",
                trim_policy="required",
                security_role="instruction",
            ),
        )

        resolved = resolve_profile_prompt_for_mode(
            legacy_prompt="legacy",
            template_id="oversized_profile",
            template_version="v1",
            segments=segments,
            mode="shadow",
            trim_max_tokens=100,
            token_estimator=len,
        )

        self.assertEqual(resolved.prompt, "legacy")
        self.assertEqual(resolved.audit_payload["status"], "render_failed")
        self.assertEqual(resolved.audit_payload["template_id"], "oversized_profile")


if __name__ == "__main__":
    unittest.main()
