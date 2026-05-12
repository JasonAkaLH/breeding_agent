from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.core.contracts import StoragePort
from src.core.enums import ArtifactType
from src.core.models import Artifact
from src.integrations.codex_skills.internal_keys import (
    SKILL_OUTPUT_ARTIFACT_INTERNAL_KEY,
    SKILL_OUTPUT_REJECTIONS_INTERNAL_KEY,
)
from src.integrations.codex_skills.manifest import SkillManifest
from src.integrations.codex_skills.output_files import (
    CollectedSkillOutputFile,
    SkillOutputFileRejection,
    collect_skill_output_files,
    create_zip_from_collected_files,
)
from src.integrations.codex_skills.script_manifest import SkillScriptEntrypoint
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    build_file_storage_ref,
    is_active_skill_output_file,
    parse_file_storage_ref,
    sanitize_download_filename,
)



@dataclass(slots=True, frozen=True)
class SkillOutputArtifactProcessingResult:
    output: dict[str, Any]
    artifact: Artifact | None
    rejections: tuple[SkillOutputFileRejection, ...]


class SkillOutputArtifactManager:
    def __init__(
        self,
        *,
        storage: StoragePort,
        file_store: LocalArtifactFileStore,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._file_store = file_store
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    async def process_for_runner(self, **kwargs: Any) -> dict[str, Any]:
        original_output = dict(kwargs.get("output") or {})
        try:
            result = await self.process_script_output(**kwargs)
        except Exception as exc:
            sanitized = dict(original_output)
            sanitized.pop("output_files", None)
            rejection = SkillOutputFileRejection(
                path="",
                reason="output_processing_failed",
                message=exc.__class__.__name__,
            )
            sanitized["output_file_diagnostics"] = [
                {"path": rejection.path, "reason": rejection.reason, "message": rejection.message}
            ]
            sanitized[SKILL_OUTPUT_REJECTIONS_INTERNAL_KEY] = (rejection,)
            return sanitized
        output = dict(result.output)
        if result.artifact is not None:
            output[SKILL_OUTPUT_ARTIFACT_INTERNAL_KEY] = result.artifact
        if result.rejections:
            output[SKILL_OUTPUT_REJECTIONS_INTERNAL_KEY] = result.rejections
        return output

    def discard_unsaved_artifact(self, artifact: Artifact) -> SkillOutputFileRejection | None:
        metadata = parse_file_storage_ref(artifact.storage_ref)
        if is_active_skill_output_file(metadata):
            try:
                self._file_store.delete(str(metadata.get("storage_key")))
            except Exception as exc:
                return SkillOutputFileRejection(
                    path=str(metadata.get("filename") or ""),
                    reason="pending_artifact_cleanup_failed",
                    message=exc.__class__.__name__,
                )
        return None

    async def process_script_output(
        self,
        *,
        output: Mapping[str, Any],
        outputs_dir: str | Path,
        manifest: SkillManifest,
        script: SkillScriptEntrypoint,
        context: Mapping[str, Any],
    ) -> SkillOutputArtifactProcessingResult:
        collection = collect_skill_output_files(output, outputs_dir, manifest=manifest)
        sanitized = dict(output)
        sanitized.pop("output_files", None)
        if collection.rejections:
            sanitized["output_file_diagnostics"] = [
                {"path": item.path, "reason": item.reason, "message": item.message}
                for item in collection.rejections
            ]
        if not collection.files:
            return SkillOutputArtifactProcessingResult(output=sanitized, artifact=None, rejections=collection.rejections)

        task_id = str(context.get("task_id") or "").strip()
        node_id = str(context.get("node_id") or "").strip()
        conversation_id = str(context.get("conversation_id") or "").strip()
        if not task_id or not node_id or not conversation_id:
            sanitized.setdefault("output_file_diagnostics", []).append(
                {"path": "", "reason": "missing_output_context", "message": "missing task/node/conversation context"}
            )
            return SkillOutputArtifactProcessingResult(output=sanitized, artifact=None, rejections=collection.rejections)

        artifact_id = f"{node_id}:skill_output:{uuid4().hex[:12]}"
        persisted_source, filename, mime_type, source_count, archive_format, source_names = self._prepare_persisted_source(
            skill_name=manifest.name,
            files=collection.files,
        )
        try:
            stored = self._file_store.save_file(artifact_id=artifact_id, filename=filename, source_path=persisted_source)
        finally:
            if archive_format == "zip":
                try:
                    persisted_source.unlink(missing_ok=True)
                    persisted_source.parent.rmdir()
                except OSError:
                    pass
        summary = self._build_summary(files=collection.files, filename=stored.filename, archive_format=archive_format)
        metadata = {
            "version": 1,
            "source_kind": "skill_output",
            "storage_key": stored.storage_key,
            "filename": stored.filename,
            "mime_type": mime_type,
            "size_bytes": stored.size_bytes,
            "sha256": stored.sha256,
            "summary": summary,
            "skill_name": manifest.name,
            "entrypoint": script.name,
            "conversation_id": conversation_id,
            "source_file_count": source_count,
            "source_filenames": source_names,
            "archive_format": archive_format,
            "retention_status": "active",
        }
        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task_id,
            producer_node_id=node_id,
            artifact_type=ArtifactType.FILE,
            storage_ref=build_file_storage_ref(metadata),
            summary=summary,
            is_complete=True,
            created_at=self._now_fn(),
        )
        artifact_saved = False
        try:
            await self._storage.save_artifact(artifact)
            artifact_saved = True
            await self._evict_active_outputs(conversation_id=conversation_id, superseded_by_artifact_id=artifact_id)
        except Exception:
            try:
                self._file_store.delete(stored.storage_key)
            except Exception:
                pass
            if artifact_saved:
                await self._mark_artifact_deleted(artifact=artifact, metadata=metadata, reason="output_processing_failed")
            raise
        sanitized["output_files"] = [self._public_descriptor(artifact_id=artifact_id, metadata=metadata)]
        return SkillOutputArtifactProcessingResult(output=sanitized, artifact=artifact, rejections=collection.rejections)

    def _prepare_persisted_source(
        self,
        *,
        skill_name: str,
        files: tuple[CollectedSkillOutputFile, ...],
    ) -> tuple[Path, str, str, int, str | None, list[str]]:
        if len(files) == 1:
            file = files[0]
            return file.source_path, file.filename, file.mime_type, 1, None, [file.filename]
        zip_filename = sanitize_download_filename(f"{skill_name}_outputs.zip")
        temp_dir = Path(tempfile.mkdtemp(prefix="skill-output-zip-"))
        zip_path = temp_dir / zip_filename
        create_zip_from_collected_files(files, zip_path)
        return zip_path, zip_filename, "application/zip", len(files), "zip", [file.filename for file in files]

    async def _evict_active_outputs(self, *, conversation_id: str, superseded_by_artifact_id: str) -> None:
        tasks = await self._storage.list_tasks_for_conversation(conversation_id)
        for task in tasks:
            for artifact in await self._storage.list_artifacts_for_task(task.task_id):
                if artifact.artifact_id == superseded_by_artifact_id:
                    continue
                if artifact.artifact_type != ArtifactType.FILE:
                    continue
                metadata = parse_file_storage_ref(artifact.storage_ref)
                if not is_active_skill_output_file(metadata):
                    continue
                storage_key = str(metadata.get("storage_key"))
                updated_metadata = dict(metadata)
                updated_metadata["retention_status"] = "superseded"
                updated_metadata["superseded_by_artifact_id"] = superseded_by_artifact_id
                updated_metadata.pop("download_url", None)
                updated_artifact = self._artifact_with_metadata(artifact, updated_metadata)
                await self._storage.save_artifact(updated_artifact)
                try:
                    self._file_store.delete(storage_key)
                except Exception as exc:
                    updated_metadata["file_delete_status"] = "failed"
                    updated_metadata["file_delete_error"] = exc.__class__.__name__
                    try:
                        await self._storage.save_artifact(self._artifact_with_metadata(artifact, updated_metadata))
                    except Exception:
                        pass

    async def _mark_artifact_deleted(self, *, artifact: Artifact, metadata: Mapping[str, Any], reason: str) -> None:
        deleted_metadata = dict(metadata)
        deleted_metadata["retention_status"] = "deleted"
        deleted_metadata["delete_reason"] = reason
        try:
            await self._storage.save_artifact(self._artifact_with_metadata(artifact, deleted_metadata))
        except Exception:
            pass

    @staticmethod
    def _artifact_with_metadata(artifact: Artifact, metadata: Mapping[str, Any]) -> Artifact:
        return Artifact(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            producer_node_id=artifact.producer_node_id,
            artifact_type=artifact.artifact_type,
            storage_ref=build_file_storage_ref(metadata),
            summary=artifact.summary,
            is_complete=artifact.is_complete,
            created_at=artifact.created_at,
        )

    @staticmethod
    def _build_summary(*, files: tuple[CollectedSkillOutputFile, ...], filename: str, archive_format: str | None) -> str:
        explicit = next((file.summary for file in files if file.summary), None)
        if explicit and len(files) == 1:
            return explicit[:200]
        if archive_format == "zip":
            names = "、".join(file.filename for file in files[:3])
            suffix = "等" if len(files) > 3 else ""
            return f"已生成 {len(files)} 个文件并打包为 {filename}（包含 {names}{suffix}）。"
        label = files[0].label or filename
        return f"已生成文件：{label}。"

    @staticmethod
    def _public_descriptor(*, artifact_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "artifact_id": artifact_id,
            "filename": metadata.get("filename"),
            "mime_type": metadata.get("mime_type"),
            "size_bytes": metadata.get("size_bytes"),
            "sha256": metadata.get("sha256"),
            "summary": metadata.get("summary"),
            "download_url": f"/api/v1/artifacts/{artifact_id}/download",
            "source_file_count": metadata.get("source_file_count"),
            "archive_format": metadata.get("archive_format"),
        }


def file_artifact_public_metadata(artifact: Artifact) -> dict[str, Any] | None:
    if artifact.artifact_type != ArtifactType.FILE:
        return None
    metadata = parse_file_storage_ref(artifact.storage_ref)
    if metadata is None:
        return None
    if metadata.get("source_kind") != "skill_output":
        return None
    return metadata
