from __future__ import annotations

import asyncio
import contextlib
import concurrent.futures
import copy
import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
from datetime import datetime, timezone

from src.core.enums import MessageRole
from src.core.models import Conversation, Message
from src.storage.postgres import (
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage import runtime_sidecar_submission_migration as migration
from src.storage.runtime_sidecar_submission_migration import (
    SubmissionAuthorityMigrationConfig,
    SubmissionAuthorityMigrationError,
    _finalization_receipt_digest,
    _spool_records,
    _validate_import_request_limits,
    apply_submission_authority_migration,
    build_submission_authority_report,
)
from src.storage.rust_contract import (
    load_runtime_sidecar_contract,
    migration_policy,
)
from src.storage.runtime_sidecar_facade import (
    validate_runtime_sidecar_migration_evidence_artifact,
)
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason


class RuntimeSidecarSubmissionMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.sqlite"
        self.sidecar = self.root / "sidecar.sqlite"
        self.importer = self.root / "maf-runtime-sidecar-submission-import"
        self.hmac_key_path = self.root / "migration.key"
        self.task_evidence_path = self.root / "task-evidence.json"
        self.commit = "a" * 40
        self.tree = "b" * 40
        self.key = b"submission-migration-test-key-32bytes"
        self._create_source()
        self._create_sidecar()
        self.importer.write_text("test importer", encoding="utf-8")
        self.importer.chmod(0o700)
        self.hmac_key_path.write_bytes(self.key)
        self.hmac_key_path.chmod(0o600)
        self._write_task_evidence()
        self.config = SubmissionAuthorityMigrationConfig(
            source_backend="sqlite",
            sqlite_path=self.source,
            postgres_dsn_env=None,
            sidecar_path=self.sidecar,
            importer_binary_path=self.importer,
            hmac_key_path=self.hmac_key_path,
            task_authority_evidence_path=self.task_evidence_path,
            key_id="migration-test-key",
            expected_tested_commit=self.commit,
            expected_tested_tree=self.tree,
            revision_provider=lambda: (self.commit, self.tree),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sqlite_report_is_redacted_and_has_closed_inventories(self) -> None:
        report = build_submission_authority_report(self.config)

        encoded = json.dumps(report, sort_keys=True)
        self.assertNotIn("alice", encoded)
        self.assertNotIn("message-1", encoded)
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["conversation_inventory"]["count"], 1)
        self.assertEqual(report["message_identity_inventory"]["count"], 1)
        self.assertEqual(report["active_task_inventory"]["count"], 0)
        self.assertTrue(report["active_task_inventory"]["finalize_empty"])
        self.assertEqual(len(report["source_identity_sha256"]), 64)
        self.assertEqual(len(report["snapshot_boundary_sha256"]), 64)
        self.assertEqual(len(report["writer_fence_sha256"]), 64)
        self.assertEqual(len(report["report_sha256"]), 64)

    def test_shared_empty_vector_matches_python_inventory_and_subject_digest(self) -> None:
        fixture = json.loads(
            Path(
                "tests/fixtures/runtime_sidecar_submission_import_vectors.json"
            ).read_text(encoding="utf-8")
        )["empty_sqlite"]
        request = json.loads(fixture["request_canonical_json"])

        blockers: set[str] = set()
        conversation_spool = _spool_records(
            "conversations",
            [],
            primary_key="conversation_id",
            blockers=blockers,
            oversize_blocker="oversize",
            duplicate_blocker="duplicate",
        )
        message_spool = _spool_records(
            "message_identities",
            [],
            primary_key="message_id",
            blockers=blockers,
            oversize_blocker="oversize",
            duplicate_blocker="duplicate",
        )
        active_task_spool = _spool_records(
            "active_tasks",
            [],
            primary_key="task_id",
            blockers=blockers,
            oversize_blocker="oversize",
            duplicate_blocker="duplicate",
        )

        self.assertEqual(
            request["inventories"],
            {
                "conversations": conversation_spool.inventory,
                "message_identities": message_spool.inventory,
                "active_tasks": active_task_spool.inventory,
            },
        )
        finalization_receipt_sha256 = request.pop(
            "finalization_receipt_sha256"
        )
        self.assertEqual(
            _finalization_receipt_digest(request),
            finalization_receipt_sha256,
        )
        self.assertEqual(
            finalization_receipt_sha256,
            fixture["finalization_receipt_sha256"],
        )
        request["finalization_receipt_sha256"] = finalization_receipt_sha256
        request["conversations"] = conversation_spool
        request["message_identities"] = message_spool
        stream = _validate_import_request_limits(request)
        try:
            self.assertEqual(
                stream.read().decode("utf-8"),
                fixture["request_canonical_json"],
            )
        finally:
            stream.close()
            conversation_spool.close()
            message_spool.close()
            active_task_spool.close()

    def test_inventory_reader_uses_thousand_row_pages_and_rejects_oversize(self) -> None:
        class Cursor:
            def __init__(self, pages):
                self.pages = iter(pages)
                self.limits = []

            def fetchmany(self, limit):
                self.limits.append(limit)
                return next(self.pages)

        cursor = Cursor([[('x',)] * 1000, [('y',)], []])
        self.assertEqual(len(list(migration._iter_rows(cursor))), 1001)
        self.assertEqual(cursor.limits, [1000, 1000, 1000])

        with self.assertRaisesRegex(
            SubmissionAuthorityMigrationError,
            "submission_authority_import_record_oversize",
        ):
            list(
                migration._iter_rows(
                    Cursor([[("x" * (64 * 1024 + 1),)], []])
                )
            )

    def test_report_blocks_double_active_task_without_exposing_ids(self) -> None:
        with contextlib.closing(sqlite3.connect(self.sidecar)) as connection:
            connection.executemany(
                "INSERT INTO submitted_tasks(task_id, conversation_id, root_message_id, status, routing_mode) VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        "active-task-a",
                        "conversation-1",
                        "message-1",
                        "running",
                        "auto",
                    ),
                    (
                        "active-task-b",
                        "conversation-1",
                        "message-1",
                        "accepted",
                        "auto",
                    ),
                ),
            )
            connection.commit()

        report = build_submission_authority_report(self.config)

        self.assertIn("conversation_double_active_task", report["blockers"])
        self.assertNotIn("active-task-a", json.dumps(report))
        self.assertNotIn("alice", json.dumps(report))

    def test_report_blocks_unknown_rooted_task_status(self) -> None:
        with contextlib.closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute(
                "INSERT INTO submitted_tasks(task_id, conversation_id, root_message_id, status, routing_mode) VALUES (?, ?, ?, ?, ?)",
                (
                    "future-task",
                    "conversation-1",
                    "message-1",
                    "future_active",
                    "auto",
                ),
            )
            connection.commit()

        report = build_submission_authority_report(self.config)

        self.assertIn("sidecar_task_status_unknown", report["blockers"])
        self.assertEqual(report["active_task_inventory"]["count"], 0)
        self.assertNotIn("future-task", json.dumps(report))

    def test_known_archived_and_locked_conversations_import_as_unavailable(self) -> None:
        for status in ("archived", "locked"):
            with self.subTest(status=status):
                with contextlib.closing(sqlite3.connect(self.source)) as connection:
                    connection.execute(
                        "UPDATE conversation SET status=? WHERE conversation_id='conversation-1'",
                        (status,),
                    )
                    connection.commit()

                with migration._locked_source_snapshot(self.config) as snapshot:
                    self.assertNotIn(
                        "conversation_status_unknown", snapshot.blockers
                    )
                    snapshot.conversation_records.stream.seek(0)
                    conversations = json.load(
                        snapshot.conversation_records.stream
                    )
                    self.assertEqual(conversations[0]["status"], "unavailable")

    def test_sqlite_inventory_order_matches_canonical_unicode_order(self) -> None:
        identifiers = ["é", "A", "中", "a"]
        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            for identifier in identifiers:
                connection.execute(
                    "INSERT INTO conversation(conversation_id, username, status, current_task_id, updated_at) VALUES (?, ?, 'active', NULL, ?)",
                    (identifier, "owner", "2026-08-27T00:00:00"),
                )
                connection.execute(
                    "INSERT INTO message(message_id, conversation_id, role, task_id, created_at, message_type) VALUES (?, ?, 'user', NULL, ?, 'chat')",
                    (f"message-{identifier}", identifier, "2026-08-27T00:00:00"),
                )
            connection.commit()

        with migration._locked_source_snapshot(self.config) as snapshot:
            snapshot.conversation_records.stream.seek(0)
            conversations = json.load(snapshot.conversation_records.stream)

        self.assertEqual(
            [record["conversation_id"] for record in conversations],
            sorted(["conversation-1", *identifiers]),
        )

    def test_active_task_root_requires_user_message_bound_to_same_task(self) -> None:
        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            connection.execute(
                "UPDATE conversation SET current_task_id='active-task' WHERE conversation_id='conversation-1'"
            )
            connection.execute(
                "UPDATE message SET role='assistant', task_id='active-task' WHERE message_id='message-1'"
            )
            connection.commit()
        with contextlib.closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute(
                "INSERT INTO submitted_tasks(task_id, conversation_id, root_message_id, status, routing_mode) VALUES (?, ?, ?, ?, ?)",
                (
                    "active-task",
                    "conversation-1",
                    "message-1",
                    "running",
                    "auto",
                ),
            )
            connection.commit()

        invalid = build_submission_authority_report(self.config)
        self.assertIn("active_task_root_message_drift", invalid["blockers"])

        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            connection.execute(
                "UPDATE message SET role='user' WHERE message_id='message-1'"
            )
            connection.commit()
        valid = build_submission_authority_report(self.config)
        self.assertNotIn("active_task_root_message_drift", valid["blockers"])

    def test_active_task_exact_canonical_size_blocks_escaped_control_payload(self) -> None:
        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            connection.execute(
                "UPDATE conversation SET current_task_id='large-task' WHERE conversation_id='conversation-1'"
            )
            connection.execute(
                "UPDATE message SET task_id='large-task' WHERE message_id='message-1'"
            )
            connection.commit()
        with contextlib.closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute(
                "INSERT INTO submitted_tasks(task_id, conversation_id, root_message_id, status, routing_mode, summary) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "large-task",
                    "conversation-1",
                    "message-1",
                    "running",
                    "auto",
                    "\0" * 12_000,
                ),
            )
            connection.commit()

        report = build_submission_authority_report(self.config)

        self.assertIn("sidecar_task_record_oversize", report["blockers"])

    def test_active_task_inventory_pages_without_retaining_full_records(self) -> None:
        with contextlib.closing(sqlite3.connect(self.sidecar)) as connection:
            connection.executemany(
                "INSERT INTO submitted_tasks(task_id, conversation_id, root_message_id, status, routing_mode, summary) VALUES (?, ?, ?, 'running', 'auto', ?)",
                (
                    (
                        f"task-{index:04d}",
                        f"conversation-{index:04d}",
                        f"message-{index:04d}",
                        "x" * 4096,
                    )
                    for index in range(1001)
                ),
            )
            connection.commit()

        task_snapshot = migration._read_task_snapshot(self.sidecar)

        self.assertEqual(task_snapshot.inventory["count"], 1001)
        self.assertEqual(len(task_snapshot.active_facts), 1001)
        self.assertEqual(
            task_snapshot.active_facts[0].__slots__,
            ("task_id", "conversation_id", "root_message_id"),
        )
        self.assertFalse(hasattr(task_snapshot.active_facts[0], "summary"))

    def test_apply_rechecks_report_backs_up_and_exact_resumes(self) -> None:
        report = build_submission_authority_report(self.config)
        calls: list[dict[str, object]] = []

        def runner(command, **kwargs):
            request = json.load(kwargs["stdin"])
            self.assertEqual(set(kwargs["env"]), {"LANG", "LC_ALL"})
            self.assertNotIn("MAF_POSTGRES_SECRET_DSN", kwargs["env"])
            calls.append(request)
            self.assertEqual(
                set(request),
                {
                    "schema",
                    "source_backend",
                    "source_identity_sha256",
                    "snapshot_boundary_sha256",
                    "writer_fence_sha256",
                    "report_sha256",
                    "schema_hash",
                    "proto_hash",
                    "supported_features_sha256",
                    "inventories",
                    "conversations",
                    "message_identities",
                    "finalization_receipt_sha256",
                },
            )
            self.assertEqual(
                command,
                [str(self.importer), "--sqlite", str(self.sidecar)],
            )
            exact_replay = self._import_destination(request)
            receipt = {
                "schema": "maf.submission_authority.import_receipt.v1",
                "result": "exact_replay" if exact_replay else "finalized",
                "finalization_receipt_sha256": request[
                    "finalization_receipt_sha256"
                ],
                "finalized_at_ms": 1_788_000_000_000,
                "source_identity_sha256": request["source_identity_sha256"],
                "snapshot_boundary_sha256": request["snapshot_boundary_sha256"],
                "writer_fence_sha256": request["writer_fence_sha256"],
                "destination_schema_sha256": "d" * 64,
                "inventories": {
                    "conversations": request["inventories"]["conversations"],
                    "message_identities": request["inventories"][
                        "message_identities"
                    ],
                    "active_tasks": request["inventories"]["active_tasks"],
                },
            }
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(receipt, separators=(",", ":")),
                stderr="",
            )

        evidence_path = self.root / "submission-evidence.json"
        receipt_path = self.root / "submission-receipt.json"
        backup_path = self.root / "sidecar.backup.sqlite"
        first = apply_submission_authority_migration(
            self.config,
            report,
            report["report_sha256"],
            evidence_path=evidence_path,
            receipt_path=receipt_path,
            backup_path=backup_path,
            runner=runner,
        )
        second = apply_submission_authority_migration(
            self.config,
            report,
            report["report_sha256"],
            evidence_path=evidence_path,
            receipt_path=receipt_path,
            backup_path=backup_path,
            runner=runner,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(oct(backup_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(evidence_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(receipt_path.stat().st_mode & 0o777), "0o600")
        artifact = json.loads(evidence_path.read_text(encoding="utf-8"))
        validated = validate_runtime_sidecar_migration_evidence_artifact(
            artifact,
            authentication_key=self.key,
        )
        self.assertEqual(
            validated["finalization_receipt_sha256"],
            calls[0]["finalization_receipt_sha256"],
        )
        signature = artifact.pop("hmac_sha256")
        self.assertEqual(
            signature,
            hmac.new(
                self.key,
                _canonical(artifact),
                hashlib.sha256,
            ).hexdigest(),
        )
        cutover = artifact["migration_plan"]["submission_authority_cutover"]
        self.assertEqual(cutover["report_sha256"], report["report_sha256"])
        self.assertEqual(
            cutover["finalization_receipt_sha256"],
            calls[0]["finalization_receipt_sha256"],
        )
        self.assertEqual(
            cutover["conversation_inventory"]["source"],
            cutover["conversation_inventory"]["destination"],
        )

    def test_apply_report_drift_fails_before_backup_or_import(self) -> None:
        report = build_submission_authority_report(self.config)
        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            connection.execute(
                "INSERT INTO message(message_id, conversation_id, role, task_id, created_at, message_type) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "message-2",
                    "conversation-1",
                    "assistant",
                    None,
                    "2026-08-27T00:00:01",
                    "chat",
                ),
            )
            connection.commit()

        with self.assertRaisesRegex(
            SubmissionAuthorityMigrationError,
            "submission_authority_report_drift",
        ):
            apply_submission_authority_migration(
                self.config,
                report,
                report["report_sha256"],
                evidence_path=self.root / "evidence.json",
                receipt_path=self.root / "receipt.json",
                backup_path=self.root / "backup.sqlite",
                runner=lambda *_args, **_kwargs: self.fail("importer called"),
            )

        self.assertFalse((self.root / "backup.sqlite").exists())

    def test_changed_destination_contract_report_conflicts_before_import(self) -> None:
        report = build_submission_authority_report(self.config)
        changed = copy.deepcopy(report)
        changed["destination_contract"]["proto_hash"] = "changed-target"
        unsigned = {key: value for key, value in changed.items() if key != "report_sha256"}
        changed["report_sha256"] = hashlib.sha256(
            _canonical(unsigned)
        ).hexdigest()
        runner = Mock()

        with self.assertRaisesRegex(
            SubmissionAuthorityMigrationError,
            "submission_authority_report_drift",
        ):
            apply_submission_authority_migration(
                self.config,
                changed,
                changed["report_sha256"],
                evidence_path=self.root / "changed-evidence.json",
                receipt_path=self.root / "changed-receipt.json",
                backup_path=self.root / "changed-backup.sqlite",
                runner=runner,
            )

        runner.assert_not_called()

    def test_preexisting_exact_backup_before_import_allows_first_finalize(self) -> None:
        report = build_submission_authority_report(self.config)
        backup_path = self.root / "preexisting-backup.sqlite"
        backup_path.write_bytes(self.sidecar.read_bytes())
        backup_path.chmod(0o600)

        def runner(_command, **kwargs):
            request = json.load(kwargs["stdin"])
            self.assertFalse(self._import_destination(request))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "schema": "maf.submission_authority.import_receipt.v1",
                        "result": "finalized",
                        "finalization_receipt_sha256": request[
                            "finalization_receipt_sha256"
                        ],
                        "finalized_at_ms": 1_788_000_000_000,
                        "source_identity_sha256": request[
                            "source_identity_sha256"
                        ],
                        "snapshot_boundary_sha256": request[
                            "snapshot_boundary_sha256"
                        ],
                        "writer_fence_sha256": request[
                            "writer_fence_sha256"
                        ],
                        "destination_schema_sha256": "d" * 64,
                        "inventories": request["inventories"],
                    },
                    separators=(",", ":"),
                ),
                stderr="",
            )

        receipt = apply_submission_authority_migration(
            self.config,
            report,
            report["report_sha256"],
            evidence_path=self.root / "preexisting-evidence.json",
            receipt_path=self.root / "preexisting-receipt.json",
            backup_path=backup_path,
            runner=runner,
        )

        self.assertEqual(receipt["import_receipt"]["result"], "finalized")

    def test_preexisting_different_backup_blocks_before_import(self) -> None:
        report = build_submission_authority_report(self.config)
        backup_path = self.root / "conflicting-backup.sqlite"
        backup_path.write_bytes(b"different")
        backup_path.chmod(0o600)
        runner = Mock()

        with self.assertRaisesRegex(
            SubmissionAuthorityMigrationError,
            "submission_authority_backup_conflict",
        ):
            apply_submission_authority_migration(
                self.config,
                report,
                report["report_sha256"],
                evidence_path=self.root / "conflict-evidence.json",
                receipt_path=self.root / "conflict-receipt.json",
                backup_path=backup_path,
                runner=runner,
            )

        runner.assert_not_called()

    def test_output_paths_cannot_alias_sidecar_or_each_other(self) -> None:
        report = build_submission_authority_report(self.config)
        runner = Mock()
        with self.assertRaisesRegex(
            SubmissionAuthorityMigrationError,
            "submission_authority_output_path_collision",
        ):
            apply_submission_authority_migration(
                self.config,
                report,
                report["report_sha256"],
                evidence_path=self.root / "alias-evidence.json",
                receipt_path=self.root / "alias-receipt.json",
                backup_path=self.sidecar,
                runner=runner,
            )
        shared = self.root / "shared-output.json"
        with self.assertRaisesRegex(
            SubmissionAuthorityMigrationError,
            "submission_authority_output_path_collision",
        ):
            apply_submission_authority_migration(
                self.config,
                report,
                report["report_sha256"],
                evidence_path=shared,
                receipt_path=shared,
                backup_path=self.root / "alias-backup.sqlite",
                runner=runner,
            )
        runner.assert_not_called()

    def test_import_limits_are_checked_before_sidecar_write_transaction(self) -> None:
        report = build_submission_authority_report(self.config)
        with (
            patch.object(
                migration,
                "_validate_import_request_limits",
                side_effect=SubmissionAuthorityMigrationError(
                    "submission_authority_import_oversize"
                ),
            ),
            patch.object(
                migration,
                "_sidecar_exclusive_snapshot",
            ) as sidecar_snapshot,
        ):
            with self.assertRaisesRegex(
                SubmissionAuthorityMigrationError,
                "submission_authority_import_oversize",
            ):
                apply_submission_authority_migration(
                    self.config,
                    report,
                    report["report_sha256"],
                    evidence_path=self.root / "limit-evidence.json",
                    receipt_path=self.root / "limit-receipt.json",
                    backup_path=self.root / "limit-backup.sqlite",
                    runner=Mock(),
                )

        sidecar_snapshot.assert_not_called()

    def test_invalid_upgrade_evidence_fails_before_backup_and_import(self) -> None:
        report = build_submission_authority_report(self.config)
        self.task_evidence_path.write_text("{}", encoding="utf-8")
        self.task_evidence_path.chmod(0o600)
        runner = Mock()
        backup = self.root / "invalid-evidence-backup.sqlite"

        with self.assertRaisesRegex(
            SubmissionAuthorityMigrationError,
            "submission_authority_task_evidence_invalid",
        ):
            apply_submission_authority_migration(
                self.config,
                report,
                report["report_sha256"],
                evidence_path=self.root / "invalid-evidence.json",
                receipt_path=self.root / "invalid-receipt.json",
                backup_path=backup,
                runner=runner,
            )

        runner.assert_not_called()
        self.assertFalse(backup.exists())

    def test_importer_permissions_and_identity_are_rechecked_before_exec(self) -> None:
        self.importer.chmod(0o722)
        with self.assertRaisesRegex(
            SubmissionAuthorityMigrationError,
            "submission_authority_importer_invalid",
        ):
            build_submission_authority_report(self.config)
        self.importer.chmod(0o700)
        report = build_submission_authority_report(self.config)
        original_snapshot = migration._sidecar_exclusive_snapshot

        @contextlib.contextmanager
        def replace_importer(path):
            with original_snapshot(path):
                self.importer.unlink()
                self.importer.write_text("replacement", encoding="utf-8")
                self.importer.chmod(0o700)
                yield

        runner = Mock()
        with (
            patch.object(
                migration,
                "_sidecar_exclusive_snapshot",
                replace_importer,
            ),
            self.assertRaisesRegex(
                SubmissionAuthorityMigrationError,
                "submission_authority_importer_identity_drift",
            ),
        ):
            apply_submission_authority_migration(
                self.config,
                report,
                report["report_sha256"],
                evidence_path=self.root / "identity-evidence.json",
                receipt_path=self.root / "identity-receipt.json",
                backup_path=self.root / "identity-backup.sqlite",
                runner=runner,
            )
        runner.assert_not_called()

    def test_backup_waits_for_sidecar_exclusive_snapshot(self) -> None:
        report = build_submission_authority_report(self.config)
        runner_called = False

        def runner(*args, **kwargs):
            nonlocal runner_called
            runner_called = True
            return self._fake_runner()(*args, **kwargs)

        writer = sqlite3.connect(self.sidecar, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    apply_submission_authority_migration,
                    self.config,
                    report,
                    report["report_sha256"],
                    evidence_path=self.root / "locked-evidence.json",
                    receipt_path=self.root / "locked-receipt.json",
                    backup_path=self.root / "locked-backup.sqlite",
                    runner=runner,
                )
                try:
                    time.sleep(0.1)
                    self.assertFalse(future.done())
                    self.assertFalse(runner_called)
                finally:
                    writer.rollback()
                receipt = future.result(timeout=5)
        finally:
            with contextlib.suppress(sqlite3.DatabaseError):
                writer.rollback()
            writer.close()

        self.assertTrue(runner_called)
        self.assertEqual(receipt["import_receipt"]["result"], "finalized")

    def test_apply_holds_writer_fence_through_import_and_verification(self) -> None:
        report = build_submission_authority_report(self.config)
        lock_path = Path(
            f"{self.sidecar}.submission-authority-migration.lock"
        )

        def assert_exclusive_fence() -> None:
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_SH | fcntl.LOCK_NB,
                    )
            finally:
                os.close(descriptor)

        runner = self._fake_runner()

        def fenced_runner(*args, **kwargs):
            assert_exclusive_fence()
            result = runner(*args, **kwargs)
            assert_exclusive_fence()
            return result

        original_verify = migration._verify_destination

        def fenced_verify(*args, **kwargs):
            assert_exclusive_fence()
            return original_verify(*args, **kwargs)

        with patch.object(
            migration,
            "_verify_destination",
            side_effect=fenced_verify,
        ):
            apply_submission_authority_migration(
                self.config,
                report,
                report["report_sha256"],
                evidence_path=self.root / "fenced-evidence.json",
                receipt_path=self.root / "fenced-receipt.json",
                backup_path=self.root / "fenced-backup.sqlite",
                runner=fenced_runner,
            )

        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    def test_online_sidecar_writer_fence_blocks_apply_before_backup(self) -> None:
        report = build_submission_authority_report(self.config)
        lock_path = Path(
            f"{self.sidecar}.submission-authority-migration.lock"
        )
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        runner = Mock()
        backup_path = self.root / "online-sidecar-backup.sqlite"
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaisesRegex(
                SubmissionAuthorityMigrationError,
                "submission_authority_sidecar_writer_not_quiesced",
            ):
                apply_submission_authority_migration(
                    self.config,
                    report,
                    report["report_sha256"],
                    evidence_path=self.root / "online-sidecar-evidence.json",
                    receipt_path=self.root / "online-sidecar-receipt.json",
                    backup_path=backup_path,
                    runner=runner,
                )
        finally:
            os.close(descriptor)

        runner.assert_not_called()
        self.assertFalse(backup_path.exists())

    def test_backup_publish_interruption_leaves_no_partial_final(self) -> None:
        report = build_submission_authority_report(self.config)
        backup_path = self.root / "interrupted-backup.sqlite"
        original_link = os.link
        interrupted = False

        def interrupt_backup(source, destination, **kwargs):
            nonlocal interrupted
            if Path(destination) == backup_path and not interrupted:
                interrupted = True
                raise OSError("backup-publish-interrupted")
            return original_link(source, destination, **kwargs)

        with patch.object(os, "link", side_effect=interrupt_backup):
            with self.assertRaisesRegex(
                OSError,
                "backup-publish-interrupted",
            ):
                apply_submission_authority_migration(
                    self.config,
                    report,
                    report["report_sha256"],
                    evidence_path=self.root / "interrupted-backup-evidence.json",
                    receipt_path=self.root / "interrupted-backup-receipt.json",
                    backup_path=backup_path,
                    runner=Mock(),
                )

        self.assertFalse(backup_path.exists())
        receipt = apply_submission_authority_migration(
            self.config,
            report,
            report["report_sha256"],
            evidence_path=self.root / "interrupted-backup-evidence.json",
            receipt_path=self.root / "interrupted-backup-receipt.json",
            backup_path=backup_path,
            runner=self._fake_runner(),
        )
        self.assertEqual(receipt["import_receipt"]["result"], "finalized")

    def test_evidence_write_fault_exact_resumes_first_stored_receipt(self) -> None:
        report = build_submission_authority_report(self.config)
        evidence_path = self.root / "fault-evidence.json"
        receipt_path = self.root / "fault-receipt.json"
        backup_path = self.root / "fault-backup.sqlite"
        runner = self._fake_runner()
        original_link = os.link
        failed = False

        def fail_first_evidence(source, destination, **kwargs):
            nonlocal failed
            if Path(destination) == evidence_path and not failed:
                failed = True
                raise OSError("evidence-write-fault")
            return original_link(source, destination, **kwargs)

        with patch.object(os, "link", side_effect=fail_first_evidence):
            with self.assertRaisesRegex(OSError, "evidence-write-fault"):
                apply_submission_authority_migration(
                    self.config,
                    report,
                    report["report_sha256"],
                    evidence_path=evidence_path,
                    receipt_path=receipt_path,
                    backup_path=backup_path,
                    runner=runner,
                )

        self.assertTrue(receipt_path.exists())
        self.assertFalse(evidence_path.exists())
        resumed = apply_submission_authority_migration(
            self.config,
            report,
            report["report_sha256"],
            evidence_path=evidence_path,
            receipt_path=receipt_path,
            backup_path=backup_path,
            runner=runner,
        )

        self.assertTrue(evidence_path.exists())
        self.assertEqual(resumed["import_receipt"]["result"], "finalized")

    def test_receipt_publish_interruption_exact_resumes_committed_import(self) -> None:
        report = build_submission_authority_report(self.config)
        evidence_path = self.root / "receipt-fault-evidence.json"
        receipt_path = self.root / "receipt-fault-receipt.json"
        backup_path = self.root / "receipt-fault-backup.sqlite"
        original_link = os.link
        failed = False

        def fail_first_receipt(source, destination, **kwargs):
            nonlocal failed
            if Path(destination) == receipt_path and not failed:
                failed = True
                raise OSError("receipt-write-fault")
            return original_link(source, destination, **kwargs)

        with patch.object(os, "link", side_effect=fail_first_receipt):
            with self.assertRaisesRegex(OSError, "receipt-write-fault"):
                apply_submission_authority_migration(
                    self.config,
                    report,
                    report["report_sha256"],
                    evidence_path=evidence_path,
                    receipt_path=receipt_path,
                    backup_path=backup_path,
                    runner=self._fake_runner(),
                )

        self.assertFalse(receipt_path.exists())
        self.assertFalse(evidence_path.exists())
        resumed = apply_submission_authority_migration(
            self.config,
            report,
            report["report_sha256"],
            evidence_path=evidence_path,
            receipt_path=receipt_path,
            backup_path=backup_path,
            runner=self._fake_runner(),
        )

        self.assertTrue(receipt_path.exists())
        self.assertTrue(evidence_path.exists())
        self.assertEqual(resumed["import_receipt"]["result"], "exact_replay")

    def test_exact_retry_settles_published_temporary_hardlink(self) -> None:
        output = self.root / "published-output.json"
        payload = {"schema": "published-output-test"}
        temporary = self.root / (
            f"{migration._publish_temporary_prefix(output)}orphan.tmp"
        )
        temporary.write_bytes(_canonical(payload) + b"\n")
        temporary.chmod(0o600)
        os.link(temporary, output)
        self.assertEqual(output.stat().st_nlink, 2)

        migration._write_json_exact(output, payload)

        self.assertFalse(temporary.exists())
        self.assertEqual(output.stat().st_nlink, 1)

    def _create_source(self) -> None:
        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            connection.executescript(
                """
                CREATE TABLE conversation(
                    conversation_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_task_id TEXT,
                    updated_at TEXT
                );
                CREATE TABLE message(
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    task_id TEXT,
                    created_at TEXT,
                    message_type TEXT NOT NULL
                );
                INSERT INTO conversation VALUES(
                    'conversation-1', 'alice', 'active', NULL,
                    '2026-08-27T00:00:00'
                );
                INSERT INTO message VALUES(
                    'message-1', 'conversation-1', 'user', NULL,
                    '2026-08-27T00:00:00', 'chat'
                );
                """
            )
        self.source.chmod(0o600)

    def _create_sidecar(self) -> None:
        with contextlib.closing(sqlite3.connect(self.sidecar)) as connection:
            connection.executescript(
                """
                CREATE TABLE submitted_tasks(
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    root_message_id TEXT,
                    status TEXT,
                    routing_mode TEXT,
                    requested_capability_id TEXT,
                    summary TEXT,
                    cancel_requested_at TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    route_mode TEXT,
                    real_path TEXT,
                    shadow_path TEXT,
                    config_version TEXT,
                    reason_code TEXT,
                    cohort_id TEXT,
                    assignment_key_hash TEXT,
                    assigned_at TEXT
                );
                CREATE TABLE submission_conversations(
                    conversation_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_task_id TEXT,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE submission_message_identities(
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    identity_kind TEXT NOT NULL,
                    role TEXT,
                    message_type TEXT,
                    message_created_at_ms INTEGER,
                    task_id TEXT,
                    request_fingerprint TEXT,
                    reserved_at_ms INTEGER NOT NULL
                );
                """
            )
        self.sidecar.chmod(0o600)

    def _write_task_evidence(self) -> None:
        contract = load_runtime_sidecar_contract()
        policy = migration_policy()
        digest = "e" * 64
        unsigned = {
            "schema": "maf.runtime_sidecar.task_authority_migration_evidence.v1",
            "component": contract["component"],
            "protocol_version": contract["protocol_version"],
            "schema_hash": contract["schema_hash"],
            "error_code_table_hash": contract["error_code_table_hash"],
            "key_id": "migration-test-key",
            "migration_plan": {
                "target_schema_version": contract["schema_hash"],
                "components": {
                    component: {
                        evidence: True
                        for evidence in policy["required_evidence"]
                    }
                    for component in policy["required_components"]
                },
                "task_authority_cutover": {
                    "backfill_import_complete": True,
                    "task_inventory": {
                        "legacy_count": 1,
                        "sidecar_count": 1,
                        "legacy_canonical_digest": digest,
                        "sidecar_canonical_digest": digest,
                    },
                    "task_node_inventory": {
                        "legacy_count": 1,
                        "sidecar_count": 1,
                        "legacy_canonical_digest": digest,
                        "sidecar_canonical_digest": digest,
                    },
                    "legacy_null_assignment_resolution": {
                        "resolution_complete": True,
                        "active_count": 0,
                        "active_canonical_digest": hashlib.sha256(
                            b"[]"
                        ).hexdigest(),
                        "terminal_historical_count": 1,
                        "terminal_historical_canonical_digest": digest,
                        "terminal_historical_remains_unassigned": True,
                    },
                },
            },
        }
        artifact = {
            **unsigned,
            "hmac_sha256": hmac.new(
                self.key,
                _canonical(unsigned),
                hashlib.sha256,
            ).hexdigest(),
        }
        self.task_evidence_path.write_bytes(_canonical(artifact) + b"\n")
        self.task_evidence_path.chmod(0o600)

    def _fake_runner(self):
        def runner(_command, **kwargs):
            request = json.load(kwargs["stdin"])
            exact_replay = self._import_destination(request)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "schema": "maf.submission_authority.import_receipt.v1",
                        "result": (
                            "exact_replay" if exact_replay else "finalized"
                        ),
                        "finalization_receipt_sha256": request[
                            "finalization_receipt_sha256"
                        ],
                        "finalized_at_ms": 1_788_000_000_000,
                        "source_identity_sha256": request[
                            "source_identity_sha256"
                        ],
                        "snapshot_boundary_sha256": request[
                            "snapshot_boundary_sha256"
                        ],
                        "writer_fence_sha256": request[
                            "writer_fence_sha256"
                        ],
                        "destination_schema_sha256": "d" * 64,
                        "inventories": request["inventories"],
                    },
                    separators=(",", ":"),
                ),
                stderr="",
            )

        return runner

    def _import_destination(self, request: dict[str, object]) -> bool:
        with contextlib.closing(sqlite3.connect(self.sidecar)) as connection:
            count = connection.execute(
                "SELECT count(*) FROM submission_conversations"
            ).fetchone()[0]
            if count:
                return True
            connection.executemany(
                "INSERT INTO submission_conversations VALUES(:conversation_id, :username, :status, :active_task_id, :updated_at_ms)",
                request["conversations"],
            )
            connection.executemany(
                "INSERT INTO submission_message_identities VALUES(:message_id, :conversation_id, :username, :identity_kind, :role, :message_type, :message_created_at_ms, :task_id, :request_fingerprint, :reserved_at_ms)",
                request["message_identities"],
            )
            connection.commit()
        return False


class RuntimeSidecarSubmissionMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dsn, skip_reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_SUBMISSION_MIGRATION_TEST_DSN",
            fallback_env="MAF_POSTGRES_TEST_DSN",
        )
        if skip_reason:
            raise unittest.SkipTest(skip_reason)
        assert dsn is not None
        cls.dsn = dsn
        cls.engine = create_postgres_engine(dsn)
        bootstrap_postgres_database(cls.engine)
        cls.storage = PostgreSQLStorage(
            create_postgres_session_factory(cls.engine)
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_report_uses_locked_postgres_snapshot_without_raw_dsn(self) -> None:
        suffix = uuid4().hex
        conversation_id = f"submission-migration-{suffix}"
        message_id = f"submission-migration-message-{suffix}"
        now = datetime(2026, 8, 27, tzinfo=timezone.utc).replace(tzinfo=None)
        asyncio.run(
            self.storage.save_conversation(
                Conversation(
                    conversation_id=conversation_id,
                    username="postgres-migration-test",
                    created_at=now,
                    updated_at=now,
                )
            )
        )
        asyncio.run(
            self.storage.save_message(
                Message(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    role=MessageRole.USER,
                    content="safe",
                    created_at=now,
                )
            )
        )
        self.addCleanup(
            lambda: asyncio.run(
                self.storage.delete_conversation_physical(conversation_id)
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sidecar = root / "sidecar.sqlite"
            with contextlib.closing(sqlite3.connect(sidecar)) as connection:
                connection.execute(
                    "CREATE TABLE submitted_tasks(task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, root_message_id TEXT, status TEXT, routing_mode TEXT, requested_capability_id TEXT, summary TEXT, cancel_requested_at TEXT, created_at TEXT, updated_at TEXT, route_mode TEXT, real_path TEXT, shadow_path TEXT, config_version TEXT, reason_code TEXT, cohort_id TEXT, assignment_key_hash TEXT, assigned_at TEXT)"
                )
                connection.commit()
            sidecar.chmod(0o600)
            importer = root / "importer"
            importer.write_text("binary", encoding="utf-8")
            importer.chmod(0o700)
            key = root / "key"
            key.write_bytes(b"x" * 32)
            key.chmod(0o600)
            task_evidence = root / "task-evidence.json"
            task_evidence.write_text("{}", encoding="utf-8")
            task_evidence.chmod(0o600)
            config = SubmissionAuthorityMigrationConfig(
                source_backend="postgresql",
                sqlite_path=None,
                postgres_dsn_env="MAF_TEST_SUBMISSION_MIGRATION_DSN",
                sidecar_path=sidecar,
                importer_binary_path=importer,
                hmac_key_path=key,
                task_authority_evidence_path=task_evidence,
                key_id="test",
                expected_tested_commit="a" * 40,
                expected_tested_tree="b" * 40,
                revision_provider=lambda: ("a" * 40, "b" * 40),
            )
            with patch.dict(
                os.environ,
                {"MAF_TEST_SUBMISSION_MIGRATION_DSN": self.dsn},
            ):
                report = build_submission_authority_report(config)
        self.assertEqual(report["source_backend"], "postgresql")
        self.assertGreaterEqual(report["conversation_inventory"]["count"], 1)
        self.assertNotIn(self.dsn, json.dumps(report))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
