from __future__ import annotations

import unittest

from pydantic import ValidationError

from src.api.dto import (
    CreateUserMCPServerRequest,
    PatchUserMCPServerRequest,
    SubmitMessageRequest,
    UserMCPCredentialInput,
)


class UserMCPDTOTest(unittest.TestCase):
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
                endpoint_url="https://mcp.example.test/rpc",
                auth_type="bearer",
            )

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
                endpoint_url="https://mcp.example.test/rpc",
            )
        with self.assertRaises(ValidationError):
            CreateUserMCPServerRequest(
                display_name="Demo",
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
