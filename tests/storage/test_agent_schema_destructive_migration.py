from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.storage import agent_schema_migration as migration
from src.storage.agent_schema_migration import (
    AgentSchemaMigrationError,
    apply_all,
    backup_all,
    build_report,
    load_state_descriptor,
    remember_report_path,
    restore_all,
    restore_check,
    write_report,
)


POSTGRES_PRE = {
    "schema_version": "pre-p7",
    "agent_row_counts": {"agent_runs": 1},
    "agent_data_digests": {"agent_runs": "sha256:" + "1" * 64},
    "table_row_counts": {
        "agent_runs": 1,
        "artifact": 1,
        "planner_replan_claim": 1,
        "task": 1,
        "task_edge": 1,
        "task_node": 1,
    },
    "schema_digest": "sha256:" + "2" * 64,
    "dag_objects": {
        "task_edge_table": True,
        "task_root_node_id": True,
        "task_node_fields": [
            "criticality",
            "dependency_type",
            "resource_class",
            "retry_policy",
            "timeout_policy",
        ],
        "planner_replan_claim_table": True,
    },
}

POSTGRES_POST = {
    **POSTGRES_PRE,
    "schema_version": "agent-only-v1",
    "table_row_counts": {
        "agent_runs": 1,
        "artifact": 1,
        "task": 1,
        "task_node": 1,
    },
    "schema_digest": "sha256:" + "3" * 64,
    "dag_objects": {
        "task_edge_table": False,
        "task_root_node_id": False,
        "task_node_fields": [],
        "planner_replan_claim_table": False,
    },
}


class AgentSchemaDestructiveMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.state_root.mkdir(mode=0o700)
        self.sqlite_path = self.root / "runtime.sqlite3"
        self.sidecar_path = self.root / "sidecar.sqlite3"
        self.binary_path = self.root / "maf-runtime-sidecar"
        self.binary_path.write_text("test-only", encoding="utf-8")
        os.chmod(self.binary_path, 0o700)
        self._create_main_database()
        self._create_sidecar_database()
        state_path = self.state_root / migration.STATE_FILE
        state_path.write_text(
            json.dumps(
                {
                    "schema": migration.STATE_SCHEMA,
                    "writers_quiesced": True,
                    "postgres_snapshot_ready": True,
                    "tested_commit": "a" * 40,
                    "tested_tree": "b" * 40,
                    "sqlite": {
                        "path": str(self.sqlite_path),
                        "agent_tables": ["agent_runs"],
                    },
                    "sidecar": {
                        "path": str(self.sidecar_path),
                        "binary_env": "TEST_P7_SIDECAR_BINARY",
                        "agent_tables": ["agent_runs"],
                    },
                    "postgres": {
                        "dsn_env": "TEST_P7_SOURCE_DSN",
                        "restore_dsn_env": "TEST_P7_RESTORE_DSN",
                        "agent_tables": ["agent_runs"],
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(state_path, 0o600)
        self.environment = patch.dict(
            os.environ,
            {
                "TEST_P7_SOURCE_DSN": "postgresql://source-secret",
                "TEST_P7_RESTORE_DSN": "postgresql://restore-secret",
                "TEST_P7_SIDECAR_BINARY": str(self.binary_path),
            },
        )
        self.environment.start()
        self.postgres_is_post = False
        self.postgres_report = patch(
            "src.storage.agent_schema_migration._postgres_report",
            side_effect=self._postgres_inventory,
        )
        self.postgres_report.start()
        self.sidecar_probe = patch(
            "src.storage.agent_schema_migration._probe_restored_sidecar"
        )
        self.sidecar_probe.start()
        self.descriptor = load_state_descriptor(self.state_root)

    def tearDown(self) -> None:
        self.sidecar_probe.stop()
        self.postgres_report.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def _create_main_database(self) -> None:
        with contextlib.closing(sqlite3.connect(self.sqlite_path)) as connection:
            connection.executescript(
                "CREATE TABLE agent_runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);"
                "CREATE TABLE task (id TEXT PRIMARY KEY, root_node_id TEXT, body TEXT);"
                "CREATE TABLE task_node (id TEXT PRIMARY KEY, criticality TEXT, "
                "dependency_type TEXT, retry_policy TEXT, timeout_policy TEXT, "
                "resource_class TEXT, body TEXT);"
                "CREATE TABLE task_edge (id TEXT PRIMARY KEY);"
                "CREATE TABLE planner_replan_claim (id TEXT PRIMARY KEY);"
                "CREATE TABLE artifact (id TEXT PRIMARY KEY, body TEXT);"
            )
            connection.execute("INSERT INTO agent_runs VALUES ('run-1', 'agent')")
            connection.execute("INSERT INTO task VALUES ('task-1', 'node-1', 'keep')")
            connection.execute(
                "INSERT INTO task_node VALUES "
                "('node-1', 'required', 'hard', '{}', '{}', 'default', 'keep')"
            )
            connection.execute("INSERT INTO task_edge VALUES ('edge-1')")
            connection.execute("INSERT INTO planner_replan_claim VALUES ('claim-1')")
            connection.execute("INSERT INTO artifact VALUES ('artifact-1', 'keep')")
            connection.commit()
        os.chmod(self.sqlite_path, 0o600)

    def _create_sidecar_database(self) -> None:
        node = json.dumps(
            {
                "node_id": "node-1",
                "criticality": "required",
                "dependency_type": "hard",
                "retry_policy": {},
                "timeout_policy": {},
                "resource_class": "default",
                "status": "running",
            },
            sort_keys=True,
        )
        with contextlib.closing(sqlite3.connect(self.sidecar_path)) as connection:
            connection.executescript(
                "CREATE TABLE agent_runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);"
                "CREATE TABLE submitted_tasks (id TEXT PRIMARY KEY, root_node_id TEXT, body TEXT);"
                "CREATE TABLE task_submit_idempotency "
                "(id TEXT PRIMARY KEY, root_node_id TEXT, body TEXT);"
                "CREATE TABLE task_nodes (id TEXT PRIMARY KEY, node_json TEXT NOT NULL);"
                "CREATE TABLE node_transition_idempotency "
                "(id TEXT PRIMARY KEY, node_json TEXT);"
                "CREATE TABLE task_edges (id TEXT PRIMARY KEY);"
                "CREATE TABLE task_edge_idempotency (id TEXT PRIMARY KEY);"
                "CREATE TABLE planner_replan_claims (id TEXT PRIMARY KEY);"
                "CREATE TABLE artifact (id TEXT PRIMARY KEY, body TEXT);"
            )
            connection.execute("INSERT INTO agent_runs VALUES ('run-1', 'agent')")
            connection.execute(
                "INSERT INTO submitted_tasks VALUES ('task-1', 'node-1', 'keep')"
            )
            connection.execute(
                "INSERT INTO task_submit_idempotency VALUES ('submit-1', 'node-1', 'keep')"
            )
            connection.execute("INSERT INTO task_nodes VALUES ('node-1', ?)", (node,))
            connection.execute(
                "INSERT INTO node_transition_idempotency VALUES ('transition-1', ?)",
                (node,),
            )
            connection.execute("INSERT INTO task_edges VALUES ('edge-1')")
            connection.execute("INSERT INTO task_edge_idempotency VALUES ('edge-write-1')")
            connection.execute("INSERT INTO planner_replan_claims VALUES ('claim-1')")
            connection.execute("INSERT INTO artifact VALUES ('artifact-1', 'keep')")
            connection.commit()
        os.chmod(self.sidecar_path, 0o600)

    def _postgres_inventory(self, dsn: str, _tables) -> dict[str, object]:
        if dsn == "postgresql://source-secret" and self.postgres_is_post:
            return dict(POSTGRES_POST)
        return dict(POSTGRES_PRE)

    def _runner(self, arguments, **kwargs):
        if arguments[0] == "pg_dump":
            output = Path(kwargs["cwd"]) / arguments[arguments.index("--file") + 1]
            output.write_bytes(b"postgres-custom-format-dump")
            os.chmod(output, 0o600)
        elif (
            arguments[0] == "pg_restore"
            and kwargs["env"]["PGDATABASE"] == "postgresql://source-secret"
        ):
            self.postgres_is_post = False
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def _prepare(self):
        report = build_report(self.descriptor)
        report_path = self.state_root / "report.json"
        write_report(report_path, report)
        remember_report_path(self.descriptor, report_path, report)
        manifest = backup_all(
            self.descriptor,
            report_path=report_path,
            expected_report_sha=str(report["report_sha256"]),
            backup_root=self.root / "backups",
            command_runner=self._runner,
        )
        manifest_path = (
            self.root
            / "backups"
            / str(manifest["backup_set_id"])
            / "manifest.json"
        )
        restore_check(
            self.descriptor,
            manifest_path=manifest_path,
            expected_backup_set_sha=str(manifest["backup_set_sha256"]),
            restore_root=self.root / "restore-check",
            command_runner=self._runner,
        )
        return (
            report,
            report_path,
            manifest,
            manifest_path,
            self.state_root / migration.RECEIPT_DIRECTORY / "02-restore_verified.json",
        )

    def _fake_postgres_apply(self, descriptor, **kwargs):
        receipt = migration._write_apply_receipt(
            descriptor,
            state="applying_postgres",
            report_sha=kwargs["report_sha"],
            backup_set_sha=kwargs["backup_set_sha"],
            pre_versions=kwargs["pre_versions"],
        )
        self.postgres_is_post = True
        return receipt

    def test_apply_removes_only_dag_storage_and_restore_all_recovers_every_backend(self) -> None:
        report, report_path, manifest, manifest_path, restore_receipt = self._prepare()
        with (
            patch(
                "src.storage.agent_schema_migration._apply_postgres_locked",
                side_effect=self._fake_postgres_apply,
            ),
            patch(
                "src.storage.agent_schema_migration._postgres_lock",
                side_effect=lambda _dsn: contextlib.nullcontext(),
            ),
        ):
            completed = apply_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                manifest_path=manifest_path,
                expected_backup_set_sha=str(manifest["backup_set_sha256"]),
                restore_receipt_path=restore_receipt,
            )
            retried = apply_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                manifest_path=manifest_path,
                expected_backup_set_sha=str(manifest["backup_set_sha256"]),
                restore_receipt_path=restore_receipt,
            )
        self.assertEqual(completed, retried)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(
            [item["state"] for item in migration._receipt_chain(self.state_root)],
            list(migration.RECEIPT_ORDER),
        )
        for path in (self.sqlite_path, self.sidecar_path):
            inventory = migration._sqlite_report(path, ("agent_runs",))
            self.assertEqual(inventory["schema_version"], "agent-only-v1")
            self.assertEqual(inventory["agent_row_counts"], {"agent_runs": 1})

        restored = restore_all(
            self.descriptor,
            manifest_path=manifest_path,
            expected_backup_set_sha=str(manifest["backup_set_sha256"]),
            command_runner=self._runner,
        )
        self.assertEqual(restored["state"], "restored")
        self.assertEqual(
            migration._semantic_backends(build_report(self.descriptor)["backends"]),
            migration._semantic_backends(report["backends"]),
        )

    def test_uncertain_applying_prefix_requires_restore_instead_of_reexecution(self) -> None:
        report, report_path, manifest, manifest_path, restore_receipt = self._prepare()
        migration._write_apply_receipt(
            self.descriptor,
            state="applying_sqlite",
            report_sha=str(report["report_sha256"]),
            backup_set_sha=str(manifest["backup_set_sha256"]),
            pre_versions=migration._schema_versions(report),
        )
        with self.assertRaisesRegex(
            AgentSchemaMigrationError, "sqlite_partial_apply_requires_restore"
        ):
            apply_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                manifest_path=manifest_path,
                expected_backup_set_sha=str(manifest["backup_set_sha256"]),
                restore_receipt_path=restore_receipt,
            )
        self.assertEqual(
            migration._current_receipt(self.state_root)["state"], "applying_sqlite"
        )
        restored = restore_all(
            self.descriptor,
            manifest_path=manifest_path,
            expected_backup_set_sha=str(manifest["backup_set_sha256"]),
            command_runner=self._runner,
        )
        self.assertEqual(restored["state"], "restored")

    def test_completed_retry_revalidates_post_migration_inventory(self) -> None:
        report, report_path, manifest, manifest_path, restore_receipt = self._prepare()
        with (
            patch(
                "src.storage.agent_schema_migration._apply_postgres_locked",
                side_effect=self._fake_postgres_apply,
            ),
            patch(
                "src.storage.agent_schema_migration._postgres_lock",
                side_effect=lambda _dsn: contextlib.nullcontext(),
            ),
        ):
            apply_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                manifest_path=manifest_path,
                expected_backup_set_sha=str(manifest["backup_set_sha256"]),
                restore_receipt_path=restore_receipt,
            )
            with contextlib.closing(sqlite3.connect(self.sqlite_path)) as connection:
                connection.execute("INSERT INTO artifact VALUES ('drift', 'changed')")
                connection.commit()
            with self.assertRaisesRegex(
                AgentSchemaMigrationError, "sqlite_row_count_drift"
            ):
                apply_all(
                    self.descriptor,
                    report_path=report_path,
                    expected_report_sha=str(report["report_sha256"]),
                    manifest_path=manifest_path,
                    expected_backup_set_sha=str(manifest["backup_set_sha256"]),
                    restore_receipt_path=restore_receipt,
                )


if __name__ == "__main__":
    unittest.main()
