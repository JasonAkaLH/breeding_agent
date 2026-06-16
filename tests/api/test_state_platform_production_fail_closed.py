from __future__ import annotations

import unittest

from src.state.runtime_factory import StatePlatformBackend, StatePlatformConfigError, build_state_platform_runtime_config


class StatePlatformProductionFailClosedTest(unittest.TestCase):
    def test_production_missing_dsn_fails_closed(self) -> None:
        with self.assertRaisesRegex(StatePlatformConfigError, "requires MAF_POSTGRES_STATE_DSN"):
            build_state_platform_runtime_config(env={"MAF_ENV": "production", "MAF_STATE_STORE_BACKEND": "postgresql"}, require_driver=False)

    def test_production_sqlite_backend_is_rejected(self) -> None:
        with self.assertRaisesRegex(StatePlatformConfigError, "does not allow sqlite"):
            build_state_platform_runtime_config(env={"MAF_ENV": "production", "MAF_STATE_STORE_BACKEND": "sqlite"}, require_driver=False)

    def test_postgresql_no_longer_requires_cutover_readiness_flag(self) -> None:
        config = build_state_platform_runtime_config(
            env={"MAF_ENV": "production", "MAF_STATE_STORE_BACKEND": "postgresql", "MAF_POSTGRES_STATE_DSN": "postgresql_fixture_dsn"},
            require_driver=False,
        )
        self.assertEqual(config.backend, StatePlatformBackend.POSTGRESQL)
        self.assertTrue(config.release_gate_configured)

    def test_runtime_sidecar_enforce_writer_conflict_fails_closed(self) -> None:
        with self.assertRaisesRegex(StatePlatformConfigError, "canonical writer conflict"):
            build_state_platform_runtime_config(
                env={
                    "MAF_ENV": "production",
                    "MAF_STATE_STORE_BACKEND": "postgresql",
                    "MAF_POSTGRES_STATE_DSN": "postgresql_fixture_dsn",
                    "MAF_RUNTIME_SIDECAR_WRITER_MODE": "enforce",
                },
                require_driver=False,
            )

    def test_missing_driver_fails_closed_when_driver_required(self) -> None:
        with self.assertRaisesRegex(StatePlatformConfigError, "PostgreSQL driver psycopg is not installed"):
            build_state_platform_runtime_config(
                env={
                    "MAF_ENV": "production",
                    "MAF_STATE_STORE_BACKEND": "postgresql",
                    "MAF_POSTGRES_STATE_DSN": "postgresql_fixture_dsn",
                },
                require_driver=True,
                driver_available=False,
            )
