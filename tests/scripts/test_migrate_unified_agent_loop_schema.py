from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.migrate_unified_agent_loop_schema import main
from src.storage import agent_schema_migration as migration
from src.storage.agent_schema_migration import (
    AgentSchemaMigrationError,
    backup_all,
    build_report,
    load_state_descriptor,
    migration_lock,
    remember_report_path,
    restore_all,
    restore_check,
    verify_tested_revision,
    write_report,
)


POSTGRES_INVENTORY = {
    "schema_version": "pre-p7",
    "agent_row_counts": {"agent_runs": 1},
    "agent_data_digests": {"agent_runs": "sha256:" + "1" * 64},
    "table_row_counts": {
        "agent_runs": 1,
        "artifacts": 1,
        "events": 1,
        "submitted_tasks": 1,
        "task_edges": 1,
        "task_nodes": 1,
    },
    "schema_digest": "sha256:" + "2" * 64,
    "dag_objects": {
        "task_edge_table": True,
        "task_root_node_id": True,
        "task_node_fields": ["criticality"],
        "planner_replan_claim_table": False,
    },
}


class AgentSchemaMigrationOperatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.state_root.mkdir(mode=0o700)
        self.sqlite_path = self.root / "runtime.sqlite3"
        self.sidecar_path = self.root / "sidecar.sqlite3"
        self._create_database(self.sqlite_path, "sqlite-agent")
        self._create_database(self.sidecar_path, "sidecar-agent")
        self.config_path = self.state_root / "agent-schema-state.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "schema": "maf.unified_agent_schema.state.v1",
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
                        "agent_tables": ["agent_runs"],
                    },
                    "postgres": {
                        "dsn_env": "TEST_AGENT_SCHEMA_SOURCE_DSN",
                        "restore_dsn_env": "TEST_AGENT_SCHEMA_RESTORE_DSN",
                        "agent_tables": ["agent_runs"],
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(self.config_path, 0o600)
        self.environment = patch.dict(
            os.environ,
            {
                "TEST_AGENT_SCHEMA_SOURCE_DSN": "postgresql://source-secret",
                "TEST_AGENT_SCHEMA_RESTORE_DSN": "postgresql://restore-secret",
            },
        )
        self.environment.start()
        self.descriptor = load_state_descriptor(self.state_root)
        self.postgres_report = patch(
            "src.storage.agent_schema_migration._postgres_report",
            return_value=POSTGRES_INVENTORY,
        )
        self.postgres_report.start()

    def tearDown(self) -> None:
        self.postgres_report.stop()
        self.environment.stop()
        self.temporary.cleanup()

    @staticmethod
    def _create_database(path: Path, agent_value: str) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "CREATE TABLE agent_runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);"
                "CREATE TABLE submitted_tasks (id TEXT PRIMARY KEY, root_node_id TEXT);"
                "CREATE TABLE task_edges (source_id TEXT, target_id TEXT);"
                "CREATE TABLE task_nodes (id TEXT PRIMARY KEY, criticality TEXT);"
                "CREATE TABLE artifacts (id TEXT PRIMARY KEY, body TEXT);"
                "CREATE TABLE events (id TEXT PRIMARY KEY, body TEXT);"
            )
            connection.execute(
                "INSERT INTO agent_runs VALUES ('agent-1', ?)", (agent_value,)
            )
            connection.execute(
                "INSERT INTO submitted_tasks VALUES ('task-1', 'node-1')"
            )
            connection.execute("INSERT INTO task_edges VALUES ('node-1', 'node-2')")
            connection.execute("INSERT INTO task_nodes VALUES ('node-1', 'high')")
            connection.execute(
                "INSERT INTO artifacts VALUES ('artifact-1', 'private-artifact')"
            )
            connection.execute("INSERT INTO events VALUES ('event-1', 'private-event')")
            connection.commit()
        finally:
            connection.close()
        os.chmod(path, 0o600)

    @staticmethod
    def _runner(arguments, **kwargs):
        if arguments[0] == "pg_dump":
            output = Path(kwargs["cwd"]) / arguments[arguments.index("--file") + 1]
            output.write_bytes(b"postgres-custom-format-dump")
            os.chmod(output, 0o600)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def _report(self) -> tuple[Path, dict[str, object]]:
        report = build_report(self.descriptor)
        path = self.state_root / "report.json"
        write_report(path, report)
        remember_report_path(self.descriptor, path, report)
        return path, report

    def _backup(self) -> tuple[Path, dict[str, object]]:
        report_path, report = self._report()
        manifest = backup_all(
            self.descriptor,
            report_path=report_path,
            expected_report_sha=str(report["report_sha256"]),
            backup_root=self.root / "persistent-backups",
            command_runner=self._runner,
        )
        path = (
            self.root
            / "persistent-backups"
            / str(manifest["backup_set_id"])
            / "manifest.json"
        )
        return path, manifest

    def test_closed_flow_restores_all_backends_and_exact_retries_are_idempotent(
        self,
    ) -> None:
        report_path, report = self._report()
        self.assertEqual(report, build_report(self.descriptor))
        remember_report_path(self.descriptor, report_path, report)

        manifest = backup_all(
            self.descriptor,
            report_path=report_path,
            expected_report_sha=str(report["report_sha256"]),
            backup_root=self.root / "persistent-backups",
            command_runner=self._runner,
        )
        manifest_path = (
            self.root
            / "persistent-backups"
            / str(manifest["backup_set_id"])
            / "manifest.json"
        )
        self.assertEqual(
            manifest,
            backup_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                backup_root=self.root / "persistent-backups",
                command_runner=self._runner,
            ),
        )

        receipt = restore_check(
            self.descriptor,
            manifest_path=manifest_path,
            expected_backup_set_sha=str(manifest["backup_set_sha256"]),
            restore_root=self.root / "isolated-restore",
            command_runner=self._runner,
        )
        self.assertEqual(receipt["state"], "restore_verified")
        self.assertEqual(
            receipt,
            restore_check(
                self.descriptor,
                manifest_path=manifest_path,
                expected_backup_set_sha=str(manifest["backup_set_sha256"]),
                restore_root=self.root / "isolated-restore",
                command_runner=self._runner,
            ),
        )

        restored = restore_all(
            self.descriptor,
            manifest_path=manifest_path,
            expected_backup_set_sha=str(manifest["backup_set_sha256"]),
            command_runner=self._runner,
        )
        self.assertEqual(restored["state"], "restored")
        self.assertEqual(
            restored,
            restore_all(
                self.descriptor,
                manifest_path=manifest_path,
                expected_backup_set_sha=str(manifest["backup_set_sha256"]),
                command_runner=self._runner,
            ),
        )
        receipts = sorted((self.state_root / "agent-schema-receipts").iterdir())
        self.assertEqual(
            [path.name for path in receipts],
            [
                "00-reported.json",
                "01-backed_up.json",
                "02-restore_verified.json",
                "03-restored.json",
            ],
        )
        encoded = json.dumps(report, sort_keys=True)
        self.assertNotIn("sqlite-agent", encoded)
        self.assertNotIn("private-artifact", encoded)
        self.assertNotIn("postgresql://", json.dumps(manifest, sort_keys=True))
        self.assertFalse(
            any(Path(ref).is_absolute() for ref in manifest["restore_refs"].values())
        )

    def test_report_drift_and_expected_sha_mismatch_fail_closed(self) -> None:
        report_path, report = self._report()
        connection = sqlite3.connect(self.sqlite_path)
        try:
            connection.execute("INSERT INTO events VALUES ('event-2', 'later')")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(AgentSchemaMigrationError, "report_drift"):
            backup_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                backup_root=self.root / "persistent-backups",
                command_runner=self._runner,
            )
        with self.assertRaisesRegex(AgentSchemaMigrationError, "report_sha_mismatch"):
            backup_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha="sha256:" + "0" * 64,
                backup_root=self.root / "persistent-backups",
                command_runner=self._runner,
            )

    def test_existing_conflicting_backup_set_is_not_overwritten(self) -> None:
        report_path, report = self._report()
        set_id = "backup-set-" + str(report["report_sha256"])[7:23]
        conflict = self.root / "persistent-backups" / set_id
        conflict.mkdir(mode=0o700, parents=True)
        os.chmod(conflict.parent, 0o700)
        with self.assertRaisesRegex(AgentSchemaMigrationError, "backup_set_exists"):
            backup_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                backup_root=self.root / "persistent-backups",
                command_runner=self._runner,
            )

    def test_backup_partial_failure_and_fsync_failure_do_not_advance_receipt(
        self,
    ) -> None:
        report_path, report = self._report()

        def failing_runner(arguments, **_kwargs):
            return subprocess.CompletedProcess(
                arguments, 1, "", "private-postgres-error"
            )

        with self.assertRaisesRegex(
            AgentSchemaMigrationError, "postgres_backup_failed"
        ):
            backup_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                backup_root=self.root / "persistent-backups",
                command_runner=failing_runner,
            )
        self.assertEqual(
            sorted(
                path.name
                for path in (self.state_root / "agent-schema-receipts").iterdir()
            ),
            ["00-reported.json"],
        )

        call_count = 0
        original_backup = migration._backup_sqlite

        def sidecar_failure(source, destination):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise AgentSchemaMigrationError("agent_schema_sidecar_backup_failed")
            return original_backup(source, destination)

        with (
            patch(
                "src.storage.agent_schema_migration._backup_sqlite",
                side_effect=sidecar_failure,
            ),
            self.assertRaisesRegex(AgentSchemaMigrationError, "sidecar_backup_failed"),
        ):
            backup_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                backup_root=self.root / "sidecar-failure-root",
                command_runner=self._runner,
            )
        self.assertEqual(
            sorted(
                path.name
                for path in (self.state_root / "agent-schema-receipts").iterdir()
            ),
            ["00-reported.json"],
        )

        second_root = self.root / "second-backup-root"
        with (
            patch(
                "src.storage.agent_schema_migration._fsync_file",
                side_effect=OSError("fsync failed"),
            ),
            self.assertRaises(OSError),
        ):
            backup_all(
                self.descriptor,
                report_path=report_path,
                expected_report_sha=str(report["report_sha256"]),
                backup_root=second_root,
                command_runner=self._runner,
            )
        self.assertEqual(
            sorted(
                path.name
                for path in (self.state_root / "agent-schema-receipts").iterdir()
            ),
            ["00-reported.json"],
        )

    def test_restore_rejects_source_target_and_backup_identity_drift(self) -> None:
        manifest_path, manifest = self._backup()
        backup_sha = str(manifest["backup_set_sha256"])
        with self.assertRaisesRegex(AgentSchemaMigrationError, "restore_target_unsafe"):
            restore_check(
                self.descriptor,
                manifest_path=manifest_path,
                expected_backup_set_sha=backup_sha,
                restore_root=self.state_root,
                command_runner=self._runner,
            )

        sqlite_backup = manifest_path.parent / "sqlite.backup"
        os.chmod(sqlite_backup, 0o640)
        with self.assertRaisesRegex(AgentSchemaMigrationError, "file_identity_invalid"):
            restore_check(
                self.descriptor,
                manifest_path=manifest_path,
                expected_backup_set_sha=backup_sha,
                restore_root=self.root / "restore-mode-drift",
                command_runner=self._runner,
            )

    def test_restore_rejects_hardlink_symlink_and_postgres_partial_failure(
        self,
    ) -> None:
        for drift in ("hardlink", "symlink", "postgres"):
            with self.subTest(drift=drift):
                self.tearDown()
                self.setUp()
                manifest_path, manifest = self._backup()
                postgres_backup = manifest_path.parent / "postgres.dump"
                runner = self._runner
                if drift == "hardlink":
                    os.link(postgres_backup, self.root / "second-link")
                elif drift == "symlink":
                    postgres_backup.unlink()
                    postgres_backup.symlink_to(self.root / "elsewhere")
                else:

                    def restore_failure(arguments, **_kwargs):
                        return subprocess.CompletedProcess(
                            arguments, 1, "", "private-restore-error"
                        )

                    runner = restore_failure
                expected = (
                    "postgres_restore_failed"
                    if drift == "postgres"
                    else "file_identity_invalid"
                )
                with self.assertRaisesRegex(AgentSchemaMigrationError, expected):
                    restore_check(
                        self.descriptor,
                        manifest_path=manifest_path,
                        expected_backup_set_sha=str(manifest["backup_set_sha256"]),
                        restore_root=self.root / "isolated-restore",
                        command_runner=runner,
                    )

    def test_lock_is_single_instance_and_apply_is_unavailable_before_p7b(self) -> None:
        with migration_lock(self.state_root):
            with self.assertRaisesRegex(AgentSchemaMigrationError, "operator_locked"):
                with migration_lock(self.state_root):
                    pass

        output = io.StringIO()
        with (
            patch("scripts.migrate_unified_agent_loop_schema.verify_tested_revision"),
            redirect_stdout(output),
        ):
            result = main(
                [
                    "apply",
                    "--state-root",
                    str(self.state_root),
                    "--report",
                    "unused",
                    "--expected-report-sha",
                    "sha256:" + "0" * 64,
                    "--backup-manifest",
                    "unused",
                    "--expected-backup-set-sha",
                    "sha256:" + "1" * 64,
                    "--restore-receipt",
                    "unused",
                ]
            )
        self.assertEqual(result, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            payload["reason_code"], "agent_schema_apply_not_available_before_p7b"
        )
        self.assertNotIn("postgresql://", output.getvalue())

    def test_quiescence_snapshot_mode_and_revision_drift_are_rejected(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        for field, reason in (
            ("writers_quiesced", "writers_not_quiesced"),
            ("postgres_snapshot_ready", "postgres_snapshot_not_ready"),
        ):
            with self.subTest(field=field):
                config[field] = False
                self.config_path.write_text(json.dumps(config), encoding="utf-8")
                os.chmod(self.config_path, 0o600)
                with self.assertRaisesRegex(AgentSchemaMigrationError, reason):
                    load_state_descriptor(self.state_root)
                config[field] = True

        os.chmod(self.config_path, 0o640)
        with self.assertRaisesRegex(AgentSchemaMigrationError, "file_identity_invalid"):
            load_state_descriptor(self.state_root)

        current_uid = os.getuid()
        with (
            patch(
                "src.storage.agent_schema_migration.os.getuid",
                return_value=current_uid + 1,
            ),
            self.assertRaisesRegex(AgentSchemaMigrationError, "file_identity_invalid"),
        ):
            migration._file_descriptor(self.sqlite_path)

        def revision_runner(arguments, **_kwargs):
            return subprocess.CompletedProcess(arguments, 0, "c" * 40 + "\n", "")

        with self.assertRaisesRegex(AgentSchemaMigrationError, "tested_revision_drift"):
            verify_tested_revision(
                self.descriptor,
                self.root,
                command_runner=revision_runner,
            )

    def test_success_stdout_is_redacted(self) -> None:
        output = io.StringIO()
        with (
            patch("scripts.migrate_unified_agent_loop_schema.verify_tested_revision"),
            redirect_stdout(output),
        ):
            result = main(
                [
                    "report",
                    "--state-root",
                    str(self.state_root),
                    "--output",
                    str(self.state_root / "report.json"),
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["result"], "reported")
        self.assertNotIn("postgresql://", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())


if __name__ == "__main__":
    unittest.main()
