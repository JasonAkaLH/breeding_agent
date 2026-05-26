from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.state.runtime_factory import StatePlatformBackend, build_state_platform_runtime_config


class StatePlatformRuntimeAssemblyTest(unittest.TestCase):
    def test_dev_default_keeps_legacy_sqlite_boundary(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = build_state_platform_runtime_config(env={"MAF_ENV": "dev"})
        self.assertEqual(config.backend, StatePlatformBackend.SQLITE_LEGACY)
        self.assertFalse(config.release_gate_configured)
        self.assertEqual(config.reason, "dev_test_sqlite_legacy")

    def test_postgresql_backend_assembles_without_leaking_dsn(self) -> None:
        env = {
            "MAF_ENV": "staging",
            "MAF_STATE_STORE_BACKEND": "postgresql",
            "MAF_POSTGRES_STATE_DSN": "postgresql_fixture_dsn",
            "MAF_STATE_PLATFORM_MIGRATION_READY": "1",
        }
        config = build_state_platform_runtime_config(env=env, require_driver=False)
        self.assertEqual(config.backend, StatePlatformBackend.POSTGRESQL)
        self.assertTrue(config.release_gate_configured)
        self.assertNotIn("user:pass", repr(config.public_dict()))
    def test_legacy_migration_ready_env_name_is_accepted_for_runtime_compatibility(self) -> None:
        env = {
            "MAF_ENV": "staging",
            "MAF_STATE_STORE_BACKEND": "postgresql",
            "MAF_POSTGRES_STATE_DSN": "postgresql_fixture_dsn",
            "MAF_POSTGRES_STATE_MIGRATION_READY": "ready",
        }
        config = build_state_platform_runtime_config(env=env, require_driver=False)
        self.assertTrue(config.migration_ready)
