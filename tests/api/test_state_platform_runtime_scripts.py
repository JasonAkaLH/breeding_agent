from __future__ import annotations

import json
import subprocess
import sys
import unittest


class StatePlatformRuntimeScriptsTest(unittest.TestCase):
    def test_runtime_validator_reports_fail_closed_without_secret(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/validate_postgresql_state_platform_runtime.py", "--env", "production", "--backend", "postgresql", "--json"],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("MAF_POSTGRES_STATE_DSN", payload["error"])
        self.assertNotIn("postgresql://", result.stdout)

    def test_runtime_validator_requires_driver_by_default_when_dsn_present(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/validate_postgresql_state_platform_runtime.py",
                "--env",
                "production",
                "--backend",
                "postgresql",
                "--dsn-env",
                "MAF_POSTGRES_STATE_DSN",
                "--migration-ready",
                "--simulate-missing-driver",
                "--json",
            ],
            check=False,
            text=True,
            capture_output=True,
            env={"MAF_POSTGRES_STATE_DSN": "postgresql_fixture_dsn"},
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertIn("driver psycopg is not installed", payload["error"])

    def test_runtime_validator_rejects_raw_dsn_cli_argument(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/validate_postgresql_state_platform_runtime.py",
                "--env",
                "production",
                "--backend",
                "postgresql",
                "--dsn",
                "postgresql_fixture_dsn",
                "--json",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertIn("raw DSN CLI arguments are not allowed", payload["error"])

    def test_runtime_validator_treats_capitalized_production_as_driver_required(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/validate_postgresql_state_platform_runtime.py",
                "--env",
                "Production",
                "--backend",
                "postgresql",
                "--dsn-env",
                "MAF_POSTGRES_STATE_DSN",
                "--migration-ready",
                "--simulate-missing-driver",
                "--allow-missing-driver",
                "--json",
            ],
            check=False,
            text=True,
            capture_output=True,
            env={"MAF_POSTGRES_STATE_DSN": "postgresql_fixture_dsn"},
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertIn("driver psycopg is not installed", payload["error"])
