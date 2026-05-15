from __future__ import annotations

import asyncio
import unittest

from src.api.upload_store import InMemoryUploadStore, UploadValidationError
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult
from src.integrations.rust_safety_contract import load_safety_contract, resource_limit


class SafetyRustContractTest(unittest.TestCase):
    def test_safety_contract_declares_four_security_crates_and_modes(self) -> None:
        contract = load_safety_contract()
        self.assertEqual(contract["component"], "maf_safety_kernels")
        self.assertEqual(
            contract["crates"],
            ["maf_artifact_store", "maf_auth_core", "maf_data_access", "maf_audit_sanitizer"],
        )
        self.assertEqual(
            contract["mode_env"],
            {
                "artifact_store": "MAF_RUST_ARTIFACT_STORE_MODE",
                "auth_core": "MAF_RUST_AUTH_CORE_MODE",
                "data_access": "MAF_RUST_DATA_ACCESS_MODE",
                "audit_sanitizer": "MAF_RUST_AUDIT_SANITIZER_MODE",
            },
        )

    def test_safety_error_prefixes_are_component_scoped(self) -> None:
        codes = {entry["code"] for entry in load_safety_contract()["error_codes"]}
        for expected in [
            "artifact_path_escape",
            "auth_token_invalid",
            "data_access_write_denied",
            "audit_sanitizer_secret_redacted",
        ]:
            self.assertIn(expected, codes)
        self.assertTrue(
            all(
                code.startswith(("artifact_", "auth_", "data_access_", "audit_sanitizer_"))
                for code in codes
            )
        )

    def test_safety_contract_freezes_resource_limits(self) -> None:
        limits = load_safety_contract()["resource_limits"]
        self.assertEqual(limits["db_row_limit"], 500)
        self.assertEqual(limits["db_column_limit"], 100)
        self.assertEqual(limits["db_result_bytes"], 10 * 1024 * 1024)
        self.assertEqual(limits["upload_preview_bytes"], 10 * 1024 * 1024)
        self.assertEqual(limits["audit_event_bytes"], 64 * 1024)

    def test_data_access_adapter_consumes_rust_safety_contract_limits(self) -> None:
        calls: list[str] = []

        def runner(sql: str) -> ReadonlyQueryResult:
            calls.append(sql)
            return ReadonlyQueryResult(columns=("id",), rows=({"id": 1},), row_count=1)

        adapter = MySQLReadonlyAdapter(runner=runner)
        with self.assertRaisesRegex(PermissionError, "readonly"):
            asyncio.run(adapter.execute_readonly("DELETE FROM users", guard_pass_token="guard:test"))
        self.assertEqual(calls, [])

        result = asyncio.run(adapter.execute_readonly("SELECT 1", guard_pass_token="guard:test"))
        self.assertEqual(result.row_count, 1)
        self.assertEqual(calls, ["SELECT 1"])

        too_many_rows = tuple({"id": index} for index in range(resource_limit("db_row_limit") + 1))
        limited = MySQLReadonlyAdapter(
            runner=lambda _: ReadonlyQueryResult(columns=("id",), rows=too_many_rows, row_count=len(too_many_rows))
        )
        with self.assertRaisesRegex(RuntimeError, "row limit"):
            asyncio.run(limited.execute_readonly("SELECT id FROM t", guard_pass_token="guard:test"))

    def test_upload_preview_limit_consumes_rust_safety_contract(self) -> None:
        store = InMemoryUploadStore(max_file_bytes=resource_limit("upload_preview_bytes") + 10)
        self.assertEqual(store.max_preview_bytes, resource_limit("upload_preview_bytes"))

        content = b"a\n" + b"x\n" * (resource_limit("upload_preview_bytes") // 2)
        with self.assertRaisesRegex(UploadValidationError, "preview"):
            store.save(
                account_id="acc-1",
                conversation_id="conv-1",
                filename="large.csv",
                content_type="text/csv",
                content=content,
            )


if __name__ == "__main__":
    unittest.main()
