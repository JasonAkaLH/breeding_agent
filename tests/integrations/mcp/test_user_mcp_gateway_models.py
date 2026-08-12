from __future__ import annotations

import unittest

from src.integrations.mcp.gateway_models import (
    MCPCallOutcome,
    MCPCallOutcomeKind,
    MCPToolDescriptor,
    ToolCatalogSnapshot,
)


class UserMCPGatewayModelsTest(unittest.TestCase):
    def test_tool_catalog_defensively_freezes_mappings(self) -> None:
        source = {"type": "object"}
        tool = MCPToolDescriptor(
            name="lookup",
            description="Lookup",
            input_schema=source,
            input_schema_sha256="abc",
        )
        source["type"] = "array"
        catalog = ToolCatalogSnapshot("srv", "2025-11-25", (tool,))

        self.assertEqual(catalog.get("lookup").input_schema["type"], "object")
        with self.assertRaises(TypeError):
            tool.input_schema["type"] = "string"  # type: ignore[index]

    def test_outcome_constructors_keep_protocol_state_opaque(self) -> None:
        completed = MCPCallOutcome.completed("result-ref")
        input_required = MCPCallOutcome.input_required(({"message": "value required"},), "sealed-ref")
        task_created = MCPCallOutcome.task_created("task-ref", status="working")

        self.assertEqual(completed.kind, MCPCallOutcomeKind.COMPLETED)
        self.assertEqual(input_required.sealed_request_state_ref, "sealed-ref")
        self.assertEqual(task_created.safe_remote_task_ref, "task-ref")

    def test_completed_outcome_includes_safe_result_metadata(self) -> None:
        completed = MCPCallOutcome.completed(
            "result-ref",
            content_type="application/json",
            byte_size=42,
        )

        self.assertEqual(completed.result_ref, "result-ref")
        self.assertEqual(completed.content_type, "application/json")
        self.assertEqual(completed.byte_size, 42)
