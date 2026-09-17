from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from sqlalchemy import create_engine, text

from src.core.enums import ConversationStatus, TaskStatus
from src.storage.rust_contract import (
    artifact_policy,
    load_runtime_sidecar_contract,
)
from src.storage.runtime_sidecar_facade import (
    load_runtime_sidecar_task_authority_v1_for_upgrade,
)


_CONFIG_SCHEMA = "maf.submission_authority.migration_config.v1"
_REPORT_SCHEMA = "maf.submission_authority.migration_report.v1"
_IMPORT_SCHEMA = "maf.submission_authority.import_request.v1"
_IMPORT_RECEIPT_SCHEMA = "maf.submission_authority.import_receipt.v1"
_FINALIZATION_SUBJECT_SCHEMA = (
    "maf.submission_authority.finalization_subject.v1"
)
_OPERATOR_RECEIPT_SCHEMA = "maf.submission_authority.operator_receipt.v1"
_EVIDENCE_SCHEMA = "maf.runtime_sidecar.task_authority_migration_evidence.v2"
_ACTIVE_TASK_STATUSES = ("accepted", "planning", "running", "cancelling")
_TASK_STATUSES = frozenset(str(status) for status in TaskStatus)
_CONVERSATION_STATUSES = frozenset(str(status) for status in ConversationStatus)
_INVENTORY_KEYS = frozenset(
    {"count", "pk_sha256", "canonical_sha256", "finalize_empty"}
)
_REPORT_KEYS = frozenset(
    {
        "schema",
        "source_backend",
        "source_identity_sha256",
        "snapshot_boundary_sha256",
        "writer_fence_sha256",
        "tested_commit",
        "tested_tree",
        "destination_contract",
        "sidecar_source_sha256",
        "conversation_inventory",
        "message_identity_inventory",
        "active_task_inventory",
        "blockers",
        "report_sha256",
    }
)
_IMPORT_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "result",
        "finalization_receipt_sha256",
        "finalized_at_ms",
        "source_identity_sha256",
        "snapshot_boundary_sha256",
        "writer_fence_sha256",
        "destination_schema_sha256",
        "inventories",
    }
)
_CONFIG_KEYS = frozenset(
    {
        "schema",
        "source_backend",
        "sqlite_path",
        "postgres_dsn_env",
        "sidecar_path",
        "importer_binary_path",
        "hmac_key_path",
        "task_authority_evidence_path",
        "key_id",
        "expected_tested_commit",
        "expected_tested_tree",
    }
)


class SubmissionAuthorityMigrationError(RuntimeError):
    pass


def _current_revision() -> tuple[str, str]:
    root = Path(__file__).resolve().parents[2]
    values: list[str] = []
    for revision in ("HEAD", "HEAD^{tree}"):
        result = subprocess.run(
            ["git", "rev-parse", revision],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_tested_revision_unavailable"
            )
        values.append(result.stdout.strip())
    return values[0], values[1]


@dataclass(frozen=True, slots=True)
class SubmissionAuthorityMigrationConfig:
    source_backend: str
    sqlite_path: Path | None
    postgres_dsn_env: str | None
    sidecar_path: Path
    importer_binary_path: Path
    hmac_key_path: Path
    task_authority_evidence_path: Path
    key_id: str
    expected_tested_commit: str
    expected_tested_tree: str
    revision_provider: Callable[[], tuple[str, str]] = field(
        default=_current_revision,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.source_backend not in {"sqlite", "postgresql"}:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_source_backend_invalid"
            )
        if (self.source_backend == "sqlite") != (self.sqlite_path is not None):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_source_config_invalid"
            )
        if (self.source_backend == "postgresql") != (
            self.postgres_dsn_env is not None
        ):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_source_config_invalid"
            )
        if self.postgres_dsn_env is not None and not _valid_env_name(
            self.postgres_dsn_env
        ):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_postgres_dsn_env_invalid"
            )
        if not self.key_id.strip() or not _hex(self.expected_tested_commit, 40) or not _hex(
            self.expected_tested_tree, 40
        ):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_config_identity_invalid"
            )


@dataclass(frozen=True, slots=True)
class _Snapshot:
    source_backend: str
    source_identity_sha256: str
    snapshot_boundary_sha256: str
    writer_fence_sha256: str
    sidecar_source_sha256: str
    conversation_records: "_CanonicalRecordSpool"
    message_identity_records: "_CanonicalRecordSpool"
    blockers: tuple[str, ...]
    conversation_inventory: dict[str, Any]
    message_identity_inventory: dict[str, Any]
    active_task_inventory: dict[str, Any]

    def close(self) -> None:
        self.conversation_records.close()
        self.message_identity_records.close()


@dataclass(slots=True)
class _CanonicalRecordSpool:
    stream: BinaryIO
    inventory: dict[str, Any]

    def copy_to(self, destination: "_BoundedWriter") -> None:
        self.stream.seek(0)
        while chunk := self.stream.read(1024 * 1024):
            destination.write(chunk)

    def close(self) -> None:
        self.stream.close()


@dataclass(frozen=True, slots=True)
class _ActiveTaskFact:
    task_id: str
    conversation_id: str
    root_message_id: str


@dataclass(frozen=True, slots=True)
class _TaskSnapshot:
    active_facts: list[_ActiveTaskFact]
    inventory: dict[str, Any]
    unknown_status: bool
    oversize: bool
    duplicate: bool


@dataclass(slots=True)
class _BoundedWriter:
    stream: BinaryIO
    limit: int
    written: int = 0

    def write(self, value: bytes) -> None:
        self.written += len(value)
        if self.written > self.limit:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_import_oversize"
            )
        self.stream.write(value)


def build_submission_authority_report(
    config: SubmissionAuthorityMigrationConfig,
) -> dict[str, Any]:
    _validate_config_files(config)
    _validate_tested_revision(config)
    with _locked_source_snapshot(config) as snapshot:
        return _report_from_snapshot(config, snapshot)


def apply_submission_authority_migration(
    config: SubmissionAuthorityMigrationConfig,
    report: Mapping[str, Any],
    expected_report_sha256: str,
    *,
    evidence_path: Path,
    receipt_path: Path,
    backup_path: Path,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    _validate_config_files(config)
    importer_identity = _importer_identity(config.importer_binary_path)
    _validate_tested_revision(config)
    expected = _validate_report(report, expected_report_sha256)
    if expected["blockers"]:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_report_blocked"
        )
    _validate_output_paths(
        config,
        evidence_path=evidence_path,
        receipt_path=receipt_path,
        backup_path=backup_path,
    )
    _ensure_private_output_parent(evidence_path)
    _ensure_private_output_parent(receipt_path)
    _ensure_private_output_parent(backup_path)
    key = _load_hmac_key(config.hmac_key_path)
    try:
        legacy_plan = load_runtime_sidecar_task_authority_v1_for_upgrade(
            config.task_authority_evidence_path,
            authentication_key_path=config.hmac_key_path,
        )
    except RuntimeError as exc:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_task_evidence_invalid"
        ) from exc
    backup_existed = backup_path.exists()
    with _locked_source_snapshot(config) as snapshot:
        current = _report_from_snapshot(config, snapshot)
        _require_report_snapshot_match(expected, current)
        destination_contract = expected["destination_contract"]
        inventories = {
            "conversations": snapshot.conversation_inventory,
            "message_identities": snapshot.message_identity_inventory,
            "active_tasks": snapshot.active_task_inventory,
        }
        request_without_receipt = {
            "schema": _IMPORT_SCHEMA,
            "source_backend": snapshot.source_backend,
            "source_identity_sha256": snapshot.source_identity_sha256,
            "snapshot_boundary_sha256": snapshot.snapshot_boundary_sha256,
            "writer_fence_sha256": snapshot.writer_fence_sha256,
            "report_sha256": expected_report_sha256,
            "schema_hash": destination_contract["schema_hash"],
            "proto_hash": destination_contract["proto_hash"],
            "supported_features_sha256": destination_contract[
                "supported_features_sha256"
            ],
            "inventories": inventories,
            "conversations": snapshot.conversation_records,
            "message_identities": snapshot.message_identity_records,
        }
        request = {
            **request_without_receipt,
            "finalization_receipt_sha256": _finalization_receipt_digest(
                request_without_receipt
            ),
        }
        request_stream = _validate_import_request_limits(request)
        try:
            with _sidecar_writer_fence(config.sidecar_path):
                with _sidecar_exclusive_snapshot(config.sidecar_path):
                    sidecar_matches_report = (
                        _sha256_file(config.sidecar_path)
                        == expected["sidecar_source_sha256"]
                    )
                    if backup_existed:
                        _require_existing_backup(
                            backup_path, expected["sidecar_source_sha256"]
                        )
                    else:
                        if not sidecar_matches_report:
                            raise SubmissionAuthorityMigrationError(
                                "submission_authority_sidecar_source_drift"
                            )
                        _backup_sidecar(config.sidecar_path, backup_path)
                        _require_existing_backup(
                            backup_path, expected["sidecar_source_sha256"]
                        )
                if (
                    _importer_identity(config.importer_binary_path)
                    != importer_identity
                ):
                    raise SubmissionAuthorityMigrationError(
                        "submission_authority_importer_identity_drift"
                    )
                result = runner(
                    [
                        str(config.importer_binary_path),
                        "--sqlite",
                        str(config.sidecar_path),
                    ],
                    stdin=request_stream,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    env=_scrubbed_subprocess_env(),
                )
                if result.returncode != 0:
                    raise SubmissionAuthorityMigrationError(
                        "submission_authority_import_failed"
                    )
                receipt = _validate_import_receipt(
                    result.stdout,
                    request=request,
                    snapshot=snapshot,
                )
                if (
                    backup_existed
                    and not sidecar_matches_report
                    and receipt["result"] != "exact_replay"
                ):
                    raise SubmissionAuthorityMigrationError(
                        "submission_authority_backup_replay_conflict"
                    )
                _verify_destination(config.sidecar_path, snapshot, receipt)
        finally:
            request_stream.close()

        evidence = _build_evidence(
            config=config,
            legacy_plan=legacy_plan,
            report=expected,
            receipt=receipt,
            key=key,
        )
        operator_receipt = {
            "schema": _OPERATOR_RECEIPT_SCHEMA,
            "report_sha256": expected_report_sha256,
            "backup_sha256": expected["sidecar_source_sha256"],
            "import_receipt": receipt,
        }
        if receipt_path.exists():
            stored_operator_receipt = _load_json_secure(receipt_path)
            _require_exact_operator_replay(
                stored_operator_receipt,
                operator_receipt,
            )
            operator_receipt = stored_operator_receipt
        _write_json_exact(receipt_path, operator_receipt)
        _write_json_exact(evidence_path, evidence)
        return operator_receipt


def _report_from_snapshot(
    config: SubmissionAuthorityMigrationConfig,
    snapshot: _Snapshot,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": _REPORT_SCHEMA,
        "source_backend": snapshot.source_backend,
        "source_identity_sha256": snapshot.source_identity_sha256,
        "snapshot_boundary_sha256": snapshot.snapshot_boundary_sha256,
        "writer_fence_sha256": snapshot.writer_fence_sha256,
        "tested_commit": config.expected_tested_commit,
        "tested_tree": config.expected_tested_tree,
        "destination_contract": _destination_contract(),
        "sidecar_source_sha256": snapshot.sidecar_source_sha256,
        "conversation_inventory": snapshot.conversation_inventory,
        "message_identity_inventory": snapshot.message_identity_inventory,
        "active_task_inventory": snapshot.active_task_inventory,
        "blockers": list(snapshot.blockers),
    }
    payload["report_sha256"] = _sha256(_canonical_bytes(payload))
    return payload


@contextlib.contextmanager
def _locked_source_snapshot(
    config: SubmissionAuthorityMigrationConfig,
) -> Iterator[_Snapshot]:
    if config.source_backend == "sqlite":
        assert config.sqlite_path is not None
        connection = sqlite3.connect(
            _sqlite_uri(config.sqlite_path, mode="rw"),
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = _build_snapshot(
                config,
                conversation_rows=_iter_rows(
                    connection.execute(
                        "SELECT conversation_id, username, status, current_task_id, updated_at FROM conversation ORDER BY conversation_id COLLATE BINARY"
                    )
                ),
                message_rows=_iter_rows(
                    connection.execute(
                        "SELECT m.message_id, m.conversation_id, c.username, m.role, m.message_type, m.created_at, m.task_id FROM message m LEFT JOIN conversation c ON c.conversation_id=m.conversation_id ORDER BY m.message_id COLLATE BINARY"
                    )
                ),
                source_identity=_sqlite_source_identity(config.sqlite_path),
                fence_kind="sqlite_begin_immediate",
            )
            try:
                yield snapshot
            finally:
                snapshot.close()
        except sqlite3.DatabaseError as exc:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_sqlite_snapshot_failed"
            ) from exc
        finally:
            with contextlib.suppress(sqlite3.DatabaseError):
                connection.rollback()
            connection.close()
        return

    assert config.postgres_dsn_env is not None
    dsn = (os.environ.get(config.postgres_dsn_env) or "").strip()
    if not dsn:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_postgres_dsn_env_missing"
        )
    engine = create_engine(dsn, future=True)
    try:
        with engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                connection.execute(
                    text(
                        "LOCK TABLE conversation, message IN SHARE ROW EXCLUSIVE MODE"
                    )
                )
                system_identifier = connection.execute(
                    text("SELECT system_identifier::text FROM pg_control_system()")
                ).scalar_one()
                database_name = connection.execute(
                    text("SELECT current_database()")
                ).scalar_one()
                conversation_rows = _iter_rows(
                    connection.execution_options(stream_results=True)
                    .execute(
                        text(
                            'SELECT conversation_id, username, status, current_task_id, updated_at FROM conversation ORDER BY conversation_id COLLATE "C"'
                        )
                    )
                    .mappings()
                )
                message_rows = _iter_rows(
                    connection.execution_options(stream_results=True)
                    .execute(
                        text(
                            'SELECT m.message_id, m.conversation_id, c.username, m.role, m.message_type, m.created_at, m.task_id FROM message m LEFT JOIN conversation c ON c.conversation_id=m.conversation_id ORDER BY m.message_id COLLATE "C"'
                        )
                    )
                    .mappings()
                )
                source_identity = _sha256(
                    _canonical_bytes(
                        {
                            "backend": "postgresql",
                            "system_identifier": str(system_identifier),
                            "database": str(database_name),
                        }
                    )
                )
                snapshot = _build_snapshot(
                    config,
                    conversation_rows=conversation_rows,
                    message_rows=message_rows,
                    source_identity=source_identity,
                    fence_kind="postgresql_repeatable_read_share_row_exclusive",
                )
                try:
                    yield snapshot
                finally:
                    snapshot.close()
            finally:
                transaction.rollback()
    except SubmissionAuthorityMigrationError:
        raise
    except Exception as exc:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_postgres_snapshot_failed"
        ) from exc
    finally:
        engine.dispose()


def _build_snapshot(
    config: SubmissionAuthorityMigrationConfig,
    *,
    conversation_rows: Iterable[Mapping[str, Any]],
    message_rows: Iterable[Mapping[str, Any]],
    source_identity: str,
    fence_kind: str,
) -> _Snapshot:
    blockers: set[str] = set()
    task_snapshot = _read_task_snapshot(config.sidecar_path)
    if task_snapshot.unknown_status:
        blockers.add("sidecar_task_status_unknown")
    if task_snapshot.oversize:
        blockers.add("sidecar_task_record_oversize")
    if task_snapshot.duplicate:
        blockers.add("sidecar_task_identity_duplicate")
    active_by_conversation: dict[str, list[_ActiveTaskFact]] = {}
    for task in task_snapshot.active_facts:
        active_by_conversation.setdefault(task.conversation_id, []).append(task)
    unseen_active_conversations = set(active_by_conversation)

    def conversation_records() -> Iterator[dict[str, Any]]:
        for raw in conversation_rows:
            conversation_id = str(raw["conversation_id"] or "")
            username = str(raw["username"] or "")
            raw_status = str(raw["status"] or "")
            updated_at_ms = _epoch_ms(raw["updated_at"])
            if not conversation_id or not username or updated_at_ms is None:
                blockers.add("conversation_identity_incomplete")
            if raw_status not in _CONVERSATION_STATUSES:
                blockers.add("conversation_status_unknown")
            active = active_by_conversation.get(conversation_id, [])
            if len(active) > 1:
                blockers.add("conversation_double_active_task")
            sidecar_pointer = active[0].task_id if len(active) == 1 else None
            sql_pointer = raw["current_task_id"]
            if sql_pointer != sidecar_pointer:
                blockers.add("conversation_active_task_pointer_drift")
            if (
                raw_status in _CONVERSATION_STATUSES - {"active"}
                and sidecar_pointer is not None
            ):
                blockers.add("unavailable_conversation_has_active_task")
            unseen_active_conversations.discard(conversation_id)
            yield {
                "conversation_id": conversation_id,
                "username": username,
                "status": (
                    "active" if raw_status == "active" else "unavailable"
                ),
                "active_task_id": sidecar_pointer,
                "updated_at_ms": -1 if updated_at_ms is None else updated_at_ms,
            }

    conversation_spool = _spool_records(
        "conversations",
        conversation_records(),
        primary_key="conversation_id",
        blockers=blockers,
        oversize_blocker="conversation_record_oversize",
        duplicate_blocker="conversation_identity_duplicate",
    )
    root_message_ids = {
        task.root_message_id for task in task_snapshot.active_facts
    }
    message_facts: dict[str, tuple[str, str | None, str | None]] = {}

    def message_records() -> Iterator[dict[str, Any]]:
        for raw in message_rows:
            message_id = str(raw["message_id"] or "")
            conversation_id = str(raw["conversation_id"] or "")
            username = raw["username"]
            created_at_ms = _epoch_ms(raw["created_at"])
            if (
                not message_id
                or not conversation_id
                or username is None
                or created_at_ms is None
            ):
                blockers.add("message_identity_incomplete_or_orphaned")
            if message_id in root_message_ids:
                message_facts[message_id] = (
                    conversation_id,
                    None if raw["role"] is None else str(raw["role"]),
                    None if raw["task_id"] is None else str(raw["task_id"]),
                )
            yield {
                "message_id": message_id,
                "conversation_id": conversation_id,
                "username": "" if username is None else str(username),
                "identity_kind": "legacy_conflict_only",
                "role": None if raw["role"] is None else str(raw["role"]),
                "message_type": (
                    None
                    if raw["message_type"] is None
                    else str(raw["message_type"])
                ),
                "message_created_at_ms": created_at_ms,
                "task_id": (
                    None if raw["task_id"] is None else str(raw["task_id"])
                ),
                "request_fingerprint": None,
                "reserved_at_ms": -1 if created_at_ms is None else created_at_ms,
            }

    message_spool = _spool_records(
        "message_identities",
        message_records(),
        primary_key="message_id",
        blockers=blockers,
        oversize_blocker="message_identity_record_oversize",
        duplicate_blocker="message_identity_duplicate",
    )
    for task in task_snapshot.active_facts:
        if message_facts.get(task.root_message_id) != (
            task.conversation_id,
            "user",
            task.task_id,
        ):
            blockers.add("active_task_root_message_drift")
    if unseen_active_conversations:
        blockers.add("active_task_conversation_orphaned")
    conversation_inventory = conversation_spool.inventory
    message_inventory = message_spool.inventory
    active_inventory = task_snapshot.inventory
    snapshot_boundary = _sha256(
        _canonical_bytes(
            {
                "source_identity_sha256": source_identity,
                "conversation_inventory": conversation_inventory,
                "message_identity_inventory": message_inventory,
                "active_task_inventory": active_inventory,
            }
        )
    )
    writer_fence = _sha256(
        _canonical_bytes(
            {
                "source_identity_sha256": source_identity,
                "fence_kind": fence_kind,
                "tables": ["conversation", "message"],
            }
        )
    )
    return _Snapshot(
        source_backend=config.source_backend,
        source_identity_sha256=source_identity,
        snapshot_boundary_sha256=snapshot_boundary,
        writer_fence_sha256=writer_fence,
        sidecar_source_sha256=_sha256_file(config.sidecar_path),
        conversation_records=conversation_spool,
        message_identity_records=message_spool,
        blockers=tuple(sorted(blockers)),
        conversation_inventory=conversation_inventory,
        message_identity_inventory=message_inventory,
        active_task_inventory=active_inventory,
    )


def _iter_rows(cursor: Any) -> Iterator[Any]:
    count = 0
    while True:
        page = cursor.fetchmany(1000)
        if not page:
            return
        count += len(page)
        if count > 0xFFFFFFFF:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_import_count_invalid"
            )
        for row in page:
            if _raw_row_size(row) > 64 * 1024:
                raise SubmissionAuthorityMigrationError(
                    "submission_authority_import_record_oversize"
                )
            yield row


def _raw_row_size(row: Any) -> int:
    values = row.values() if isinstance(row, Mapping) else row
    size = 2
    for value in values:
        if value is None:
            size += 4
        elif isinstance(value, bytes):
            size += len(value)
        else:
            size += len(str(value).encode("utf-8")) + 4
    return size


def _spool_records(
    name: str,
    records: Iterable[Mapping[str, Any]],
    *,
    primary_key: str,
    blockers: set[str],
    oversize_blocker: str,
    duplicate_blocker: str,
) -> _CanonicalRecordSpool:
    stream = tempfile.TemporaryFile(mode="w+b")
    pk_digest = hashlib.sha256()
    pk_digest.update(b"maf.submission_authority.inventory.pk.v1\0")
    pk_digest.update(name.encode("utf-8"))
    pk_digest.update(b"\0[")
    row_digest = hashlib.sha256()
    row_digest.update(b"maf.submission_authority.inventory.rows.v1\0")
    row_digest.update(name.encode("utf-8"))
    row_digest.update(b"\0[")
    stream.write(b"[")
    count = 0
    previous_key: str | None = None
    try:
        for record in records:
            key = str(record[primary_key])
            if previous_key is not None:
                if key < previous_key:
                    raise SubmissionAuthorityMigrationError(
                        "submission_authority_inventory_order_invalid"
                    )
                if key == previous_key:
                    blockers.add(duplicate_blocker)
            encoded_key = _canonical_bytes(key)
            encoded_record = _canonical_bytes(record)
            if len(encoded_record) > 64 * 1024:
                blockers.add(oversize_blocker)
            if count:
                pk_digest.update(b",")
                row_digest.update(b",")
                stream.write(b",")
            pk_digest.update(encoded_key)
            row_digest.update(encoded_record)
            stream.write(encoded_record)
            count += 1
            if count > 0xFFFFFFFF:
                raise SubmissionAuthorityMigrationError(
                    "submission_authority_import_count_invalid"
                )
            previous_key = key
        pk_digest.update(b"]")
        row_digest.update(b"]")
        stream.write(b"]")
        stream.flush()
        stream.seek(0)
        return _CanonicalRecordSpool(
            stream=stream,
            inventory={
                "count": count,
                "pk_sha256": pk_digest.hexdigest(),
                "canonical_sha256": row_digest.hexdigest(),
                "finalize_empty": count == 0,
            },
        )
    except BaseException:
        stream.close()
        raise


def _read_task_snapshot(
    sidecar_path: Path,
) -> _TaskSnapshot:
    active_facts: list[_ActiveTaskFact] = []
    unknown_status = False
    oversize = False
    task_blockers: set[str] = set()

    def active_records(rows: Iterable[Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
        nonlocal unknown_status, oversize
        for row in rows:
            assignment = None
            if row["route_mode"] is not None:
                assignment = {
                    "route_mode": str(row["route_mode"]),
                    "real_path": str(row["real_path"] or ""),
                    "shadow_path": str(row["shadow_path"] or ""),
                    "config_version": str(row["config_version"] or ""),
                    "reason_code": str(row["reason_code"] or ""),
                    "cohort_id": row["cohort_id"],
                    "assignment_key_hash": row["assignment_key_hash"],
                    "assigned_at": row["assigned_at"],
                }
            record = {
                "task_id": str(row["task_id"]),
                "conversation_id": str(row["conversation_id"]),
                "root_message_id": str(row["root_message_id"]),
                "status": str(row["status"]),
                "routing_mode": str(row["routing_mode"] or ""),
                "requested_capability_id": row["requested_capability_id"],
                "summary": row["summary"],
                "cancel_requested_at": row["cancel_requested_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "assignment": assignment,
            }
            if record["status"] not in _TASK_STATUSES:
                unknown_status = True
            if len(_canonical_bytes(record)) > 64 * 1024:
                oversize = True
            if record["status"] in _ACTIVE_TASK_STATUSES:
                active_facts.append(
                    _ActiveTaskFact(
                        task_id=record["task_id"],
                        conversation_id=record["conversation_id"],
                        root_message_id=record["root_message_id"],
                    )
                )
                yield record

    try:
        with contextlib.closing(
            sqlite3.connect(_sqlite_uri(sidecar_path, mode="ro"), uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            rows = _iter_rows(
                connection.execute(
                    "SELECT task_id, conversation_id, root_message_id, status, routing_mode, requested_capability_id, summary, cancel_requested_at, created_at, updated_at, route_mode, real_path, shadow_path, config_version, reason_code, cohort_id, assignment_key_hash, assigned_at FROM submitted_tasks WHERE root_message_id IS NOT NULL ORDER BY task_id COLLATE BINARY"
                )
            )
            spool = _spool_records(
                "active_tasks",
                active_records(rows),
                primary_key="task_id",
                blockers=task_blockers,
                oversize_blocker="sidecar_task_record_oversize",
                duplicate_blocker="sidecar_task_identity_duplicate",
            )
            try:
                inventory = spool.inventory
            finally:
                spool.close()
    except sqlite3.DatabaseError as exc:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_sidecar_inventory_failed"
        ) from exc
    return _TaskSnapshot(
        active_facts=active_facts,
        inventory=inventory,
        unknown_status=unknown_status,
        oversize=oversize or "sidecar_task_record_oversize" in task_blockers,
        duplicate="sidecar_task_identity_duplicate" in task_blockers,
    )


def _destination_inventories(sidecar_path: Path) -> dict[str, dict[str, Any]]:
    task_snapshot = _read_task_snapshot(sidecar_path)
    if task_snapshot.unknown_status:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_sidecar_task_status_unknown"
        )
    if task_snapshot.oversize:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_sidecar_task_record_oversize"
        )
    if task_snapshot.duplicate:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_sidecar_task_identity_duplicate"
        )
    blockers: set[str] = set()
    try:
        with contextlib.ExitStack() as resources:
            connection = resources.enter_context(
                contextlib.closing(
                    sqlite3.connect(
                        _sqlite_uri(sidecar_path, mode="ro"), uri=True
                    )
                )
            )
            connection.row_factory = sqlite3.Row
            conversation_spool = _spool_records(
                "conversations",
                (
                    dict(row)
                    for row in _iter_rows(
                        connection.execute(
                            "SELECT conversation_id, username, status, active_task_id, updated_at_ms FROM submission_conversations ORDER BY conversation_id COLLATE BINARY"
                        )
                    )
                ),
                primary_key="conversation_id",
                blockers=blockers,
                oversize_blocker="destination_conversation_record_oversize",
                duplicate_blocker="destination_conversation_duplicate",
            )
            resources.callback(conversation_spool.close)
            identity_spool = _spool_records(
                "message_identities",
                (
                    dict(row)
                    for row in _iter_rows(
                        connection.execute(
                            "SELECT message_id, conversation_id, username, identity_kind, role, message_type, message_created_at_ms, task_id, request_fingerprint, reserved_at_ms FROM submission_message_identities ORDER BY message_id COLLATE BINARY"
                        )
                    )
                ),
                primary_key="message_id",
                blockers=blockers,
                oversize_blocker="destination_message_record_oversize",
                duplicate_blocker="destination_message_duplicate",
            )
            resources.callback(identity_spool.close)
            if blockers:
                raise SubmissionAuthorityMigrationError(
                    "submission_authority_destination_inventory_failed"
                )
            return {
                "conversations": conversation_spool.inventory,
                "message_identities": identity_spool.inventory,
                "active_tasks": task_snapshot.inventory,
            }
    except sqlite3.DatabaseError as exc:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_destination_inventory_failed"
        ) from exc


def _verify_destination(
    sidecar_path: Path,
    snapshot: _Snapshot,
    receipt: Mapping[str, Any],
) -> None:
    destination = _destination_inventories(sidecar_path)
    source = {
        "conversations": snapshot.conversation_inventory,
        "message_identities": snapshot.message_identity_inventory,
        "active_tasks": snapshot.active_task_inventory,
    }
    if destination != source or receipt["inventories"] != source:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_destination_inventory_drift"
        )


def _validate_import_receipt(
    raw: str,
    *,
    request: Mapping[str, Any],
    snapshot: _Snapshot,
) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_import_receipt_invalid"
        ) from exc
    if not isinstance(value, dict) or set(value) != _IMPORT_RECEIPT_KEYS:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_import_receipt_invalid"
        )
    if (
        value["schema"] != _IMPORT_RECEIPT_SCHEMA
        or value["result"] not in {"finalized", "exact_replay"}
        or not _sha(value["finalization_receipt_sha256"])
        or not isinstance(value["finalized_at_ms"], int)
        or isinstance(value["finalized_at_ms"], bool)
        or value["finalized_at_ms"] < 0
        or not _sha(value["destination_schema_sha256"])
        or value["source_identity_sha256"] != request["source_identity_sha256"]
        or value["snapshot_boundary_sha256"]
        != request["snapshot_boundary_sha256"]
        or value["writer_fence_sha256"] != request["writer_fence_sha256"]
        or value["finalization_receipt_sha256"]
        != request["finalization_receipt_sha256"]
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_import_receipt_invalid"
        )
    inventories = value["inventories"]
    if not isinstance(inventories, dict) or set(inventories) != {
        "conversations",
        "message_identities",
        "active_tasks",
    }:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_import_receipt_invalid"
        )
    expected = {
        "conversations": snapshot.conversation_inventory,
        "message_identities": snapshot.message_identity_inventory,
        "active_tasks": snapshot.active_task_inventory,
    }
    for key, inventory in inventories.items():
        _validate_inventory(inventory)
        if inventory != expected[key]:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_import_receipt_drift"
            )
    return value


def _build_evidence(
    *,
    config: SubmissionAuthorityMigrationConfig,
    legacy_plan: Mapping[str, Any],
    report: Mapping[str, Any],
    receipt: Mapping[str, Any],
    key: bytes,
) -> dict[str, Any]:
    contract = load_runtime_sidecar_contract()
    required_plan_keys = {
        "target_schema_version",
        "components",
        "task_authority_cutover",
    }
    if not isinstance(legacy_plan, Mapping) or set(legacy_plan) != required_plan_keys:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_task_evidence_invalid"
        )

    def matching_inventory(name: str) -> dict[str, Any]:
        source = dict(report[name])
        destination = dict(source)
        return {
            "source": source,
            "destination": destination,
            "ambiguity_count": 0,
        }

    cutover = {
        "source_backend": report["source_backend"],
        "source_identity_sha256": report["source_identity_sha256"],
        "snapshot_boundary_sha256": report["snapshot_boundary_sha256"],
        "writer_fence_sha256": report["writer_fence_sha256"],
        "report_sha256": report["report_sha256"],
        "tested_commit": report["tested_commit"],
        "tested_tree": report["tested_tree"],
        "destination_contract": report["destination_contract"],
        "conversation_inventory": matching_inventory("conversation_inventory"),
        "message_identity_inventory": matching_inventory(
            "message_identity_inventory"
        ),
        "active_task_inventory": matching_inventory("active_task_inventory"),
        "finalization_receipt_sha256": receipt[
            "finalization_receipt_sha256"
        ],
        "finalized_at_ms": receipt["finalized_at_ms"],
    }
    plan = {
        "target_schema_version": contract["schema_hash"],
        "components": dict(legacy_plan["components"]),
        "task_authority_cutover": dict(legacy_plan["task_authority_cutover"]),
        "submission_authority_cutover": cutover,
    }
    unsigned = {
        "schema": _EVIDENCE_SCHEMA,
        "component": contract["component"],
        "protocol_version": contract["protocol_version"],
        "schema_hash": contract["schema_hash"],
        "error_code_table_hash": contract["error_code_table_hash"],
        "key_id": config.key_id,
        "migration_plan": plan,
    }
    return {
        **unsigned,
        "hmac_sha256": hmac.new(
            key,
            _canonical_bytes(unsigned),
            hashlib.sha256,
        ).hexdigest(),
    }


def _validate_report(
    report: Mapping[str, Any], expected_sha256: str
) -> dict[str, Any]:
    if not isinstance(report, Mapping) or set(report) != _REPORT_KEYS:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_report_invalid"
        )
    value = json.loads(json.dumps(dict(report)))
    actual = value.pop("report_sha256", None)
    if (
        value.get("schema") != _REPORT_SCHEMA
        or not _sha(expected_sha256)
        or actual != expected_sha256
        or _sha256(_canonical_bytes(value)) != actual
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_report_sha256_mismatch"
        )
    for key in (
        "source_identity_sha256",
        "snapshot_boundary_sha256",
        "writer_fence_sha256",
        "sidecar_source_sha256",
    ):
        if not _sha(value.get(key)):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_report_invalid"
            )
    for key in (
        "conversation_inventory",
        "message_identity_inventory",
        "active_task_inventory",
    ):
        _validate_inventory(value.get(key))
    if not isinstance(value.get("blockers"), list) or not all(
        isinstance(item, str) for item in value["blockers"]
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_report_invalid"
        )
    return dict(report)


def _require_report_snapshot_match(
    expected: Mapping[str, Any], current: Mapping[str, Any]
) -> None:
    compared = (
        "source_backend",
        "source_identity_sha256",
        "snapshot_boundary_sha256",
        "writer_fence_sha256",
        "tested_commit",
        "tested_tree",
        "destination_contract",
        "conversation_inventory",
        "message_identity_inventory",
        "active_task_inventory",
        "blockers",
    )
    if any(expected[key] != current[key] for key in compared):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_report_drift"
        )


def _finalization_receipt_digest(request: Mapping[str, Any]) -> str:
    inventories = request.get("inventories")
    if not isinstance(inventories, Mapping) or set(inventories) != {
        "conversations",
        "message_identities",
        "active_tasks",
    }:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_inventory_invalid"
        )
    subject = {
        "schema": _FINALIZATION_SUBJECT_SCHEMA,
        "source_backend": request["source_backend"],
        "source_identity_sha256": request["source_identity_sha256"],
        "snapshot_boundary_sha256": request["snapshot_boundary_sha256"],
        "writer_fence_sha256": request["writer_fence_sha256"],
        "report_sha256": request["report_sha256"],
        "schema_hash": request["schema_hash"],
        "proto_hash": request["proto_hash"],
        "supported_features_sha256": request["supported_features_sha256"],
        "conversation_inventory": inventories["conversations"],
        "message_identity_inventory": inventories["message_identities"],
        "active_task_inventory": inventories["active_tasks"],
    }
    return _domain_sha256(
        "maf.submission_authority.finalization.v1",
        _canonical_bytes(subject),
    )


def _validate_import_request_limits(request: Mapping[str, Any]) -> BinaryIO:
    inventories = request["inventories"]
    conversations = request["conversations"]
    identities = request["message_identities"]
    if (
        not isinstance(conversations, _CanonicalRecordSpool)
        or not isinstance(identities, _CanonicalRecordSpool)
        or inventories["active_tasks"]["count"] > 0xFFFFFFFF
        or inventories["conversations"] != conversations.inventory
        or inventories["message_identities"] != identities.inventory
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_import_count_invalid"
        )
    stream = tempfile.TemporaryFile(mode="w+b")
    writer = _BoundedWriter(stream=stream, limit=1024 * 1024 * 1024)
    try:
        writer.write(b"{")
        for index, key in enumerate(sorted(request)):
            if index:
                writer.write(b",")
            writer.write(_canonical_bytes(key))
            writer.write(b":")
            value = request[key]
            if isinstance(value, _CanonicalRecordSpool):
                value.copy_to(writer)
            else:
                writer.write(_canonical_bytes(value))
        writer.write(b"}")
        stream.flush()
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


def _validate_inventory(value: Any) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != _INVENTORY_KEYS
        or not isinstance(value.get("count"), int)
        or isinstance(value.get("count"), bool)
        or not 0 <= value["count"] <= 0xFFFFFFFF
        or not _sha(value.get("pk_sha256"))
        or not _sha(value.get("canonical_sha256"))
        or value.get("finalize_empty") is not (value["count"] == 0)
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_inventory_invalid"
        )


def _destination_contract() -> dict[str, str]:
    contract = load_runtime_sidecar_contract()
    return {
        "schema_hash": str(contract["schema_hash"]),
        "proto_hash": str(artifact_policy()["expected_proto_hash"]),
        "error_code_table_hash": str(contract["error_code_table_hash"]),
        "supported_features_sha256": _sha256(
            _canonical_bytes(contract["supported_features"])
        ),
    }


def _validate_config_files(config: SubmissionAuthorityMigrationConfig) -> None:
    if config.sqlite_path is not None:
        _secure_regular_file(config.sqlite_path)
        if config.sqlite_path.resolve() == config.sidecar_path.resolve():
            raise SubmissionAuthorityMigrationError(
                "submission_authority_source_sidecar_collision"
            )
    _secure_regular_file(config.sidecar_path)
    _importer_identity(config.importer_binary_path)
    _secure_regular_file(config.hmac_key_path, mode=0o600)
    _secure_regular_file(config.task_authority_evidence_path, mode=0o600)
    for suffix in ("-wal", "-shm"):
        companion = Path(str(config.sidecar_path) + suffix)
        if companion.exists() and companion.stat().st_size:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_sidecar_writer_not_quiesced"
            )


def _validate_output_paths(
    config: SubmissionAuthorityMigrationConfig,
    *,
    evidence_path: Path,
    receipt_path: Path,
    backup_path: Path,
) -> None:
    outputs = {
        evidence_path.resolve(),
        receipt_path.resolve(),
        backup_path.resolve(),
    }
    if len(outputs) != 3:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_output_path_collision"
        )
    protected = {
        config.sidecar_path.resolve(),
        config.importer_binary_path.resolve(),
        config.hmac_key_path.resolve(),
        config.task_authority_evidence_path.resolve(),
    }
    if config.sqlite_path is not None:
        protected.add(config.sqlite_path.resolve())
    if outputs.intersection(protected):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_output_path_collision"
        )
    output_paths = (evidence_path, receipt_path, backup_path)
    protected_paths = (
        config.sidecar_path,
        config.importer_binary_path,
        config.hmac_key_path,
        config.task_authority_evidence_path,
        *(() if config.sqlite_path is None else (config.sqlite_path,)),
    )
    for index, left in enumerate(output_paths):
        if left.exists() and any(
            target.exists() and os.path.samefile(left, target)
            for target in protected_paths
        ):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_output_path_collision"
            )
        if left.exists() and any(
            right.exists() and os.path.samefile(left, right)
            for right in output_paths[index + 1 :]
        ):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_output_path_collision"
            )


@contextlib.contextmanager
def _sidecar_writer_fence(path: Path) -> Iterator[None]:
    lock_path = Path(f"{path}.submission-authority-migration.lock")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_sidecar_writer_fence_invalid"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_sidecar_writer_not_quiesced"
            ) from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def _sidecar_exclusive_snapshot(path: Path) -> Iterator[None]:
    connection = sqlite3.connect(
        _sqlite_uri(path, mode="rw"),
        uri=True,
        isolation_level=None,
        timeout=5,
    )
    try:
        connection.execute("BEGIN EXCLUSIVE")
        if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            raise SubmissionAuthorityMigrationError(
                "submission_authority_sidecar_writer_not_quiesced"
            )
        yield
    except SubmissionAuthorityMigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_sidecar_snapshot_failed"
        ) from exc
    finally:
        with contextlib.suppress(sqlite3.DatabaseError):
            connection.rollback()
        connection.close()


def _validate_tested_revision(config: SubmissionAuthorityMigrationConfig) -> None:
    if config.revision_provider() != (
        config.expected_tested_commit,
        config.expected_tested_tree,
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_tested_revision_drift"
        )


def _sqlite_source_identity(path: Path) -> str:
    metadata = os.stat(path, follow_symlinks=False)
    return _sha256(
        _canonical_bytes(
            {
                "backend": "sqlite",
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
            }
        )
    )


def _epoch_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _backup_sidecar(source: Path, destination: Path) -> None:
    source_descriptor = os.open(
        source,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=_publish_temporary_prefix(destination),
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        with contextlib.ExitStack() as stack:
            reader = stack.enter_context(os.fdopen(source_descriptor, "rb"))
            source_descriptor = -1
            writer = stack.enter_context(os.fdopen(descriptor, "wb"))
            descriptor = -1
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(temporary, 0o600, follow_symlinks=False)
        if _sha256_file(temporary) != _sha256_file(source):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_backup_conflict"
            )
        _publish_no_clobber(temporary, destination)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            if temporary is not None:
                temporary.unlink()


def _require_existing_backup(path: Path, expected_sha256: str) -> None:
    _settle_published_file(path)
    _secure_regular_file(path, mode=0o600)
    if _sha256_file(path) != expected_sha256:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_backup_conflict"
        )


def _write_json_exact(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = _canonical_bytes(payload) + b"\n"
    if path.exists():
        _settle_published_file(path)
        if _read_regular_0600(path) != encoded:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_output_conflict"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=_publish_temporary_prefix(path),
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600, follow_symlinks=False)
        if _read_regular_0600(temporary) != encoded:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_output_conflict"
            )
        _publish_no_clobber(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    _settle_published_file(path)
    if _read_regular_0600(path) != encoded:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_output_conflict"
        )


def _publish_no_clobber(source: Path, destination: Path) -> bool:
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError:
        return False
    _fsync_directory(destination.parent)
    source.unlink()
    _fsync_directory(destination.parent)
    return True


def _publish_temporary_prefix(destination: Path) -> str:
    name_digest = hashlib.sha256(os.fsencode(destination.name)).hexdigest()[:16]
    return f".submission-authority-{name_digest}-"


def _settle_published_file(path: Path) -> None:
    try:
        published = os.lstat(path)
    except FileNotFoundError:
        return
    prefix = _publish_temporary_prefix(path)
    removed = False
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        with os.scandir(directory_descriptor) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix) or not entry.name.endswith(".tmp"):
                    continue
                try:
                    candidate = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (
                    stat.S_ISREG(candidate.st_mode)
                    and candidate.st_uid == os.getuid()
                    and stat.S_IMODE(candidate.st_mode) == 0o600
                    and (candidate.st_dev, candidate.st_ino)
                    == (published.st_dev, published.st_ino)
                ):
                    os.unlink(entry.name, dir_fd=directory_descriptor)
                    removed = True
    finally:
        os.close(directory_descriptor)
    if removed:
        _fsync_directory(path.parent)


def _require_exact_operator_replay(
    stored: Mapping[str, Any],
    current: Mapping[str, Any],
) -> None:
    if set(stored) != {
        "schema",
        "report_sha256",
        "backup_sha256",
        "import_receipt",
    } or any(
        stored.get(key) != current.get(key)
        for key in ("schema", "report_sha256", "backup_sha256")
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_output_conflict"
        )
    stored_import = stored.get("import_receipt")
    current_import = current.get("import_receipt")
    if not isinstance(stored_import, Mapping) or not isinstance(
        current_import, Mapping
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_output_conflict"
        )
    compared = set(_IMPORT_RECEIPT_KEYS) - {"result"}
    if any(stored_import.get(key) != current_import.get(key) for key in compared):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_output_conflict"
        )


def _load_hmac_key(path: Path) -> bytes:
    key = _read_regular_0600(path)
    if len(key) < 32:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_hmac_key_invalid"
        )
    return key


def _secure_regular_file(path: Path, *, mode: int | None = None) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_file_missing"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_file_identity_invalid"
        )


def _read_regular_0600(path: Path) -> bytes:
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_file_identity_invalid"
        )
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise SubmissionAuthorityMigrationError(
                "submission_authority_file_identity_invalid"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _ensure_private_output_parent(path: Path) -> None:
    parent = path.parent.resolve()
    metadata = os.lstat(parent)
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_output_directory_invalid"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    with os.fdopen(descriptor, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _importer_identity(path: Path) -> tuple[int, int, int, int]:
    metadata = os.lstat(path)
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or mode & 0o022
        or not mode & stat.S_IXUSR
    ):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_importer_invalid"
        )
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _scrubbed_subprocess_env() -> dict[str, str]:
    return {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": "C.UTF-8",
    }


def _sqlite_uri(path: Path, *, mode: str) -> str:
    return f"{path.resolve().as_uri()}?mode={mode}"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _domain_sha256(domain: str, value: bytes) -> str:
    return _sha256(domain.encode("utf-8") + b"\0" + value)


def _sha(value: Any) -> bool:
    return isinstance(value, str) and _hex(value, 64)


def _hex(value: str, size: int) -> bool:
    return len(value) == size and all(character in "0123456789abcdef" for character in value)


def _valid_env_name(value: str) -> bool:
    return bool(value) and value.upper() == value and value.replace("_", "").isalnum()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_config(path: Path) -> SubmissionAuthorityMigrationConfig:
    try:
        value = json.loads(_read_regular_0600(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_config_invalid"
        ) from exc
    if not isinstance(value, dict) or set(value) != _CONFIG_KEYS or value.get(
        "schema"
    ) != _CONFIG_SCHEMA:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_config_invalid"
        )
    root = path.parent.resolve()

    def configured_path(key: str) -> Path:
        raw = value.get(key)
        if not isinstance(raw, str) or not raw or ".." in Path(raw).parts:
            raise SubmissionAuthorityMigrationError(
                "submission_authority_config_path_invalid"
            )
        candidate = Path(raw)
        configured = candidate if candidate.is_absolute() else root / candidate
        if configured.is_symlink():
            raise SubmissionAuthorityMigrationError(
                "submission_authority_config_path_invalid"
            )
        return configured.resolve()

    sqlite_path = value.get("sqlite_path")
    return SubmissionAuthorityMigrationConfig(
        source_backend=str(value["source_backend"]),
        sqlite_path=None if sqlite_path is None else configured_path("sqlite_path"),
        postgres_dsn_env=(
            None
            if value.get("postgres_dsn_env") is None
            else str(value["postgres_dsn_env"])
        ),
        sidecar_path=configured_path("sidecar_path"),
        importer_binary_path=configured_path("importer_binary_path"),
        hmac_key_path=configured_path("hmac_key_path"),
        task_authority_evidence_path=configured_path(
            "task_authority_evidence_path"
        ),
        key_id=str(value["key_id"]),
        expected_tested_commit=str(value["expected_tested_commit"]),
        expected_tested_tree=str(value["expected_tested_tree"]),
    )


def _load_json_secure(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular_0600(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SubmissionAuthorityMigrationError(
            "submission_authority_json_invalid"
        ) from exc
    if not isinstance(value, dict):
        raise SubmissionAuthorityMigrationError(
            "submission_authority_json_invalid"
        )
    return value


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    _ensure_private_output_parent(path)
    _write_json_exact(path, report)


__all__ = [
    "SubmissionAuthorityMigrationConfig",
    "apply_submission_authority_migration",
    "build_submission_authority_report",
]
