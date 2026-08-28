from __future__ import annotations

import json
import fcntl
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from src.core.enums import ArtifactType
from src.core.enums import TaskStatus
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    build_file_storage_ref,
    parse_file_storage_ref,
    sanitize_storage_component,
)

from .models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentRun,
    AgentRunStatus,
    AgentStagedArtifact,
)
from .result_projection import skill_result_artifact_id


SKILL_RESULT_FILENAME = "skill_result.json"
SKILL_RESULT_MIME_TYPE = "application/json"
SKILL_RESULT_MANIFEST_SCHEMA = "maf.agent.skill_result_stage_manifest.v1"
SKILL_RESULT_STORAGE_SCHEMA = "maf.agent.skill_result.storage.v1"


@dataclass(frozen=True, slots=True)
class AgentSkillResultStage:
    artifact: AgentStagedArtifact
    manifest_path: Path


class AgentSkillResultArtifactStager:
    """Stage deterministic Skill raw JSON without publishing Artifact metadata."""

    def __init__(
        self,
        *,
        file_store: LocalArtifactFileStore,
        manifest_root: str | Path,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._file_store = file_store
        self._manifest_root = Path(manifest_root)
        self._manifest_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._manifest_root, 0o700, follow_symlinks=False)
        _validate_private_directory(self._manifest_root)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    @property
    def manifest_root(self) -> Path:
        return self._manifest_root

    def stage(
        self,
        *,
        run: AgentRun,
        call_item: AgentItem,
        node_id: str,
        canonical_raw_bytes: bytes | None,
        raw_sha256: str | None,
        projection_revision: str | None,
        expected_artifact_id: str | None,
    ) -> AgentStagedArtifact:
        if (
            not isinstance(canonical_raw_bytes, bytes)
            or not canonical_raw_bytes
            or not isinstance(raw_sha256, str)
            or len(raw_sha256) != 64
            or not isinstance(projection_revision, str)
            or not projection_revision
            or not isinstance(expected_artifact_id, str)
        ):
            raise ValueError("agent_skill_result_stage_identity_invalid")
        artifact_id = skill_result_artifact_id(
            call_item_id=call_item.item_id,
            raw_sha256=raw_sha256,
            projection_revision=projection_revision,
        )
        if artifact_id != expected_artifact_id:
            raise ValueError("agent_skill_result_stage_identity_invalid")
        stable_manifest = {
            "schema": SKILL_RESULT_MANIFEST_SCHEMA,
            "artifact_id": artifact_id,
            "task_id": run.task_id,
            "conversation_id": run.conversation_id,
            "node_id": node_id,
            "call_item_id": call_item.item_id,
            "raw_sha256": raw_sha256,
            "projection_revision": projection_revision,
            "storage_key": (
                f"{sanitize_storage_component(artifact_id)}/{SKILL_RESULT_FILENAME}"
            ),
            "size_bytes": len(canonical_raw_bytes),
        }
        manifest_path = self._manifest_path(artifact_id)
        manifest = self._create_or_validate_manifest(
            manifest_path,
            stable_manifest=stable_manifest,
        )
        stored = self._file_store.save_bytes(
            artifact_id=artifact_id,
            filename=SKILL_RESULT_FILENAME,
            content=canonical_raw_bytes,
        )
        if (
            stored.storage_key != stable_manifest["storage_key"]
            or stored.size_bytes != stable_manifest["size_bytes"]
            or stored.sha256 != raw_sha256
            or manifest["storage_key"] != stored.storage_key
        ):
            raise ValueError("agent_skill_result_stage_content_conflict")
        storage_ref = build_file_storage_ref(
            {
                "schema": SKILL_RESULT_STORAGE_SCHEMA,
                "source_kind": "skill_result",
                "retention_status": "active",
                "task_id": run.task_id,
                "conversation_id": run.conversation_id,
                "node_id": node_id,
                "call_item_id": call_item.item_id,
                "raw_sha256": raw_sha256,
                "projection_revision": projection_revision,
                "filename": SKILL_RESULT_FILENAME,
                "mime_type": SKILL_RESULT_MIME_TYPE,
                "size_bytes": stored.size_bytes,
                "sha256": stored.sha256,
                "storage_key": stored.storage_key,
            }
        )
        return AgentStagedArtifact(
            artifact_id=artifact_id,
            artifact_type=str(ArtifactType.FILE),
            storage_ref=storage_ref,
            summary="完整 Skill 结构化结果",
        )

    def _manifest_path(self, artifact_id: str) -> Path:
        return self._manifest_root / (
            sanitize_storage_component(artifact_id) + ".json"
        )

    def _create_or_validate_manifest(
        self,
        path: Path,
        *,
        stable_manifest: Mapping[str, object],
    ) -> dict[str, object]:
        root_descriptor = os.open(
            self._manifest_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            fcntl.flock(root_descriptor, fcntl.LOCK_EX)
            if path.exists():
                return _load_exact_manifest(
                    path,
                    stable_manifest=stable_manifest,
                )
            manifest = {
                **dict(stable_manifest),
                "staged_at": self._now().isoformat(),
            }
            body = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(path, 0o600, follow_symlinks=False)
                _fsync_directory(self._manifest_root)
            except Exception:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
            return manifest
        finally:
            fcntl.flock(root_descriptor, fcntl.LOCK_UN)
            os.close(root_descriptor)


@dataclass(frozen=True, slots=True)
class AgentSkillResultJanitorResult:
    manifests_removed: int = 0
    raw_files_removed: int = 0
    retained: int = 0


class AgentSkillResultArtifactJanitor:
    def __init__(
        self,
        *,
        file_store: LocalArtifactFileStore,
        manifest_root: str | Path,
        storage,
        runs,
        now_fn: Callable[[], datetime] | None = None,
        retention: timedelta = timedelta(hours=24),
    ) -> None:
        self._file_store = file_store
        self._manifest_root = Path(manifest_root)
        self._storage = storage
        self._runs = runs
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._retention = retention

    async def run_once(self) -> AgentSkillResultJanitorResult:
        if not self._manifest_root.exists():
            return AgentSkillResultJanitorResult()
        removed_manifests = 0
        removed_raw = 0
        retained = 0
        for path in sorted(self._manifest_root.iterdir()):
            try:
                manifest = load_skill_result_stage_manifest(path)
                artifact = await self._storage.get_artifact(
                    str(manifest["artifact_id"])
                )
                if artifact is not None:
                    if not _artifact_matches_manifest(artifact, manifest):
                        retained += 1
                        continue
                    path.unlink()
                    _fsync_directory(self._manifest_root)
                    removed_manifests += 1
                    continue
                age = self._now().timestamp() - path.stat().st_mtime
                if age < self._retention.total_seconds():
                    retained += 1
                    continue
                run = await self._runs.get_run_for_task(str(manifest["task_id"]))
                if run is not None:
                    if run.status in {
                        AgentRunStatus.RUNNING,
                        AgentRunStatus.WAITING_FOR_INPUT,
                        AgentRunStatus.WAITING_FOR_DEPENDENCY,
                    }:
                        retained += 1
                        continue
                    items = await self._runs.list_items(run.run_id)
                    result = next(
                        (
                            item
                            for item in items
                            if item.kind is AgentItemKind.TOOL_RESULT
                            and item.source_call_item_id
                            == manifest["call_item_id"]
                        ),
                        None,
                    )
                    if result is not None and result.state is AgentItemState.RESERVED:
                        retained += 1
                        continue
                task = await self._storage.get_task(str(manifest["task_id"]))
                if task is not None and task.status not in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    retained += 1
                    continue
                raw_deleted = self._file_store.delete(
                    str(manifest["storage_key"])
                )
                path.unlink()
                _fsync_directory(self._manifest_root)
                removed_manifests += 1
                removed_raw += int(raw_deleted)
            except Exception:
                retained += 1
        return AgentSkillResultJanitorResult(
            manifests_removed=removed_manifests,
            raw_files_removed=removed_raw,
            retained=retained,
        )


def _load_exact_manifest(
    path: Path,
    *,
    stable_manifest: Mapping[str, object],
) -> dict[str, object]:
    _validate_private_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("agent_skill_result_stage_manifest_invalid") from exc
    expected_keys = {*stable_manifest, "staged_at"}
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or any(value[key] != expected for key, expected in stable_manifest.items())
        or not isinstance(value.get("staged_at"), str)
        or not value["staged_at"]
    ):
        raise ValueError("agent_skill_result_stage_manifest_conflict")
    return value


def load_skill_result_stage_manifest(path: Path) -> dict[str, object]:
    _validate_private_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("agent_skill_result_stage_manifest_invalid") from exc
    exact_keys = {
        "schema",
        "artifact_id",
        "task_id",
        "conversation_id",
        "node_id",
        "call_item_id",
        "raw_sha256",
        "projection_revision",
        "storage_key",
        "size_bytes",
        "staged_at",
    }
    if (
        not isinstance(value, dict)
        or set(value) != exact_keys
        or value.get("schema") != SKILL_RESULT_MANIFEST_SCHEMA
        or any(
            not isinstance(value.get(key), str) or not value[key]
            for key in exact_keys - {"size_bytes", "schema"}
        )
        or not isinstance(value.get("size_bytes"), int)
        or isinstance(value.get("size_bytes"), bool)
        or int(value["size_bytes"]) <= 0
    ):
        raise ValueError("agent_skill_result_stage_manifest_invalid")
    return value


def _artifact_matches_manifest(artifact, manifest: Mapping[str, object]) -> bool:
    metadata = parse_skill_result_storage_ref(artifact.storage_ref)
    return bool(
        metadata
        and artifact.artifact_id == manifest["artifact_id"]
        and artifact.task_id == manifest["task_id"]
        and artifact.producer_node_id == manifest["node_id"]
        and artifact.artifact_type == ArtifactType.FILE
        and metadata["task_id"] == manifest["task_id"]
        and metadata["conversation_id"] == manifest["conversation_id"]
        and metadata["node_id"] == manifest["node_id"]
        and metadata["call_item_id"] == manifest["call_item_id"]
        and metadata["raw_sha256"] == manifest["raw_sha256"]
        and metadata["projection_revision"] == manifest["projection_revision"]
        and metadata["storage_key"] == manifest["storage_key"]
        and metadata["size_bytes"] == manifest["size_bytes"]
    )


def parse_skill_result_storage_ref(storage_ref: str) -> dict[str, object] | None:
    try:
        value = json.loads(storage_ref)
    except (TypeError, json.JSONDecodeError):
        return None
    exact_keys = {
        "schema",
        "source_kind",
        "retention_status",
        "task_id",
        "conversation_id",
        "node_id",
        "call_item_id",
        "raw_sha256",
        "projection_revision",
        "filename",
        "mime_type",
        "size_bytes",
        "sha256",
        "storage_key",
    }
    if (
        not isinstance(value, dict)
        or set(value) != exact_keys
        or value.get("schema") != SKILL_RESULT_STORAGE_SCHEMA
        or value.get("source_kind") != "skill_result"
        or value.get("retention_status") != "active"
        or value.get("filename") != SKILL_RESULT_FILENAME
        or value.get("mime_type") != SKILL_RESULT_MIME_TYPE
    ):
        return None
    return value


def validate_skill_result_staged_artifact(
    artifact: AgentStagedArtifact,
    *,
    run: AgentRun,
    node_id: str,
    call_item_id: str,
    safe_result: Mapping[str, object] | None,
) -> None:
    generic = parse_file_storage_ref(artifact.storage_ref)
    if not generic or generic.get("source_kind") != "skill_result":
        return
    metadata = parse_skill_result_storage_ref(artifact.storage_ref)
    if metadata is None or safe_result is None:
        raise ValueError("agent_skill_result_artifact_metadata_invalid")
    raw_sha256 = safe_result.get("raw_sha256")
    projection_revision = safe_result.get("projection_revision")
    expected_artifact_id = (
        skill_result_artifact_id(
            call_item_id=call_item_id,
            raw_sha256=str(raw_sha256),
            projection_revision=str(projection_revision),
        )
        if isinstance(raw_sha256, str) and isinstance(projection_revision, str)
        else None
    )
    if (
        artifact.artifact_type != str(ArtifactType.FILE)
        or artifact.artifact_id != expected_artifact_id
        or safe_result.get("schema") != "maf.agent.model_result.v1"
        or safe_result.get("projection_mode") != "artifact_backed"
        or safe_result.get("projection_truncated") is not True
        or metadata["task_id"] != run.task_id
        or metadata["conversation_id"] != run.conversation_id
        or metadata["node_id"] != node_id
        or metadata["call_item_id"] != call_item_id
        or metadata["raw_sha256"] != raw_sha256
        or metadata["projection_revision"] != projection_revision
        or metadata["sha256"] != raw_sha256
        or not isinstance(metadata["size_bytes"], int)
        or isinstance(metadata["size_bytes"], bool)
        or int(metadata["size_bytes"]) <= 0
    ):
        raise ValueError("agent_skill_result_artifact_metadata_invalid")


def _validate_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("agent_skill_result_stage_directory_invalid")


def _validate_private_file(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise ValueError("agent_skill_result_stage_manifest_invalid")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
