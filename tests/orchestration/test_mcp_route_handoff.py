from __future__ import annotations

import unittest

from src.orchestration.mcp_route_handoff import (
    MCP_SELECTED_ROUTE_NOT_AUTHORIZED,
    normalize_selected_mcp_route,
)


class MCPRouteHandoffTest(unittest.TestCase):
    def test_non_mcp_capability_is_unchanged(self) -> None:
        metadata = {"mcp_binding_mode": "automatic", "custom": {"value": 1}}

        result = normalize_selected_mcp_route(
            capability_id="main.agent",
            input_payload={"server_id": "server-a"},
            node_metadata=metadata,
            pinned_server_id_present=False,
            pinned_server_id=None,
            available_server_ids=frozenset(),
        )

        self.assertEqual(result.normalized_node_metadata, metadata)
        self.assertIsNone(result.rejection_code)
        self.assertEqual(metadata, {"mcp_binding_mode": "automatic", "custom": {"value": 1}})

    def test_malformed_mcp_payload_is_left_for_existing_validation(self) -> None:
        cases = (
            {},
            {"server_id": ""},
            {"server_id": None},
            {"server_id": "server-a", "unexpected": True},
        )
        metadata = {"mcp_binding_mode": "automatic"}

        for payload in cases:
            with self.subTest(payload=payload):
                result = normalize_selected_mcp_route(
                    capability_id="mcp.dispatch",
                    input_payload=payload,
                    node_metadata=metadata,
                    pinned_server_id_present=True,
                    pinned_server_id="server-a",
                    available_server_ids=frozenset({"server-a"}),
                )

                self.assertEqual(result.normalized_node_metadata, metadata)
                self.assertIsNone(result.rejection_code)

    def test_pinned_server_takes_precedence_over_allowlist(self) -> None:
        accepted = normalize_selected_mcp_route(
            capability_id="mcp.dispatch",
            input_payload={"server_id": " server-a "},
            node_metadata={"mcp_binding_mode": "automatic"},
            pinned_server_id_present=True,
            pinned_server_id=" server-a ",
            available_server_ids=frozenset(),
        )
        rejected = normalize_selected_mcp_route(
            capability_id="mcp.dispatch",
            input_payload={"server_id": "server-a"},
            node_metadata={"mcp_binding_mode": "automatic"},
            pinned_server_id_present=True,
            pinned_server_id="server-b",
            available_server_ids=frozenset({"server-a"}),
        )

        self.assertIsNone(accepted.rejection_code)
        self.assertEqual(rejected.rejection_code, MCP_SELECTED_ROUTE_NOT_AUTHORIZED)

    def test_invalid_present_pinned_server_never_falls_back_to_allowlist(self) -> None:
        for pinned_server_id in (None, 123, "", "   "):
            with self.subTest(pinned_server_id=pinned_server_id):
                result = normalize_selected_mcp_route(
                    capability_id="mcp.dispatch",
                    input_payload={"server_id": "server-a"},
                    node_metadata={},
                    pinned_server_id_present=True,
                    pinned_server_id=pinned_server_id,
                    available_server_ids=frozenset({"server-a"}),
                )

                self.assertEqual(
                    result.rejection_code,
                    MCP_SELECTED_ROUTE_NOT_AUTHORIZED,
                )

    def test_automatic_server_must_be_in_nonempty_allowlist(self) -> None:
        accepted = normalize_selected_mcp_route(
            capability_id="mcp.dispatch",
            input_payload={"server_id": " server-a "},
            node_metadata={},
            pinned_server_id_present=False,
            pinned_server_id=None,
            available_server_ids=frozenset({"server-a"}),
        )

        self.assertIsNone(accepted.rejection_code)
        for available_server_ids in (frozenset(), frozenset({"server-b"})):
            with self.subTest(available_server_ids=available_server_ids):
                rejected = normalize_selected_mcp_route(
                    capability_id="mcp.dispatch",
                    input_payload={"server_id": "server-a"},
                    node_metadata={},
                    pinned_server_id_present=False,
                    pinned_server_id=None,
                    available_server_ids=available_server_ids,
                )
                self.assertEqual(
                    rejected.rejection_code,
                    MCP_SELECTED_ROUTE_NOT_AUTHORIZED,
                )

    def test_success_creates_canonical_metadata_without_mutating_inputs(self) -> None:
        input_payload = {"server_id": " server-a "}
        node_metadata = {
            "mcp_binding_mode": "automatic",
            "mcp_dispatch_server_id": "server-a",
            "forced_by_mcp_command": True,
            "mcp_command": "$OCR",
            "custom": "keep",
        }

        result = normalize_selected_mcp_route(
            capability_id="mcp.dispatch",
            input_payload=input_payload,
            node_metadata=node_metadata,
            pinned_server_id_present=False,
            pinned_server_id=None,
            available_server_ids=frozenset({"server-a"}),
        )

        self.assertEqual(
            result.normalized_node_metadata,
            {"mcp_binding_mode": "explicit_command", "custom": "keep"},
        )
        self.assertIsNone(result.rejection_code)
        self.assertEqual(input_payload, {"server_id": " server-a "})
        self.assertEqual(
            node_metadata,
            {
                "mcp_binding_mode": "automatic",
                "mcp_dispatch_server_id": "server-a",
                "forced_by_mcp_command": True,
                "mcp_command": "$OCR",
                "custom": "keep",
            },
        )


if __name__ == "__main__":
    unittest.main()
