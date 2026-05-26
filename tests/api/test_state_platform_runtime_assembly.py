from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.state.runtime_factory import StatePlatformBackend, build_state_platform_runtime_config


class StatePlatformRuntimeAssemblyTest(unittest.TestCase):
    def test_dev_default_keeps_legacy_sqlite_boundary(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = build_state_platform_runtime_config(env={"MAF_ENV": "dev"})
        self.assertEqual(config.backend, StatePlatformBackend.SQLITE_LEGACY)
        self.assertFalse(config.release_gate_configured)
        self.assertEqual(config.reason, "dev_test_sqlite_legacy")

    def test_postgresql_backend_assembles_without_cutover_gate_or_dsn_leak(self) -> None:
        env = {
            "MAF_ENV": "staging",
            "MAF_STATE_STORE_BACKEND": "postgresql",
            "MAF_POSTGRES_STATE_DSN": "postgresql_fixture_dsn",
        }
        config = build_state_platform_runtime_config(env=env, require_driver=False)
        self.assertEqual(config.backend, StatePlatformBackend.POSTGRESQL)
        self.assertTrue(config.release_gate_configured)
        self.assertEqual(config.reason, "postgresql_state_platform_configured")
        public = config.public_dict()
        self.assertNotIn("cutover_ready", public)
        self.assertNotIn("migration_ready", public)
        self.assertNotIn("postgresql_fixture_dsn", repr(public))

    def test_api_runtime_bootstraps_postgres_schema_without_cutover_readiness_gate(self) -> None:
        from src.api import runtime as runtime_module

        fake_engine = object()
        fake_storage = object()
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "postgresql",
                "MAF_POSTGRES_STATE_DSN": "postgresql_fixture_dsn",
            },
            clear=True,
        ), patch.object(runtime_module, "create_postgres_engine", return_value=fake_engine) as create_engine, patch.object(
            runtime_module, "bootstrap_postgres_database"
        ) as bootstrap, patch.object(runtime_module, "create_postgres_session_factory", return_value="session_factory"), patch.object(
            runtime_module, "PostgreSQLStorage", return_value=fake_storage
        ):
            runtime = runtime_module.build_api_runtime(
                database_path=Path(tmpdir) / "api.sqlite3",
                audit_log_path=Path(tmpdir) / "audit.jsonl",
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_skill_input_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
            )
        create_engine.assert_called_once_with("postgresql_fixture_dsn")
        bootstrap.assert_called_once_with(fake_engine)
        self.assertIs(runtime.storage, fake_storage)


class StatePlatformRuntimeConfigBridgeTest(unittest.TestCase):
    def test_state_platform_config_bridge_exports_postgres_env_without_cutover_gate(self) -> None:
        from src.api import runtime as runtime_module
        with patch.dict(os.environ, {}, clear=True), patch.object(
            runtime_module,
            "load_config",
            return_value={
                "state_platform": {
                    "enabled": True,
                    "backend": "postgresql",
                    "postgres": {"dsn": "postgresql_fixture_dsn", "schema": "public"},
                }
            },
        ):
            runtime_module._bootstrap_state_platform_config_env()
            config = build_state_platform_runtime_config(env=os.environ, require_driver=False)
            schema = os.environ["MAF_POSTGRES_STATE_SCHEMA"]
        self.assertEqual(config.backend, StatePlatformBackend.POSTGRESQL)
        self.assertTrue(config.release_gate_configured)
        self.assertEqual(schema, "public")
        self.assertNotIn("MAF_STATE_PLATFORM_CUTOVER_READY", os.environ)
        self.assertNotIn("postgresql_fixture_dsn", repr(config.public_dict()))
