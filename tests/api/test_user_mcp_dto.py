from __future__ import annotations

import unittest

from pydantic import ValidationError

from src.api.dto import (
    CreateUserMCPServerRequest,
    PatchUserMCPServerRequest,
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
