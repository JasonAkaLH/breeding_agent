from __future__ import annotations

import dataclasses
import inspect
import unittest

from src.core import contracts, enums
from src.core.models import AuthUserToken, Conversation, ConversationMemorySummary, Task
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
        for model in (Task, Conversation, ConversationMemorySummary, AuthUserToken):
            self.assertEqual(
                contract["models"][model.__name__],
                [field.name for field in dataclasses.fields(model)],
                model.__name__,
            )
        self.assertIn("username", contract["models"]["Conversation"])
        self.assertNotIn("account_id", contract["models"]["Conversation"])
        self.assertIn("username", contract["models"]["ConversationMemorySummary"])
        self.assertNotIn("account_id", contract["models"]["ConversationMemorySummary"])
        self.assertEqual(
            contract["models"]["AuthUserToken"],
            [field.name for field in dataclasses.fields(AuthUserToken)],
        )
        self.assertNotIn("AuthUser", contract["models"])
        self.assertNotIn("AuthSession", contract["models"])
        self.assertNotIn("CaptchaChallenge", contract["models"])
        self.assertNotIn("AuthApiToken", contract["models"])


    def test_legacy_auth_models_are_absent_from_python_and_contract(self) -> None:
        legacy_names = {"AuthUser", "CaptchaChallenge", "AuthSession", "AuthApiToken"}
        from src.core import models as core_models

        for name in legacy_names:
            self.assertFalse(hasattr(core_models, name), name)

        contract = load_core_contract()
        self.assertFalse(legacy_names.intersection(contract["models"]), contract["models"].keys())
        self.assertIn("AuthUserToken", contract["models"])

    def test_storage_port_legacy_auth_methods_are_absent(self) -> None:
        legacy_methods = {
            "save_auth_user",
            "get_auth_user",
            "save_captcha_challenge",
            "get_captcha_challenge",
            "save_auth_session",
            "get_auth_session",
            "save_auth_api_token",
            "get_auth_api_token",
            "get_auth_api_token_by_hash",
            "list_auth_api_tokens_for_user",
            "touch_auth_api_token_last_used",
            "revoke_auth_api_token_for_user",
        }
        methods = {name for name, _value in inspect.getmembers(contracts.StoragePort, inspect.isfunction)}
        self.assertFalse(legacy_methods.intersection(methods))
        for method in (
            "save_auth_user_token",
            "get_auth_user_token",
            "get_auth_user_token_by_hash",
            "touch_auth_user_token_last_used",
            "clear_auth_user_token",
            "rotate_auth_user_token",
        ):
            self.assertIn(method, methods)

    def test_schema_hash_marks_legacy_auth_removal(self) -> None:
        contract = load_core_contract()
        self.assertNotEqual(
            contract["schema_hash"],
            "maf_core_types_core_v1_schema_20260525_username_token",
        )
        self.assertIn("legacy_auth_removed", contract["schema_hash"])

    def test_core_error_codes_are_stable_and_prefixed(self) -> None:
        contract = load_core_contract()
        codes = {entry["code"] for entry in contract["error_codes"]}
        self.assertIn("core_contract_validation_failed", codes)
        self.assertIn("core_contract_mismatch", codes)
        self.assertTrue(all(code.startswith("core_") for code in codes))


if __name__ == "__main__":
    unittest.main()
