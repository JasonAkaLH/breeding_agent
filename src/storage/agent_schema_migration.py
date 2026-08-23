from __future__ import annotations

import contextlib
import base64
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


STATE_SCHEMA = "maf.unified_agent_schema.state.v1"
REPORT_SCHEMA = "maf.unified_agent_schema.report.v1"
BACKUP_SCHEMA = "maf.unified_agent_schema.backup_set.v1"
RECEIPT_SCHEMA = "maf.unified_agent_schema.receipt.v1"
STATE_FILE = "agent-schema-state.json"
RECEIPT_DIRECTORY = "agent-schema-receipts"
LOCK_FILE = ".agent-schema-migration.lock"

RECEIPT_ORDER = (
    "reported",
    "backed_up",
    "restore_verified",
    "applying_sqlite",
    "sqlite_applied",
    "applying_postgres",
    "postgres_applied",
    "applying_sidecar",
    "sidecar_applied",
    "verified",
    "completed",
)


class AgentSchemaMigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StateDescriptor:
    state_root: Path
    sqlite_path: Path
    sidecar_path: Path
    postgres_dsn_env: str
    postgres_restore_dsn_env: str
    sqlite_agent_tables: tuple[str, ...]
    sidecar_agent_tables: tuple[str, ...]
    postgres_agent_tables: tuple[str, ...]
    tested_commit: str
    tested_tree: str


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def load_state_descriptor(state_root: str | os.PathLike[str]) -> StateDescriptor:
    root = _secure_directory(Path(state_root), require_existing=True)
    config_path = _secure_file(root / STATE_FILE, require_mode=0o600)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema") != STATE_SCHEMA:
        raise AgentSchemaMigrationError("agent_schema_state_invalid")
    if payload.get("writers_quiesced") is not True:
        raise AgentSchemaMigrationError("agent_schema_writers_not_quiesced")
    if payload.get("postgres_snapshot_ready") is not True:
        raise AgentSchemaMigrationError("agent_schema_postgres_snapshot_not_ready")
    commit = str(payload.get("tested_commit") or "")
    tree = str(payload.get("tested_tree") or "")
    if not _hex(commit, 40) or not _hex(tree, 40):
        raise AgentSchemaMigrationError("agent_schema_tested_revision_invalid")
    sqlite_config = _mapping(payload, "sqlite")
    sidecar_config = _mapping(payload, "sidecar")
    postgres_config = _mapping(payload, "postgres")
    sqlite_path = _configured_regular_file(root, sqlite_config.get("path"))
    sidecar_path = _configured_regular_file(root, sidecar_config.get("path"))
    if sqlite_path == sidecar_path:
        raise AgentSchemaMigrationError("agent_schema_backend_path_collision")
    return StateDescriptor(
        state_root=root,
        sqlite_path=sqlite_path,
        sidecar_path=sidecar_path,
        postgres_dsn_env=_env_name(postgres_config.get("dsn_env")),
        postgres_restore_dsn_env=_env_name(postgres_config.get("restore_dsn_env")),
        sqlite_agent_tables=_table_names(
            sqlite_config.get("agent_tables"), require_nonempty=True
        ),
        sidecar_agent_tables=_table_names(
            sidecar_config.get("agent_tables"), require_nonempty=False
        ),
        postgres_agent_tables=_table_names(
            postgres_config.get("agent_tables"), require_nonempty=True
        ),
        tested_commit=commit,
        tested_tree=tree,
    )


@contextlib.contextmanager
def migration_lock(state_root: Path):
    lock_path = state_root / LOCK_FILE
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
            raise AgentSchemaMigrationError("agent_schema_lock_identity_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AgentSchemaMigrationError("agent_schema_operator_locked") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def build_report(descriptor: StateDescriptor) -> dict[str, Any]:
    sqlite_report = _sqlite_report(
        descriptor.sqlite_path, descriptor.sqlite_agent_tables
    )
    sidecar_report = _sqlite_report(
        descriptor.sidecar_path, descriptor.sidecar_agent_tables
    )
    postgres_report = _postgres_report(
        _required_secret_env(descriptor.postgres_dsn_env),
        descriptor.postgres_agent_tables,
    )
    payload: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "tested_commit": descriptor.tested_commit,
        "tested_tree": descriptor.tested_tree,
        "backends": {
            "sqlite": sqlite_report,
            "postgres": postgres_report,
            "sidecar": sidecar_report,
        },
        "blockers": [],
    }
    payload["report_sha256"] = sha256_bytes(canonical_bytes(payload))
    return payload


def verify_tested_revision(
    descriptor: StateDescriptor,
    repo_root: str | os.PathLike[str],
    *,
    command_runner=subprocess.run,
) -> None:
    root = Path(repo_root).resolve()
    for revision, expected in (
        ("HEAD", descriptor.tested_commit),
        ("HEAD^{tree}", descriptor.tested_tree),
    ):
        result = command_runner(
            ["git", "rev-parse", revision],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or result.stdout.strip() != expected:
            raise AgentSchemaMigrationError("agent_schema_tested_revision_drift")


def write_report(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> None:
    _validate_report_sha(payload)
    target = Path(path)
    if target.exists():
        if _load_json_secure(target, 0o600) == dict(payload):
            return
        raise AgentSchemaMigrationError("agent_schema_report_exists")
    _write_json_no_clobber(target, payload, mode=0o600)


def backup_all(
    descriptor: StateDescriptor,
    *,
    report_path: str | os.PathLike[str],
    expected_report_sha: str,
    backup_root: str | os.PathLike[str],
    command_runner=subprocess.run,
) -> dict[str, Any]:
    report = _load_json_secure(Path(report_path), 0o600)
    _validate_report_sha(report)
    _require_sha(expected_report_sha, report.get("report_sha256"), "report")
    current_receipt = _current_receipt(descriptor.state_root)
    if current_receipt.get("report_sha256") != expected_report_sha:
        raise AgentSchemaMigrationError("agent_schema_receipt_input_drift")
    current = build_report(descriptor)
    _require_backend_inventory(report, current)
    root = _secure_directory(Path(backup_root), require_existing=False)
    set_id = "backup-set-" + expected_report_sha.removeprefix("sha256:")[:16]
    backup_dir = root / set_id
    manifest_path = backup_dir / "manifest.json"
    if backup_dir.exists():
        if current_receipt.get("state") == "reported":
            raise AgentSchemaMigrationError("agent_schema_backup_set_exists")
        manifest = _load_json_secure(manifest_path, 0o600)
        _validate_manifest(manifest, expected_report_sha=expected_report_sha)
        _validate_manifest_descriptor(manifest, descriptor)
        _validate_backup_files(
            _secure_directory(backup_dir, require_existing=True), manifest
        )
        return manifest
    if current_receipt.get("state") != "reported":
        raise AgentSchemaMigrationError("agent_schema_receipt_prefix_missing")
    try:
        backup_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise AgentSchemaMigrationError("agent_schema_backup_set_exists") from exc
    _fsync_directory(root)
    files: dict[str, Any] = {}
    try:
        files["sqlite"] = _backup_sqlite(
            descriptor.sqlite_path, backup_dir / "sqlite.backup"
        )
        files["sidecar"] = _backup_sqlite(
            descriptor.sidecar_path, backup_dir / "sidecar.backup"
        )
        postgres_path = backup_dir / "postgres.dump"
        _create_empty_file(postgres_path, 0o600)
        result = command_runner(
            ["pg_dump", "--format=custom", "--file", postgres_path.name],
            cwd=backup_dir,
            env={
                **os.environ,
                "PGDATABASE": _required_secret_env(descriptor.postgres_dsn_env),
            },
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AgentSchemaMigrationError("agent_schema_postgres_backup_failed")
        files["postgres"] = _file_descriptor(postgres_path)
        manifest: dict[str, Any] = {
            "schema": BACKUP_SCHEMA,
            "backup_set_id": set_id,
            "tested_commit": descriptor.tested_commit,
            "tested_tree": descriptor.tested_tree,
            "report_sha256": expected_report_sha,
            "schema_versions": _schema_versions(report),
            "created_at": _utcnow(),
            "files": files,
            "restore_refs": {
                "sqlite": "sqlite.backup",
                "postgres": "postgres.dump",
                "sidecar": "sidecar.backup",
            },
        }
        manifest["backup_set_sha256"] = sha256_bytes(canonical_bytes(manifest))
        _write_json_no_clobber(manifest_path, manifest, mode=0o600)
        _write_receipt(
            descriptor.state_root,
            state="backed_up",
            input_sha=expected_report_sha,
            payload={
                "report_sha256": expected_report_sha,
                "backup_set_sha256": manifest["backup_set_sha256"],
                "tested_commit": descriptor.tested_commit,
                "tested_tree": descriptor.tested_tree,
                "schema_versions": _schema_versions(report),
            },
        )
        return manifest
    except BaseException:
        _fsync_directory(backup_dir)
        raise


def restore_check(
    descriptor: StateDescriptor,
    *,
    manifest_path: str | os.PathLike[str],
    expected_backup_set_sha: str,
    restore_root: str | os.PathLike[str],
    command_runner=subprocess.run,
) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    manifest = _load_json_secure(manifest_path, 0o600)
    _validate_manifest(manifest)
    _validate_manifest_descriptor(manifest, descriptor)
    _require_sha(
        expected_backup_set_sha, manifest.get("backup_set_sha256"), "backup_set"
    )
    current_receipt = _current_receipt(descriptor.state_root)
    if current_receipt.get("backup_set_sha256") == expected_backup_set_sha and (
        current_receipt.get("state") == "restored"
        or _receipt_at_or_after(str(current_receipt.get("state")), "restore_verified")
    ):
        return current_receipt
    if (
        current_receipt.get("state") != "backed_up"
        or current_receipt.get("backup_set_sha256") != expected_backup_set_sha
    ):
        raise AgentSchemaMigrationError("agent_schema_receipt_prefix_missing")
    backup_dir = _secure_directory(manifest_path.parent, require_existing=True)
    _validate_backup_files(backup_dir, manifest)
    requested_target = Path(restore_root).expanduser()
    if requested_target.is_symlink():
        raise AgentSchemaMigrationError("agent_schema_restore_target_unsafe")
    target = requested_target.resolve()
    if target == descriptor.state_root or target == backup_dir:
        raise AgentSchemaMigrationError("agent_schema_restore_target_unsafe")
    target = _secure_directory(target, require_existing=False)
    if any(target.iterdir()):
        raise AgentSchemaMigrationError("agent_schema_restore_target_not_empty")
    _restore_sqlite(backup_dir / "sqlite.backup", target / "sqlite.restored")
    _restore_sqlite(backup_dir / "sidecar.backup", target / "sidecar.restored")
    source_postgres_dsn = _required_secret_env(descriptor.postgres_dsn_env)
    restore_postgres_dsn = _required_secret_env(descriptor.postgres_restore_dsn_env)
    if source_postgres_dsn == restore_postgres_dsn:
        raise AgentSchemaMigrationError("agent_schema_restore_target_unsafe")
    result = command_runner(
        [
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "postgres.dump",
        ],
        cwd=backup_dir,
        env={
            **os.environ,
            "PGDATABASE": restore_postgres_dsn,
        },
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AgentSchemaMigrationError("agent_schema_postgres_restore_failed")
    restored = {
        "sqlite": _sqlite_report(
            target / "sqlite.restored", descriptor.sqlite_agent_tables
        ),
        "sidecar": _sqlite_report(
            target / "sidecar.restored", descriptor.sidecar_agent_tables
        ),
        "postgres": _postgres_report(
            restore_postgres_dsn,
            descriptor.postgres_agent_tables,
        ),
    }
    report = _load_json_secure(
        Path(_receipt_input_report(descriptor.state_root)), 0o600
    )
    _validate_report_sha(report)
    if _semantic_backends(restored) != _semantic_backends(report.get("backends")):
        raise AgentSchemaMigrationError("agent_schema_restore_inventory_mismatch")
    receipt = _write_receipt(
        descriptor.state_root,
        state="restore_verified",
        input_sha=expected_backup_set_sha,
        payload={
            "report_sha256": manifest["report_sha256"],
            "backup_set_sha256": expected_backup_set_sha,
            "tested_commit": descriptor.tested_commit,
            "tested_tree": descriptor.tested_tree,
            "schema_versions": manifest["schema_versions"],
            "restored_backends": ["sqlite", "postgres", "sidecar"],
            "checks": {
                "backend_readiness": True,
                "agent_storage": True,
                "task_history": True,
                "artifact_event": True,
            },
        },
    )
    return receipt


def restore_all(
    descriptor: StateDescriptor,
    *,
    manifest_path: str | os.PathLike[str],
    expected_backup_set_sha: str,
    command_runner=subprocess.run,
) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    manifest = _load_json_secure(manifest_path, 0o600)
    _validate_manifest(manifest)
    _validate_manifest_descriptor(manifest, descriptor)
    _require_sha(
        expected_backup_set_sha, manifest.get("backup_set_sha256"), "backup_set"
    )
    receipt = _current_receipt(descriptor.state_root)
    if (
        receipt.get("state") == "restored"
        and receipt.get("backup_set_sha256") == expected_backup_set_sha
    ):
        return receipt
    if receipt.get("backup_set_sha256") != expected_backup_set_sha or not (
        receipt.get("state") in {"backed_up", "restore_verified"}
        or _receipt_at_or_after(str(receipt.get("state")), "applying_sqlite")
    ):
        raise AgentSchemaMigrationError("agent_schema_receipt_prefix_missing")
    backup_dir = _secure_directory(manifest_path.parent, require_existing=True)
    _validate_backup_files(backup_dir, manifest)
    _restore_sqlite(
        backup_dir / "sidecar.backup", descriptor.sidecar_path, replace=True
    )
    result = command_runner(
        [
            "pg_restore",
            "--exit-on-error",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "postgres.dump",
        ],
        cwd=backup_dir,
        env={
            **os.environ,
            "PGDATABASE": _required_secret_env(descriptor.postgres_dsn_env),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AgentSchemaMigrationError("agent_schema_postgres_restore_all_failed")
    _restore_sqlite(backup_dir / "sqlite.backup", descriptor.sqlite_path, replace=True)
    return _write_receipt(
        descriptor.state_root,
        state="restored",
        input_sha=expected_backup_set_sha,
        payload={
            "report_sha256": manifest["report_sha256"],
            "backup_set_sha256": expected_backup_set_sha,
            "tested_commit": descriptor.tested_commit,
            "tested_tree": descriptor.tested_tree,
            "schema_versions": manifest["schema_versions"],
            "restored_backends": ["sidecar", "postgres", "sqlite"],
        },
    )


def remember_report_path(
    descriptor: StateDescriptor,
    report_path: Path,
    report: Mapping[str, Any],
) -> None:
    state_root = descriptor.state_root
    reference = report_path.resolve()
    if reference.parent != state_root:
        raise AgentSchemaMigrationError("agent_schema_report_path_not_private")
    value = reference.name
    receipts = _receipt_chain(state_root)
    if receipts:
        if receipts[-1].get("report_sha256") == report.get("report_sha256"):
            return
        raise AgentSchemaMigrationError("agent_schema_receipt_input_drift")
    ref_path = state_root / ".agent-schema-report-ref.json"
    ref_payload = {"report_path": value}
    if ref_path.exists():
        if _load_json_secure(ref_path, 0o600) != ref_payload:
            raise AgentSchemaMigrationError("agent_schema_report_reference_drift")
    else:
        _write_json_no_clobber(ref_path, ref_payload, mode=0o600)
    _write_receipt(
        state_root,
        state="reported",
        input_sha=str(report["report_sha256"]),
        payload={
            "report_sha256": report["report_sha256"],
            "tested_commit": descriptor.tested_commit,
            "tested_tree": descriptor.tested_tree,
            "schema_versions": _schema_versions(report),
        },
    )


def _receipt_input_report(state_root: Path) -> Path:
    payload = _load_json_secure(state_root / ".agent-schema-report-ref.json", 0o600)
    value = str(payload.get("report_path") or "")
    path = Path(value)
    if not value or path.is_absolute() or len(path.parts) != 1:
        raise AgentSchemaMigrationError("agent_schema_report_reference_drift")
    return state_root / path


def _write_receipt(
    state_root: Path,
    *,
    state: str,
    input_sha: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    _schema_versions(payload)
    receipts = _receipt_chain(state_root)
    previous = receipts[-1] if receipts else None
    if previous is not None:
        if previous.get("state") == state and previous.get("input_sha256") == input_sha:
            return previous
        previous_state = str(previous.get("state"))
        if not _receipt_transition_allowed(previous_state, state):
            raise AgentSchemaMigrationError("agent_schema_receipt_transition_invalid")
    elif state != "reported":
        raise AgentSchemaMigrationError("agent_schema_receipt_prefix_missing")
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "state": state,
        "input_sha256": input_sha,
        "predecessor_sha256": None if previous is None else previous["receipt_sha256"],
        "created_at": _utcnow(),
        **dict(payload),
    }
    receipt["receipt_sha256"] = sha256_bytes(canonical_bytes(receipt))
    directory = _receipt_directory(state_root, create=True)
    path = directory / f"{len(receipts):02d}-{state}.json"
    _write_json_no_clobber(path, receipt, mode=0o600)
    return receipt


def _current_receipt(state_root: Path) -> dict[str, Any]:
    receipts = _receipt_chain(state_root)
    if not receipts:
        raise AgentSchemaMigrationError("agent_schema_receipt_prefix_missing")
    return receipts[-1]


def _receipt_chain(state_root: Path) -> list[dict[str, Any]]:
    directory = _receipt_directory(state_root, create=False)
    if directory is None:
        return []
    receipts: list[dict[str, Any]] = []
    for index, path in enumerate(sorted(directory.iterdir())):
        if path.name != f"{index:02d}-{path.stem.split('-', 1)[-1]}.json":
            raise AgentSchemaMigrationError("agent_schema_receipt_chain_invalid")
        receipt = _load_json_secure(path, 0o600)
        state = str(receipt.get("state") or "")
        if (
            receipt.get("schema") != RECEIPT_SCHEMA
            or not _sha(receipt.get("input_sha256"))
            or not _hex(str(receipt.get("tested_commit") or ""), 40)
            or not _hex(str(receipt.get("tested_tree") or ""), 40)
        ):
            raise AgentSchemaMigrationError("agent_schema_receipt_chain_invalid")
        _schema_versions(receipt)
        receipt_payload = dict(receipt)
        claimed_receipt_sha = receipt_payload.pop("receipt_sha256", None)
        if claimed_receipt_sha != sha256_bytes(canonical_bytes(receipt_payload)):
            raise AgentSchemaMigrationError("agent_schema_receipt_chain_invalid")
        if path.name != f"{index:02d}-{state}.json":
            raise AgentSchemaMigrationError("agent_schema_receipt_chain_invalid")
        predecessor = None if not receipts else receipts[-1]["receipt_sha256"]
        if receipt.get("predecessor_sha256") != predecessor:
            raise AgentSchemaMigrationError("agent_schema_receipt_chain_invalid")
        if receipts and not _receipt_transition_allowed(
            str(receipts[-1]["state"]), state
        ):
            raise AgentSchemaMigrationError("agent_schema_receipt_chain_invalid")
        receipts.append(receipt)
    return receipts


def _receipt_directory(state_root: Path, *, create: bool) -> Path | None:
    path = state_root / RECEIPT_DIRECTORY
    if not path.exists():
        if not create:
            return None
        path.mkdir(mode=0o700)
        _fsync_directory(state_root)
    return _secure_directory(path, require_existing=True)


def _receipt_transition_allowed(previous: str, current: str) -> bool:
    if current == "restored":
        return previous in {"backed_up", "restore_verified"} or _receipt_at_or_after(
            previous, "applying_sqlite"
        )
    try:
        return RECEIPT_ORDER.index(current) == RECEIPT_ORDER.index(previous) + 1
    except ValueError:
        return False


def _receipt_at_or_after(state: str, minimum: str) -> bool:
    try:
        return RECEIPT_ORDER.index(state) >= RECEIPT_ORDER.index(minimum)
    except ValueError:
        return False


def _sqlite_report(path: Path, agent_tables: Sequence[str]) -> dict[str, Any]:
    _secure_file(path, require_mode=None)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise AgentSchemaMigrationError("agent_schema_sqlite_integrity_failed")
        schema_rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table','index') ORDER BY type, name"
        ).fetchall()
        tables = {
            str(name)
            for object_type, name, _ in schema_rows
            if object_type == "table" and name and not str(name).startswith("sqlite_")
        }
        if set(agent_tables) - tables:
            raise AgentSchemaMigrationError("agent_schema_agent_table_missing")
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in agent_tables
            if table in tables
        }
        digests = {
            table: _sqlite_table_digest(connection, table)
            for table in agent_tables
            if table in tables
        }
        row_counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in sorted(tables)
        }
        columns = {
            table: [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for table in sorted(tables)
        }
    finally:
        connection.close()
    return {
        "schema_version": "pre-p7",
        "file_sha256": _file_descriptor(path)["sha256"],
        "agent_row_counts": counts,
        "agent_data_digests": digests,
        "table_row_counts": row_counts,
        "schema_digest": sha256_bytes(
            canonical_bytes(
                {
                    "columns": columns,
                    "objects": [list(row) for row in schema_rows],
                }
            )
        ),
        "dag_objects": _dag_objects(columns, tables),
    }


def _postgres_report(dsn: str, agent_tables: Sequence[str]) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute(
                "SELECT columns.table_name, columns.column_name "
                "FROM information_schema.columns AS columns "
                "JOIN pg_catalog.pg_tables AS tables "
                "ON tables.schemaname = columns.table_schema "
                "AND tables.tablename = columns.table_name "
                "WHERE columns.table_schema='public' "
                "ORDER BY columns.table_name, columns.ordinal_position"
            )
            columns: dict[str, list[str]] = {}
            for table, column in cursor.fetchall():
                columns.setdefault(str(table), []).append(str(column))
            if set(agent_tables) - set(columns):
                raise AgentSchemaMigrationError("agent_schema_agent_table_missing")
            counts: dict[str, int] = {}
            digests: dict[str, str] = {}
            for table in agent_tables:
                if table not in columns:
                    continue
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                counts[table] = int(cursor.fetchone()[0])
                cursor.execute(
                    f'SELECT to_jsonb(value)::text FROM "{table}" AS value '
                    "ORDER BY to_jsonb(value)::text"
                )
                digest = hashlib.sha256()
                for (row_json,) in cursor:
                    digest.update(str(row_json).encode("utf-8"))
                    digest.update(b"\n")
                digests[table] = "sha256:" + digest.hexdigest()
            table_counts: dict[str, int] = {}
            for table in sorted(columns):
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                table_counts[table] = int(cursor.fetchone()[0])
    tables = set(columns)
    return {
        "schema_version": "pre-p7",
        "agent_row_counts": counts,
        "agent_data_digests": digests,
        "table_row_counts": table_counts,
        "schema_digest": sha256_bytes(canonical_bytes({"columns": columns})),
        "dag_objects": _dag_objects(columns, tables),
    }


def _sqlite_table_digest(connection: sqlite3.Connection, table: str) -> str:
    digest = hashlib.sha256()
    cursor = connection.execute(f'SELECT * FROM "{table}"')
    columns = [str(item[0]) for item in cursor.description or ()]
    digest.update(canonical_bytes({"columns": columns}))
    normalized_rows = [
        [_json_scalar(value) for value in row] for row in cursor.fetchall()
    ]
    for row in sorted(normalized_rows, key=lambda item: canonical_bytes({"row": item})):
        digest.update(canonical_bytes({"row": row}))
    return "sha256:" + digest.hexdigest()


def _json_scalar(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _dag_objects(
    columns: Mapping[str, Sequence[str]], tables: set[str]
) -> dict[str, Any]:
    task_columns = set(columns.get("submitted_tasks", ())) | set(
        columns.get("tasks", ())
    )
    node_columns = set(columns.get("task_nodes", ()))
    return {
        "task_edge_table": "task_edges" in tables,
        "task_root_node_id": "root_node_id" in task_columns,
        "task_node_fields": sorted(
            node_columns
            & {
                "criticality",
                "dependency_type",
                "retry_policy",
                "timeout_policy",
                "resource_class",
            }
        ),
        "planner_replan_claim_table": "planner_replan_claims" in tables,
    }


def _backup_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    _create_empty_file(destination, 0o600)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600, follow_symlinks=False)
    _fsync_file(destination)
    return _file_descriptor(destination)


def _restore_sqlite(source: Path, destination: Path, *, replace: bool = False) -> None:
    if destination.exists() and not replace:
        raise AgentSchemaMigrationError("agent_schema_restore_file_exists")
    if destination.exists() and destination.is_symlink():
        raise AgentSchemaMigrationError("agent_schema_restore_symlink")
    temporary = destination.with_name(destination.name + ".restore.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise AgentSchemaMigrationError("agent_schema_restore_temporary_exists")
    _backup_sqlite(source, temporary)
    if replace:
        os.replace(temporary, destination)
        os.chmod(destination, 0o600, follow_symlinks=False)
        _fsync_file(destination)
        _fsync_directory(destination.parent)
    else:
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)


def _validate_backup_files(root: Path, manifest: Mapping[str, Any]) -> None:
    files = _mapping(manifest, "files")
    for backend, name in {
        "sqlite": "sqlite.backup",
        "postgres": "postgres.dump",
        "sidecar": "sidecar.backup",
    }.items():
        actual = _file_descriptor(_secure_file(root / name, require_mode=0o600))
        if actual != files.get(backend):
            raise AgentSchemaMigrationError("agent_schema_backup_file_drift")


def _validate_report_sha(report: Mapping[str, Any]) -> None:
    if report.get("schema") != REPORT_SCHEMA:
        raise AgentSchemaMigrationError("agent_schema_report_invalid")
    payload = dict(report)
    claimed = payload.pop("report_sha256", None)
    if not _sha(claimed) or claimed != sha256_bytes(canonical_bytes(payload)):
        raise AgentSchemaMigrationError("agent_schema_report_sha_mismatch")


def _validate_manifest(
    manifest: Mapping[str, Any], *, expected_report_sha: str | None = None
) -> None:
    if manifest.get("schema") != BACKUP_SCHEMA:
        raise AgentSchemaMigrationError("agent_schema_backup_manifest_invalid")
    payload = dict(manifest)
    claimed = payload.pop("backup_set_sha256", None)
    if not _sha(claimed) or claimed != sha256_bytes(canonical_bytes(payload)):
        raise AgentSchemaMigrationError("agent_schema_backup_set_sha_mismatch")
    if (
        expected_report_sha is not None
        and manifest.get("report_sha256") != expected_report_sha
    ):
        raise AgentSchemaMigrationError("agent_schema_report_sha_mismatch")
    refs = manifest.get("restore_refs")
    if refs != {
        "sqlite": "sqlite.backup",
        "postgres": "postgres.dump",
        "sidecar": "sidecar.backup",
    }:
        raise AgentSchemaMigrationError("agent_schema_restore_reference_invalid")
    _schema_versions(manifest)


def _validate_manifest_descriptor(
    manifest: Mapping[str, Any], descriptor: StateDescriptor
) -> None:
    if (
        manifest.get("tested_commit") != descriptor.tested_commit
        or manifest.get("tested_tree") != descriptor.tested_tree
    ):
        raise AgentSchemaMigrationError("agent_schema_tested_revision_drift")


def _schema_versions(value: Mapping[str, Any]) -> dict[str, str]:
    explicit = value.get("schema_versions")
    if explicit is not None:
        if not isinstance(explicit, Mapping):
            raise AgentSchemaMigrationError("agent_schema_version_inventory_invalid")
        versions = {str(key): str(item) for key, item in explicit.items()}
    else:
        backends = value.get("backends")
        if not isinstance(backends, Mapping):
            raise AgentSchemaMigrationError("agent_schema_version_inventory_invalid")
        versions = {}
        for backend in ("sqlite", "postgres", "sidecar"):
            inventory = backends.get(backend)
            if not isinstance(inventory, Mapping) or not inventory.get(
                "schema_version"
            ):
                raise AgentSchemaMigrationError(
                    "agent_schema_version_inventory_invalid"
                )
            versions[backend] = str(inventory["schema_version"])
    if set(versions) != {"sqlite", "postgres", "sidecar"}:
        raise AgentSchemaMigrationError("agent_schema_version_inventory_invalid")
    return versions


def _require_backend_inventory(
    expected: Mapping[str, Any], current: Mapping[str, Any]
) -> None:
    if (
        expected.get("tested_commit") != current.get("tested_commit")
        or expected.get("tested_tree") != current.get("tested_tree")
        or expected.get("backends") != current.get("backends")
        or expected.get("blockers") != []
    ):
        raise AgentSchemaMigrationError("agent_schema_report_drift")


def _semantic_backends(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentSchemaMigrationError("agent_schema_backend_inventory_invalid")
    normalized = json.loads(json.dumps(value))
    for backend in ("sqlite", "sidecar"):
        if isinstance(normalized.get(backend), dict):
            normalized[backend].pop("file_sha256", None)
    return normalized


def _file_descriptor(path: Path) -> dict[str, Any]:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise AgentSchemaMigrationError("agent_schema_file_identity_invalid")
    digest = hashlib.sha256()
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    with os.fdopen(descriptor, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return {
        "name": path.name,
        "sha256": "sha256:" + digest.hexdigest(),
        "size_bytes": int(metadata.st_size),
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _secure_directory(path: Path, *, require_existing: bool) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise AgentSchemaMigrationError("agent_schema_directory_identity_invalid")
    if not expanded.exists():
        if require_existing:
            raise AgentSchemaMigrationError("agent_schema_directory_missing")
        expanded.mkdir(mode=0o700, parents=True)
    resolved = expanded.resolve()
    metadata = os.lstat(resolved)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise AgentSchemaMigrationError("agent_schema_directory_mode_invalid")
    return resolved


def _secure_file(path: Path, require_mode: int | None) -> Path:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or (require_mode is not None and stat.S_IMODE(metadata.st_mode) != require_mode)
    ):
        raise AgentSchemaMigrationError("agent_schema_file_identity_invalid")
    return path


def _configured_regular_file(root: Path, value: Any) -> Path:
    raw = str(value or "")
    candidate = Path(raw)
    if not raw or ".." in candidate.parts or candidate.is_symlink():
        raise AgentSchemaMigrationError("agent_schema_state_path_invalid")
    resolved = (
        candidate.resolve(strict=True)
        if candidate.is_absolute()
        else (root / candidate).resolve(strict=True)
    )
    return _secure_file(resolved, require_mode=None)


def _create_empty_file(path: Path, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        mode,
    )
    os.close(descriptor)


def _write_json_no_clobber(
    path: Path, payload: Mapping[str, Any], *, mode: int
) -> None:
    _create_empty_file(path, mode)
    try:
        with path.open("wb") as handle:
            handle.write(canonical_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode, follow_symlinks=False)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        raise


def _load_json_secure(path: Path, mode: int) -> dict[str, Any]:
    _secure_file(path, require_mode=mode)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AgentSchemaMigrationError("agent_schema_json_invalid")
    return payload


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise AgentSchemaMigrationError("agent_schema_state_invalid")
    return nested


def _table_names(value: Any, *, require_nonempty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (require_nonempty and not value):
        raise AgentSchemaMigrationError("agent_schema_table_list_invalid")
    names = tuple(str(item) for item in value)
    if any(not name.replace("_", "").isalnum() for name in names):
        raise AgentSchemaMigrationError("agent_schema_table_name_invalid")
    return names


def _env_name(value: Any) -> str:
    name = str(value or "")
    if not name or not name.replace("_", "").isalnum() or name.upper() != name:
        raise AgentSchemaMigrationError("agent_schema_dsn_env_invalid")
    return name


def _required_secret_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise AgentSchemaMigrationError("agent_schema_dsn_env_missing")
    return value


def _require_sha(expected: str, actual: Any, kind: str) -> None:
    if not _sha(expected) or expected != actual:
        raise AgentSchemaMigrationError(f"agent_schema_{kind}_sha_mismatch")


def _sha(value: Any) -> bool:
    text = str(value or "")
    return text.startswith("sha256:") and _hex(text.removeprefix("sha256:"), 64)


def _hex(value: str, length: int) -> bool:
    return len(value) == length and all(
        character in "0123456789abcdef" for character in value
    )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "AgentSchemaMigrationError",
    "BACKUP_SCHEMA",
    "REPORT_SCHEMA",
    "RECEIPT_SCHEMA",
    "RECEIPT_DIRECTORY",
    "STATE_FILE",
    "STATE_SCHEMA",
    "StateDescriptor",
    "backup_all",
    "build_report",
    "load_state_descriptor",
    "migration_lock",
    "remember_report_path",
    "restore_all",
    "restore_check",
    "verify_tested_revision",
    "write_report",
]
