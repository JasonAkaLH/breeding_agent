from __future__ import annotations

import dataclasses
import unittest

from src.core import enums
from src.core.models import AuthUserToken, Conversation, ConversationMemorySummary, FileUploadMessageProjection, Message, Task
from src.core.rust_contract import load_core_contract


class CoreRustContractArtifactTest(unittest.TestCase):
    def test_core_enums_are_backed_by_rust_contract_artifact(self) -> None:
        contract = load_core_contract()
        self.assertEqual(contract["component"], "maf_core_types")
        self.assertEqual(contract["contract_version"], "core.v1")

        task_status = contract["enums"]["TaskStatus"]
        self.assertEqual(
            [member["value"] for member in task_status],
            [member.value for member in enums.TaskStatus],
        )
        self.assertEqual(task_status[0], {"name": "ACCEPTED", "value": "accepted"})

    def test_core_model_fields_are_backed_by_rust_contract_artifact(self) -> None:
        contract = load_core_contract()
        self.assertIn("auth_generation", contract["schema_hash"])
        for legacy_model in ("AuthUser", "AuthSession", "CaptchaChallenge", "AuthApiToken"):
            self.assertNotIn(legacy_model, contract["models"])

        for model in (Task, Conversation, ConversationMemorySummary, AuthUserToken, Message, FileUploadMessageProjection):
            self.assertEqual(
                contract["models"][model.__name__],
                [field.name for field in dataclasses.fields(model)],
                model.__name__,
            )
        self.assertEqual(
            contract["models"]["AuthUserToken"],
            [
                "username",
                "api_token_hash",
                "token_issued_at",
                "token_last_used_at",
                "auth_generation",
                "auth_generation_updated_at",
                "created_at",
                "updated_at",
            ],
        )
        self.assertIn("username", contract["models"]["Conversation"])
        self.assertNotIn("account_id", contract["models"]["Conversation"])
        self.assertIn("username", contract["models"]["ConversationMemorySummary"])
        self.assertNotIn("account_id", contract["models"]["ConversationMemorySummary"])

    def test_core_error_codes_are_stable_and_prefixed(self) -> None:
        contract = load_core_contract()
        codes = {entry["code"] for entry in contract["error_codes"]}
        self.assertIn("core_contract_validation_failed", codes)
        self.assertIn("core_contract_mismatch", codes)
        self.assertTrue(all(code.startswith("core_") for code in codes))


if __name__ == "__main__":
    unittest.main()
