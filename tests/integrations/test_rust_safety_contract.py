from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.api.upload_store import InMemoryUploadStore, UploadValidationError
from src.integrations.audit_logger import JsonlAuditSink
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult
from src.integrations import rust_safety_contract
from src.integrations.rust_safety_contract import (
    RustSafetyContractError,
    configure_safety_shadow_sink,
    ensure_readonly_sql,
    hmac_sha256_hex,
    load_safety_contract,
    normalize_storage_key,
    resource_limit,
    sanitize_audit_payload,
    sha256_hex,
    validate_data_access_shape,
)


class _PasswordHasherForSafetyContract:
    scheme = "pbkdf2_sha256"
    iterations = 200_000

    def hash_password(self, password: str, *, salt: str = "fixed") -> tuple[str, str, str]:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            self.iterations,
        ).hex()
        return digest, salt, self.scheme

    def verify_password(self, password: str, user) -> bool:
        expected, _, _ = self.hash_password(password, salt=user.password_salt)
        return rust_safety_contract.verify_auth_token(expected, user.password_hash)


class SafetyRustContractTest(unittest.TestCase):
    def tearDown(self) -> None:
        load_safety_contract.cache_clear()
        configure_safety_shadow_sink(None)
        sys.modules.pop("fake_maf_safety_kernels_pyo3", None)
        sys.modules.pop("bad_maf_safety_kernels_pyo3", None)

    def test_safety_contract_declares_four_security_crates_and_modes(self) -> None:
        contract = load_safety_contract()
        self.assertEqual(contract["component"], "maf_safety_kernels")
        self.assertEqual(contract["contract_version"], "safety-kernels.v1")
        self.assertEqual(contract["schema_hash"], "maf_safety_kernels_schema_v1_20260517")
        self.assertEqual(
            contract["error_code_table_hash"],
            "maf_safety_kernels_error_table_v2_20260517",
        )
        for feature in [
            "artifact_store_kernel",
            "auth_core_kernel",
            "data_access_kernel",
            "audit_sanitizer_kernel",
            "pyo3_safety_facade",
        ]:
            self.assertIn(feature, contract["supported_features"])
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
            "data_access_deadline_exceeded",
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

        with self.assertRaisesRegex(RuntimeError, "row limit"):
            validate_data_access_shape(
                row_count=resource_limit("db_row_limit") + 1,
                column_count=1,
                result_bytes=32,
            )

        too_many_rows = tuple({"id": index} for index in range(resource_limit("db_row_limit") + 1))
        limited = MySQLReadonlyAdapter(
            runner=lambda _: ReadonlyQueryResult(columns=("id",), rows=too_many_rows, row_count=len(too_many_rows))
        )
        trimmed = asyncio.run(limited.execute_readonly("SELECT id FROM t", guard_pass_token="guard:test"))
        self.assertEqual(trimmed.row_count, resource_limit("db_row_limit"))
        self.assertEqual(trimmed.source_row_count, resource_limit("db_row_limit") + 1)
        self.assertTrue(trimmed.row_limit_trimmed)
        self.assertEqual(trimmed.rows[0], {"id": 1})

    def test_upload_preview_limit_consumes_rust_safety_contract(self) -> None:
        store = InMemoryUploadStore(max_file_bytes=resource_limit("upload_preview_bytes") + 10)
        self.assertEqual(store.max_preview_bytes, resource_limit("upload_preview_bytes"))

        content = b"a\n" + b"x\n" * (resource_limit("upload_preview_bytes") // 2)
        with self.assertRaisesRegex(UploadValidationError, "preview"):
            store.save(
                username="acc-1",
                conversation_id="conv-1",
                filename="large.csv",
                content_type="text/csv",
                content=content,
            )

    def test_enforce_requires_prebuilt_safety_pyo3_module(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_ARTIFACT_STORE_MODE": "enforce",
                "MAF_SAFETY_KERNELS_PYO3_MODULE": "missing_maf_safety_kernels_pyo3",
            },
            clear=False,
        ):
            load_safety_contract.cache_clear()
            with self.assertRaisesRegex(RustSafetyContractError, "requires a prebuilt PyO3 module"):
                load_safety_contract()

    def test_enforce_fails_closed_on_safety_pyo3_contract_mismatch(self) -> None:
        module = types.ModuleType("bad_maf_safety_kernels_pyo3")
        contract = _safety_contract()
        contract["schema_hash"] = "wrong"
        module.contract_json = lambda: json.dumps(contract)
        sys.modules[module.__name__] = module

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_DATA_ACCESS_MODE": "enforce",
                "MAF_SAFETY_KERNELS_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            load_safety_contract.cache_clear()
            with self.assertRaisesRegex(RustSafetyContractError, "contract mismatch"):
                load_safety_contract()

    def test_enforce_calls_safety_pyo3_kernel_bridges(self) -> None:
        module = _fake_safety_pyo3_module()
        sys.modules[module.__name__] = module

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_ARTIFACT_STORE_MODE": "enforce",
                "MAF_RUST_AUTH_CORE_MODE": "enforce",
                "MAF_RUST_DATA_ACCESS_MODE": "enforce",
                "MAF_RUST_AUDIT_SANITIZER_MODE": "enforce",
                "MAF_SAFETY_KERNELS_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            load_safety_contract.cache_clear()
            self.assertEqual(normalize_storage_key("task/report.csv"), "task/report.csv")
            self.assertEqual(sha256_hex(b"abc"), hashlib.sha256(b"abc").hexdigest())
            self.assertTrue(rust_safety_contract.verify_auth_token("same", "same"))
            self.assertFalse(rust_safety_contract.verify_auth_token("same", "diff"))
            ensure_readonly_sql("SELECT 1")
            validate_data_access_shape(row_count=1, column_count=1, result_bytes=16)
            self.assertEqual(
                sanitize_audit_payload({"secret": "x", "safe": {"value": 1}}),
                {"secret": "[REDACTED]", "safe": {"value": 1}},
            )

        self.assertIn("normalize_storage_key_json", module.calls)
        self.assertIn("sha256_hex_bytes", module.calls)
        self.assertIn("verify_token_json", module.calls)
        self.assertIn("ensure_readonly_sql_json", module.calls)
        self.assertIn("validate_shape_json", module.calls)
        self.assertIn("sanitize_value_json", module.calls)

    def test_shadow_mode_keeps_python_result_and_records_sanitized_diff(self) -> None:
        module = _fake_safety_pyo3_module()
        sys.modules[module.__name__] = module
        shadow_events: list[dict[str, str]] = []
        configure_safety_shadow_sink(shadow_events.append)

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_ARTIFACT_STORE_MODE": "shadow",
                "MAF_RUST_AUTH_CORE_MODE": "shadow",
                "MAF_RUST_DATA_ACCESS_MODE": "shadow",
                "MAF_RUST_AUDIT_SANITIZER_MODE": "shadow",
                "MAF_SAFETY_KERNELS_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            load_safety_contract.cache_clear()
            self.assertEqual(normalize_storage_key("task/report.csv"), "task/report.csv")
            ensure_readonly_sql("SELECT 1")
            self.assertFalse(rust_safety_contract.verify_auth_token("same", "diff"))
            self.assertEqual(
                sanitize_audit_payload({"message": "token=do-not-log dsn=mysql://u:p@host/db path=/tmp/secret"}),
                {"message": "token=[REDACTED] dsn=[REDACTED] path=[REDACTED]"},
            )

        self.assertGreaterEqual(len(shadow_events), 4)
        self.assertTrue(all(event["component"] in {"artifact_store", "auth_core", "data_access", "audit_sanitizer"} for event in shadow_events))
        self.assertTrue(all("input_fingerprint" in event and "legacy_output_fingerprint" in event for event in shadow_events))
        self.assertNotIn("do-not-log", json.dumps(shadow_events, ensure_ascii=False))
        self.assertIn("normalize_storage_key_json", module.calls)
        self.assertIn("ensure_readonly_sql_json", module.calls)

    def test_password_hasher_uses_safety_auth_facade_in_enforce_mode(self) -> None:
        module = _fake_safety_pyo3_module()
        sys.modules[module.__name__] = module
        hasher = _PasswordHasherForSafetyContract()
        password_hash, password_salt, password_scheme = hasher.hash_password("secret123", salt="fixed")
        user = types.SimpleNamespace(
            password_hash=password_hash,
            password_salt=password_salt,
            password_scheme=password_scheme,
        )

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_AUTH_CORE_MODE": "enforce",
                "MAF_SAFETY_KERNELS_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            load_safety_contract.cache_clear()
            self.assertTrue(hasher.verify_password("secret123", user))
            self.assertFalse(hasher.verify_password("wrong123", user))

        self.assertIn("verify_token_json", module.calls)

    def test_hmac_rejects_missing_secret_in_python_fallback(self) -> None:
        with self.assertRaisesRegex(RustSafetyContractError, "auth secret"):
            hmac_sha256_hex("", "payload")

    def test_jsonl_audit_sink_sanitizes_sensitive_payload_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "audit.jsonl"
            sink = JsonlAuditSink(path)

            sink.record_sync(
                "security.test",
                {
                    "safe": "ok",
                    "message": "token=do-not-log dsn=mysql://u:p@host/db path=/tmp/secret",
                    "token": "do-not-log",
                    "nested": {"base_url": "https://internal", "count": 1},
                    "rows": [{"secret": "row-secret"}],
                    "prompt": "full prompt should not log",
                    "prompt_recorded": False,
                    "real_path": "/tmp/secret",
                },
            )

            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["payload"]["safe"], "ok")
            self.assertEqual(
                record["payload"]["message"],
                "token=[REDACTED] dsn=[REDACTED] path=[REDACTED]",
            )
            self.assertEqual(record["payload"]["token"], "[REDACTED]")
            self.assertEqual(record["payload"]["nested"]["base_url"], "[REDACTED]")
            self.assertEqual(record["payload"]["nested"]["count"], 1)
            self.assertEqual(record["payload"]["rows"], "[REDACTED]")
            self.assertEqual(record["payload"]["prompt"], "[REDACTED]")
            self.assertIs(record["payload"]["prompt_recorded"], False)
            self.assertEqual(record["payload"]["real_path"], "[REDACTED]")
            self.assertNotIn("do-not-log", json.dumps(record, ensure_ascii=False))
            self.assertNotIn("full prompt should not log", json.dumps(record, ensure_ascii=False))
            self.assertNotIn("mysql://u:p@host/db", json.dumps(record, ensure_ascii=False))
            self.assertNotIn("/tmp/secret", json.dumps(record, ensure_ascii=False))

    def test_audit_sanitizer_enforces_event_size_in_python_fallback(self) -> None:
        with self.assertRaisesRegex(RustSafetyContractError, "event size"):
            sanitize_audit_payload({"safe": "x" * (resource_limit("audit_event_bytes") + 1)})

    def test_readonly_sql_denies_file_export_and_locking_forms(self) -> None:
        ensure_readonly_sql("SELECT/* ordinary comment */ 1")
        ensure_readonly_sql("SELECT 1 -- harmless comment\n")
        ensure_readonly_sql("SELECT 1 -- harmless comment\r")
        ensure_readonly_sql("SELECT 1 # harmless comment\r\n")

        for sql in [
            "SELECT * FROM users INTO OUTFILE '/tmp/leak'",
            "SELECT * FROM users INTO\nOUTFILE '/tmp/leak'",
            "SELECT * FROM users INTO   OUTFILE '/tmp/leak'",
            "SELECT * FROM users INTO/**/OUTFILE '/tmp/leak'",
            "SELECT * FROM users IN/**/TO OUT/**/FILE '/tmp/leak'",
            "SELECT * FROM users /*!50000 INTO OUTFILE '/tmp/leak' */",
            "SELECT * FROM users INTO DUMPFILE '/tmp/leak'",
            "SELECT * FROM users FOR UPDATE",
            "SELECT * FROM users FOR\nUPDATE",
            "SELECT * FROM users FOR/**/UPDATE",
            "SELECT * FROM users FO/**/R UP/**/DATE",
            "SELECT * FROM users FOR SHARE",
            "SELECT * FROM users FOR/**/SHARE",
            "SELECT * FROM users FO/**/R SH/**/ARE",
            "SELECT GET_LOCK('x', 1)",
            "SELECT GET_LOCK ('x', 1)",
            "SELECT GET/**/_LOCK ('x', 1)",
            "SELECT LOAD_FILE/**/('/tmp/leak')",
            "SELECT RELEASE_LOCK ('x')",
            "SELECT 1; SELECT 2",
            "SELECT 1--1; DELETE FROM users",
            "SELECT 1--x; UPDATE users SET id = 1",
            "SELECT 1--\r; DELETE FROM users",
            "SELECT 1#\r; DELETE FROM users",
            "SELECT 1-- \r; UPDATE users SET id = 1",
        ]:
            with self.subTest(sql=sql):
                with self.assertRaises(PermissionError):
                    ensure_readonly_sql(sql)


def _safety_contract() -> dict:
    load_safety_contract.cache_clear()
    with patch.dict(
        "os.environ",
        {
            "MAF_RUST_ARTIFACT_STORE_MODE": "off",
            "MAF_RUST_AUTH_CORE_MODE": "off",
            "MAF_RUST_DATA_ACCESS_MODE": "off",
            "MAF_RUST_AUDIT_SANITIZER_MODE": "off",
        },
        clear=False,
    ):
        return dict(load_safety_contract())


def _fake_safety_pyo3_module() -> types.ModuleType:
    module = types.ModuleType("fake_maf_safety_kernels_pyo3")
    module.calls = []
    module.contract_json = lambda: json.dumps(_safety_contract())

    def normalize_storage_key_json(payload: str) -> str:
        module.calls.append("normalize_storage_key_json")
        value = json.loads(payload)["key"]
        if ".." in value:
            return json.dumps({"value": None, "error": {"code": "artifact_path_escape", "message": "bad"}})
        return json.dumps({"value": value, "error": None})

    def sha256_hex_bytes(content: bytes) -> str:
        module.calls.append("sha256_hex_bytes")
        return hashlib.sha256(content).hexdigest()

    def verify_token_json(payload: str) -> str:
        module.calls.append("verify_token_json")
        request = json.loads(payload)
        matches = request["expected"] == request["actual"]
        return json.dumps(
            {
                "valid": matches,
                "error": None
                if matches
                else {"code": "auth_token_invalid", "message": "invalid", "category": "security", "retriable": False},
            }
        )

    def ensure_readonly_sql_json(payload: str) -> str:
        module.calls.append("ensure_readonly_sql_json")
        sql = json.loads(payload)["sql"].strip().lower()
        if not (sql.startswith("select") or sql.startswith("with")):
            return json.dumps({"allowed": False, "error": {"code": "data_access_write_denied", "message": "write"}})
        return json.dumps({"allowed": True, "error": None})

    def validate_shape_json(payload: str) -> str:
        module.calls.append("validate_shape_json")
        request = json.loads(payload)
        if request["row_count"] > resource_limit("db_row_limit"):
            return json.dumps(
                {
                    "valid": False,
                    "error": {"code": "data_access_row_limit_exceeded", "message": "too many rows"},
                }
            )
        return json.dumps({"valid": True, "error": None})

    def sanitize_value_json(payload: str) -> str:
        module.calls.append("sanitize_value_json")
        request = json.loads(payload)
        sanitized = {
            key: "[REDACTED]" if key in {"secret", "token"} else value for key, value in request["value"].items()
        }
        return json.dumps({"value": sanitized, "error": None})

    module.normalize_storage_key_json = normalize_storage_key_json
    module.sha256_hex_bytes = sha256_hex_bytes
    module.verify_token_json = verify_token_json
    module.ensure_readonly_sql_json = ensure_readonly_sql_json
    module.validate_shape_json = validate_shape_json
    module.sanitize_value_json = sanitize_value_json
    return module


if __name__ == "__main__":
    unittest.main()
