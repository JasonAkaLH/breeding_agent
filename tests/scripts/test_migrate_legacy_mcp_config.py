from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from scripts.migrate_legacy_mcp_config import (
    ARTIFACT_SCHEMA,
    MigrationCommandError,
    _build_local_applier,
    build_artifact,
    load_classifications,
    run,
    validate_artifact_for_apply,
    _fingerprint,
)
from src.integrations.mcp.config import load_mcp_server_config
from src.integrations.mcp.legacy_migration import (
    LegacyMigrationPlan,
    LegacyMigrationHealthResult,
    LegacyMigrationValidationError,
    LEGACY_MIGRATION_HEALTH_POLICY,
    legacy_capability_contract_set_fingerprint,
    legacy_migration_catalog_fingerprint,
    legacy_target_consumer_reference,
)
from src.integrations.mcp.legacy_migration_apply import LegacyMigrationLiveHealthRequest
from src.state.runtime_factory import (
    StatePlatformBackend,
    StatePlatformRuntimeConfig,
)
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from tests.master_key_support import audit_reference_signer, credential_cipher

SERVICE_CONSUMER_REF = legacy_target_consumer_reference(
    audit_reference_signer(b"k" * 32),
    "service-owner",
)


class _NoNetworkHealthClient:
    server_capabilities: dict[str, object] = {"tools": {}}

    async def initialize(self):
        return {}

    async def list_tools(self):
        return [
            {
                "name": "lookup",
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
            }
        ]

    async def close(self):
        return None


class _NoNetworkClientFactory:
    calls = 0

    def __init__(self, _endpoint_policy):
        pass

    async def create(self, _server, _headers):
        self.__class__.calls += 1
        return _NoNetworkHealthClient()


class LegacyMCPMigrationCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config_path = self.root / "legacy.json"
        self.classifications_path = self.root / "classifications.json"
        self.artifact_path = self.root / "artifact.json"
        self._write_config()
        self._write_classifications(
            {
                "crm": {
                    "disposition": "migrate_owner",
                    "consumer_scope": "service_account_only",
                    "owner_user_id": "service-owner",
                }
            }
        )

    def test_removed_credential_key_argument_is_rejected(self) -> None:
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            run(
                self._args(
                    "--credential-key-file",
                    str(self.root / "removed.key"),
                ),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_dry_run_is_default_and_artifact_is_secret_safe(self) -> None:
        stdout = io.StringIO()
        code = run(
            self._args("--artifact-out", str(self.artifact_path)),
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(code, 0)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual(artifact["schema"], ARTIFACT_SCHEMA)
        self.assertFalse(artifact["apply_validation"]["ready"])
        serialized = json.dumps(artifact, sort_keys=True)
        for secret in (
            "secret-host",
            "endpoint-secret",
            "Secret-Header",
            "header-secret",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual(json.loads(self.artifact_path.read_text()), artifact)

    def test_classification_mapping_is_explicit_complete_and_unambiguous(self) -> None:
        self.classifications_path.write_text(
            '{"servers":{"crm":{"disposition":"migrate_owner",'
            '"consumer_scope":"service_account_only","owner_user_id":"a"},'
            '"crm":{"disposition":"retire","consumer_scope":"unknown"}}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MigrationCommandError, "duplicate_json_key:crm"):
            load_classifications(self.classifications_path)

        self._write_classifications({})
        with self.assertRaisesRegex(
            LegacyMigrationValidationError, "Missing legacy classifications"
        ):
            build_artifact(
                config=load_mcp_server_config(path=self.config_path),
                classifications=load_classifications(self.classifications_path),
            )

    def test_apply_rejects_tampered_and_stale_artifacts(self) -> None:
        artifact = build_artifact(
            config=load_mcp_server_config(path=self.config_path),
            classifications=load_classifications(self.classifications_path),
        )
        self.assertFalse(artifact["apply_validation"]["ready"])
        artifact["plan"]["mapping_candidates"][0]["target_server_id"] = "attacker"
        with self.assertRaisesRegex(
            MigrationCommandError, "migration_artifact_tampered"
        ):
            self._validate(artifact)

        artifact = self._healthy_artifact()
        self._write_config(server_id="other")
        self._write_classifications(
            {
                "other": {
                    "disposition": "migrate_owner",
                    "consumer_scope": "service_account_only",
                    "owner_user_id": "service-owner",
                }
            }
        )
        with self.assertRaisesRegex(MigrationCommandError, "migration_artifact_stale"):
            self._validate(artifact)

    def test_rehashed_artifact_cannot_smuggle_forged_health_types(self) -> None:
        artifact = self._healthy_artifact()
        artifact["health_results"][0]["attempts"] = True
        unsigned = dict(artifact)
        unsigned.pop("artifact_fingerprint")
        artifact["artifact_fingerprint"] = _fingerprint(unsigned)
        with self.assertRaisesRegex(
            MigrationCommandError, "migration_health_evidence_invalid"
        ):
            self._validate(artifact)

        artifact = self._healthy_artifact()
        artifact["health_results"][0]["handshake_ok"] = 1
        unsigned = dict(artifact)
        unsigned.pop("artifact_fingerprint")
        artifact["artifact_fingerprint"] = _fingerprint(unsigned)
        with self.assertRaisesRegex(
            MigrationCommandError, "migration_health_evidence_invalid"
        ):
            self._validate(artifact)

        artifact = self._healthy_artifact()
        artifact["health_results"][0]["safe_error_code"] = "forged_success"
        unsigned = dict(artifact)
        unsigned.pop("artifact_fingerprint")
        artifact["artifact_fingerprint"] = _fingerprint(unsigned)
        with self.assertRaisesRegex(
            MigrationCommandError, "migration_health_evidence_invalid"
        ):
            self._validate(artifact)

    def test_apply_rejects_retained_entries_but_not_informational_health(self) -> None:
        self._write_classifications(
            {
                "crm": {
                    "disposition": "retain_for_rollback",
                    "consumer_scope": "service_account_only",
                }
            }
        )
        retained = build_artifact(
            config=load_mcp_server_config(path=self.config_path),
            classifications=load_classifications(self.classifications_path),
        )
        with self.assertRaisesRegex(
            MigrationCommandError, "retained_legacy_servers_block_apply"
        ):
            self._validate(retained)

        self._write_classifications(
            {
                "crm": {
                    "disposition": "migrate_owner",
                    "consumer_scope": "service_account_only",
                    "owner_user_id": "service-owner",
                }
            }
        )
        unhealthy = self._artifact_with_health(
            LegacyMigrationHealthResult("crm", 2, True, True, True, False)
        )
        self.assertEqual(
            self._validate(unhealthy).mapping_candidates[0].source_server_id, "crm"
        )

        excessive_attempts = self._artifact_with_health(
            LegacyMigrationHealthResult("crm", 3, True, True, True, True)
        )
        self.assertEqual(
            self._validate(excessive_attempts).mapping_candidates[0].source_server_id,
            "crm",
        )

    def test_retirement_requires_approved_evidence_before_assembly_off(self) -> None:
        self._write_classifications(
            {
                "crm": {
                    "disposition": "retire",
                    "consumer_scope": "unknown",
                    "retirement_reason": "No supported consumers remain",
                }
            }
        )
        blocked = build_artifact(
            config=load_mcp_server_config(path=self.config_path),
            classifications=load_classifications(self.classifications_path),
        )
        with self.assertRaisesRegex(
            MigrationCommandError, "assembly_off_classification_blocked"
        ):
            self._validate(blocked)

        self._write_classifications(
            {
                "crm": {
                    "disposition": "retire",
                    "consumer_scope": "unknown",
                    "retirement_approver": "platform-owner",
                    "retirement_reason": "No supported consumers remain",
                    "impact_accepted": True,
                }
            }
        )
        allowed = build_artifact(
            config=load_mcp_server_config(path=self.config_path),
            classifications=load_classifications(self.classifications_path),
        )
        plan = self._validate(allowed)
        self.assertEqual(plan.retired_server_ids, ("crm",))

        self._write_classifications(
            {
                "crm": {
                    "disposition": "retire",
                    "consumer_scope": "unknown",
                    "retirement_approver": "platform-owner",
                    "retirement_reason": "A different retirement decision",
                    "impact_accepted": True,
                }
            }
        )
        with self.assertRaisesRegex(MigrationCommandError, "retirement_evidence_stale"):
            self._validate(allowed)

    def test_apply_backend_is_fail_closed_and_injected_apply_is_idempotent(
        self,
    ) -> None:
        artifact = self._healthy_artifact()
        self.artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
        stderr = io.StringIO()
        code = run(
            self._args("--apply", "--artifact", str(self.artifact_path)),
            stdout=io.StringIO(),
            stderr=stderr,
        )
        self.assertEqual(code, 2)
        self.assertIn("apply_backend_options_required", stderr.getvalue())

        applied: set[str] = set()

        def applier(_artifact: dict[str, Any], *, idempotency_key: str) -> str:
            if idempotency_key in applied:
                return "already_applied"
            applied.add(idempotency_key)
            return "applied"

        first = io.StringIO()
        second = io.StringIO()
        self.assertEqual(
            run(
                self._args("--apply", "--artifact", str(self.artifact_path)),
                artifact_applier=applier,
                stdout=first,
                stderr=io.StringIO(),
            ),
            0,
        )
        self.assertEqual(
            run(
                self._args("--apply", "--artifact", str(self.artifact_path)),
                artifact_applier=applier,
                stdout=second,
                stderr=io.StringIO(),
            ),
            0,
        )
        self.assertEqual(json.loads(first.getvalue())["status"], "applied")
        self.assertEqual(json.loads(second.getvalue())["status"], "already_applied")
        self.assertEqual(len(applied), 1)

    def test_builtin_apply_persists_config_and_writes_secret_safe_audit(self) -> None:
        self._write_config(endpoint="https://8.8.8.8/rpc")
        artifact = build_artifact(
            config=load_mcp_server_config(path=self.config_path),
            classifications=load_classifications(self.classifications_path),
        )
        self.assertFalse(artifact["apply_validation"]["ready"])
        self.artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
        database = self.root / "state.db"
        engine = create_sqlite_engine(database)
        bootstrap_sqlite_database(engine)
        engine.dispose()
        key_file = self.root / "credential.key"
        key_file.write_bytes(base64.b64encode(b"k" * 32) + b"\n")
        key_file.chmod(0o400)
        audit = self.root / "migration-audit.json"
        args = self._args(
            "--apply",
            "--artifact",
            str(self.artifact_path),
            "--database-path",
            str(database),
            "--master-key-file",
            str(key_file.resolve()),
            "--service-account-owner",
            "service-owner",
            "--audit-out",
            str(audit),
        )

        first = io.StringIO()
        second = io.StringIO()
        _NoNetworkClientFactory.calls = 0
        first_error = io.StringIO()
        second_error = io.StringIO()
        with patch(
            "src.integrations.mcp.user_client.UserMCPClientFactory",
            _NoNetworkClientFactory,
        ):
            self.assertEqual(
                run(args, stdout=first, stderr=first_error),
                0,
                first_error.getvalue(),
            )
            self.assertEqual(
                run(args, stdout=second, stderr=second_error),
                0,
                second_error.getvalue(),
            )
        self.assertEqual(_NoNetworkClientFactory.calls, 2)
        self.assertEqual(json.loads(first.getvalue())["status"], "applied")
        self.assertEqual(json.loads(second.getvalue())["status"], "already_applied")
        durable_record_id = json.loads(first.getvalue())["durable_record_ids"][0]

        read_engine = create_sqlite_engine(database)
        storage = SQLiteStorage(create_sqlite_session_factory(read_engine))
        target_id = artifact["plan"]["mapping_candidates"][0]["target_server_id"]
        server = asyncio.run(storage.get_user_mcp_server("service-owner", target_id))
        self.assertIsNotNone(server)
        credential = asyncio.run(
            storage.get_user_mcp_credential("service-owner", target_id)
        )
        self.assertEqual(
            credential_cipher(b"k" * 32).decrypt(
                credential,
                owner_user_id="service-owner",
                server_id=target_id,
                auth_type="static_headers",
            ),
            {"values": {"secret-header": "header-secret"}},
        )
        public_output = first.getvalue() + second.getvalue() + audit.read_text()
        for secret in ("endpoint-secret", "Secret-Header", "header-secret"):
            self.assertNotIn(secret, public_output)
        self.assertNotIn("service-owner", public_output)
        audit_payload = json.loads(audit.read_text())
        self.assertEqual(
            audit_payload["role"], "non_authoritative_export"
        )
        self.assertEqual(
            audit_payload["authority_store"], "mcp_legacy_migration_record"
        )
        self.assertFalse(audit_payload["runtime_state_migrated"])
        with read_engine.connect() as connection:
            durable = connection.execute(
                text(
                    "SELECT migration_id, event_type, owner_consumer_ref, "
                    "credential_digest "
                    "FROM mcp_legacy_migration_record"
                )
            ).one()
        self.assertEqual(durable.migration_id, durable_record_id)
        self.assertEqual(durable.event_type, "mcp.legacy.config_migrated")
        self.assertRegex(durable.owner_consumer_ref, r"^hmac-sha256:[0-9a-f]{64}$")
        self.assertRegex(durable.credential_digest, r"^hmac-sha256:[0-9a-f]{64}$")
        read_engine.dispose()

        failed_export = self.root / "audit-directory"
        failed_export.mkdir()
        export_stdout = io.StringIO()
        with patch(
            "src.integrations.mcp.user_client.UserMCPClientFactory",
            _NoNetworkClientFactory,
        ):
            self.assertEqual(
                run(
                    [
                        *args[:-1],
                        str(failed_export),
                    ],
                    stdout=export_stdout,
                    stderr=io.StringIO(),
                ),
                0,
            )
        self.assertEqual(
            json.loads(export_stdout.getvalue())["audit_export"],
            "failed_non_authoritative",
        )

    def test_builtin_apply_selects_canonical_postgresql_without_bootstrap(self) -> None:
        plan = self._validate(self._healthy_artifact())
        key_file = self.root / "postgres-credential.key"
        key_file.write_bytes(base64.b64encode(b"p" * 32) + b"\n")
        key_file.chmod(0o400)
        args = SimpleNamespace(
            database_path=None,
            master_key_file=str(key_file.resolve()),
            audit_out=str(self.root / "postgres-audit.json"),
            service_account_owner="service-owner",
            allowlist_domain=[],
            allowlist_cidr=[],
        )
        state_config = StatePlatformRuntimeConfig(
            backend=StatePlatformBackend.POSTGRESQL,
            release_gate_configured=True,
            dsn="postgresql+psycopg://configured-by-env/db",
        )
        engine = MagicMock()
        storage = MagicMock()
        fake_applier = MagicMock(return_value="applied")
        migration_dsn = (
            "postgresql+psycopg://phase3_migrator@configured-by-env/db"
        )
        with (
            patch.dict(
                os.environ,
                {"MAF_MCP_LEGACY_MIGRATION_DSN": migration_dsn},
            ),
            patch(
                "scripts.migrate_legacy_mcp_config.build_state_platform_runtime_config",
                return_value=state_config,
            ) as build_runtime_config,
            patch(
                "src.storage.postgres.create_postgres_engine",
                return_value=engine,
            ) as create_engine,
            patch(
                "src.storage.postgres.create_postgres_session_factory",
                return_value="session-factory",
            ),
            patch(
                "src.storage.postgres.PostgreSQLStorage",
                return_value=storage,
            ) as postgres_storage,
            patch(
                "src.storage.postgres.session."
                "validate_mcp_legacy_migration_connection_role",
            ) as validate_migration_role,
            patch(
                "src.storage.postgres.bootstrap_postgres_database"
            ) as bootstrap_postgres,
            patch(
                "scripts.migrate_legacy_mcp_config.LocalLegacyMigrationApplier",
                return_value=fake_applier,
            ),
        ):
            apply = _build_local_applier(
                args,
                config=load_mcp_server_config(path=self.config_path),
                plan=plan,
                live_health_validator=self._live_healthy,
                live_validator_provenance="test-validator-v1",
            )
            self.assertEqual(
                apply({}, idempotency_key=plan.plan_fingerprint),
                "applied",
            )

        build_runtime_config.assert_called_once_with(
            env=os.environ,
            require_driver=True,
        )
        create_engine.assert_called_once_with(migration_dsn)
        validate_migration_role.assert_called_once_with(
            engine,
            "phase3_migrator",
        )
        postgres_storage.assert_called_once_with(
            "session-factory",
            mcp_legacy_migration_session_factory="session-factory",
            mcp_legacy_migration_role="phase3_migrator",
        )
        bootstrap_postgres.assert_not_called()
        engine.dispose.assert_called_once_with()

        args.database_path = str(self.root / "forbidden.db")
        with patch(
            "scripts.migrate_legacy_mcp_config.build_state_platform_runtime_config",
            return_value=state_config,
        ):
            with self.assertRaisesRegex(
                MigrationCommandError,
                "postgresql_database_path_forbidden",
            ):
                _build_local_applier(
                    args,
                    config=load_mcp_server_config(path=self.config_path),
                    plan=plan,
                    live_health_validator=self._live_healthy,
                    live_validator_provenance="test-validator-v1",
                )

    @staticmethod
    def _live_healthy(
        request: LegacyMigrationLiveHealthRequest,
    ) -> LegacyMigrationHealthResult:
        catalog = [
            {
                "name": tool_name,
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
            }
            for _capability_id, tool_name, _fingerprint in request.capability_bindings
        ]
        contracts = tuple(
            (capability_id, fingerprint)
            for capability_id, _tool_name, fingerprint in request.capability_bindings
        )
        capabilities = tuple(item[0] for item in contracts)
        return LegacyMigrationHealthResult(
            request.source_server_id,
            1,
            True,
            True,
            True,
            True,
            target_server_id=request.target_server_id,
            source_fingerprint=request.source_fingerprint,
            target_consumer_set_digest=request.target_consumer_set_digest,
            catalog_fingerprint=legacy_migration_catalog_fingerprint(catalog),
            capability_fingerprint=legacy_capability_contract_set_fingerprint(
                contracts
            ),
            available_capability_ids=capabilities,
            available_capability_contracts=contracts,
            observed_at=request.observed_at,
            expires_at=request.expires_at,
        )

    def _healthy_artifact(self) -> dict[str, Any]:
        return self._artifact_with_health(
            LegacyMigrationHealthResult("crm", 1, True, True, True, True)
        )

    def _artifact_with_health(
        self, result: LegacyMigrationHealthResult
    ) -> dict[str, Any]:
        return build_artifact(
            config=load_mcp_server_config(path=self.config_path),
            classifications=load_classifications(self.classifications_path),
            health_validator=lambda _config, plan, _policy: (
                self._bind_health_result(plan, result),
            ),
        )

    @staticmethod
    def _bind_health_result(
        plan: LegacyMigrationPlan,
        result: LegacyMigrationHealthResult,
    ) -> LegacyMigrationHealthResult:
        candidate = plan.mapping_candidates[0]
        impact = plan.consumer_capability_impact[0]
        observed_at = datetime.now(timezone.utc)
        capabilities = impact.exposed_capability_ids if result.healthy else ()
        expected_by_id = {
            obligation.capability_id: obligation.source_contract_fingerprint
            for obligation in impact.obligations
        }
        contracts = tuple(
            (capability_id, expected_by_id[capability_id])
            for capability_id in capabilities
        )
        return LegacyMigrationHealthResult(
            server_id=result.server_id,
            attempts=result.attempts,
            handshake_ok=result.handshake_ok,
            discovery_ok=result.discovery_ok,
            full_paginated_tool_list_ok=result.full_paginated_tool_list_ok,
            nonempty_legal_tool_ok=result.nonempty_legal_tool_ok,
            safe_error_code=result.safe_error_code,
            target_server_id=candidate.target_server_id,
            source_fingerprint=candidate.source_fingerprint,
            target_consumer_set_digest=impact.target_consumer_set_digest,
            catalog_fingerprint=f"sha256:{'3' * 64}",
            capability_fingerprint=legacy_capability_contract_set_fingerprint(
                contracts
            ),
            available_capability_ids=capabilities,
            available_capability_contracts=contracts,
            observed_at=observed_at.isoformat(),
            expires_at=(
                observed_at
                + timedelta(
                    seconds=LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds
                )
            ).isoformat(),
        )

    def _validate(self, artifact: dict[str, Any]):
        return validate_artifact_for_apply(
            artifact,
            current_config=load_mcp_server_config(path=self.config_path),
            current_classifications=load_classifications(self.classifications_path),
        )

    def _args(self, *extra: str) -> list[str]:
        return [
            "--config",
            str(self.config_path),
            "--classifications",
            str(self.classifications_path),
            *extra,
        ]

    def _write_config(
        self,
        server_id: str = "crm",
        endpoint: str = "https://secret-host.example/rpc?token=endpoint-secret",
    ) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        server_id: {
                            "url": endpoint,
                            "headers": {"Secret-Header": "header-secret"},
                            "tools": [
                                {
                                    "name": "lookup",
                                    "expose": True,
                                    "input_schema": {"type": "object"},
                                    "output_schema": {"type": "object"},
                                }
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def _write_classifications(self, servers: dict[str, Any]) -> None:
        normalized = {
            server_id: {
                **value,
                "target_consumer_refs": value.get(
                    "target_consumer_refs", [SERVICE_CONSUMER_REF]
                ),
            }
            for server_id, value in servers.items()
        }
        self.classifications_path.write_text(
            json.dumps({"servers": normalized}), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
