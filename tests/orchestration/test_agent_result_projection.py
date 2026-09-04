from __future__ import annotations

import json
import math
import unittest

from src.integrations.token_counter import TokenBoundedText
from src.orchestration.agent_loop.result_projection import (
    SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT,
    AgentCallResultProjector,
    build_tool_result_reuse_receipt,
    parse_tool_result_reuse_receipt,
)
from src.orchestration.agent_loop.skill_activation import (
    build_delegated_skill_instruction_result,
)
from src.storage.agent_payload import AGENT_PAYLOAD_MAX_BYTES, canonicalize_agent_payload


def _mcp_bundle(*contents: str, source_truncated: bool = False) -> dict:
    results = [
        {
            "call_sequence": index,
            "content": content,
            "source_truncated": source_truncated,
            "carrier_truncated": False,
        }
        for index, content in enumerate(contents, start=1)
    ]
    return {
        "schema": "maf.mcp.agent_result_bundle.v1",
        "result_count": len(results),
        "included_count": len(results),
        "omitted_count": 0,
        "truncated": source_truncated,
        "results": results,
    }


class AgentCallResultProjectorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.token_calls: list[tuple[str, dict]] = []

        async def keep_all(text: str, **kwargs) -> TokenBoundedText:
            self.token_calls.append((text, kwargs))
            return TokenBoundedText(
                text=text,
                total_tokens=1,
                truncated=False,
                cutoff=len(text),
            )

        self.projector = AgentCallResultProjector(token_budgeter=keep_all)

    async def project(self, capability_id: str, payload: dict, **overrides):
        return await self.projector.project(
            capability_id=capability_id,
            output_payload=payload,
            call_item_id=overrides.pop("call_item_id", "call-item-1"),
            outcome=overrides.pop("outcome", "completed"),
            safe_error_code=overrides.pop("safe_error_code", None),
            artifact_ids=overrides.pop("artifact_ids", ()),
            continuation_locator=overrides.pop("continuation_locator", None),
            model_edition=overrides.pop("model_edition", "edition-a"),
            **overrides,
        )

    async def test_small_skill_result_is_model_bound_and_deterministic(self) -> None:
        payload = {
            "answer": "found",
            "records": [{"name": "A"}, {"name": "B"}],
            "source_url": "https://example.test/result",
        }

        first = await self.project("skill.lookup", payload)
        second = await self.project("skill.lookup", payload)

        self.assertTrue(first.accepted)
        self.assertEqual(first, second)
        self.assertEqual(first.projection_mode, "inline")
        self.assertFalse(first.projection_truncated)
        self.assertEqual(first.safe_result_payload["model_view"], payload)
        self.assertEqual(len(self.token_calls), 2)
        self.assertTrue(
            all(call[1]["model_edition"] == "edition-a" for call in self.token_calls)
        )
        self.assertTrue(
            all(call[1]["max_tokens"] == 50_000 for call in self.token_calls)
        )

    async def test_reuse_receipt_has_exact_bounded_schema(self) -> None:
        receipt = build_tool_result_reuse_receipt(
            source_result_item_id="result-root",
            source_result_payload_sha256="a" * 64,
        )

        self.assertEqual(
            parse_tool_result_reuse_receipt(receipt),
            ("result-root", "a" * 64),
        )
        with self.assertRaisesRegex(
            ValueError, "agent_reused_tool_result_unavailable"
        ):
            parse_tool_result_reuse_receipt(
                {**receipt, "artifact_refs": ["forbidden"]}
            )

    async def test_legacy_large_skill_stages_safe_projection_not_raw(self) -> None:
        payload = {
            "answer": "检索完成",
            "records": ["育种研究" * 20_000],
            "password": "do-not-stage",
        }

        projected = await self.project("skill.bioinfo_daily", payload)

        self.assertTrue(projected.accepted)
        self.assertTrue(projected.spill_required)
        self.assertEqual(projected.projection_mode, "artifact_backed")
        self.assertFalse(projected.projection_truncated)
        self.assertIsNotNone(projected.spill_content_bytes)
        staged = json.loads(projected.spill_content_bytes)
        self.assertEqual(staged["model_view"]["answer"], "检索完成")
        self.assertNotIn("password", staged["model_view"])
        self.assertNotIn("do-not-stage", projected.spill_content_bytes.decode())
        self.assertNotEqual(
            projected.spill_content_sha256,
            projected.raw_sha256,
        )

    async def test_result_above_old_80k_limit_remains_inline_under_agent_item_limit(self) -> None:
        payload = {"answer": "x" * 100_000}

        projected = await self.project(
            "skill.lookup",
            payload,
            skill_projection_policy=(
                SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT
            ),
        )

        self.assertTrue(projected.accepted)
        self.assertEqual(projected.projection_mode, "inline")
        self.assertFalse(projected.projection_truncated)
        self.assertEqual(projected.safe_result_payload["model_view"], payload)
        self.assertGreater(
            canonicalize_agent_payload(projected.safe_result_payload).size_bytes,
            80_000,
        )

    async def test_agent_item_overflow_uses_reference_without_content_truncation(self) -> None:
        payload = {"records": ["x" * 150_000]}

        projected = await self.project(
            "skill.lookup",
            payload,
            skill_projection_policy=(
                SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT
            ),
        )

        self.assertTrue(projected.accepted)
        self.assertEqual(projected.projection_revision, "skill-result-v2")
        self.assertEqual(projected.projection_mode, "transient_staged")
        self.assertFalse(projected.projection_truncated)
        self.assertTrue(projected.transient_stage_required)
        self.assertIsNotNone(projected.transient_content_bytes)
        staged = json.loads(projected.transient_content_bytes)
        self.assertEqual(staged["model_view"], payload)
        self.assertLessEqual(
            canonicalize_agent_payload(projected.safe_result_payload).size_bytes,
            AGENT_PAYLOAD_MAX_BYTES,
        )

    async def test_token_overflow_uses_single_structured_preview(self) -> None:
        calls = []

        async def truncate(text: str, **kwargs) -> TokenBoundedText:
            calls.append((text, kwargs))
            return TokenBoundedText(
                text=text[:24],
                total_tokens=50_001,
                truncated=True,
                cutoff=24,
            )

        projector = AgentCallResultProjector(token_budgeter=truncate)
        projected = await projector.project(
            capability_id="skill.lookup",
            output_payload={"records": ["业务正文" * 100]},
            call_item_id="call-item-1",
            outcome="completed",
            safe_error_code=None,
            model_edition="edition-b",
        )

        self.assertTrue(projected.accepted)
        self.assertTrue(projected.projection_truncated)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["model_edition"], "edition-b")
        self.assertEqual(calls[0][1]["max_tokens"], 50_000)
        self.assertEqual(
            projected.safe_result_payload["model_view"],
            {
                "schema": "maf.agent.tool_result_preview.v1",
                "structured_preview": calls[0][0][:24],
            },
        )

    async def test_sensitive_fields_are_removed_before_tokenization(self) -> None:
        projected = await self.project(
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
        self.assertNotIn("not-for-model", self.token_calls[0][0])

    async def test_mcp_bundle_is_not_tokenized_twice(self) -> None:
        bundle = _mcp_bundle("bounded agent projection")
        projected = await self.project(
            "mcp.dispatch",
            {
                "agent_projection": bundle,
                "mcp_status": "completed",
                "business_result": {"private_user_view": [1, 2, 3]},
                "structured_content": {"private_raw": [1, 2, 3]},
                "truncated": False,
            },
        )

        self.assertTrue(projected.accepted)
        self.assertEqual(self.token_calls, [])
        model_view = projected.safe_result_payload["model_view"]
        self.assertEqual(model_view["agent_projection"], bundle)
        self.assertNotIn("business_result", model_view)
        self.assertNotIn("structured_content", model_view)

    async def test_large_mcp_bundle_uses_reference_without_carrier_truncation(self) -> None:
        old = "old-start-" + "x" * 70_000 + "-old-end"
        new = "new-start-" + "y" * 70_000 + "-new-end"
        projected = await self.project(
            "mcp.dispatch",
            {
                "agent_projection": _mcp_bundle(old, new),
                "mcp_status": "completed",
                "truncated": False,
            },
        )

        self.assertTrue(projected.accepted)
        self.assertEqual(projected.projection_mode, "transient_staged")
        self.assertFalse(projected.projection_truncated)
        staged = json.loads(projected.transient_content_bytes)
        staged_bundle = staged["model_view"]["agent_projection"]
        self.assertEqual(staged_bundle["included_count"], 2)
        self.assertIn("old-end", staged_bundle["results"][0]["content"])
        self.assertIn("new-end", staged_bundle["results"][1]["content"])
        self.assertTrue(
            all(not item["carrier_truncated"] for item in staged_bundle["results"])
        )

    async def test_legacy_mcp_result_is_tokenized_once(self) -> None:
        projected = await self.project(
            "mcp.crm.lookup",
            {"mcp_status": "completed", "text": "business", "truncated": False},
        )

        self.assertTrue(projected.accepted)
        self.assertEqual(len(self.token_calls), 1)
        self.assertIn("business", self.token_calls[0][0])

    async def test_delegated_result_uses_same_model_bound_budget(self) -> None:
        delegated = build_delegated_skill_instruction_result(
            capability_id="skill.report",
            pinned_bundle_revision="revision-1",
            profile_digest="a" * 64,
            instruction_body="# Instructions\nUse the workflow.",
        )

        projected = await self.project("skill.report", delegated)

        self.assertTrue(projected.accepted)
        self.assertEqual(
            projected.projection_revision,
            "delegated-skill-instruction-v1",
        )
        self.assertEqual(
            projected.safe_result_payload["model_view"],
            delegated["model_view"],
        )
        self.assertEqual(len(self.token_calls), 1)

    async def test_failed_or_waiting_result_does_not_call_tokenization(self) -> None:
        failed = await self.project(
            "skill.lookup",
            {"error": "safe"},
            outcome="failed",
            safe_error_code="skill_failed",
        )
        waiting = await self.project(
            "skill.lookup",
            {"question": "need input"},
            outcome="waiting_for_input",
            continuation_locator={
                "schema": "maf.agent.continuation_locator.v1",
                "safe_ref": "r1",
            },
        )

        self.assertTrue(failed.accepted)
        self.assertTrue(waiting.accepted)
        self.assertEqual(self.token_calls, [])

    async def test_invalid_mcp_bundles_and_json_values_fail_closed(self) -> None:
        invalid_bundles = [
            "legacy agent projection",
            {**_mcp_bundle("safe"), "unknown": True},
            {**_mcp_bundle("safe"), "included_count": 2},
            {**_mcp_bundle("safe"), "truncated": True},
        ]
        for agent_projection in invalid_bundles:
            projected = await self.project(
                "mcp.dispatch",
                {
                    "agent_projection": agent_projection,
                    "mcp_status": "completed",
                    "truncated": False,
                },
            )
            self.assertEqual(projected.error_code, "agent_result_invalid")

        invalid_values = (
            {"value": math.nan},
            {"value": math.inf},
            {1: "non-string-key"},
            {"value": object()},
            {"value": "\ud800"},
        )
        for value in invalid_values:
            projected = await self.project("skill.lookup", value)
            self.assertEqual(projected.error_code, "agent_result_invalid")

    async def test_full_tool_result_envelope_overflow_is_typed(self) -> None:
        projected = await self.project(
            "skill.lookup",
            {"answer": "small"},
            artifact_ids=tuple(
                f"artifact-{index}-" + "x" * 200 for index in range(700)
            ),
        )

        self.assertEqual(projected.error_code, "agent_result_projection_too_large")
        self.assertGreater(projected.original_size_bytes, 0)
        self.assertRegex(projected.raw_sha256 or "", r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
