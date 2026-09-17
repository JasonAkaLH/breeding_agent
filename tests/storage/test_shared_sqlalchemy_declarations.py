from __future__ import annotations

import ast
import unittest
from pathlib import Path

from src.state.postgres import runtime_schema
from src.storage import sqlalchemy_models
from src.storage import sqlalchemy_base
from src.storage.sqlalchemy_base import (
    DateTimeText,
    JSONText,
    NAMING_CONVENTION,
    SQLiteBase,
)
from src.storage.sqlite import base as legacy_base
from src.storage.sqlite import models as legacy_models


class SharedSQLAlchemyDeclarationsTest(unittest.TestCase):
    _AWARE_DATETIME_FIELDS = {
        "MCPLegacyMigrationRecordRow": {"occurred_at", "evidence_expires_at"},
        "MCPRolloutGateScopeRow": {"created_at"},
        "MCPRolloutDrillObservationRow": {
            "observed_at",
            "recorded_at",
            "expires_at",
        },
        "MCPRolloutMetricBucketRow": {
            "bucket_started_at",
            "bucket_ended_at",
            "created_at",
            "updated_at",
        },
        "MCPRolloutEvidenceSnapshotRow": {
            "window_started_at",
            "window_ended_at",
            "recorded_at",
        },
        "MCPShadowAuditSampleRow": {"observed_at", "recorded_at", "expires_at"},
        "MCPRolloutStageApprovalRow": {"created_at"},
        "MCPRolloutDeploymentActivationRow": {"created_at"},
        "MCPRolloutPromotionBlockRow": {"created_at"},
        "MCPRolloutBlockResolutionRow": {"created_at"},
        "MCPRolloutInstanceConfigRow": {
            "lease_expires_at",
            "created_at",
            "updated_at",
        },
        "MAFMasterKeyValidationRow": {"created_at"},
        "MCPCP7SafetyLedgerRow": {
            "bucket_started_at",
            "bucket_ended_at",
            "recorded_at",
        },
        "MCPCP7ReadyEpochEventRow": {"boundary_at"},
        "MCPCP7CandidateGuardRow": {
            "first_invalid_at",
            "created_at",
            "updated_at",
        },
    }

    def test_sqlite_compat_paths_reexport_shared_objects(self) -> None:
        self.assertIs(legacy_base.SQLiteBase, SQLiteBase)
        self.assertIs(legacy_base.JSONText, JSONText)
        self.assertIs(legacy_base.DateTimeText, DateTimeText)
        self.assertIs(legacy_base.NAMING_CONVENTION, NAMING_CONVENTION)

        root = Path(__file__).resolve().parents[2]
        shared_tree = ast.parse(
            (root / "src/storage/sqlalchemy_models.py").read_text(encoding="utf-8")
        )
        row_names = tuple(
            node.name for node in shared_tree.body if isinstance(node, ast.ClassDef)
        )
        self.assertEqual(len(row_names), 61)
        self.assertEqual(tuple(legacy_models.__all__), row_names)
        for name in row_names:
            shared_row = getattr(sqlalchemy_models, name)
            self.assertIs(getattr(legacy_models, name), shared_row, name)
            self.assertIs(shared_row.__table__.metadata, SQLiteBase.metadata, name)
            self.assertEqual(shared_row.__module__, "src.storage.sqlalchemy_models")

    def test_legacy_modules_contain_no_second_declaration_owner(self) -> None:
        root = Path(__file__).resolve().parents[2]
        for relative_path in (
            "src/storage/sqlite/base.py",
            "src/storage/sqlite/models.py",
        ):
            tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
            self.assertFalse(
                any(isinstance(node, ast.ClassDef) for node in tree.body),
                relative_path,
            )

    def test_postgres_runtime_schema_uses_the_shared_metadata(self) -> None:
        self.assertEqual(len(SQLiteBase.metadata.tables), 61)
        self.assertEqual(
            runtime_schema.POSTGRES_RUNTIME_TABLES,
            tuple(sorted(SQLiteBase.metadata.tables)),
        )

    def test_datetime_fields_use_exact_awareness_contracts(self) -> None:
        aware_type = getattr(sqlalchemy_base, "AwareUTCDateTimeText")
        aware_actual: dict[str, set[str]] = {}
        ordinary_count = 0
        aware_count = 0

        for name in legacy_models.__all__:
            row = getattr(sqlalchemy_models, name)
            for column in row.__table__.columns:
                if type(column.type) is aware_type:
                    aware_actual.setdefault(name, set()).add(column.name)
                    aware_count += 1
                elif type(column.type) is DateTimeText:
                    ordinary_count += 1

        self.assertEqual(aware_actual, self._AWARE_DATETIME_FIELDS)
        self.assertEqual(aware_count, 31)
        self.assertEqual(ordinary_count, 138)


if __name__ == "__main__":
    unittest.main()
