from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import produce_user_mcp_shadow_evidence as producer


class ProduceUserMCPShadowEvidenceContractTest(unittest.TestCase):
    def test_cli_has_no_caller_supplied_payload_or_sample_json(self) -> None:
        parser = producer._parser()
        option_strings = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        self.assertNotIn("--payload", option_strings)
        self.assertNotIn("--samples", option_strings)
        self.assertNotIn("--metrics", option_strings)

    def test_producer_uses_atomic_storage_boundary(self) -> None:
        source = inspect.getsource(producer.produce)
        self.assertIn("produce_mcp_shadow_evidence_snapshot", source)
        self.assertIn("metric_records_to_domain", source)
        self.assertIn("build_internal_shadow_evidence_payload", source)

    def test_postgres_uses_only_snapshot_role_dsn_and_validates_login(self) -> None:
        engine = Mock()
        storage = object()
        factory = object()
        with (
            patch.dict(
                os.environ,
                {"MAF_MCP_ROLLOUT_SNAPSHOT_DSN": "postgresql+psycopg://snapshot"},
                clear=True,
            ),
            patch.object(
                producer,
                "build_state_platform_runtime_config",
                return_value=SimpleNamespace(
                    backend=producer.StatePlatformBackend.POSTGRESQL
                ),
            ),
            patch.object(
                producer, "create_postgres_engine", return_value=engine
            ) as create_engine,
            patch.object(
                producer, "create_postgres_session_factory", return_value=factory
            ),
            patch.object(
                producer, "validate_mcp_rollout_connection_role"
            ) as validate_role,
            patch.object(producer, "PostgreSQLStorage", return_value=storage) as storage_cls,
        ):
            configured, configured_engine = producer._storage("ignored.sqlite3")
        self.assertIs(configured, storage)
        self.assertIs(configured_engine, engine)
        create_engine.assert_called_once_with("postgresql+psycopg://snapshot")
        validate_role.assert_called_once_with(engine, "snapshot")
        storage_cls.assert_called_once_with(
            factory,
            mcp_rollout_session_factory=factory,
            mcp_rollout_role="snapshot",
        )

    def test_postgres_role_failure_masks_dsn(self) -> None:
        engine = Mock()
        secret = "postgresql+psycopg://snapshot:secret-password@db/rollout"
        with (
            patch.dict(
                os.environ,
                {"MAF_MCP_ROLLOUT_SNAPSHOT_DSN": secret},
                clear=True,
            ),
            patch.object(
                producer,
                "build_state_platform_runtime_config",
                return_value=SimpleNamespace(
                    backend=producer.StatePlatformBackend.POSTGRESQL
                ),
            ),
            patch.object(producer, "create_postgres_engine", return_value=engine),
            patch.object(
                producer,
                "validate_mcp_rollout_connection_role",
                side_effect=RuntimeError(secret),
            ),
            self.assertRaisesRegex(ValueError, "snapshot role is invalid") as caught,
        ):
            producer._storage("ignored.sqlite3")
        self.assertNotIn(secret, str(caught.exception))
        engine.dispose.assert_called_once_with()

    def test_postgres_producer_does_not_forward_caller_materialization_fields(self) -> None:
        calls = []

        class FakePostgreSQLStorage:
            async def produce_mcp_rollout_evidence_snapshot_db_derived(
                self, environment_id, deployment_id, **kwargs
            ):
                calls.append((environment_id, deployment_id, kwargs))
                return SimpleNamespace(evidence_id="db-derived")

        engine = Mock()
        args = SimpleNamespace(
            database_path="ignored.sqlite3",
            environment_id="production",
            deployment_id="deploy-a",
            git_sha="a" * 40,
            window_started_at="2026-08-13T00:00:00Z",
            window_ended_at="2026-08-13T01:00:00Z",
            attestation_key_id="key-a",
            config_fingerprint="caller-config",
            manifest_fingerprint="caller-manifest",
            fixture_fingerprint="caller-fixture",
            mapping_fingerprint="caller-mapping",
            evidence_id="caller-evidence",
            nonce="caller-nonce",
            snapshot_id=999,
        )
        with (
            patch.object(producer, "PostgreSQLStorage", FakePostgreSQLStorage),
            patch.object(
                producer,
                "_storage",
                return_value=(FakePostgreSQLStorage(), engine),
            ),
            patch.object(producer, "_attestation_key", return_value=b"test-key"),
        ):
            result = asyncio.run(producer.produce(args))
        self.assertEqual(result.evidence_id, "db-derived")
        self.assertEqual(
            calls,
            [
                (
                    "production",
                    "deploy-a",
                    {
                        "git_sha": "a" * 40,
                        "window_started_at": producer._timestamp(
                            "2026-08-13T00:00:00Z"
                        ),
                        "window_ended_at": producer._timestamp(
                            "2026-08-13T01:00:00Z"
                        ),
                        "attestation_key_id": "key-a",
                        "attestation_key": b"test-key",
                    },
                )
            ],
        )
        engine.dispose.assert_called_once_with()
