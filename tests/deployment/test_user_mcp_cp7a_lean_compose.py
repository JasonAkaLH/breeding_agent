from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"
DOCKERFILE_PATH = ROOT / "Dockerfile"


class UserMCPCP7ALeanComposeTest(unittest.TestCase):
    def test_compose_has_exact_three_services_and_cp7a_environment(self) -> None:
        compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(
            set(compose["services"]),
            {"runtime-sidecar", "backend", "frontend"},
        )
        backend = compose["services"]["backend"]
        environment = backend["environment"]
        self.assertEqual(
            {
                key: environment[key]
                for key in (
                    "MCP_USER_SCOPED_GATEWAY_ENABLED",
                    "MCP_ROUTING_MODE",
                    "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED",
                    "MCP_ENFORCE_COHORTS",
                    "MCP_ENFORCE_PERCENT",
                    "MCP_ENFORCE_HASH_SALT",
                    "MCP_ENFORCE_COHORT_CONFIG_FILE",
                )
            },
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false",
                "MCP_ENFORCE_COHORTS": "",
                "MCP_ENFORCE_PERCENT": "100",
                "MCP_ENFORCE_HASH_SALT": "main-cp7a-user-scoped-v1",
                "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
            },
        )
        self.assertEqual(environment["MAF_API_ENV"], "dev")
        self.assertEqual(environment["MAF_STATE_STORE_BACKEND"], "sqlite")
        self.assertEqual(environment["MAF_STATE_PLATFORM_CONFIG_BRIDGE"], "0")
        self.assertEqual(
            environment["MAF_RUNTIME_SIDECAR_ENDPOINT"],
            "unix:///run/maf-runtime-sidecar/runtime.sock",
        )
        self.assertEqual(environment["MAF_MASTER_KEY_FILE"], "/run/secrets/maf-master.key")
        self.assertNotIn("MCP_CREDENTIAL_KEY_FILE", environment)
        self.assertEqual(environment["MAF_USER_MCP_MAX_ACTIVE_CALLS"], "8")
        self.assertEqual(
            environment["MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES"],
            "1048576",
        )
        for key in (
            "MAF_RUST_RUNTIME_STORE_MODE",
            "MAF_RUST_EVENT_LOG_MODE",
            "MAF_RUST_TASK_DISPATCHER_MODE",
        ):
            self.assertEqual(environment[key], "off")

        self.assertEqual(
            backend["depends_on"]["runtime-sidecar"]["condition"],
            "service_healthy",
        )
        self.assertEqual(
            compose["services"]["frontend"]["depends_on"]["backend"]["condition"],
            "service_healthy",
        )

    def test_compose_requires_host_master_key_path_and_renders_with_fixture(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("Docker CLI is unavailable")
        missing = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_PATH), "config", "--quiet"],
            cwd=ROOT,
            env={key: value for key, value in os.environ.items() if key != "MAF_MASTER_KEY_FILE_HOST"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("MAF_MASTER_KEY_FILE_HOST is required", missing.stderr)

        rendered = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_PATH), "config", "--quiet"],
            cwd=ROOT,
            env={**os.environ, "MAF_MASTER_KEY_FILE_HOST": "/tmp/cp7a-test.key"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)

    def test_runtime_sidecar_target_is_minimal_and_semantically_probed(self) -> None:
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.assertIn("FROM rust:1.95-bookworm AS runtime-sidecar-build", dockerfile)
        self.assertIn(
            "cargo build --locked --release -p maf_runtime_sidecar --bin maf-runtime-sidecar",
            dockerfile,
        )
        self.assertIn("FROM debian:bookworm-slim AS runtime-sidecar", dockerfile)
        self.assertIn(
            '["/usr/local/bin/maf-runtime-sidecar", "--probe", '
            '"unix:///run/maf-runtime-sidecar/runtime.sock"]',
            dockerfile,
        )
        for forbidden in ("SBOM", "provenance", "allowlist", "validation-runner"):
            self.assertNotIn(forbidden, dockerfile)


if __name__ == "__main__":
    unittest.main()
