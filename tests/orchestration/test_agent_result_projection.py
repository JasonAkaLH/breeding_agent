from __future__ import annotations

import json
import math
import unittest

from src.orchestration.agent_loop.result_projection import (
    MODEL_RESULT_MAX_BYTES,
    MODEL_VIEW_MAX_CODE_POINTS,
    AgentCallResultProjector,
)
from src.orchestration.agent_loop.skill_activation import (
    build_delegated_skill_instruction_result,
)
from src.storage.agent_payload import canonicalize_agent_payload


class AgentCallResultProjectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.projector = AgentCallResultProjector()

    def project(self, capability_id: str, payload: dict, **overrides):
        return self.projector.project(
            capability_id=capability_id,
            output_payload=payload,
            call_item_id=overrides.pop("call_item_id", "call-item-1"),
            outcome=overrides.pop("outcome", "completed"),
            safe_error_code=overrides.pop("safe_error_code", None),
            artifact_ids=overrides.pop("artifact_ids", ()),
            continuation_locator=overrides.pop("continuation_locator", None),
            **overrides,
        )

    def test_small_skill_result_is_deterministic_inline_model_result(self) -> None:
        payload = {
            "answer": "found",
            "records": [{"name": "A"}, {"name": "B"}],
            "source_url": "https://example.test/result",
        }

        first = self.project("skill.lookup", payload)
        second = self.project("skill.lookup", payload)

        self.assertTrue(first.accepted)
        self.assertEqual(first, second)
        self.assertEqual(first.projection_mode, "inline")
        self.assertFalse(first.projection_truncated)
        self.assertFalse(first.spill_required)
        result = dict(first.safe_result_payload or {})
        self.assertEqual(result["schema"], "maf.agent.model_result.v1")
        self.assertEqual(result["projection_revision"], "skill-result-v1")
        self.assertEqual(result["model_view"], payload)
        self.assertEqual(
            result["projected_size_bytes"],
            canonicalize_agent_payload(result).size_bytes,
        )
        self.assertEqual(first.original_size_bytes, len(first.canonical_raw_bytes or b""))

    def test_large_duplicate_articles_spill_once_with_small_priority_preview(self) -> None:
        articles = [
            {
                "title": f"article-{index}",
                "abstract": "育种研究" * 900,
                "url": f"https://example.test/articles/{index}",
            }
            for index in range(28)
        ]
        payload = {
            "answer": "检索完成",
            "search_summary": "找到 28 篇文献",
            "articles": articles,
            "structured_content": {"articles": articles},
        }

        projected = self.project("skill.bioinfo_daily", payload)

        self.assertTrue(projected.accepted)
        self.assertTrue(projected.spill_required)
        self.assertEqual(projected.projection_mode, "artifact_backed")
        self.assertTrue(projected.projection_truncated)
        self.assertGreater(projected.original_size_bytes, 250_000)
        self.assertTrue(projected.spill_artifact_id.startswith("agent-skill-result:"))
        model_view = projected.safe_result_payload["model_view"]
        self.assertEqual(
            model_view["summary_fields"],
            {"answer": "检索完成", "search_summary": "找到 28 篇文献"},
        )
        serialized = json.dumps(projected.safe_result_payload, ensure_ascii=False)
        self.assertNotIn("article-0", serialized)
        self.assertLessEqual(
            canonicalize_agent_payload(projected.safe_result_payload).size_bytes,
            MODEL_RESULT_MAX_BYTES,
        )

    def test_spill_rejects_internal_authority_but_allows_business_urls(self) -> None:
        large = ["x" * 10_000 for _ in range(20)]
        allowed = self.project(
            "skill.lookup",
            {"articles": large, "source_url": "https://example.test/a"},
        )
        denied = self.project(
            "skill.lookup",
            {"articles": large, "storage_key": "private/object"},
        )
        denied_text = self.project(
            "skill.lookup",
            {"articles": large, "note": "access_token=do-not-persist"},
        )

        self.assertTrue(allowed.spill_required)
        self.assertEqual(denied.error_code, "agent_result_invalid")
        self.assertEqual(denied_text.error_code, "agent_result_invalid")
        self.assertIsNone(denied.canonical_raw_bytes)

    def test_inline_sanitizer_removes_sensitive_fields_and_assignments(self) -> None:
        projected = self.project(
            "skill.lookup",
            {
                "answer": "safe",
                "password": "not-for-model",
                "note": "api_key=not-for-model",
                "token_count": 12,
            },
        )

        self.assertTrue(projected.accepted)
        self.assertEqual(
            projected.safe_result_payload["model_view"],
            {"answer": "safe", "token_count": 12},
        )

    def test_mcp_uses_agent_projection_without_business_or_raw_duplication(self) -> None:
        projected = self.project(
            "mcp.dispatch",
            {
                "text": "bounded agent projection",
                "mcp_status": "completed",
                "mcp_tool": {"name": "lookup"},
                "business_result": {"private_user_view": [1, 2, 3]},
                "structured_content": {"private_raw": [1, 2, 3]},
                "raw_result": "do-not-copy",
                "truncated": False,
            },
        )

        self.assertTrue(projected.accepted)
        self.assertFalse(projected.spill_required)
        model_view = projected.safe_result_payload["model_view"]
        self.assertEqual(model_view["text"], "bounded agent projection")
        self.assertEqual(model_view["mcp_status"], "completed")
        serialized = json.dumps(model_view)
        for forbidden in ("business_result", "structured_content", "raw_result"):
            self.assertNotIn(forbidden, serialized)

    def test_delegated_model_result_is_validated_without_second_envelope(self) -> None:
        delegated = build_delegated_skill_instruction_result(
            capability_id="skill.report",
            pinned_bundle_revision="revision-1",
            profile_digest="a" * 64,
            instruction_body="# Instructions\nUse the workflow.",
        )

        projected = self.project("skill.report", delegated)

        self.assertTrue(projected.accepted)
        self.assertEqual(projected.safe_result_payload, delegated)
        self.assertEqual(
            projected.projection_revision,
            "delegated-skill-instruction-v1",
        )

    def test_invalid_strict_json_values_fail_closed(self) -> None:
        values = (
            {"value": math.nan},
            {"value": math.inf},
            {1: "non-string-key"},
            {"value": object()},
            {"value": "\ud800"},
        )
        for value in values:
            with self.subTest(value=repr(value)):
                projected = self.project("skill.lookup", value)
                self.assertEqual(projected.error_code, "agent_result_invalid")
                self.assertIsNone(projected.safe_result_payload)

        nested: dict[str, object] = {}
        cursor = nested
        for _ in range(66):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child
        self.assertEqual(
            self.project("skill.lookup", nested).error_code,
            "agent_result_invalid",
        )

    def test_multibyte_and_escape_heavy_views_stay_within_both_budgets(self) -> None:
        for value in (
            "中" * MODEL_VIEW_MAX_CODE_POINTS,
            "😀" * MODEL_VIEW_MAX_CODE_POINTS,
            ('"\\\n' * MODEL_VIEW_MAX_CODE_POINTS),
        ):
            with self.subTest(prefix=value[:1]):
                projected = self.project("skill.lookup", {"answer": value})
                self.assertTrue(projected.accepted)
                result = projected.safe_result_payload
                encoded = canonicalize_agent_payload(result)
                rendered_view = json.dumps(
                    result["model_view"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.assertLessEqual(len(rendered_view), MODEL_VIEW_MAX_CODE_POINTS)
                self.assertLessEqual(encoded.size_bytes, MODEL_RESULT_MAX_BYTES)

    def test_continuation_locator_is_bounded_inside_model_view(self) -> None:
        locator = {"schema": "maf.agent.continuation_locator.v1", "safe_ref": "r1"}
        projected = self.project(
            "skill.lookup",
            {"question": "need input"},
            outcome="waiting_for_input",
            continuation_locator=locator,
        )
        self.assertEqual(
            projected.safe_result_payload["model_view"]["continuation_locator"],
            locator,
        )

    def test_full_tool_result_envelope_overflow_returns_typed_projection_failure(self) -> None:
        projected = self.project(
            "skill.lookup",
            {"answer": "small"},
            artifact_ids=tuple(f"artifact-{index}-" + "x" * 200 for index in range(700)),
        )
        self.assertEqual(
            projected.error_code,
            "agent_result_projection_too_large",
        )
        self.assertGreater(projected.original_size_bytes, 0)
        self.assertRegex(projected.raw_sha256 or "", r"^[0-9a-f]{64}$")
