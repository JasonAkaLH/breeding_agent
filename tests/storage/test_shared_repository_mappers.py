from __future__ import annotations

import ast
import unittest
from pathlib import Path

from src.storage import mcp_legacy_records, row_mappers
from src.storage.postgres import repositories as postgres_repositories
from src.storage.sqlite import repositories as sqlite_repositories


ROW_MAPPER_NAMES = (
    "_mcp_owner_server_set_fingerprint",
    "_row_to_conversation",
    "_row_to_mcp_remote_task",
    "_row_to_mcp_rollout_block_resolution",
    "_row_to_mcp_rollout_deployment_activation",
    "_row_to_mcp_rollout_drill_observation",
    "_row_to_mcp_rollout_evidence_snapshot",
    "_row_to_mcp_rollout_gate_scope",
    "_row_to_mcp_rollout_instance_config",
    "_row_to_mcp_rollout_metric_bucket",
    "_row_to_mcp_rollout_promotion_block",
    "_row_to_mcp_rollout_stage_approval",
    "_row_to_mcp_shadow_audit_sample",
)
LEGACY_HELPER_NAMES = (
    "_mcp_legacy_migration_record_values",
    "_user_mcp_server_insert_values",
    "_validate_mcp_legacy_migration_record",
)


class SharedRepositoryMappersTest(unittest.TestCase):
    def test_sqlite_and_postgres_share_one_mapper_owner(self) -> None:
        for name in ROW_MAPPER_NAMES:
            shared = getattr(row_mappers, name)
            self.assertIs(getattr(sqlite_repositories, name), shared, name)
            self.assertIs(getattr(postgres_repositories, name), shared, name)
            self.assertEqual(shared.__module__, "src.storage.row_mappers")

    def test_legacy_record_helpers_have_one_function_body(self) -> None:
        repository = sqlite_repositories.SQLiteStateRepository
        for name in LEGACY_HELPER_NAMES:
            shared = getattr(mcp_legacy_records, name)
            self.assertIs(getattr(repository, name), shared, name)
            self.assertIs(getattr(postgres_repositories, name), shared, name)
            self.assertEqual(shared.__module__, "src.storage.mcp_legacy_records")

    def test_postgres_imports_no_sqlite_private_helpers(self) -> None:
        root = Path(__file__).resolve().parents[2]
        postgres_tree = ast.parse(
            (root / "src/storage/postgres/repositories.py").read_text(
                encoding="utf-8"
            )
        )
        sqlite_imports = {
            alias.name
            for node in ast.walk(postgres_tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "src.storage.sqlite.repositories"
            for alias in node.names
        }
        self.assertEqual(
            sqlite_imports,
            {"SQLiteStateRepository", "SQLiteStorage"},
        )


if __name__ == "__main__":
    unittest.main()
