from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from src.core.enums import ConversationStatus, NodeStatus, TaskStatus
from src.core.models import Conversation, ConversationFileResource, Interrupt, Task, TaskNode
from src.orchestration.visible_message_history import persist_interrupt_question_message
from src.storage.conversation_files import build_file_upload_message_projection

from .upload_store import UploadedFileRecord, UploadValidationError


@dataclass(slots=True, frozen=True)
class SubmissionUploadReference:
    upload_id: str
    conversation_id: str
    sha256: str
    size_bytes: int
    selected_sheet: str | None

    def to_continuation_dict(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "conversation_id": self.conversation_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "selected_sheet": self.selected_sheet,
        }


@dataclass(slots=True, frozen=True)
class SubmissionUploadResolution:
    upload_refs: tuple[SubmissionUploadReference, ...]
    uploaded_artifacts: tuple[Mapping[str, Any], ...]
    skill_artifacts: tuple[Mapping[str, Any], ...]
    missing_upload_ids: tuple[str, ...]
    pending_sheet_selections: tuple[Mapping[str, Any], ...]

    def continuation_upload_refs(self) -> list[dict[str, Any]]:
        refs_by_upload_id = {ref.upload_id: ref for ref in self.upload_refs}
        return [
            refs_by_upload_id[upload_id].to_continuation_dict()
            for upload_id in sorted(refs_by_upload_id)
        ]

    def to_upload_context(self) -> dict[str, Any]:
        return {
            "uploaded_artifacts": [dict(item) for item in self.uploaded_artifacts],
            "skill_artifacts": [dict(item) for item in self.skill_artifacts],
            "missing_upload_ids": list(self.missing_upload_ids),
            "pending_sheet_selections": [dict(item) for item in self.pending_sheet_selections],
        }


class ConversationUploadRuntimeMixin:
    def _read_conversation_file_resource_bytes_exact(
        self, resource: ConversationFileResource
    ) -> bytes:
        content = self.conversation_file_store.read_bytes(resource.storage_key)
        if (
            len(content) != resource.size_bytes
            or hashlib.sha256(content).hexdigest() != resource.sha256
        ):
            raise UploadValidationError("conversation_upload_blob_drift")
        return content

    async def save_upload(
        self,
        *,
        conversation_id: str,
        username: str,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> UploadedFileRecord:
        existing_conversation = await self.ensure_upload_allowed(conversation_id, username)
        if existing_conversation is not None:
            await self._repair_conversation_file_index_if_due(conversation_id, username)
        now = self._utcnow_naive()
        try:
            self.conversation_file_store.conversation_dir(conversation_id)
        except ValueError as exc:
            raise UploadValidationError("conversation_id failed file storage safety validation") from exc
        record = self.upload_store.save(
            username=username,
            conversation_id=conversation_id,
            filename=filename,
            content_type=content_type,
            content=content,
        )
        try:
            stored = self.conversation_file_store.save_original(
                conversation_id=conversation_id,
                upload_id=record.upload_id,
                content=record.content_bytes,
            )
            description_status, description_summary, description_ref = self._initial_file_description(record)
        except Exception as exc:
            await self._delete_request_upload_artifacts(
                conversation_id=conversation_id,
                username=username,
                upload_id=record.upload_id,
            )
            raise UploadValidationError("Uploaded file failed file storage safety validation") from exc
        resource = ConversationFileResource(
            file_id=record.upload_id,
            conversation_id=conversation_id,
            username=username,
            original_filename=record.filename,
            content_type=record.content_type,
            file_type=record.file_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            storage_key=stored.storage_key,
            preview=dict(record.preview),
            description_status=description_status,
            description_summary=description_summary,
            description_ref=description_ref,
            status="active",
            normalized_filename=record.normalized_filename,
            normalized_content_type=record.normalized_content_type,
            requires_sheet_selection=record.requires_sheet_selection,
            selected_sheet=record.selected_sheet,
            created_at=record.created_at,
            updated_at=now,
        )
        conversation_created = False
        try:
            if existing_conversation is None:
                await self.storage.save_conversation(
                    Conversation(
                        conversation_id=conversation_id,
                        username=username,
                        created_at=now,
                        updated_at=now,
                    )
                )
                conversation_created = True
            await self.storage.save_conversation_file_resource_with_upload_message(
                resource,
                build_file_upload_message_projection(resource),
                now=now,
            )
        except Exception as exc:
            await self._delete_request_upload_artifacts(
                conversation_id=conversation_id,
                username=username,
                upload_id=record.upload_id,
            )
            if conversation_created:
                await self.storage.delete_conversation_physical(conversation_id)
            raise UploadValidationError("Uploaded file could not be persisted") from exc
        try:
            index_ok = await self._rewrite_conversation_file_index_with_repair(
                conversation_id,
                username,
                affected_upload_ids=(record.upload_id,),
                reason_code="upload_index_write_failed",
            )
        except Exception as exc:
            try:
                await self._compensate_failed_upload_after_index_error(
                    conversation_id=conversation_id,
                    username=username,
                    upload_id=record.upload_id,
                    reason_code="index_write_failed_marker_failed",
                    now=self._utcnow_naive(),
                )
            finally:
                await self._delete_request_upload_artifacts(
                    conversation_id=conversation_id,
                    username=username,
                    upload_id=record.upload_id,
                )
            raise UploadValidationError("Uploaded file could not be indexed") from exc
        if not index_ok:
            try:
                await self._compensate_failed_upload_after_index_error(
                    conversation_id=conversation_id,
                    username=username,
                    upload_id=record.upload_id,
                    reason_code="index_write_failed",
                    now=self._utcnow_naive(),
                )
            finally:
                await self._delete_request_upload_artifacts(
                    conversation_id=conversation_id,
                    username=username,
                    upload_id=record.upload_id,
                )
            raise UploadValidationError("Uploaded file could not be indexed")
        if self._audit_sink is not None:
            await self._audit_sink.record(
                "conversation_file.upload_persisted",
                {
                    "upload_id": record.upload_id,
                    "file_type": record.file_type,
                    "size_bytes": record.size_bytes,
                    "description_status": description_status,
                },
                conversation_id=conversation_id,
            )
        return record

    async def ensure_upload_allowed(self, conversation_id: str, username: str) -> Conversation | None:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        return existing_conversation

    async def list_uploads(
        self,
        conversation_id: str,
        username: str,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[UploadedFileRecord]:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        if existing_conversation is not None:
            await self._repair_conversation_file_index_if_due(conversation_id, username)
        resources = await self.storage.list_conversation_file_resources(
            conversation_id,
            username,
            include_deleted=include_deleted,
            limit=limit,
            cursor=cursor,
        )
        return [self._upload_record_from_resource(resource, content_bytes=b"") for resource in resources]

    async def delete_upload(self, conversation_id: str, username: str, upload_id: str) -> bool:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        if existing_conversation is not None:
            await self._repair_conversation_file_index_if_due(conversation_id, username)
        deleted = await self.storage.mark_conversation_file_resource_and_upload_message_deleted(
            conversation_id,
            username,
            upload_id,
            updated_at=self._utcnow_naive(),
        )
        if deleted is None:
            return False
        local_files_deleted = True
        try:
            self.conversation_file_store.delete_resource_dir(
                conversation_id=deleted.conversation_id,
                upload_id=deleted.file_id,
            )
        except Exception as exc:
            local_files_deleted = False
            if self._audit_sink is not None:
                await self._audit_sink.record(
                    "conversation_file.upload_directory_cleanup_failed",
                    {"upload_id": upload_id, "error_type": exc.__class__.__name__},
                    conversation_id=conversation_id,
                )
        try:
            await self._rewrite_conversation_file_index_with_repair(
                conversation_id,
                username,
                affected_upload_ids=(upload_id,),
                reason_code="delete_index_write_failed",
            )
        except Exception as exc:
            if self._audit_sink is not None:
                await self._audit_sink.record(
                    "conversation_file.delete_index_repair_marker_failed",
                    {"upload_id": upload_id, "error_type": exc.__class__.__name__},
                    conversation_id=conversation_id,
                )
            raise UploadValidationError("Deleted file could not be indexed") from exc
        if self._audit_sink is not None:
            await self._audit_sink.record(
                "conversation_file.delete_marked",
                {"upload_id": upload_id, "local_files_deleted": local_files_deleted},
                conversation_id=conversation_id,
            )
        return True

    async def resolve_uploads_for_message(
        self,
        conversation_id: str,
        username: str,
        upload_ids,
        *,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = await self._resolve_uploads(
            conversation_id,
            username,
            upload_ids,
            upload_sheet_selections=upload_sheet_selections,
            repair_index=True,
            persist_sheet_selection=True,
        )
        return resolved.to_upload_context()

    async def resolve_uploads_for_submission(
        self,
        conversation_id: str,
        username: str,
        upload_ids,
        *,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> SubmissionUploadResolution:
        return await self._resolve_uploads(
            conversation_id,
            username,
            upload_ids,
            upload_sheet_selections=upload_sheet_selections,
            repair_index=False,
            persist_sheet_selection=False,
        )

    async def _resolve_uploads(
        self,
        conversation_id: str,
        username: str,
        upload_ids,
        *,
        upload_sheet_selections: Mapping[str, Any] | None,
        repair_index: bool,
        persist_sheet_selection: bool,
    ) -> SubmissionUploadResolution:
        if upload_ids is None:
            upload_ids = ()
        if isinstance(upload_ids, str):
            upload_ids = [upload_ids]
        if not isinstance(upload_ids, list | tuple):
            raise UploadValidationError("metadata.upload_ids must be a list")
        if repair_index:
            await self._repair_conversation_file_index_if_due(conversation_id, username)
        sheet_selections = self._normalize_upload_sheet_selections(upload_sheet_selections)
        upload_refs: list[SubmissionUploadReference] = []
        uploaded_artifacts: list[dict[str, Any]] = []
        skill_artifacts: list[dict[str, Any]] = []
        pending_sheet_selections: list[dict[str, Any]] = []
        missing_upload_ids: list[str] = []
        for upload_id in upload_ids:
            upload_id_text = str(upload_id).strip()
            if not upload_id_text:
                continue
            resource = await self.storage.get_conversation_file_resource(conversation_id, username, upload_id_text)
            if resource is None:
                existing_resource = await self.storage.get_conversation_file_resource_by_id(upload_id_text)
                if existing_resource is not None:
                    raise PermissionError(f"Upload does not belong to conversation: {upload_id_text}")
                missing_upload_ids.append(upload_id_text)
                continue
            if resource.status == "deleted":
                missing_upload_ids.append(upload_id_text)
                continue
            selected_sheet = sheet_selections.get(upload_id_text)
            if selected_sheet:
                if persist_sheet_selection:
                    resource = await self._apply_conversation_file_sheet_selection(resource, selected_sheet)
                elif resource.file_type != "spreadsheet":
                    raise UploadValidationError(
                        f"Sheet selection is only supported for spreadsheet uploads: {resource.file_id}"
                    )
            selected_sheet = selected_sheet or resource.selected_sheet
            record = self._upload_record_from_resource(
                resource,
                content_bytes=self._read_conversation_file_resource_bytes_exact(
                    resource
                ),
                selected_sheet=selected_sheet,
            )
            upload_refs.append(
                SubmissionUploadReference(
                    upload_id=record.upload_id,
                    conversation_id=record.conversation_id,
                    sha256=record.sha256,
                    size_bytes=record.size_bytes,
                    selected_sheet=record.selected_sheet,
                )
            )
            uploaded_artifacts.append(record.to_summary())
            if record.requires_sheet_selection and not selected_sheet:
                pending_sheet_selections.append(record.sheet_selection_payload())
                skill_artifacts.append(record.to_summary())
                continue
            skill_artifact = record.to_skill_artifact(selected_sheet=selected_sheet)
            skill_artifact["storage_key"] = resource.storage_key
            skill_artifact["conversation_id"] = resource.conversation_id
            skill_artifacts.append(skill_artifact)
        return SubmissionUploadResolution(
            upload_refs=tuple(upload_refs),
            uploaded_artifacts=tuple(uploaded_artifacts),
            skill_artifacts=tuple(skill_artifacts),
            missing_upload_ids=tuple(missing_upload_ids),
            pending_sheet_selections=tuple(pending_sheet_selections),
        )

    async def resolve_conversation_uploads_for_message(
        self,
        conversation_id: str,
        username: str,
        *,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._repair_conversation_file_index_if_due(conversation_id, username)
        resources = await self.storage.list_conversation_file_resources(
            conversation_id,
            username,
            include_deleted=False,
        )
        upload_ids = [resource.file_id for resource in resources if resource.status != "deleted"]
        return await self.resolve_uploads_for_message(
            conversation_id,
            username,
            upload_ids,
            upload_sheet_selections=upload_sheet_selections,
        )

    async def resolve_conversation_uploads_for_submission(
        self,
        conversation_id: str,
        username: str,
        *,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> SubmissionUploadResolution:
        resources = await self.storage.list_conversation_file_resources(
            conversation_id,
            username,
            include_deleted=False,
        )
        upload_ids = [resource.file_id for resource in resources if resource.status != "deleted"]
        return await self.resolve_uploads_for_submission(
            conversation_id,
            username,
            upload_ids,
            upload_sheet_selections=upload_sheet_selections,
        )

    @staticmethod
    def _upload_context_metadata(upload_context: Mapping[str, Any]) -> dict[str, Any]:
        uploaded_artifacts = [
            dict(item)
            for item in upload_context.get("uploaded_artifacts", [])
            if isinstance(item, Mapping)
        ]
        skill_artifacts = [
            dict(item)
            for item in upload_context.get("skill_artifacts", [])
            if isinstance(item, Mapping)
        ]
        metadata: dict[str, Any] = {}
        if uploaded_artifacts:
            metadata["uploaded_artifacts"] = uploaded_artifacts
        if skill_artifacts:
            metadata["skill_artifacts"] = skill_artifacts
        return metadata

    async def _conversation_file_context_metadata_for_task(
        self,
        task: Task,
        *,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        conversation = await self.storage.get_conversation(task.conversation_id)
        if conversation is None:
            return {}
        upload_context = await self.resolve_conversation_uploads_for_message(
            task.conversation_id,
            conversation.username,
            upload_sheet_selections=upload_sheet_selections,
        )
        return self._upload_context_metadata(upload_context)

    @staticmethod
    def _raise_missing_uploads(missing_upload_ids: object, *, context: str) -> None:
        if not isinstance(missing_upload_ids, list | tuple) or not missing_upload_ids:
            return
        missing = [str(upload_id).strip() for upload_id in missing_upload_ids if str(upload_id).strip()]
        if not missing:
            return
        raise UploadValidationError(f"Missing or expired uploads required for {context}: {', '.join(missing)}")

    @staticmethod
    def _normalize_upload_sheet_selections(raw: Any) -> dict[str, str]:
        if raw in (None, ""):
            return {}
        if not isinstance(raw, Mapping):
            raise UploadValidationError("upload_sheet_selections must be an object")
        selections: dict[str, str] = {}
        for key, value in raw.items():
            upload_id = str(key).strip()
            sheet_name = str(value).strip()
            if upload_id and sheet_name:
                selections[upload_id] = sheet_name
        return selections

    async def _open_sheet_selection_interrupt(
        self,
        *,
        task: Task,
        metadata: Mapping[str, Any],
        pending_sheet_selections: list[dict[str, Any]],
    ) -> None:
        now = self._utcnow_naive()
        node_id = f"{task.task_id}:sheet_selection"
        node = TaskNode(
            node_id=node_id,
            task_id=task.task_id,
            capability_id=task.requested_capability_id or "agent.sheet_selection",
            status=NodeStatus.RUNNING,
            started_at=now,
        )
        await self.storage.save_task_node(node)
        await self.storage.save_task(
            replace(
                task,
                status=TaskStatus.RUNNING,
                updated_at=now,
            )
        )
        required_upload_ids: list[str] = []
        options_by_upload_id: dict[str, list[str]] = {}
        labels_by_upload_id: dict[str, str] = {}
        details_by_upload_id: dict[str, Any] = {}
        for pending in pending_sheet_selections:
            required_upload_ids.extend(str(item) for item in pending.get("required_upload_ids", []) if str(item).strip())
            options_by_upload_id.update(
                {
                    str(upload_id): [str(option) for option in options]
                    for upload_id, options in dict(pending.get("options_by_upload_id", {})).items()
                    if isinstance(options, list | tuple)
                }
            )
            labels_by_upload_id.update({str(key): str(value) for key, value in dict(pending.get("labels_by_upload_id", {})).items()})
            details_by_upload_id.update(dict(pending.get("details_by_upload_id", {})))
        required_upload_ids = list(dict.fromkeys(required_upload_ids))
        required_fields = {
            "upload_sheet_selections": {
                "type": "sheet_selection",
                "description": "请选择每个 Excel 文件要用于执行的 sheet。",
                "required_upload_ids": required_upload_ids,
                "options_by_upload_id": options_by_upload_id,
                "labels_by_upload_id": labels_by_upload_id,
                "details_by_upload_id": details_by_upload_id,
            }
        }
        interrupt = Interrupt(
            interrupt_id=f"{node_id}:interrupt:sheet_selection_required",
            conversation_id=task.conversation_id,
            task_id=task.task_id,
            node_id=node_id,
            source_agent=task.requested_capability_id or "agent.sheet_selection",
            source_message_id=task.root_message_id,
            question=self._sheet_selection_question(labels_by_upload_id, options_by_upload_id),
            reason_code="sheet_selection_required",
            required_fields=required_fields,
        )
        self._task_sheet_selection_resume_metadata[task.task_id] = dict(metadata)
        saved_interrupt = await self.interrupt_service.open_interrupt(interrupt, now=now)
        await persist_interrupt_question_message(self.storage, saved_interrupt, created_at=now)
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=node_id,
                event_type="node.waiting_for_input",
                payload={
                    "reason": saved_interrupt.reason_code,
                    "reason_code": saved_interrupt.reason_code,
                    "interrupt_id": saved_interrupt.interrupt_id,
                    "required_upload_ids": required_upload_ids,
                },
                created_at=now,
            )
        )

    @staticmethod
    def _sheet_selection_question(labels_by_upload_id: Mapping[str, str], options_by_upload_id: Mapping[str, list[str]]) -> str:
        parts = []
        for upload_id, label in labels_by_upload_id.items():
            options = "、".join(options_by_upload_id.get(upload_id, ()))
            parts.append(f"{label}（可选 sheet：{options}）")
        detail = "；".join(parts) if parts else "上传的 Excel 文件"
        return f"检测到多 sheet Excel：{detail}。请为每个文件选择一个 sheet 后继续。"
