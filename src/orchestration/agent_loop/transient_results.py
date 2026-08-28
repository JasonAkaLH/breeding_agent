from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from src.core.enums import TaskStatus
from src.storage.agent_payload import canonicalize_agent_payload
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    sanitize_storage_component,
)

from .models import AgentItem, AgentItemKind, AgentRun


AGENT_TRANSIENT_SKILL_RESULT_MANIFEST_SCHEMA = (
    "maf.agent.transient_skill_result_stage_manifest.v1"
)
AGENT_TRANSIENT_SKILL_RESULT_SOURCE_KIND = "agent_transient_skill_result"
AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION = "skill-result-v2"
_STAGE_REF_PREFIX = "agent-transient-skill-result:"
_RAW_FILENAME = "result.json"


@dataclass(frozen=True, slots=True)
class AgentTransientSkillResultStage:
    stage_ref: str
    raw_size_bytes: int
    raw_sha256: str
    projection_revision: str


@dataclass(frozen=True, slots=True)
class AgentTransientSkillResultRecoveryStage:
    stage_ref: str
    capability_id: str
    node_id: str
    raw_size_bytes: int
    raw_sha256: str
    projection_revision: str


class AgentTransientSkillResultStore:
    """Private, non-Artifact store for oversized ordinary Skill results."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._root, 0o700, follow_symlinks=False)
        _validate_private_directory(self._root)
        self._raw_root = self._root / "raw"
        self._raw_store = LocalArtifactFileStore(self._raw_root)
        self._manifest_root = self._root / "manifests"
        self._manifest_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._manifest_root, 0o700, follow_symlinks=False)
        _validate_private_directory(self._manifest_root)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    @property
    def raw_root(self) -> Path:
        return self._raw_root

    @property
    def manifest_root(self) -> Path:
        return self._manifest_root

    def manifest_path(self, stage_ref: str) -> Path:
        digest = _stage_ref_digest(stage_ref)
        return self._manifest_root / f"{digest}.json"

    def manifest_paths(self) -> tuple[Path, ...]:
        return tuple(sorted(self._manifest_root.iterdir()))

    def load_manifest(self, stage_ref: str) -> dict[str, object]:
        try:
            return _load_manifest(self.manifest_path(stage_ref))
        except (OSError, TypeError, ValueError):
            raise ValueError(
                "agent_transient_skill_result_unavailable"
            ) from None

    def read_raw(
        self,
        stage_ref: str,
        *,
        expected_size_bytes: int,
        expected_sha256: str,
    ) -> bytes:
        try:
            _stage_ref_digest(stage_ref)
            storage_key = (
                f"{sanitize_storage_component(stage_ref)}/{_RAW_FILENAME}"
            )
            text = self._raw_store.read_utf8(
                storage_key,
                expected_size_bytes=expected_size_bytes,
                expected_sha256=expected_sha256,
            )
            return text.encode("utf-8")
        except (OSError, TypeError, ValueError):
            raise ValueError(
                "agent_transient_skill_result_unavailable"
            ) from None

    def recover_stage(
        self,
        *,
        run: AgentRun,
        call_item: AgentItem,
        result_item: AgentItem,
    ) -> AgentTransientSkillResultRecoveryStage | None:
        matches: list[dict[str, object]] = []
        for path in self.manifest_paths():
            try:
                manifest = _load_manifest(path)
            except (OSError, TypeError, ValueError):
                continue
            if (
                manifest["run_id"] == run.run_id
                and manifest["call_item_id"] == call_item.item_id
                and manifest["result_item_id"] == result_item.item_id
            ):
                matches.append(manifest)
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("agent_transient_skill_result_unavailable")
        manifest = matches[0]
        try:
            call_payload = json.loads(call_item.payload_json)
            if (
                not isinstance(call_payload, Mapping)
                or call_item.kind is not AgentItemKind.TOOL_CALL
                or result_item.kind is not AgentItemKind.TOOL_RESULT
                or result_item.source_call_item_id != call_item.item_id
                or manifest["task_id"] != run.task_id
                or manifest["conversation_id"] != run.conversation_id
                or manifest["node_id"] != call_payload.get("node_id")
                or manifest["capability_id"]
                != call_payload.get("capability_id")
            ):
                raise ValueError("identity_drift")
            raw = self.read_raw(
                str(manifest["stage_ref"]),
                expected_size_bytes=int(manifest["raw_size_bytes"]),
                expected_sha256=str(manifest["raw_sha256"]),
            )
            value = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(value, dict)
                or (
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
                != raw
            ):
                raise ValueError("raw_drift")
            return AgentTransientSkillResultRecoveryStage(
                stage_ref=str(manifest["stage_ref"]),
                capability_id=str(manifest["capability_id"]),
                node_id=str(manifest["node_id"]),
                raw_size_bytes=int(manifest["raw_size_bytes"]),
                raw_sha256=str(manifest["raw_sha256"]),
                projection_revision=str(manifest["projection_revision"]),
            )
        except (OSError, TypeError, ValueError):
            raise ValueError(
                "agent_transient_skill_result_unavailable"
            ) from None

    def delete_stage(
        self,
        stage_ref: str,
        *,
        expected_run_id: str | None = None,
        expected_result_item_id: str | None = None,
    ) -> bool:
        try:
            manifest = _load_manifest(self.manifest_path(stage_ref))
            if (
                (expected_run_id is not None and manifest["run_id"] != expected_run_id)
                or (
                    expected_result_item_id is not None
                    and manifest["result_item_id"] != expected_result_item_id
                )
            ):
                raise ValueError("identity_drift")
            storage_key = (
                f"{sanitize_storage_component(stage_ref)}/{_RAW_FILENAME}"
            )
            raw_deleted = self._raw_store.delete(storage_key)
            manifest_path = self.manifest_path(stage_ref)
            manifest_path.unlink()
            _fsync_directory(self._manifest_root)
            return raw_deleted
        except (OSError, TypeError, ValueError):
            raise ValueError(
                "agent_transient_skill_result_cleanup_failed"
            ) from None

    def stage(
        self,
        *,
        run: AgentRun,
        call_item: AgentItem,
        result_item_id: str,
        node_id: str,
        capability_id: str,
        canonical_raw_bytes: bytes | None,
        raw_sha256: str | None,
        projection_revision: str | None,
        expected_stage_ref: str | None,
    ) -> AgentTransientSkillResultStage:
        try:
            return self._stage(
                run=run,
                call_item=call_item,
                result_item_id=result_item_id,
                node_id=node_id,
                capability_id=capability_id,
                canonical_raw_bytes=canonical_raw_bytes,
                raw_sha256=raw_sha256,
                projection_revision=projection_revision,
                expected_stage_ref=expected_stage_ref,
            )
        except ValueError as exc:
            if str(exc) in {
                "agent_transient_skill_result_stage_identity_invalid",
                "agent_transient_skill_result_stage_conflict",
            }:
                raise
            raise ValueError(
                "agent_transient_skill_result_stage_invalid"
            ) from None
        except (OSError, TypeError):
            raise ValueError(
                "agent_transient_skill_result_stage_invalid"
            ) from None

    def _stage(
        self,
        *,
        run: AgentRun,
        call_item: AgentItem,
        result_item_id: str,
        node_id: str,
        capability_id: str,
        canonical_raw_bytes: bytes | None,
        raw_sha256: str | None,
        projection_revision: str | None,
        expected_stage_ref: str | None,
    ) -> AgentTransientSkillResultStage:
        if (
            not isinstance(canonical_raw_bytes, bytes)
            or not canonical_raw_bytes
            or not _is_sha256(raw_sha256)
            or projection_revision
            != AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
            or not isinstance(result_item_id, str)
            or not result_item_id.strip()
            or not isinstance(node_id, str)
            or not node_id.strip()
            or not isinstance(capability_id, str)
            or not capability_id.startswith("skill.")
            or call_item.kind is not AgentItemKind.TOOL_CALL
            or call_item.run_id != run.run_id
            or call_item.task_id != run.task_id
        ):
            raise ValueError(
                "agent_transient_skill_result_stage_identity_invalid"
            )
        assert raw_sha256 is not None
        if hashlib.sha256(canonical_raw_bytes).hexdigest() != raw_sha256:
            raise ValueError(
                "agent_transient_skill_result_stage_identity_invalid"
            )
        stage_ref = transient_skill_result_stage_ref(
            call_item_id=call_item.item_id,
            raw_sha256=raw_sha256,
            projection_revision=projection_revision,
        )
        if expected_stage_ref != stage_ref:
            raise ValueError(
                "agent_transient_skill_result_stage_identity_invalid"
            )
        stable_manifest: dict[str, object] = {
            "schema": AGENT_TRANSIENT_SKILL_RESULT_MANIFEST_SCHEMA,
            "source_kind": AGENT_TRANSIENT_SKILL_RESULT_SOURCE_KIND,
            "stage_ref": stage_ref,
            "run_id": run.run_id,
            "task_id": run.task_id,
            "conversation_id": run.conversation_id,
            "node_id": node_id,
            "call_item_id": call_item.item_id,
            "result_item_id": result_item_id,
            "capability_id": capability_id,
            "raw_size_bytes": len(canonical_raw_bytes),
            "raw_sha256": raw_sha256,
            "projection_revision": projection_revision,
        }
        stored = self._raw_store.save_bytes(
            artifact_id=stage_ref,
            filename=_RAW_FILENAME,
            content=canonical_raw_bytes,
        )
        if (
            stored.size_bytes != stable_manifest["raw_size_bytes"]
            or stored.sha256 != raw_sha256
        ):
            raise ValueError("agent_transient_skill_result_stage_conflict")
        self._create_or_validate_manifest(
            self.manifest_path(stage_ref),
            stable_manifest=stable_manifest,
        )
        self._raw_store.open_verified_path(
            stored.storage_key,
            expected_size_bytes=len(canonical_raw_bytes),
            expected_sha256=raw_sha256,
        )
        return AgentTransientSkillResultStage(
            stage_ref=stage_ref,
            raw_size_bytes=len(canonical_raw_bytes),
            raw_sha256=raw_sha256,
            projection_revision=projection_revision,
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
            if path.exists() or path.is_symlink():
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
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                os.fsync(handle.fileno())
            _fsync_directory(self._manifest_root)
            return _load_exact_manifest(
                path,
                stable_manifest=stable_manifest,
            )
        finally:
            fcntl.flock(root_descriptor, fcntl.LOCK_UN)
            os.close(root_descriptor)


class AgentTransientSkillResultResolver:
    def __init__(self, store: AgentTransientSkillResultStore) -> None:
        self._store = store

    def resolve_tool_result(
        self,
        *,
        run: AgentRun,
        call_item: AgentItem,
        result_item: AgentItem,
        durable_payload: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            return self._resolve_tool_result(
                run=run,
                call_item=call_item,
                result_item=result_item,
                durable_payload=durable_payload,
            )
        except (OSError, TypeError, ValueError):
            raise ValueError(
                "agent_transient_skill_result_unavailable"
            ) from None

    def _resolve_tool_result(
        self,
        *,
        run: AgentRun,
        call_item: AgentItem,
        result_item: AgentItem,
        durable_payload: Mapping[str, object],
    ) -> dict[str, object]:
        safe_result = durable_payload.get("safe_result")
        if not isinstance(safe_result, Mapping):
            raise ValueError("receipt_invalid")
        receipt_keys = {
            "schema",
            "projection_revision",
            "projection_mode",
            "model_view",
            "original_size_bytes",
            "projected_size_bytes",
            "raw_sha256",
            "projection_truncated",
        }
        model_view = safe_result.get("model_view")
        if (
            set(safe_result) != receipt_keys
            or safe_result.get("schema") != "maf.agent.model_result.v1"
            or safe_result.get("projection_revision")
            != AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
            or safe_result.get("projection_mode") != "transient_staged"
            or safe_result.get("projection_truncated") is not True
            or not isinstance(model_view, Mapping)
            or set(model_view)
            != {
                "complete_result_pending_context_injection",
                "schema",
                "stage_ref",
            }
            or model_view.get("complete_result_pending_context_injection")
            is not True
            or model_view.get("schema")
            != "maf.agent.transient_skill_result_receipt.v1"
            or not isinstance(model_view.get("stage_ref"), str)
            or durable_payload.get("outcome") != "completed"
            or durable_payload.get("safe_error_code") is not None
            or durable_payload.get("artifact_refs") != []
            or durable_payload.get("call_item_id") != call_item.item_id
            or result_item.kind is not AgentItemKind.TOOL_RESULT
            or result_item.source_call_item_id != call_item.item_id
            or result_item.run_id != run.run_id
            or result_item.task_id != run.task_id
        ):
            raise ValueError("receipt_invalid")
        original_size = safe_result.get("original_size_bytes")
        projected_size = safe_result.get("projected_size_bytes")
        raw_sha256 = safe_result.get("raw_sha256")
        stage_ref = str(model_view["stage_ref"])
        if (
            isinstance(original_size, bool)
            or not isinstance(original_size, int)
            or original_size <= 0
            or isinstance(projected_size, bool)
            or not isinstance(projected_size, int)
            or projected_size <= 0
            or not _is_sha256(raw_sha256)
            or transient_skill_result_stage_ref(
                call_item_id=call_item.item_id,
                raw_sha256=str(raw_sha256),
                projection_revision=(
                    AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
                ),
            )
            != stage_ref
            or canonicalize_agent_payload(dict(safe_result)).size_bytes
            != projected_size
        ):
            raise ValueError("receipt_invalid")
        call_payload = json.loads(call_item.payload_json)
        if not isinstance(call_payload, Mapping):
            raise ValueError("call_invalid")
        capability_id = call_payload.get("capability_id")
        node_id = call_payload.get("node_id")
        manifest = self._store.load_manifest(stage_ref)
        expected_manifest = {
            "schema": AGENT_TRANSIENT_SKILL_RESULT_MANIFEST_SCHEMA,
            "source_kind": AGENT_TRANSIENT_SKILL_RESULT_SOURCE_KIND,
            "stage_ref": stage_ref,
            "run_id": run.run_id,
            "task_id": run.task_id,
            "conversation_id": run.conversation_id,
            "node_id": node_id,
            "call_item_id": call_item.item_id,
            "result_item_id": result_item.item_id,
            "capability_id": capability_id,
            "raw_size_bytes": original_size,
            "raw_sha256": raw_sha256,
            "projection_revision": (
                AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
            ),
        }
        if any(
            manifest.get(key) != value
            for key, value in expected_manifest.items()
        ):
            raise ValueError("manifest_conflict")
        raw_bytes = self._store.read_raw(
            stage_ref,
            expected_size_bytes=original_size,
            expected_sha256=str(raw_sha256),
        )
        raw_value = json.loads(raw_bytes.decode("utf-8"))
        if (
            not isinstance(raw_value, dict)
            or (
                json.dumps(
                    raw_value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            != raw_bytes
        ):
            raise ValueError("raw_invalid")
        return {
            "artifact_refs": [],
            "outcome": "completed",
            "safe_error_code": None,
            "safe_result": {
                "schema": "maf.agent.skill_result_full.v1",
                "result": raw_value,
            },
        }


class AgentTransientSkillResultCleaner:
    def __init__(self, store: AgentTransientSkillResultStore) -> None:
        self._store = store

    def cleanup_terminal(
        self,
        *,
        run: AgentRun,
        items: tuple[AgentItem, ...],
    ) -> int:
        terminal = str(run.status) in {"completed", "failed", "cancelled"}
        if not terminal:
            raise ValueError("agent_transient_skill_result_cleanup_not_terminal")
        return self._cleanup(run=run, items=items)

    def cleanup_covered(
        self,
        *,
        run: AgentRun,
        items: tuple[AgentItem, ...],
        covered_end_sequence: int,
    ) -> int:
        return self._cleanup(
            run=run,
            items=tuple(
                item
                for item in items
                if item.sequence <= covered_end_sequence
            ),
        )

    def _cleanup(
        self,
        *,
        run: AgentRun,
        items: tuple[AgentItem, ...],
    ) -> int:
        removed = 0
        for item in items:
            stage_ref = transient_stage_ref_from_result_item(item)
            if stage_ref is None:
                continue
            self._store.delete_stage(
                stage_ref,
                expected_run_id=run.run_id,
                expected_result_item_id=item.item_id,
            )
            removed += 1
        return removed


@dataclass(frozen=True, slots=True)
class AgentTransientSkillResultJanitorResult:
    stages_removed: int = 0
    retained: int = 0
    unmanifested_raw_retained: int = 0


class AgentTransientSkillResultJanitor:
    def __init__(
        self,
        *,
        store: AgentTransientSkillResultStore,
        runs: object,
        storage: object,
        now_fn: Callable[[], datetime] | None = None,
        retention_seconds: int = 24 * 60 * 60,
    ) -> None:
        self._store = store
        self._runs = runs
        self._storage = storage
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._retention_seconds = retention_seconds

    async def run_once(self) -> AgentTransientSkillResultJanitorResult:
        removed = 0
        retained = 0
        manifested_stage_refs: set[str] = set()
        for path in self._store.manifest_paths():
            try:
                manifest = _load_manifest(path)
                stage_ref = str(manifest["stage_ref"])
                manifested_stage_refs.add(stage_ref)
                run = await self._runs.get_run(str(manifest["run_id"]))
                should_delete = False
                if run is not None:
                    if (
                        run.task_id != manifest["task_id"]
                        or run.conversation_id != manifest["conversation_id"]
                    ):
                        retained += 1
                        continue
                    items = await self._runs.list_items(run.run_id)
                    result = next(
                        (
                            item
                            for item in items
                            if item.item_id == manifest["result_item_id"]
                        ),
                        None,
                    )
                    if result is None:
                        retained += 1
                        continue
                    should_delete = bool(
                        run.status.value in {"completed", "failed", "cancelled"}
                        or result.sequence <= run.compacted_through_sequence
                    )
                else:
                    staged_at = datetime.fromisoformat(str(manifest["staged_at"]))
                    now = self._now()
                    if staged_at.tzinfo is None:
                        staged_at = staged_at.replace(tzinfo=timezone.utc)
                    if now.tzinfo is None:
                        now = now.replace(tzinfo=timezone.utc)
                    if (now - staged_at).total_seconds() >= self._retention_seconds:
                        task = await self._storage.get_task(
                            str(manifest["task_id"])
                        )
                        should_delete = bool(
                            task is None
                            or task.status
                            in {
                                TaskStatus.COMPLETED,
                                TaskStatus.FAILED,
                                TaskStatus.CANCELLED,
                            }
                        )
                if should_delete:
                    self._store.delete_stage(
                        stage_ref,
                        expected_run_id=str(manifest["run_id"]),
                        expected_result_item_id=str(manifest["result_item_id"]),
                    )
                    removed += 1
                else:
                    retained += 1
            except Exception:
                retained += 1
        unmanifested = 0
        for path in self._store.raw_root.rglob(_RAW_FILENAME):
            stage_ref = path.parent.name
            if stage_ref not in manifested_stage_refs:
                unmanifested += 1
        return AgentTransientSkillResultJanitorResult(
            stages_removed=removed,
            retained=retained,
            unmanifested_raw_retained=unmanifested,
        )


def transient_stage_ref_from_result_item(item: AgentItem) -> str | None:
    if (
        item.kind is not AgentItemKind.TOOL_RESULT
        or item.state.value != "committed"
    ):
        return None
    try:
        payload = json.loads(item.payload_json)
    except json.JSONDecodeError:
        return None
    safe_result = payload.get("safe_result") if isinstance(payload, dict) else None
    model_view = (
        safe_result.get("model_view")
        if isinstance(safe_result, dict)
        else None
    )
    if (
        not isinstance(model_view, dict)
        or safe_result.get("schema") != "maf.agent.model_result.v1"
        or safe_result.get("projection_revision")
        != AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
        or safe_result.get("projection_mode") != "transient_staged"
        or safe_result.get("projection_truncated") is not True
        or model_view.get("schema")
        != "maf.agent.transient_skill_result_receipt.v1"
        or not isinstance(model_view.get("stage_ref"), str)
    ):
        return None
    _stage_ref_digest(model_view["stage_ref"])
    return model_view["stage_ref"]

def transient_skill_result_stage_ref(
    *,
    call_item_id: str,
    raw_sha256: str,
    projection_revision: str,
) -> str:
    if (
        not isinstance(call_item_id, str)
        or not call_item_id.strip()
        or not _is_sha256(raw_sha256)
        or projection_revision
        != AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
    ):
        raise ValueError("agent_transient_skill_result_stage_identity_invalid")
    digest = hashlib.sha256(
        b"maf.agent.transient_skill_result_stage.v1\0"
        + call_item_id.encode("utf-8")
        + b"\0"
        + raw_sha256.encode("ascii")
        + b"\0"
        + projection_revision.encode("utf-8")
    ).hexdigest()
    return f"{_STAGE_REF_PREFIX}{digest}"


def _load_exact_manifest(
    path: Path,
    *,
    stable_manifest: Mapping[str, object],
) -> dict[str, object]:
    value = _read_private_json(path)
    if (
        set(value) != {*stable_manifest, "staged_at"}
        or any(value.get(key) != expected for key, expected in stable_manifest.items())
        or not isinstance(value.get("staged_at"), str)
        or not value["staged_at"]
    ):
        raise ValueError("agent_transient_skill_result_stage_conflict")
    return value


def _load_manifest(path: Path) -> dict[str, object]:
    value = _read_private_json(path)
    exact_keys = {
        "schema",
        "source_kind",
        "stage_ref",
        "run_id",
        "task_id",
        "conversation_id",
        "node_id",
        "call_item_id",
        "result_item_id",
        "capability_id",
        "raw_size_bytes",
        "raw_sha256",
        "projection_revision",
        "staged_at",
    }
    string_keys = exact_keys - {"raw_size_bytes"}
    if (
        set(value) != exact_keys
        or value.get("schema")
        != AGENT_TRANSIENT_SKILL_RESULT_MANIFEST_SCHEMA
        or value.get("source_kind")
        != AGENT_TRANSIENT_SKILL_RESULT_SOURCE_KIND
        or value.get("projection_revision")
        != AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
        or any(
            not isinstance(value.get(key), str) or not value[key]
            for key in string_keys
        )
        or isinstance(value.get("raw_size_bytes"), bool)
        or not isinstance(value.get("raw_size_bytes"), int)
        or int(value["raw_size_bytes"]) <= 0
        or not _is_sha256(value.get("raw_sha256"))
    ):
        raise ValueError("agent_transient_skill_result_stage_invalid")
    _stage_ref_digest(str(value["stage_ref"]))
    return value


def _read_private_json(path: Path) -> dict[str, object]:
    before = path.stat(follow_symlinks=False)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        body = handle.read()
        after = os.fstat(handle.fileno())
    after_path = path.stat(follow_symlinks=False)
    identities = {
        (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_uid,
            item.st_nlink,
            item.st_size,
        )
        for item in (before, opened, after, after_path)
    }
    if (
        len(identities) != 1
        or path.is_symlink()
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink != 1
        or opened.st_size != len(body)
    ):
        raise ValueError("agent_transient_skill_result_stage_invalid")
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("agent_transient_skill_result_stage_invalid")
    return value


def _stage_ref_digest(stage_ref: str) -> str:
    if not isinstance(stage_ref, str) or not stage_ref.startswith(
        _STAGE_REF_PREFIX
    ):
        raise ValueError("agent_transient_skill_result_stage_identity_invalid")
    digest = stage_ref.removeprefix(_STAGE_REF_PREFIX)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("agent_transient_skill_result_stage_identity_invalid")
    return digest


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    resolved = path.resolve(strict=True)
    resolved_metadata = resolved.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (metadata.st_dev, metadata.st_ino)
        != (resolved_metadata.st_dev, resolved_metadata.st_ino)
    ):
        raise ValueError("agent_transient_skill_result_stage_invalid")


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
