from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.core.enums import ArtifactType
from src.core.models import Artifact
from src.orchestration.agent_loop.context import AgentContextBuilder, AgentContextRules
from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentToolDescriptor,
)
from src.orchestration.agent_loop.tool_catalog import AgentToolCatalog
from src.orchestration.agent_loop.result_projection import (
    SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_LEGACY,
    SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT,
    build_tool_result_reuse_receipt,
)
from src.orchestration.agent_loop.result_artifacts import (
    AgentSkillResultArtifactResolver,
    AgentSkillResultArtifactStager,
)
from src.orchestration.agent_loop.transient_results import (
    AgentTransientSkillResultResolver,
    AgentTransientSkillResultStore,
)
from src.storage.artifact_files import LocalArtifactFileStore
from tests.orchestration.support import make_agent_result_projector


def _item(
    item_id: str,
    sequence: int,
    kind: AgentItemKind,
    payload: dict,
    **values,
) -> AgentItem:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    return AgentItem(
        item_id,
        "run-1",
        "task-1",
        sequence,
        kind,
        values.pop("state", AgentItemState.COMMITTED),
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        **values,
    )


class AgentContextBuilderTest(unittest.IsolatedAsyncioTestCase):
    def test_budgeted_initial_user_is_reinserted_once_after_compaction(self) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
            next_item_sequence=3,
            compacted_through_sequence=1,
            revision=2,
        )
        user = _item(
            "user",
            1,
            AgentItemKind.USER_MESSAGE,
            {
                "context_budget": {
                    "compact_threshold_percent": 90,
                    "model_context_window_tokens": 450_000,
                    "policy_revision": "maf.agent.total_context_budget.v1",
                    "total_context_limit_tokens": 405_000,
                },
                "text": "exact current question",
            },
        )
        summary = _item(
            "summary",
            2,
            AgentItemKind.CONTEXT_SUMMARY,
            {"covered_end_sequence": 1, "summary": "history only"},
        )

        request = AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        ).build(
            run=run,
            items=(user, summary),
            catalog=AgentToolCatalog((), {}),
            current_user_input="exact current question",
        )

        self.assertEqual(
            [
                message.content
                for message in request.messages
                if message.role == "user"
            ],
            ["exact current question"],
        )

    async def test_transient_receipt_is_resolved_to_full_model_only_result(self) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
            next_item_sequence=5,
            revision=4,
        )
        user = _item("user", 1, AgentItemKind.USER_MESSAGE, {"text": "question"})
        assistant = _item(
            "assistant", 2, AgentItemKind.ASSISTANT_MESSAGE, {"text": ""}
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
        raw_payload = {"records": ["BEGIN-SENTINEL", "x" * 150_000, "END-SENTINEL"]}
        projection = await make_agent_result_projector().project(
            capability_id="skill.lookup",
            output_payload=raw_payload,
            call_item_id=call.item_id,
            outcome="completed",
            safe_error_code=None,
            skill_projection_policy=(
                SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT
            ),
            model_edition="edition-a",
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
                canonical_raw_bytes=projection.transient_content_bytes,
                raw_sha256=projection.transient_content_sha256,
                projection_revision=projection.projection_revision,
                expected_stage_ref=projection.transient_stage_ref,
            )
            builder = AgentContextBuilder(
                AgentContextRules("stable", "tool rules", "final guard"),
                transient_result_resolver=AgentTransientSkillResultResolver(
                    store
                ),
            )
            first = builder.build(
                run=run,
                items=(user, assistant, call, result),
                catalog=AgentToolCatalog((), {}),
            )
            replay = builder.build(
                run=run,
                items=(user, assistant, call, result),
                catalog=AgentToolCatalog((), {}),
            )
            store.delete_stage(
                projection.transient_stage_ref,
                expected_run_id=run.run_id,
                expected_result_item_id=result.item_id,
            )
            summary = _item(
                "summary",
                5,
                AgentItemKind.CONTEXT_SUMMARY,
                {"covered_end_sequence": 4, "summary": "closed result"},
            )
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
            summary_only = builder.build(
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

        self.assertEqual(first, replay)
        tool_content = next(
            message.content for message in first.messages if message.role == "tool"
        )
        self.assertIn("BEGIN-SENTINEL", tool_content or "")
        self.assertIn("END-SENTINEL", tool_content or "")
        self.assertIn("maf.agent.skill_result_full.v1", tool_content or "")
        self.assertNotIn("stage_ref", tool_content or "")
        self.assertNotIn("pending_context_injection", tool_content or "")
        summary_content = next(
            message.content
            for message in summary_only.messages
            if message.role == "tool"
        )
        self.assertIn("duplicate_call_suppressed", summary_content or "")
        self.assertIn("context_summary_only", summary_content or "")
        self.assertNotIn("BEGIN-SENTINEL", summary_content or "")

    async def test_artifact_backed_result_is_full_only_with_preloaded_authority(
        self,
    ) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
            next_item_sequence=5,
            revision=4,
        )
        user = _item(
            "user",
            1,
            AgentItemKind.USER_MESSAGE,
            {
                "context_budget": {
                    "compact_threshold_percent": 90,
                    "model_context_window_tokens": 450_000,
                    "policy_revision": "maf.agent.total_context_budget.v1",
                    "total_context_limit_tokens": 405_000,
                },
                "text": "question",
            },
        )
        assistant = _item(
            "assistant", 2, AgentItemKind.ASSISTANT_MESSAGE, {"text": ""}
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
        raw_payload = {
            "records": ["BEGIN-ARTIFACT", "x" * 150_000, "END-ARTIFACT"]
        }
        projection = await make_agent_result_projector().project(
            capability_id="skill.lookup",
            output_payload=raw_payload,
            call_item_id=call.item_id,
            outcome="completed",
            safe_error_code=None,
            artifact_ids=("business-artifact",),
            skill_projection_policy=(
                SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_LEGACY
            ),
            model_edition="edition-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            file_store = LocalArtifactFileStore(Path(directory) / "artifacts")
            staged = AgentSkillResultArtifactStager(
                file_store=file_store,
                manifest_root=Path(directory) / "manifests",
            ).stage(
                run=run,
                call_item=call,
                node_id="node-1",
                canonical_raw_bytes=projection.spill_content_bytes,
                raw_sha256=projection.spill_content_sha256,
                projection_revision=projection.projection_revision,
                expected_artifact_id=projection.spill_artifact_id,
            )
            artifact = Artifact(
                artifact_id=staged.artifact_id,
                task_id=run.task_id,
                producer_node_id="node-1",
                artifact_type=ArtifactType.FILE,
                storage_ref=staged.storage_ref,
                is_complete=True,
            )
            result = _item(
                "result-1",
                4,
                AgentItemKind.TOOL_RESULT,
                {
                    "artifact_refs": [
                        "business-artifact",
                        staged.artifact_id,
                    ],
                    "call_item_id": call.item_id,
                    "outcome": "completed",
                    "safe_error_code": None,
                    "safe_result": projection.safe_result_payload,
                },
                parent_item_id=assistant.item_id,
                source_call_item_id=call.item_id,
            )
            builder = AgentContextBuilder(
                AgentContextRules("stable", "tool rules", "final guard"),
                skill_result_artifact_resolver=(
                    AgentSkillResultArtifactResolver(file_store)
                ),
            )
            resolved = builder.build(
                run=run,
                items=(user, assistant, call, result),
                catalog=AgentToolCatalog((), {}),
                skill_result_artifacts={staged.artifact_id: artifact},
            )
            legacy = builder.build(
                run=run,
                items=(user, assistant, call, result),
                catalog=AgentToolCatalog((), {}),
            )
            summary = _item(
                "summary",
                5,
                AgentItemKind.CONTEXT_SUMMARY,
                {"covered_end_sequence": 4, "summary": "closed history"},
            )
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
            reused = builder.build(
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
                skill_result_artifacts={staged.artifact_id: artifact},
            )
            with self.assertRaisesRegex(
                ValueError, "agent_reused_tool_result_unavailable"
            ):
                builder.build(
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
                    skill_result_artifacts={staged.artifact_id: None},
                )

        resolved_content = next(
            message.content
            for message in resolved.messages
            if message.role == "tool"
        )
        legacy_content = next(
            message.content
            for message in legacy.messages
            if message.role == "tool"
        )
        reused_content = next(
            message.content
            for message in reused.messages
            if message.role == "tool"
        )
        self.assertIn("BEGIN-ARTIFACT", resolved_content or "")
        self.assertIn("END-ARTIFACT", resolved_content or "")
        self.assertIn("business-artifact", resolved_content or "")
        self.assertIn(staged.artifact_id, resolved_content or "")
        self.assertNotIn("BEGIN-ARTIFACT", legacy_content or "")
        self.assertIn("skill_result_projection_receipt", legacy_content or "")
        self.assertIn("BEGIN-ARTIFACT", reused_content or "")
        self.assertIn("END-ARTIFACT", reused_content or "")
        self.assertIn("business-artifact", reused_content or "")

    def test_reuse_receipt_renders_root_payload_for_current_provider_call(
        self,
    ) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
            next_item_sequence=8,
            revision=7,
        )
        first_assistant = _item(
            "assistant-1", 1, AgentItemKind.ASSISTANT_MESSAGE, {"text": ""}
        )
        root_call = _item(
            "call-root",
            2,
            AgentItemKind.TOOL_CALL,
            {
                "arguments_json": '{"query":"rice"}',
                "call_id": "provider-root",
                "capability_id": "skill.lookup",
                "node_id": "node-root",
                "provider_safe_name": "skill_lookup",
            },
            parent_item_id=first_assistant.item_id,
            call_ordinal=0,
        )
        root_result = _item(
            "result-root",
            3,
            AgentItemKind.TOOL_RESULT,
            {
                "artifact_refs": ["business-artifact"],
                "call_item_id": root_call.item_id,
                "outcome": "completed",
                "safe_error_code": None,
                "safe_result": {"answer": "ROOT-ANSWER"},
            },
            parent_item_id=first_assistant.item_id,
            source_call_item_id=root_call.item_id,
        )
        second_assistant = _item(
            "assistant-2", 4, AgentItemKind.ASSISTANT_MESSAGE, {"text": ""}
        )
        current_call = _item(
            "call-current",
            5,
            AgentItemKind.TOOL_CALL,
            {
                "arguments_json": '{"query":"rice"}',
                "call_id": "provider-current",
                "capability_id": "skill.lookup",
                "node_id": "node-current",
                "provider_safe_name": "skill_lookup",
            },
            parent_item_id=second_assistant.item_id,
            call_ordinal=0,
        )
        current_result = _item(
            "result-current",
            6,
            AgentItemKind.TOOL_RESULT,
            {
                "artifact_refs": [],
                "call_item_id": current_call.item_id,
                "outcome": "completed",
                "safe_error_code": None,
                "safe_result": build_tool_result_reuse_receipt(
                    source_result_item_id=root_result.item_id,
                    source_result_payload_sha256=root_result.payload_sha256,
                ),
            },
            parent_item_id=second_assistant.item_id,
            source_call_item_id=current_call.item_id,
        )

        request = AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        ).build(
            run=run,
            items=(
                first_assistant,
                root_call,
                root_result,
                second_assistant,
                current_call,
                current_result,
            ),
            catalog=AgentToolCatalog((), {}),
        )

        tool_messages = [
            message for message in request.messages if message.role == "tool"
        ]
        self.assertEqual(tool_messages[-1].tool_call_id, "provider-current")
        self.assertIn("ROOT-ANSWER", tool_messages[-1].content or "")
        self.assertIn("business-artifact", tool_messages[-1].content or "")
        self.assertNotIn("source_result_item_id", tool_messages[-1].content or "")

    def test_initial_hint_activation_renders_before_current_user_message(self) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
            next_item_sequence=3,
            revision=1,
        )
        user = _item(
            "user",
            1,
            AgentItemKind.USER_MESSAGE,
            {
                "context_budget": {
                    "compact_threshold_percent": 90,
                    "model_context_window_tokens": 450_000,
                    "policy_revision": "maf.agent.total_context_budget.v1",
                    "total_context_limit_tokens": 405_000,
                },
                "text": "what is this",
            },
        )
        activation = _item(
            "activation",
            2,
            AgentItemKind.SKILL_ACTIVATION,
            {
                "binding_mode": "hint",
                "pinned_bundle_revision": "revision-1",
                "profile": {"capability_id": "skill.one", "description": "safe"},
                "profile_digest": "a" * 64,
            },
        )

        request = AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        ).build(
            run=run,
            items=(user, activation),
            catalog=AgentToolCatalog((), {}),
        )

        self.assertEqual([message.role for message in request.messages], ["system", "system", "system", "user", "system"])
        hint = json.loads(request.messages[2].content or "{}")
        self.assertIn("Selection does not mean execution", hint["instruction"])
        self.assertEqual(
            hint["skill_activation"]["profile"]["capability_id"],
            "skill.one",
        )
        self.assertEqual(request.messages[3].content, "what is this")
        self.assertNotIn("context_budget", request.messages[3].content or "")

    def test_recovered_summary_and_skill_activation_keep_order_and_become_system_messages(self) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
            next_item_sequence=4,
            compacted_through_sequence=1,
            revision=3,
        )
        old_user = _item("user", 1, AgentItemKind.USER_MESSAGE, {"text": "old"})
        summary = _item(
            "summary",
            2,
            AgentItemKind.CONTEXT_SUMMARY,
            {"covered_end_sequence": 1, "summary": "compressed"},
        )
        activation = _item(
            "activation",
            3,
            AgentItemKind.SKILL_ACTIVATION,
            {"skill_id": "skill.one"},
        )

        request = AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        ).build(
            run=run,
            items=(activation, old_user, summary),
            catalog=AgentToolCatalog((), {}),
        )

        self.assertEqual(
            [(message.role, message.content) for message in request.messages],
            [
                ("system", "stable"),
                ("system", "tool rules"),
                ("system", '{"covered_end_sequence":1,"summary":"compressed"}'),
                ("system", '{"skill_id":"skill.one"}'),
                ("system", "final guard"),
            ],
        )

    def test_multi_call_sample_is_rebuilt_as_one_assistant_message_then_ordered_results(self) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
            next_item_sequence=6,
            revision=3,
        )
        assistant = _item("assistant", 1, AgentItemKind.ASSISTANT_MESSAGE, {"text": ""})
        call_two = _item(
            "call-2",
            4,
            AgentItemKind.TOOL_CALL,
            {"call_id": "provider-2", "provider_safe_name": "tool_two", "arguments_json": "{}"},
            parent_item_id="assistant",
            call_ordinal=1,
        )
        call_one = _item(
            "call-1",
            2,
            AgentItemKind.TOOL_CALL,
            {"call_id": "provider-1", "provider_safe_name": "tool_one", "arguments_json": "{}"},
            parent_item_id="assistant",
            call_ordinal=0,
        )
        result_one = _item(
            "result-1",
            3,
            AgentItemKind.TOOL_RESULT,
            {"outcome": "completed", "safe_result": {"value": 1}},
            parent_item_id="assistant",
            source_call_item_id="call-1",
        )
        result_two = _item(
            "result-2",
            5,
            AgentItemKind.TOOL_RESULT,
            {"outcome": "failed", "safe_error_code": "ordinary"},
            parent_item_id="assistant",
            source_call_item_id="call-2",
        )
        catalog = AgentToolCatalog(
            (
                AgentToolDescriptor.for_capability(
                    "skill.one", description="one", input_schema={"type": "object"}
                ),
            ),
            {},
        )
        request = AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        ).build(
            run=run,
            items=(assistant, call_two, call_one, result_one, result_two),
            catalog=catalog,
            trusted_facts=("fact",),
        )

        assistant_messages = [message for message in request.messages if message.role == "assistant"]
        self.assertEqual(len(assistant_messages), 1)
        self.assertEqual(
            [call.call_id for call in assistant_messages[0].tool_calls],
            ["provider-1", "provider-2"],
        )
        tool_messages = [message for message in request.messages if message.role == "tool"]
        self.assertEqual(
            [message.tool_call_id for message in tool_messages],
            ["provider-1", "provider-2"],
        )
        self.assertEqual(request.messages[0].content, "stable")
        self.assertEqual(request.messages[-1].content, "final guard")
        self.assertEqual(
            [(message.role, message.content) for message in request.messages if message.role == "system"],
            [
                ("system", "stable"),
                ("system", "tool rules"),
                ("system", '{"trusted_facts":["fact"]}'),
                ("system", "final guard"),
            ],
        )
        self.assertNotIn("developer", {message.role for message in request.messages})
        self.assertEqual(request.binding, run.binding)
