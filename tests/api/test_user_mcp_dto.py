from __future__ import annotations

import unittest

from pydantic import ValidationError

from src.api.dto import (
    CreateUserMCPServerRequest,
    MCPBusinessResultView,
    PatchUserMCPServerRequest,
    SubmitMessageRequest,
    UserMCPCredentialInput,
)


class UserMCPDTOTest(unittest.TestCase):
    def test_mcp_business_result_view_rejects_non_json_without_size_budget(self) -> None:
        base = {
            "schema": "maf.mcp.business_result_view.v1",
            "availability": "ready",
            "outcome": "succeeded",
            "projection_truncated": False,
        }
        with self.assertRaises(ValidationError):
            MCPBusinessResultView.model_validate(
                {**base, "primary": {"kind": "structured", "value": float("nan"), "truncated": False}}
            )
        view = MCPBusinessResultView.model_validate(
            {
                **base,
                "primary": {
                    "kind": "text",
                    "text": "x" * 220_000,
                    "truncated": False,
                },
            }
        )
        self.assertEqual(len(view.primary.text), 220_000)

    def test_create_rejects_owner_fields_and_requires_auth_credential(self) -> None:
        with self.assertRaises(ValidationError):
            CreateUserMCPServerRequest.model_validate(
                {
                    "owner_user_id": "mallory",
                    "display_name": "Demo",
                    "endpoint_url": "https://mcp.example.test/rpc",
                }
            )
        with self.assertRaisesRegex(ValidationError, "configured auth requires a credential"):
            CreateUserMCPServerRequest(
                display_name="Demo",
                routing_description="Demo routing",
                endpoint_url="https://mcp.example.test/rpc",
                auth_type="bearer",
            )

    def test_routing_description_is_required_for_create_and_when_present_on_patch(self) -> None:
        create_payload = {
            "display_name": "Demo",
            "endpoint_url": "https://mcp.example.test/rpc",
        }
        for invalid_fields in (
            {},
            {"routing_description": None},
            {"routing_description": ""},
            {"routing_description": " \t\n "},
        ):
            with self.subTest(create_fields=invalid_fields), self.assertRaises(ValidationError):
                CreateUserMCPServerRequest.model_validate(
                    {**create_payload, **invalid_fields}
                )

        created = CreateUserMCPServerRequest.model_validate(
            {**create_payload, "routing_description": "  Query breeding data  "}
        )
        self.assertEqual(created.routing_description, "Query breeding data")

        omitted = PatchUserMCPServerRequest(display_name="Renamed")
        self.assertNotIn("routing_description", omitted.model_dump(exclude_unset=True))
        for invalid in (None, "", " \t\n "):
            with self.subTest(patch_value=invalid), self.assertRaises(ValidationError):
                PatchUserMCPServerRequest(routing_description=invalid)

        patched = PatchUserMCPServerRequest(routing_description="  Updated route  ")
        self.assertEqual(patched.routing_description, "Updated route")

    def test_secret_values_are_masked_in_repr_and_dump(self) -> None:
        canary = "mcp-secret-canary"
        credential = UserMCPCredentialInput(secret_value=canary)

        self.assertNotIn(canary, repr(credential))
        self.assertNotIn(canary, str(credential.model_dump()))
        self.assertEqual(credential.secret_value.get_secret_value(), canary)

    def test_patch_credential_action_is_explicit_three_state(self) -> None:
        retained = PatchUserMCPServerRequest(display_name="Renamed")
        cleared = PatchUserMCPServerRequest(credential_action="clear")
        replaced = PatchUserMCPServerRequest(
            credential_action="replace",
            credential=UserMCPCredentialInput(secret_value="new-secret"),
        )

        self.assertEqual(retained.credential_action, "retain")
        self.assertEqual(cleared.credential_action, "clear")
        self.assertEqual(replaced.credential_action, "replace")
        with self.assertRaises(ValidationError):
            PatchUserMCPServerRequest(credential=UserMCPCredentialInput(secret_value="implicit-secret"))
        with self.assertRaises(ValidationError):
            PatchUserMCPServerRequest(credential_action="replace")

    def test_input_limits_are_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            CreateUserMCPServerRequest(
                display_name="x" * 101,
                routing_description="route",
                endpoint_url="https://mcp.example.test/rpc",
            )
        with self.assertRaises(ValidationError):
            CreateUserMCPServerRequest(
                display_name="Demo",
                routing_description="route",
                endpoint_url="https://mcp.example.test/" + "a" * 2048,
            )

    def test_mcp_binding_is_closed_and_requires_exact_forced_route(self) -> None:
        request = SubmitMessageRequest.model_validate(
            {
                "conversation_id": "conv-1",
                "content": "查询",
                "routing_mode": "force_capability",
                "capability_id": "mcp.dispatch",
                "metadata": {
                    "mcp_server_binding": {"server_id": " mcp-server-1 "},
                    "deep_thinking": True,
                },
            }
        )
        self.assertEqual(
            request.metadata["mcp_server_binding"],
            {"server_id": "mcp-server-1"},
        )

        invalid_payloads = (
            {
                "routing_mode": "force_capability",
                "capability_id": "mcp.dispatch",
                "metadata": {},
            },
            {
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"mcp_server_binding": {"server_id": "mcp-server-1"}},
            },
            {
                "routing_mode": "force_capability",
                "capability_id": "mcp.dispatch",
                "metadata": {"mcp_server_binding": {"server_id": "mcp-server-1", "endpoint": "https://evil"}},
            },
            {
                "routing_mode": "force_capability",
                "capability_id": "mcp.dispatch",
                "metadata": {
                    "mcp_server_binding": {"server_id": "mcp-server-1"},
                    "credential": "secret",
                },
            },
            {
                "routing_mode": "force_capability",
                "capability_id": "mcp.dispatch",
                "metadata": {"mcp_server_binding": {"server_id": "x" * 129}},
            },
            {
                "routing_mode": "force_capability",
                "capability_id": "mcp.dispatch",
                "metadata": {"mcp_server_binding": {"server_id": "mcp\u0000server"}},
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                SubmitMessageRequest.model_validate(
                    {
                        "conversation_id": "conv-1",
                        "content": "查询",
                        **payload,
                    }
                )
