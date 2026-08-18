from __future__ import annotations

import unittest
from copy import deepcopy

from src.core.enums import EdgeType
from src.core.models import Task, TaskEdge, TaskInputAttachment, TaskNode
from src.integrations.mcp.cp7_artifacts import canonical_json_bytes
from src.integrations.mcp.resume_envelope import (
    MCPDispatchResumeEnvelopeError,
    MCP_DISPATCH_RESUME_ENVELOPE_SCHEMA_V2,
    build_mcp_dispatch_resume_envelope_v2,
    mcp_dispatch_resume_envelope_version,
    validate_mcp_dispatch_resume_envelope_v2,
)


class MCPDispatchResumeEnvelopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.task = Task(
            task_id="task-a",
            conversation_id="conversation-a",
            root_message_id="message-a",
            mcp_execution_mode="user_scoped",
            mcp_shadow_enabled=False,
            mcp_rollout_config_version="rollout-a",
            mcp_route_reason_code="enforce_selected",
            mcp_rollout_mode="enforce",
        )
        self.node = TaskNode(
            node_id="node-b",
            task_id="task-a",
            capability_id="mcp.dispatch",
        )
        self.dependency = TaskNode(
            node_id="node-a",
            task_id="task-a",
            capability_id="main.agent",
            output_refs=("artifact-b", "artifact-a"),
        )

    def _build(self, **overrides):
        values = {
            "task": self.task,
            "node": self.node,
            "edges": (
                TaskEdge("node-a", "node-b", EdgeType.DATA),
            ),
            "attachments": (),
            "dependency_nodes": (self.dependency,),
            "server_id": "server-a",
        }
        values.update(overrides)
        return build_mcp_dispatch_resume_envelope_v2(**values)

    def test_large_attachment_body_is_not_copied_into_v2(self) -> None:
        attachment = TaskInputAttachment(
            attachment_id="attachment-a",
            task_id="task-a",
            conversation_id="conversation-a",
            source_kind="upload",
            skill_artifact={"content_base64": "x" * 2_326_771},
            source_payload={"content_base64": "y" * 2_326_771},
        )

        envelope = self._build(attachments=(attachment,))
        rendered = canonical_json_bytes(envelope)

        self.assertEqual(
            envelope["schema"], MCP_DISPATCH_RESUME_ENVELOPE_SCHEMA_V2
        )
        self.assertEqual(envelope["input_attachment_ids"], ["attachment-a"])
        self.assertNotIn(b"content_base64", rendered)
        self.assertNotIn(b"xxxx", rendered)
        self.assertLess(len(rendered), 4 * 1024)

    def test_builder_sorts_dependency_and_artifact_ids(self) -> None:
        dependency_b = TaskNode(
            node_id="node-z",
            task_id="task-a",
            capability_id="main.agent",
            output_refs=("artifact-z",),
        )

        envelope = self._build(
            dependency_nodes=(dependency_b, self.dependency)
        )

        self.assertEqual(
            envelope["dependency_output_refs"],
            [
                {
                    "node_id": "node-a",
                    "artifact_ids": ["artifact-a", "artifact-b"],
                },
                {"node_id": "node-z", "artifact_ids": ["artifact-z"]},
            ],
        )

    def test_nested_forbidden_field_is_rejected(self) -> None:
        envelope = self._build()
        envelope["node_snapshot"]["retry_policy"] = {
            "nested": {"metadata": {"secret": "value"}}
        }

        with self.assertRaisesRegex(
            MCPDispatchResumeEnvelopeError,
            "mcp_dispatch_resume_envelope_forbidden_field",
        ):
            validate_mcp_dispatch_resume_envelope_v2(envelope)

    def test_canonical_limit_remains_64_kib(self) -> None:
        oversized_node = TaskNode(
            node_id="node-b",
            task_id="task-a",
            capability_id="mcp.dispatch",
            retry_policy={"backoff_note": "x" * (64 * 1024)},
        )

        with self.assertRaisesRegex(
            MCPDispatchResumeEnvelopeError,
            "mcp_target_intent_resume_envelope_too_large",
        ):
            self._build(node=oversized_node)

    def test_unknown_field_is_rejected(self) -> None:
        envelope = self._build()
        envelope["unexpected"] = True

        with self.assertRaisesRegex(
            MCPDispatchResumeEnvelopeError,
            "mcp_dispatch_resume_envelope_invalid",
        ):
            validate_mcp_dispatch_resume_envelope_v2(envelope)

    def test_assignment_is_validated_without_constant_overwrite(self) -> None:
        damaged_task = Task(
            task_id="task-a",
            conversation_id="conversation-a",
            root_message_id="message-a",
            mcp_execution_mode="user_scoped",
            mcp_shadow_enabled=True,
            mcp_rollout_config_version="rollout-a",
            mcp_route_reason_code="enforce_selected",
            mcp_rollout_mode="enforce",
        )

        with self.assertRaisesRegex(
            MCPDispatchResumeEnvelopeError,
            "mcp_target_intent_task_assignment_invalid",
        ):
            self._build(task=damaged_task)

    def test_unsorted_ids_are_rejected_by_parser(self) -> None:
        envelope = deepcopy(self._build())
        envelope["dependency_output_refs"][0]["artifact_ids"] = [
            "artifact-b",
            "artifact-a",
        ]

        with self.assertRaisesRegex(
            MCPDispatchResumeEnvelopeError,
            "mcp_dispatch_resume_envelope_invalid",
        ):
            validate_mcp_dispatch_resume_envelope_v2(envelope)

    def test_schema_dispatch_keeps_legacy_and_rejects_unknown(self) -> None:
        self.assertEqual(
            mcp_dispatch_resume_envelope_version({"task_id": "legacy"}),
            "legacy_v1",
        )
        with self.assertRaisesRegex(
            MCPDispatchResumeEnvelopeError,
            "mcp_dispatch_resume_envelope_schema_unsupported",
        ):
            mcp_dispatch_resume_envelope_version({"schema": "future"})


if __name__ == "__main__":
    unittest.main()
