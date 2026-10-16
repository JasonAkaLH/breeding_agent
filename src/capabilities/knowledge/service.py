from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from src.auth.services import normalize_username

from .access import AccessSubjectResolver, KnowledgeError, LibraryAccessService
from .extraction import LiteratureMetadata, decode_markdown, validate_metadata

logger = logging.getLogger(__name__)
_PUBLIC_FIELDS = frozenset({
    "library_id", "document_id", "import_id", "grant_id", "owner_user_id", "name", "description",
    "role", "status", "revision", "index_revision", "published_index_revision", "publication_state",
    "filename", "sha256", "size_bytes", "replacement_document_id", "target_revision", "extraction_model",
    "error_code", "error", "created_at", "updated_at", "subject_type", "subject_id", "cleanup_pending", "model_edition",
    *LiteratureMetadata.model_fields,
})


def _public(record: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in record.items() if key in _PUBLIC_FIELDS}
    if "owner_user_id" in result:
        result["owner_username"] = result["owner_user_id"]
    if "subject_type" in result and "subject_id" in result:
        result["grant_id"] = f"{result['subject_type']}:{result['subject_id']}"
    if "model_edition" in result:
        result["extraction_model"] = result["model_edition"]
    if result.get("error"):
        result["error_code"] = result["error"]
    return result


class KnowledgeService:
    """Owner management and live authorized reads over the shared knowledge repository."""

    def __init__(
        self,
        repository: Any,
        files: Any,
        *,
        extractor: Callable[..., Any] | None = None,
        user_exists: Callable[[str], Any] | None = None,
        subject_resolver: AccessSubjectResolver | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.repository = repository
        self.files = files
        self.access = LibraryAccessService(repository, subject_resolver)
        self.extractor = extractor
        self.user_exists = user_exists
        self.poll_interval = poll_interval
        self.worker_id = f"knowledge-{uuid4().hex}"
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    async def _run(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(operation, *args, **kwargs)
        except KnowledgeError:
            raise
        except Exception as exc:
            name = type(exc).__name__
            if name == "KnowledgeConflict":
                raise KnowledgeError(getattr(exc, "code", "knowledge_conflict"), status_code=409) from exc
            if name == "KnowledgeNotFound":
                raise KnowledgeError(getattr(exc, "code", "knowledge_unavailable"), status_code=404) from exc
            if isinstance(exc, OSError):
                raise KnowledgeError("knowledge_file_unavailable", status_code=503) from exc
            if isinstance(exc, ValueError):
                raise KnowledgeError("invalid_knowledge_input") from exc
            raise

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop(), name="knowledge-processing")

    async def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def _worker_loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.process_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Provider/file exceptions can include reference text, so log only the event.
                logger.warning("knowledge_processing_scan_failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass

    async def create_library(self, actor: str, name: str, description: str = "") -> dict[str, Any]:
        if not name.strip():
            raise KnowledgeError("library_name_required")
        record = await self._run(self.repository.create_library, normalize_username(actor), name.strip(), description)
        await self._run(self._ensure_index, record["library_id"])
        return await self.get_library(actor, record["library_id"])

    async def list_libraries(self, actor: str) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            accessible = self.access.list_accessible_libraries(actor)
            accessible.extend(
                {**item, "role": "owner"}
                for item in self.repository.list_libraries(normalize_username(actor), include_deleted=True)
                if item["status"] == "deleted" and item.get("cleanup_pending")
            )
            return [self._library_public(item) for item in accessible]
        return await self._run(load)

    def _library_public(self, library: Mapping[str, Any]) -> dict[str, Any]:
        return {**_public(library), "document_count": len(self.repository.list_documents(library["library_id"])),
                "index_state": "ready" if library["index_revision"] == library["published_index_revision"] else "pending"}

    async def get_library(self, actor: str, library_id: str) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            library = self.repository.get_library(library_id)
            if library and library["status"] == "deleted" and library["owner_user_id"] == normalize_username(actor):
                return self._library_public({**library, "role": "owner"})
            return self._library_public(self.access.require_permission(actor, library_id))
        return await self._run(load)

    async def update_library(
        self, actor: str, library_id: str, *, expected_revision: int,
        name: str | None = None, description: str | None = None,
    ) -> dict[str, Any]:
        def update() -> dict[str, Any]:
            self.access.require_permission(actor, library_id, "write")
            changes: dict[str, Any] = {}
            if name is not None:
                if not name.strip():
                    raise KnowledgeError("library_name_required")
                changes["name"] = name.strip()
            if description is not None:
                changes["description"] = description
            self.repository.update_library(library_id, expected_revision, **changes)
            self._ensure_index(library_id)
            return self._library_public(self.access.require_permission(actor, library_id))
        return await self._run(update)

    def _import(self, actor: str, library_id: str, import_id: str) -> dict[str, Any]:
        self.access.require_permission(actor, library_id, "write")
        item = self.repository.get_import(import_id)
        if not item or item["library_id"] != library_id:
            raise KnowledgeError("import_unavailable", status_code=404)
        return item

    def _document(self, actor: str, library_id: str, document_id: str) -> dict[str, Any]:
        self.access.require_permission(actor, library_id)
        item = self.repository.get_document(document_id)
        if (not item or item["library_id"] != library_id or item["status"] != "active"
                or item.get("publication_state", "complete") != "complete"):
            raise KnowledgeError("document_unavailable", status_code=404)
        return item

    async def upload_imports(
        self, actor: str, library_id: str, uploads: Sequence[tuple[str, bytes]], *,
        replacement_document_id: str | None = None,
    ) -> list[dict[str, Any]]:
        await self._run(self.access.require_permission, actor, library_id, "write")
        started = time.perf_counter()
        results = []
        for filename, raw in uploads:
            try:
                result = await self._run(self._upload, actor, library_id, filename, raw, replacement_document_id)
                results.append(result)
            except KnowledgeError as exc:
                results.append({"filename": filename, "status": "failed", "error": exc.code})
        self._wake.set()
        logger.info("knowledge_upload_batch_completed", extra={"library_id": library_id, "count": len(results), "status": "processed", "duration_ms": round((time.perf_counter() - started) * 1000)})
        return results

    def _upload(
        self, actor: str, library_id: str, filename: str, raw: bytes, replacement_document_id: str | None,
    ) -> dict[str, Any]:
        self.access.require_permission(actor, library_id, "write")
        if not filename.lower().endswith((".md", ".markdown")):
            raise KnowledgeError("markdown_file_required")
        decode_markdown(raw)
        sha = hashlib.sha256(raw).hexdigest()
        target_revision = None
        if replacement_document_id:
            target = self._document(actor, library_id, replacement_document_id)
            if target["sha256"] == sha:
                return {**_public(target), "duplicate_type": "document", "unchanged": True}
            target_revision = target["revision"]
        import_id = uuid4().hex
        body_ref = self.files.build_reference(library_id, import_id, sha)
        item = self.repository.register_import(
            library_id, sha, filename, body_ref, replacement_document_id=replacement_document_id,
            target_revision=target_revision, import_id=import_id, status="uploading",
        )
        if item.get("duplicate_type"):
            return {**_public(item), "duplicate_type": item["duplicate_type"]}
        try:
            self.files.put_body(library_id, import_id, raw)
            # Deletion/discard can race disk I/O; a late upload must never resurrect a draft.
            self.access.require_permission(actor, library_id, "write")
            current = self.repository.get_import(import_id)
            if current["status"] != "uploading":
                raise KnowledgeError("import_state_changed", status_code=409)
            item = self.repository.update_import(import_id, current["revision"], status="pending", error=None)
        except Exception:
            current = self.repository.get_import(import_id)
            if current and current["status"] == "uploading":
                self.repository.update_import(import_id, current["revision"], status="failed", error="upload_failed")
            self.files.delete_body(body_ref)
            raise
        return _public(item)

    async def list_imports(self, actor: str, library_id: str) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            self.access.require_permission(actor, library_id, "write")
            return [_public(item) for item in self.repository.list_imports(library_id, include_discarded=True)
                    if item["status"] != "discarded" or item.get("cleanup_pending") or item.get("error")]
        return await self._run(load)

    async def get_import(self, actor: str, library_id: str, import_id: str) -> dict[str, Any]:
        return await self._run(lambda: _public(self._import(actor, library_id, import_id)))

    async def preview_import(self, actor: str, library_id: str, import_id: str) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            item = self._import(actor, library_id, import_id)
            if item["status"] == "discarded":
                raise KnowledgeError("import_unavailable", status_code=404)
            raw = self.files.read_body(item["body_ref"], sha256=item["sha256"])
            self._import(actor, library_id, import_id)
            return {"text": decode_markdown(raw), "sha256": item["sha256"], "filename": item["filename"]}
        return await self._run(read)

    async def edit_import(
        self, actor: str, library_id: str, import_id: str, metadata: Mapping[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        def edit() -> dict[str, Any]:
            item = self._import(actor, library_id, import_id)
            if item["status"] not in {"awaiting_review", "failed", "pending", "processing"}:
                raise KnowledgeError("import_state_changed", status_code=409)
            merged = {key: item.get(key, field.get_default(call_default_factory=True))
                      for key, field in LiteratureMetadata.model_fields.items()}
            merged.update(metadata)
            fields = validate_metadata(merged)
            result = _public(self.repository.update_import(
                import_id, expected_revision, **fields, status="awaiting_review", error=None,
            ))
            logger.info("knowledge_review_saved", extra={"import_id": import_id, "status": result["status"]})
            return result
        return await self._run(edit)

    async def retry_import(
        self, actor: str, library_id: str, import_id: str, *, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        def retry() -> dict[str, Any]:
            item = self._import(actor, library_id, import_id)
            if item["status"] != "failed":
                raise KnowledgeError("import_state_changed", status_code=409)
            self.files.read_body(item["body_ref"], sha256=item["sha256"])
            return _public(self.repository.update_import(
                import_id, expected_revision if expected_revision is not None else item["revision"],
                status="pending", error=None,
            ))
        result = await self._run(retry)
        self._wake.set()
        return result

    async def confirm_import(
        self, actor: str, library_id: str, import_id: str, *, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        def confirm() -> dict[str, Any]:
            item = self._import(actor, library_id, import_id)
            if item["status"] == "published":
                return _public(item)
            if item["status"] == "publishing":
                self._finish_import(item)
                return _public(self.repository.get_import(import_id))
            if item["status"] not in {"awaiting_review", "failed"}:
                raise KnowledgeError("import_not_ready", status_code=409)
            validate_metadata({key: item.get(key) for key in LiteratureMetadata.model_fields}, required=True)
            self.files.read_body(item["body_ref"], sha256=item["sha256"])
            self.repository.publish_import(
                import_id, expected_revision if expected_revision is not None else item["revision"],
            )
            self._finish_import(self.repository.get_import(import_id))
            return _public(self.repository.get_import(import_id))
        result = await self._run(confirm)
        self._wake.set()
        logger.info("knowledge_confirmation_processed", extra={"import_id": import_id, "status": result["status"]})
        return result

    async def batch_confirm(
        self, actor: str, library_id: str, imports: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        await self._run(self.access.require_permission, actor, library_id, "write")
        results = []
        for item in imports:
            import_id = str(item["import_id"])
            try:
                results.append(await self.confirm_import(
                    actor, library_id, import_id, expected_revision=item.get("revision"),
                ))
            except KnowledgeError as exc:
                results.append({"import_id": import_id, "status": "failed", "error": exc.code})
        return results

    async def discard_import(
        self, actor: str, library_id: str, import_id: str, *, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        def discard() -> dict[str, Any]:
            self._import(actor, library_id, import_id)
            item = self.repository.discard_import(import_id, expected_revision)
            self._finish_discard(item)
            return _public(self.repository.get_import(import_id))
        return await self._run(discard)

    async def list_documents(self, actor: str, library_id: str) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            library = self.access.require_permission(actor, library_id)
            if library["role"] == "owner":
                records = self.repository.list_documents(library_id, include_pending=True, include_deleted=True)
                return [_public(item) for item in records if item["status"] == "active" or item.get("cleanup_pending")]
            return [_public(item) for item in self.repository.list_documents(library_id)]
        return await self._run(load)

    async def update_document(
        self, actor: str, library_id: str, document_id: str, metadata: Mapping[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        def update() -> dict[str, Any]:
            self.access.require_permission(actor, library_id, "write")
            item = self._document(actor, library_id, document_id)
            merged = {key: item.get(key) for key in LiteratureMetadata.model_fields}
            merged.update(metadata)
            result = self.repository.update_document(document_id, expected_revision, **validate_metadata(merged, required=True))
            self._ensure_index(library_id)
            return _public(result)
        return await self._run(update)

    async def read_index(self, actor: str, library_id: str) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            self.access.require_permission(actor, library_id)
            self._require_complete_publications(library_id)
            self._ensure_index(library_id)
            revision, text = self.files.read_index(library_id)
            current = self.access.require_permission(actor, library_id)
            if revision != current["index_revision"]:
                raise KnowledgeError("index_changed", status_code=409)
            self._require_complete_publications(library_id)
            return {"library_id": library_id, "index_revision": revision, "text": text}
        return await self._run(read)

    def _require_complete_publications(self, library_id: str) -> None:
        if any(item["publication_state"] != "complete"
               for item in self.repository.list_documents(library_id, include_pending=True)):
            raise KnowledgeError("publication_incomplete", status_code=409)

    async def read_document(self, actor: str, library_id: str, document_id: str) -> dict[str, Any]:
        result = await self.download_document(actor, library_id, document_id)
        return {key: value for key, value in {**result, "text": decode_markdown(result["content"])}.items() if key != "content"}

    async def download_document(self, actor: str, library_id: str, document_id: str) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            item = self._document(actor, library_id, document_id)
            raw = self.files.read_body(item["body_ref"], sha256=item["sha256"])
            current = self._document(actor, library_id, document_id)
            if current["sha256"] != item["sha256"]:
                raise KnowledgeError("document_changed", status_code=409)
            return {"library_id": library_id, "document_id": document_id, "sha256": item["sha256"],
                    "filename": item["filename"], "content": raw,
                    "metadata": {key: item.get(key) for key in LiteratureMetadata.model_fields}}
        return await self._run(read)

    async def list_for_scope(self, actor: str, scope: Mapping[str, Any]) -> dict[str, Any]:
        accessible = {item["library_id"]: item for item in await self.list_libraries(actor) if item["status"] == "active"}
        candidates = list(scope.get("candidate_library_ids", []))
        selected = list(scope.get("selected_library_ids", candidates))
        mode = scope.get("mode", "auto")
        allowed = [accessible[item] for item in candidates if item in accessible]
        missing = [item for item in (selected if mode == "selected" else candidates) if item not in accessible]
        return {"libraries": allowed, "unavailable_library_ids": missing,
                "requires_reselection": mode == "selected" and not allowed, "mode": mode}

    async def list_grants(self, actor: str, library_id: str) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            self.access.require_permission(actor, library_id, "write")
            return [_public(item) for item in self.repository.list_grants(library_id)]
        return await self._run(load)

    async def add_grant(
        self, actor: str, library_id: str, subject_id: str, *, subject_type: str = "user", role: str = "reader",
    ) -> dict[str, Any]:
        await self._run(self.access.require_permission, actor, library_id, "write")
        if subject_type != "user" or role != "reader":
            raise KnowledgeError("unsupported_knowledge_grant")
        subject_id = normalize_username(subject_id)
        exists = self.user_exists(subject_id) if self.user_exists else False
        if inspect.isawaitable(exists):
            exists = await exists
        if not exists:
            raise KnowledgeError("knowledge_user_not_found", status_code=404)
        def grant() -> dict[str, Any]:
            self.access.require_permission(actor, library_id, "write")
            return _public(self.repository.grant(library_id, subject_id, subject_type, role))
        result = await self._run(grant)
        logger.info("knowledge_grant_saved", extra={"library_id": library_id, "status": "reader", "count": 1})
        return result

    async def revoke_grant(self, actor: str, library_id: str, grant_id: str) -> dict[str, Any]:
        def revoke() -> dict[str, Any]:
            self.access.require_permission(actor, library_id, "write")
            grants = self.repository.list_grants(library_id)
            grant = next((item for item in grants if _public(item)["grant_id"] == grant_id), None)
            if grant:
                self.repository.revoke(library_id, grant["subject_id"], grant["subject_type"])
            return {"status": "revoked", "grant_id": grant_id}
        return await self._run(revoke)

    def _ensure_index(self, library_id: str) -> None:
        try:
            self._publish_index(library_id)
        except Exception:
            self.repository.set_operation_error("library", library_id, "index_incomplete")
            raise

    def _publish_index(self, library_id: str) -> None:
        for _ in range(5):
            snapshot = self.repository.index_snapshot(library_id)
            library, documents = snapshot["library"], snapshot["documents"]
            if library["status"] != "active":
                return
            revision = library["index_revision"]
            try:
                stored_revision, _ = self.files.read_index(library_id)
            except (FileNotFoundError, ValueError):
                stored_revision = -1
            if stored_revision != revision:
                lines = [f"# {library['name']}", "", f"library_id: {library_id}",
                         f"index_revision: {revision}", "", f"库描述：{library['description']}", ""]
                for item in documents:
                    lines.extend([
                        f"## {item['document_id']}｜{item['title']}", "",
                        f"- 作者：{'、'.join(item.get('authors') or [])}", f"- 年份：{item.get('year') or ''}",
                        f"- DOI：{item.get('doi') or ''}", f"- 关键词：{'、'.join(item.get('keywords') or [])}",
                        f"- 全文引用：{item['document_id']}", "", "### 检索简述", "", item["summary"], "",
                    ])
                self.files.publish_index(library_id, revision, "\n".join(lines))
            if self.repository.set_index_published(library_id, revision):
                return
        raise KnowledgeError("index_changed", status_code=409)

    def _finish_import(self, item: Mapping[str, Any]) -> None:
        try:
            self._ensure_index(item["library_id"])
            document = self.repository.get_document(item["document_id"])
            self._cleanup_document(document)
            for ref in item.get("cleanup_refs", []):
                self.files.delete_body(ref)
            self.repository.finish_import_cleanup(item["import_id"], item["revision"])
            self.repository.complete_publication(item["import_id"])
        except Exception:
            self.repository.set_operation_error("import", item["import_id"], "publication_incomplete")
            logger.warning("knowledge_publication_incomplete", extra={"import_id": item["import_id"]})

    def _finish_discard(self, item: Mapping[str, Any]) -> None:
        for ref in item.get("cleanup_refs", []):
            self.files.delete_body(ref)
        self.repository.finish_import_cleanup(item["import_id"], item["revision"])
        logger.info("knowledge_import_cleanup_completed", extra={"import_id": item["import_id"], "status": "complete", "count": len(item.get("cleanup_refs", []))})

    async def delete_document(
        self, actor: str, library_id: str, document_id: str, *, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        def delete() -> dict[str, Any]:
            self.access.require_permission(actor, library_id, "write")
            item = self.repository.get_document(document_id)
            if not item or item["library_id"] != library_id:
                raise KnowledgeError("document_unavailable", status_code=404)
            self.repository.delete_document(document_id, expected_revision)
            try:
                self._ensure_index(library_id)
                self._cleanup_document(self.repository.get_document(document_id))
            except Exception:
                logger.warning("knowledge_document_cleanup_incomplete", extra={"document_id": document_id})
            return _public(self.repository.get_document(document_id))
        return await self._run(delete)

    def _cleanup_document(self, item: Mapping[str, Any]) -> None:
        for ref in item.get("cleanup_refs", []):
            self.files.delete_body(ref)
        self.repository.finish_document_cleanup(item["document_id"], item["revision"])
        logger.info("knowledge_document_cleanup_completed", extra={"document_id": item["document_id"], "status": "complete", "count": len(item.get("cleanup_refs", []))})

    async def delete_library(
        self, actor: str, library_id: str, *, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        def delete() -> dict[str, Any]:
            library = self.repository.get_library(library_id)
            if not library or library["owner_user_id"] != normalize_username(actor):
                self.access.require_permission(actor, library_id, "write")
            self.repository.delete_library(library_id, expected_revision)
            try:
                self._cleanup_library(library_id)
            except Exception:
                self.repository.set_operation_error("library", library_id, "cleanup_incomplete")
                logger.warning("knowledge_library_cleanup_incomplete", extra={"library_id": library_id})
            return _public(self.repository.get_library(library_id))
        return await self._run(delete)

    def _cleanup_library(self, library_id: str) -> None:
        self.files.delete_library_files(library_id)
        current = self.repository.get_library(library_id)
        self.repository.finish_library_cleanup(library_id, current["revision"])
        logger.info("knowledge_library_cleanup_completed", extra={"library_id": library_id, "status": "complete"})

    async def process_pending(self) -> None:
        await self._run(self._recover_pending)
        while True:
            item = await self._run(self.repository.claim_import, self.worker_id, lease_seconds=120)
            if item is None:
                return
            heartbeat = asyncio.create_task(self._renew_claim(item))
            started = time.perf_counter()
            try:
                if self.extractor is None:
                    raise KnowledgeError("knowledge_extractor_unavailable")
                raw = await self._run(self.files.read_body, item["body_ref"], sha256=item["sha256"])
                text = decode_markdown(raw)
                result = self.extractor(text)
                if inspect.isawaitable(result):
                    result = await result
                metadata = dict(result)
                model = metadata.pop("extraction_model", None)
                fields = validate_metadata(metadata)
                completed = await self._run(self._finish_extraction, item, fields, model)
                if completed:
                    logger.info("knowledge_extraction_completed", extra={"import_id": item["import_id"], "status": "awaiting_review", "duration_ms": round((time.perf_counter() - started) * 1000)})
            except asyncio.CancelledError:
                await self._run(self._fail_extraction, item, "pending", None)
                raise
            except Exception:
                await self._run(self._fail_extraction, item, "failed", "extraction_failed")
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

    async def _renew_claim(self, item: Mapping[str, Any]) -> None:
        while True:
            await asyncio.sleep(30)
            renewed = await self._run(
                self.repository.renew_import_claim, item["import_id"], item["claim_token"], lease_seconds=120,
            )
            if not renewed:
                return

    def _finish_extraction(self, item: Mapping[str, Any], fields: Mapping[str, Any], model: str | None) -> bool:
        library = self.repository.get_library(item["library_id"])
        current = self.repository.get_import(item["import_id"])
        if (not library or library["status"] != "active" or not current or current["status"] != "processing"
                or current.get("claim_token") != item.get("claim_token") or current["sha256"] != item["sha256"]):
            return False
        self.repository.update_import(
            item["import_id"], item["revision"], **fields, status="awaiting_review", model_edition=model,
            claim_token=item.get("claim_token"), error=None,
        )
        return True

    def _fail_extraction(self, item: Mapping[str, Any], status: str, error: str | None) -> None:
        current = self.repository.get_import(item["import_id"])
        if (current and current["status"] == "processing" and current.get("claim_token") == item.get("claim_token")):
            self.repository.update_import(
                current["import_id"], current["revision"], status=status, error=error, claim_token=item.get("claim_token"),
            )

    def _recover_pending(self) -> None:
        pending = self.repository.pending_work()
        for item in pending.get("libraries", []):
            try:
                if item["status"] == "deleted":
                    self._cleanup_library(item["library_id"])
                else:
                    self._ensure_index(item["library_id"])
            except Exception:
                logger.warning("knowledge_library_recovery_pending", extra={"library_id": item["library_id"]})
        for item in pending.get("documents", []):
            if item["status"] == "deleted":
                try:
                    self._ensure_index(item["library_id"])
                    self._cleanup_document(item)
                except Exception:
                    logger.warning("knowledge_document_recovery_pending", extra={"document_id": item["document_id"]})
        for item in pending.get("imports", []):
            try:
                if item["status"] == "publishing":
                    self._finish_import(item)
                elif item["status"] == "discarded":
                    self._finish_discard(item)
                elif item["status"] == "uploading" and (item.get("claim_expires_at") or 0) <= time.time():
                    try:
                        self.files.read_body(item["body_ref"], sha256=item["sha256"])
                        self.repository.update_import(item["import_id"], item["revision"], status="pending")
                    except FileNotFoundError:
                        discarded = self.repository.discard_import(item["import_id"], item["revision"])
                        self.repository.set_operation_error("import", item["import_id"], "upload_incomplete")
                        self._finish_discard(discarded)
            except Exception:
                logger.warning("knowledge_import_recovery_pending", extra={"import_id": item["import_id"]})
