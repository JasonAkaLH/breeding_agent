from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.core.enums import ArtifactType
from src.core.models import Artifact
from src.orchestration.agent_loop.context import (
    AgentContextBuilder,
    AgentContextRules,
)
from src.orchestration.agent_loop.context_budget import AgentContextBudget
from src.orchestration.agent_loop.context_preflight import (
    AgentContextCandidateBuilder,
    AgentContextPreflightDecision,
)
from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentToolChoice,
    AgentToolDescriptor,
)
from src.orchestration.agent_loop.result_projection import (
    SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_LEGACY,
    SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT,
    AgentCallResultProjector,
    build_tool_result_reuse_receipt,
)
from src.orchestration.agent_loop.tool_catalog import AgentToolCatalog
from src.orchestration.agent_loop.transient_results import (
    AgentTransientSkillResultResolver,
    AgentTransientSkillResultStore,
)


def _item(
    item_id: str,
    sequence: int,
    kind: AgentItemKind,
    payload: dict,
    **values,
) -> AgentItem:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    return AgentItem(
        item_id=item_id,
        run_id="run-1",
        task_id="task-1",
        sequence=sequence,
        kind=kind,
        state=values.pop("state", AgentItemState.COMMITTED),
        payload_json=text,
        payload_sha256=hashlib.sha256(text.encode()).hexdigest(),
        **values,
    )


def _user(window: int, text: str) -> AgentItem:
    return _item(
        "user-1",
        1,
        AgentItemKind.USER_MESSAGE,
        {
            "context_budget": AgentContextBudget.from_model_context_window(
                window
            ).to_payload(),
            "text": text,
        },
    )


def _run(next_sequence: int) -> AgentRun:
    return AgentRun(
        "run-1",
        "task-1",
        "conversation-1",
        AgentRunStatus.RUNNING,
        AgentModelBinding("edition-a"),
        next_item_sequence=next_sequence,
    )


def _count_characters(fragments, _binding) -> int:
    return sum(len(fragment) for fragment in fragments)


class AgentContextPreflightTest(unittest.IsolatedAsyncioTestCase):
    async def test_fits_counts_complete_tools_choice_and_each_segment_once(self) -> None:
        user = _user(100_000, "current question")
        tool = AgentToolDescriptor.for_capability(
            "skill.lookup",
            description="complete lookup description",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        )
        catalog = AgentToolCatalog((tool,), {tool.provider_safe_name: object()})
        candidate = await AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "safe", "final")
            ),
            token_counter=_count_characters,
        ).build(
            run=_run(2),
            items=(user,),
            catalog=catalog,
            tool_choice=AgentToolChoice("required", tool.provider_safe_name),
        )

        result = candidate.preflight
        self.assertEqual(result.decision, AgentContextPreflightDecision.FITS)
        self.assertGreater(result.required_tokens, 0)
        self.assertGreater(result.tool_tokens, len(tool.description))
        self.assertEqual(result.history_tokens, 0)
        self.assertEqual(result.transient_tokens, 0)
        self.assertEqual(
            result.total_tokens,
            result.required_tokens + result.history_tokens + result.tool_tokens,
        )
        self.assertEqual(candidate.request.tools, (tool,))
        self.assertEqual(candidate.request.tool_choice.mode, "required")

    async def test_only_real_closed_history_allows_compaction(self) -> None:
        items = (_user(1_000, "question"),) + tuple(
            _item(
                f"assistant-{sequence}",
                sequence,
                AgentItemKind.ASSISTANT_MESSAGE,
                {"text": "history" * 120},
            )
            for sequence in range(2, 7)
        )
        candidate = await AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "safe", "final")
            ),
            token_counter=_count_characters,
        ).build(
            run=_run(7),
            items=items,
            catalog=AgentToolCatalog((), {}),
        )

        self.assertEqual(
            candidate.preflight.decision,
            AgentContextPreflightDecision.HISTORY_COMPACTION_REQUIRED,
        )
        self.assertTrue(candidate.preflight.eligible_closed_history)
        self.assertGreater(candidate.preflight.history_tokens, 0)

    async def test_required_current_user_over_limit_is_fatal_without_history(self) -> None:
        candidate = await AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "safe", "final")
            ),
            token_counter=_count_characters,
        ).build(
            run=_run(2),
            items=(_user(1_000, "x" * 5_000),),
            catalog=AgentToolCatalog((), {}),
        )

        self.assertEqual(
            candidate.preflight.decision,
            AgentContextPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE,
        )
        self.assertFalse(candidate.preflight.eligible_closed_history)
        self.assertGreater(
            candidate.preflight.required_tokens,
            candidate.preflight.total_context_limit_tokens,
        )

    async def test_unconsumed_transient_raw_is_required_and_counted_once(self) -> None:
        run = _run(5)
        user = _user(300_000, "question")
        assistant = _item(
            "assistant-1", 2, AgentItemKind.ASSISTANT_MESSAGE, {"text": ""}
        )
        call = _item(
            "call-1",
            3,
            AgentItemKind.TOOL_CALL,
            {
                "arguments_json": "{}",
                "call_id": "provider-call-1",
                "capability_id": "skill.lookup",
                "node_id": "node-1",
                "provider_safe_name": "skill_lookup",
            },
            parent_item_id=assistant.item_id,
            call_ordinal=0,
        )
        raw_payload = {"records": ["BEGIN", "x" * 150_000, "END"]}
        projection = AgentCallResultProjector().project(
            capability_id="skill.lookup",
            output_payload=raw_payload,
            call_item_id=call.item_id,
            outcome="completed",
            safe_error_code=None,
            skill_projection_policy=(
                SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT
            ),
        )
        result = _item(
            "result-1",
            4,
            AgentItemKind.TOOL_RESULT,
            {
                "artifact_refs": [],
                "call_item_id": call.item_id,
                "outcome": "completed",
                "safe_error_code": None,
                "safe_result": projection.safe_result_payload,
            },
            parent_item_id=assistant.item_id,
            source_call_item_id=call.item_id,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = AgentTransientSkillResultStore(Path(directory) / "transient")
            store.stage(
                run=run,
                call_item=call,
                result_item_id=result.item_id,
                node_id="node-1",
                capability_id="skill.lookup",
                canonical_raw_bytes=projection.canonical_raw_bytes,
                raw_sha256=projection.raw_sha256,
                projection_revision=projection.projection_revision,
                expected_stage_ref=projection.transient_stage_ref,
            )
            candidate = await AgentContextCandidateBuilder(
                context_builder=AgentContextBuilder(
                    AgentContextRules("stable", "safe", "final"),
                    transient_result_resolver=(
                        AgentTransientSkillResultResolver(store)
                    ),
                ),
                token_counter=_count_characters,
            ).build(
                run=run,
                items=(user, assistant, call, result),
                catalog=AgentToolCatalog((), {}),
            )

        self.assertEqual(
            candidate.preflight.decision, AgentContextPreflightDecision.FITS
        )
        self.assertGreater(candidate.preflight.transient_tokens, 150_000)
        self.assertEqual(candidate.preflight.history_tokens, 0)
        self.assertLessEqual(
            candidate.preflight.total_tokens,
            candidate.preflight.total_context_limit_tokens,
        )
        rendered = "\n".join(
            message.content or "" for message in candidate.request.messages
        )
        self.assertIn("BEGIN", rendered)
        self.assertIn("END", rendered)
        self.assertNotIn("stage_ref", rendered)

    async def test_unconsumed_artifact_backed_result_is_preloaded_and_required_once(
        self,
    ) -> None:
        run = _run(5)
        user = _user(300_000, "question")
        assistant = _item(
            "assistant-1", 2, AgentItemKind.ASSISTANT_MESSAGE, {"text": ""}
        )
        call = _item(
            "call-1",
            3,
            AgentItemKind.TOOL_CALL,
            {
                "arguments_json": "{}",
                "call_id": "provider-call-1",
                "capability_id": "skill.lookup",
                "node_id": "node-1",
                "provider_safe_name": "skill_lookup",
            },
            parent_item_id=assistant.item_id,
            call_ordinal=0,
        )
        raw_payload = {"records": ["BEGIN", "x" * 150_000, "END"]}
        projection = AgentCallResultProjector().project(
            capability_id="skill.lookup",
            output_payload=raw_payload,
            call_item_id=call.item_id,
            outcome="completed",
            safe_error_code=None,
            artifact_ids=("business-artifact",),
            skill_projection_policy=(
                SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_LEGACY
            ),
        )
        result = _item(
            "result-1",
            4,
            AgentItemKind.TOOL_RESULT,
            {
                "artifact_refs": [
                    "business-artifact",
                    projection.spill_artifact_id,
                ],
                "call_item_id": call.item_id,
                "outcome": "completed",
                "safe_error_code": None,
                "safe_result": projection.safe_result_payload,
            },
            parent_item_id=assistant.item_id,
            source_call_item_id=call.item_id,
        )
        artifact = Artifact(
            artifact_id=projection.spill_artifact_id,
            task_id=run.task_id,
            producer_node_id="node-1",
            artifact_type=ArtifactType.FILE,
            storage_ref="{}",
            is_complete=True,
        )
        loaded: list[str] = []

        async def load_artifact(artifact_id: str):
            loaded.append(artifact_id)
            return artifact

        class Resolver:
            @staticmethod
            def resolve_tool_result(**_kwargs):
                return {
                    "artifact_refs": [
                        "business-artifact",
                        projection.spill_artifact_id,
                    ],
                    "outcome": "completed",
                    "safe_error_code": None,
                    "safe_result": {
                        "schema": "maf.agent.skill_result_full.v1",
                        "result": raw_payload,
                    },
                }

        candidate = await AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "safe", "final"),
                skill_result_artifact_resolver=Resolver(),
            ),
            token_counter=_count_characters,
            skill_result_artifact_loader=load_artifact,
        ).build(
            run=run,
            items=(user, assistant, call, result),
            catalog=AgentToolCatalog((), {}),
        )

        self.assertEqual(loaded, [projection.spill_artifact_id])
        self.assertEqual(candidate.preflight.history_tokens, 0)
        self.assertGreater(candidate.preflight.transient_tokens, 150_000)
        rendered = "\n".join(
            message.content or "" for message in candidate.request.messages
        )
        self.assertIn("BEGIN", rendered)
        self.assertIn("END", rendered)
        self.assertIn("business-artifact", rendered)

        with self.assertRaisesRegex(
            ValueError, "agent_skill_result_artifact_unavailable"
        ):
            await AgentContextCandidateBuilder(
                context_builder=AgentContextBuilder(
                    AgentContextRules("stable", "safe", "final"),
                    skill_result_artifact_resolver=Resolver(),
                ),
                token_counter=_count_characters,
            ).build(
                run=run,
                items=(user, assistant, call, result),
                catalog=AgentToolCatalog((), {}),
            )

        loaded.clear()
        summary = _item(
            "summary-1",
            5,
            AgentItemKind.CONTEXT_SUMMARY,
            {"covered_end_sequence": 4, "summary": "closed history"},
        )
        await AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "safe", "final"),
                skill_result_artifact_resolver=Resolver(),
            ),
            token_counter=_count_characters,
            skill_result_artifact_loader=load_artifact,
        ).build(
            run=replace(
                run,
                next_item_sequence=6,
                compacted_through_sequence=4,
            ),
            items=(user, assistant, call, result, summary),
            catalog=AgentToolCatalog((), {}),
        )
        self.assertEqual(loaded, [])

        current_assistant = _item(
            "assistant-current",
            6,
            AgentItemKind.ASSISTANT_MESSAGE,
            {"text": ""},
        )
        current_call = _item(
            "call-current",
            7,
            AgentItemKind.TOOL_CALL,
            {
                "arguments_json": "{}",
                "call_id": "provider-current",
                "capability_id": "skill.lookup",
                "node_id": "node-current",
                "provider_safe_name": "skill_lookup",
            },
            parent_item_id=current_assistant.item_id,
            call_ordinal=0,
        )
        current_result = _item(
            "result-current",
            8,
            AgentItemKind.TOOL_RESULT,
            {
                "artifact_refs": [],
                "call_item_id": current_call.item_id,
                "outcome": "completed",
                "safe_error_code": None,
                "safe_result": build_tool_result_reuse_receipt(
                    source_result_item_id=result.item_id,
                    source_result_payload_sha256=result.payload_sha256,
                ),
            },
            parent_item_id=current_assistant.item_id,
            source_call_item_id=current_call.item_id,
        )
        loaded.clear()
        reused = await AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "safe", "final"),
                skill_result_artifact_resolver=Resolver(),
            ),
            token_counter=_count_characters,
            skill_result_artifact_loader=load_artifact,
        ).build(
            run=replace(
                run,
                next_item_sequence=9,
                compacted_through_sequence=4,
            ),
            items=(
                user,
                assistant,
                call,
                result,
                summary,
                current_assistant,
                current_call,
                current_result,
            ),
            catalog=AgentToolCatalog((), {}),
        )
        self.assertEqual(loaded, [projection.spill_artifact_id])
        self.assertEqual(reused.preflight.history_tokens, 0)
        self.assertGreater(reused.preflight.required_tokens, 150_000)
