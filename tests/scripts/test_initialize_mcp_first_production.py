from contextlib import redirect_stdout
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch, MagicMock

from scripts import initialize_mcp_first_production as cli


class FirstEnablementCLITest(unittest.TestCase):
    def setUp(self):
        self.args = [
            "check",
            "--database",
            "biobin_test",
            "--git-sha",
            "a" * 40,
            "--backend-image-digest",
            "sha256:" + "b" * 64,
        ]
        self.env = {
            "MAF_API_ENV": "prod",
            "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
            "MCP_ROUTING_MODE": "enforce",
            "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false",
            "MCP_ENFORCE_PERCENT": "100",
            "MCP_ENFORCE_HASH_SALT": "test-salt",
            "MCP_ROLLOUT_STAGE": "legacy_assembly_off",
            "MCP_ROLLOUT_ENVIRONMENT_ID": "prod",
            "MCP_ROLLOUT_DEPLOYMENT_ID": "release",
            "MCP_ROLLOUT_ACTIVATION_ID": "activation",
            "MAF_MCP_INITIALIZATION_ADMIN_DSN": "postgresql+psycopg://admin:SECRET@db/prod",
            "MAF_MCP_ROLLOUT_APP_DSN": "postgresql+psycopg://app:SECRET@db/prod",
        }

    def invoke(self, args=None):
        output = StringIO()
        with redirect_stdout(output):
            code = cli.main(args or self.args)
        return code, json.loads(output.getvalue())

    def test_check_mode_has_no_apply_and_closes_connections(self):
        admin, app = MagicMock(), MagicMock()
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(cli, "validate_production_materials"),
            patch.object(cli, "create_postgres_engine", side_effect=[admin, app]),
            patch.object(
                cli, "initialize_mcp_first_production", return_value={"status": "ready"}
            ) as run,
        ):
            code, result = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(run.call_args.kwargs["apply"])
        admin.dispose.assert_called_once()
        app.dispose.assert_called_once()

    def test_material_or_database_error_never_discloses_credentials(self):
        for failure in ("material", "database"):
            target = (
                "validate_production_materials"
                if failure == "material"
                else "create_postgres_engine"
            )
            with (
                patch.dict(os.environ, self.env, clear=True),
                patch.object(cli, "validate_production_materials"),
                patch.object(
                    cli, target, side_effect=RuntimeError("SECRET password DSN body")
                ),
            ):
                code, result = self.invoke()
            self.assertEqual(code, 1)
            self.assertNotIn("SECRET", json.dumps(result))

    def test_dev_and_apply_without_report_are_rejected(self):
        for env, args in (
            ({**self.env, "MAF_API_ENV": "dev"}, self.args),
            (self.env, ["apply", *self.args[1:]]),
        ):
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(cli, "validate_production_materials"),
                patch.object(cli, "create_postgres_engine") as connect,
            ):
                code, _ = self.invoke(args)
            self.assertEqual(code, 1)
            connect.assert_not_called()
