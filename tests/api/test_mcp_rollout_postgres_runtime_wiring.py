from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.api import runtime as runtime_module


class MCPRolloutPostgresRuntimeWiringTest(unittest.TestCase):
    def test_rollout_ledger_activation_matches_admission_contract(self) -> None:
        off_config = runtime_module.MCPRolloutConfig()
        enforce_config = runtime_module.MCPRolloutConfig(
            gateway_enabled=True,
            routing_mode=runtime_module.MCPRoutingMode.ENFORCE,
            legacy_enabled=True,
            enforce_percent=100,
            enforce_hash_salt="stable-test-salt",
        )

        self.assertFalse(
            runtime_module._mcp_rollout_ledger_is_active(
                config=off_config,
                deployment_env="production",
                env={},
            )
        )
        self.assertTrue(
            runtime_module._mcp_rollout_ledger_is_active(
                config=enforce_config,
                deployment_env="production",
                env={},
            )
        )
        self.assertFalse(
            runtime_module._mcp_rollout_ledger_is_active(
                config=enforce_config,
                deployment_env="test",
                env={},
            )
        )
        self.assertTrue(
            runtime_module._mcp_rollout_ledger_is_active(
                config=off_config,
                deployment_env="test",
                env={"MCP_ROLLOUT_STAGE": "internal_shadow"},
            )
        )

    def test_inactive_ledger_does_not_require_rollout_app_role(self) -> None:
        with patch.object(runtime_module, "create_postgres_engine") as create_engine:
            rollout_runtime = (
                runtime_module._resolve_postgres_mcp_rollout_app_runtime(
                    rollout_ledger_active=False,
                    env={},
                )
            )

        self.assertIsNone(rollout_runtime)
        create_engine.assert_not_called()

    def test_canonical_postgres_runtime_requires_rollout_app_dsn(self) -> None:
        for env in ({}, {"MAF_MCP_ROLLOUT_APP_DSN": "   "}):
            with self.subTest(env=env), self.assertRaisesRegex(
                ValueError,
                "MAF_MCP_ROLLOUT_APP_DSN is required",
            ):
                runtime_module._resolve_postgres_mcp_rollout_app_runtime(
                    rollout_ledger_active=True,
                    env=env,
                )

    def test_canonical_postgres_runtime_rejects_privileged_role_dsns(self) -> None:
        for name in (
            "MAF_MCP_ROLLOUT_SNAPSHOT_DSN",
            "MAF_MCP_ROLLOUT_EVALUATOR_DSN",
            "MAF_MCP_ROLLOUT_OPERATOR_DSN",
            "MAF_MCP_ROLLOUT_DRILL_DSN",
            "MAF_MCP_LEGACY_MIGRATION_DSN",
        ):
            secret_dsn = f"postgresql://sensitive-{name.lower()}"
            with self.subTest(name=name), self.assertRaises(ValueError) as raised:
                runtime_module._resolve_postgres_mcp_rollout_app_runtime(
                    rollout_ledger_active=True,
                    env={
                        "MAF_MCP_ROLLOUT_APP_DSN": "postgresql://app-role",
                        name: secret_dsn,
                    },
                )

            self.assertIn(name, str(raised.exception))
            self.assertNotIn(secret_dsn, str(raised.exception))

    def test_invalid_app_dsn_or_role_fails_without_leaking_dsn(self) -> None:
        secret_dsn = "postgresql://app-user:secret-password@db.invalid/app"
        failure_points = ("engine", "role")
        for failure_point in failure_points:
            fake_engine = object()
            engine_side_effect = (
                RuntimeError(f"could not connect to {secret_dsn}")
                if failure_point == "engine"
                else None
            )
            role_side_effect = (
                RuntimeError(f"wrong role for {secret_dsn}")
                if failure_point == "role"
                else None
            )
            with self.subTest(failure_point=failure_point), patch.object(
                runtime_module,
                "create_postgres_engine",
                return_value=fake_engine,
                side_effect=engine_side_effect,
            ), patch.object(
                runtime_module,
                "validate_mcp_rollout_connection_role",
                side_effect=role_side_effect,
            ), self.assertRaisesRegex(
                RuntimeError,
                "MCP rollout PostgreSQL app role is invalid or unavailable",
            ) as raised:
                runtime_module._resolve_postgres_mcp_rollout_app_runtime(
                    rollout_ledger_active=True,
                    env={"MAF_MCP_ROLLOUT_APP_DSN": secret_dsn},
                )

            self.assertNotIn(secret_dsn, str(raised.exception))

    def test_canonical_off_postgres_runtime_does_not_require_app_dsn(self) -> None:
        state_engine = object()
        storage = object()
        env = {
            "MAF_API_ENV": "production",
            "MAF_STATE_STORE_BACKEND": "postgresql",
            "MAF_POSTGRES_STATE_DSN": "postgresql://state-role",
            "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
            "MCP_ROUTING_MODE": "off",
            "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
            "MCP_ENFORCE_COHORTS": "",
            "MCP_ENFORCE_PERCENT": "0",
            "MCP_ENFORCE_HASH_SALT": "",
            "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
        }
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            env,
            clear=True,
        ), patch.object(
            runtime_module,
            "create_postgres_engine",
            return_value=state_engine,
        ) as create_engine, patch.object(
            runtime_module,
            "bootstrap_postgres_database",
        ), patch.object(
            runtime_module,
            "validate_mcp_rollout_connection_role",
        ) as validate_role, patch.object(
            runtime_module,
            "create_postgres_session_factory",
            return_value="state_session_factory",
        ), patch.object(
            runtime_module,
            "PostgreSQLStorage",
            return_value=storage,
        ) as storage_type:
            runtime = runtime_module.build_api_runtime(
                database_path=Path(tmpdir) / "api.sqlite3",
                audit_log_path=Path(tmpdir) / "audit.jsonl",
                master_key_bytes=b"p" * 32,
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_skill_input_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                skill_roots=(),
            )

        self.assertIs(runtime.storage, storage)
        create_engine.assert_called_once_with("postgresql://state-role")
        validate_role.assert_not_called()
        storage_call = storage_type.call_args
        self.assertNotIn("mcp_rollout_session_factory", storage_call.kwargs)
        self.assertNotIn("mcp_rollout_role", storage_call.kwargs)
        self.assertIsNone(runtime._mcp_rollout_engine)

    def test_canonical_postgres_runtime_wires_distinct_validated_app_session(self) -> None:
        state_engine = object()
        rollout_engine = object()
        storage = object()
        env = {
            "MAF_API_ENV": "test",
            "MAF_STATE_STORE_BACKEND": "postgresql",
            "MAF_POSTGRES_STATE_DSN": "postgresql://state-role",
            "MAF_MCP_ROLLOUT_APP_DSN": "postgresql://rollout-app-role",
            "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
            "MCP_ROUTING_MODE": "off",
            "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
            "MCP_ENFORCE_COHORTS": "",
            "MCP_ENFORCE_PERCENT": "0",
            "MCP_ENFORCE_HASH_SALT": "",
            "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
            "MCP_ROLLOUT_ENVIRONMENT_ID": "staging",
        }
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            env,
            clear=True,
        ), patch.object(
            runtime_module,
            "create_postgres_engine",
            side_effect=(state_engine, rollout_engine),
        ) as create_engine, patch.object(
            runtime_module,
            "bootstrap_postgres_database",
        ) as bootstrap, patch.object(
            runtime_module,
            "validate_mcp_rollout_connection_role",
        ) as validate_role, patch.object(
            runtime_module,
            "create_postgres_session_factory",
            side_effect=("rollout_session_factory", "state_session_factory"),
        ), patch.object(
            runtime_module,
            "PostgreSQLStorage",
            return_value=storage,
        ) as storage_type, patch.object(
            runtime_module,
            "_resolve_mcp_rollout_instance_admission",
            return_value=None,
        ):
            runtime = runtime_module.build_api_runtime(
                database_path=Path(tmpdir) / "api.sqlite3",
                audit_log_path=Path(tmpdir) / "audit.jsonl",
                master_key_bytes=b"p" * 32,
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_skill_input_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                skill_roots=(),
            )

        self.assertIs(runtime.storage, storage)
        self.assertEqual(
            create_engine.call_args_list,
            [
                unittest.mock.call("postgresql://state-role"),
                unittest.mock.call("postgresql://rollout-app-role"),
            ],
        )
        bootstrap.assert_called_once_with(state_engine)
        validate_role.assert_called_once_with(rollout_engine, "app")
        storage_type.assert_called_once()
        storage_call = storage_type.call_args
        self.assertEqual(storage_call.args, ("state_session_factory",))
        self.assertEqual(
            storage_call.kwargs["mcp_rollout_session_factory"],
            "rollout_session_factory",
        )
        self.assertEqual(storage_call.kwargs["mcp_rollout_role"], "app")
        self.assertIs(runtime._mcp_rollout_engine, rollout_engine)


if __name__ == "__main__":
    unittest.main()
