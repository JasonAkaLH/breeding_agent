from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

from .sqlalchemy_models import (
    ConversationKnowledgeSelectionRow,
    ConversationRow,
    KnowledgeDocumentRow,
    KnowledgeImportRow,
    KnowledgeLibraryRow,
    KnowledgeQueryScopeRow,
    LibraryGrantRow,
)


class KnowledgeConflict(ValueError):
    status_code = 409

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class KnowledgeNotFound(LookupError):
    status_code = 404

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_METADATA = frozenset({"title", "authors", "year", "doi", "keywords", "summary"})
_LIVE_IMPORTS = (
    "uploading",
    "pending",
    "processing",
    "awaiting_review",
    "failed",
    "publishing",
)
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _record(row: Any) -> dict:
    result = {
        column.name: getattr(row, column.name) for column in row.__table__.columns
    }
    if isinstance(row, KnowledgeQueryScopeRow):
        result["actor"] = result["owner_user_id"]
        result["library_ids"] = list(result["selected_library_ids"])
    return result


def _revision(row: Any, expected: int | None) -> None:
    if expected is not None and row.revision != expected:
        raise KnowledgeConflict("knowledge_revision_conflict")


def _body_refs(row: Any) -> list[str]:
    return [ref for ref in (row.body_ref, row.parsed_body_ref) if ref]


class KnowledgeRepository:
    """Shared synchronous SQLAlchemy transactions; no Task authority or body payloads."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    @contextmanager
    def _write(self, *, lock_key: str | None = None):
        with self.session_factory() as session:
            try:
                if session.get_bind().dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                elif lock_key is not None:
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                        {"key": f"knowledge:{lock_key}"},
                    )
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    @staticmethod
    def _library(
        session: Session, library_id: str, *, active: bool = True
    ) -> KnowledgeLibraryRow:
        row = session.execute(
            select(KnowledgeLibraryRow)
            .where(KnowledgeLibraryRow.library_id == library_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None or (active and row.status != "active"):
            raise KnowledgeNotFound("knowledge_library_unavailable")
        return row

    @staticmethod
    def _touch(library: KnowledgeLibraryRow) -> None:
        library.revision += 1
        library.index_revision += 1
        library.updated_at = int(time.time())

    @staticmethod
    def _accessible(session: Session, actor: str) -> list[str]:
        shared = select(LibraryGrantRow.library_id).where(
            LibraryGrantRow.subject_type == "user",
            LibraryGrantRow.subject_id == actor,
            LibraryGrantRow.role == "reader",
        )
        return list(
            session.scalars(
                select(KnowledgeLibraryRow.library_id)
                .where(
                    KnowledgeLibraryRow.status == "active",
                    or_(
                        KnowledgeLibraryRow.owner_user_id == actor,
                        KnowledgeLibraryRow.library_id.in_(shared),
                    ),
                )
                .order_by(KnowledgeLibraryRow.library_id)
            )
        )

    def create_library(
        self,
        owner_user_id: str,
        name: str,
        description: str = "",
        library_id: str | None = None,
    ) -> dict:
        if not name.strip():
            raise ValueError("knowledge_library_name_required")
        now = int(time.time())
        row = KnowledgeLibraryRow(
            library_id=library_id or uuid4().hex,
            owner_user_id=owner_user_id,
            name=name,
            description=description,
            status="active",
            revision=1,
            index_revision=1,
            published_index_revision=0,
            cleanup_refs=[],
            cleanup_pending=False,
            error=None,
            created_at=now,
            updated_at=now,
        )
        with self._write() as session:
            session.add(row)
            session.flush()
            return _record(row)

    def get_library(self, library_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.get(KnowledgeLibraryRow, library_id)
            return _record(row) if row is not None else None

    def list_libraries(
        self, owner_user_id: str | None = None, *, include_deleted: bool = False
    ) -> list[dict]:
        query = select(KnowledgeLibraryRow).order_by(KnowledgeLibraryRow.library_id)
        if owner_user_id is not None:
            query = query.where(KnowledgeLibraryRow.owner_user_id == owner_user_id)
        if not include_deleted:
            query = query.where(KnowledgeLibraryRow.status == "active")
        with self.session_factory() as session:
            return [_record(row) for row in session.scalars(query)]

    def update_library(
        self, library_id: str, expected_revision: int | None = None, **changes
    ) -> dict:
        if set(changes) - {"name", "description", "error"}:
            raise ValueError("invalid_knowledge_library_update")
        if "name" in changes and not changes["name"].strip():
            raise ValueError("knowledge_library_name_required")
        with self._write() as session:
            row = self._library(session, library_id)
            _revision(row, expected_revision)
            for key, value in changes.items():
                setattr(row, key, value)
            self._touch(row)
            session.flush()
            return _record(row)

    def list_grants(self, library_id: str) -> list[dict]:
        with self.session_factory() as session:
            return [
                _record(row)
                for row in session.scalars(
                    select(LibraryGrantRow)
                    .where(LibraryGrantRow.library_id == library_id)
                    .order_by(LibraryGrantRow.subject_id)
                )
            ]

    def grant(
        self,
        library_id: str,
        subject_id: str,
        subject_type: str = "user",
        role: str = "reader",
    ) -> dict:
        if subject_type != "user" or role != "reader":
            raise ValueError("unsupported_knowledge_grant")
        with self._write() as session:
            self._library(session, library_id)
            key = (library_id, subject_type, subject_id)
            row = session.get(LibraryGrantRow, key)
            if row is None:
                row = LibraryGrantRow(
                    library_id=library_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    role=role,
                )
                session.add(row)
                session.flush()
            return _record(row)

    def revoke(
        self, library_id: str, subject_id: str, subject_type: str = "user"
    ) -> None:
        if subject_type != "user":
            raise ValueError("unsupported_knowledge_subject")
        with self._write() as session:
            self._library(session, library_id)
            session.execute(
                delete(LibraryGrantRow).where(
                    LibraryGrantRow.library_id == library_id,
                    LibraryGrantRow.subject_type == subject_type,
                    LibraryGrantRow.subject_id == subject_id,
                )
            )

    @staticmethod
    def _duplicate(
        session: Session,
        library_id: str,
        sha256: str,
        *,
        exclude_import: str | None = None,
    ) -> tuple[str, Any] | None:
        document = session.scalars(
            select(KnowledgeDocumentRow).where(
                KnowledgeDocumentRow.library_id == library_id,
                KnowledgeDocumentRow.sha256 == sha256,
                KnowledgeDocumentRow.status == "active",
            )
        ).first()
        if document is not None:
            return "document", document
        query = select(KnowledgeImportRow).where(
            KnowledgeImportRow.library_id == library_id,
            KnowledgeImportRow.sha256 == sha256,
            KnowledgeImportRow.status.in_(_LIVE_IMPORTS),
        )
        if exclude_import is not None:
            query = query.where(KnowledgeImportRow.import_id != exclude_import)
        imported = session.scalars(query).first()
        return ("import", imported) if imported is not None else None

    def register_import(
        self,
        library_id: str,
        sha256: str,
        filename: str,
        body_ref: str,
        replacement_document_id: str | None = None,
        target_revision: int | None = None,
        import_id: str | None = None,
        status: str = "pending",
    ) -> dict:
        if not _SHA.fullmatch(sha256) or status not in {"uploading", "pending"}:
            raise ValueError("invalid_knowledge_import")
        from .knowledge_files import KnowledgeFileStore

        parts = body_ref.split("/")
        if (
            len(parts) != 3
            or KnowledgeFileStore.build_reference(library_id, parts[1], sha256, "pdf" if filename.lower().endswith(".pdf") else "md")
            != body_ref
        ):
            raise ValueError("invalid_knowledge_body_reference")
        with self._write() as session:
            self._library(session, library_id)
            target = None
            if replacement_document_id is not None:
                target = session.get(KnowledgeDocumentRow, replacement_document_id)
                if (
                    target is None
                    or target.library_id != library_id
                    or target.status != "active"
                ):
                    raise KnowledgeNotFound("knowledge_document_unavailable")
                _revision(target, target_revision)
            duplicate = self._duplicate(session, library_id, sha256)
            if duplicate is not None:
                kind, existing = duplicate
                return dict(_record(existing), duplicate_type=kind)
            now = int(time.time())
            row = KnowledgeImportRow(
                import_id=import_id or uuid4().hex,
                library_id=library_id,
                filename=filename,
                body_ref=body_ref,
                sha256=sha256,
                replacement_document_id=replacement_document_id,
                target_revision=target.revision if target is not None else None,
                document_id=None,
                title="",
                authors=[],
                year=None,
                doi=None,
                keywords=[],
                summary="",
                status=status,
                publication_state="draft",
                revision=1,
                claim_token=(
                    f"upload:{uuid4().hex}" if status == "uploading" else None
                ),
                claim_expires_at=(now + 300 if status == "uploading" else None),
                model_edition=None,
                error=None,
                cleanup_refs=[],
                cleanup_pending=False,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return _record(row)

    def get_import(self, import_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.get(KnowledgeImportRow, import_id)
            return _record(row) if row is not None else None

    def list_imports(
        self, library_id: str, *, include_discarded: bool = False
    ) -> list[dict]:
        query = (
            select(KnowledgeImportRow)
            .where(KnowledgeImportRow.library_id == library_id)
            .order_by(KnowledgeImportRow.created_at, KnowledgeImportRow.import_id)
        )
        if not include_discarded:
            query = query.where(KnowledgeImportRow.status != "discarded")
        with self.session_factory() as session:
            return [_record(row) for row in session.scalars(query)]

    def update_import(
        self,
        import_id: str,
        expected_revision: int | None = None,
        *,
        claim_token: str | None = None,
        **changes,
    ) -> dict:
        allowed = _METADATA | {
            "status",
            "model_edition",
            "error",
            "cleanup_refs",
            "cleanup_pending",
            "publication_state",
        }
        if set(changes) - allowed:
            raise ValueError("invalid_knowledge_import_update")
        with self._write() as session:
            row = session.get(KnowledgeImportRow, import_id)
            if row is None:
                raise KnowledgeNotFound("knowledge_import_unavailable")
            self._library(session, row.library_id)
            session.refresh(row)
            _revision(row, expected_revision)
            if row.status in {"discarded", "published", "publishing"}:
                raise KnowledgeConflict("knowledge_import_no_longer_draft")
            if claim_token is not None and (row.claim_token != claim_token or (row.claim_expires_at or 0) <= time.time()):
                raise KnowledgeConflict("knowledge_import_claim_conflict")
            if changes.get("status") in {"publishing", "published", "discarded"}:
                raise ValueError("knowledge_import_transition_requires_operation")
            for key, value in changes.items():
                setattr(row, key, value)
            if changes.get("status") != "processing":
                row.claim_token = None
                row.claim_expires_at = None
            row.revision += 1
            row.updated_at = int(time.time())
            session.flush()
            return _record(row)

    def claim_import(self, worker_id: str, lease_seconds: int = 300) -> dict | None:
        now = int(time.time())
        # Lock order is always library -> import, matching upload/confirm/delete.
        with self._write(lock_key="import-claim") as session:
            query = (
                select(KnowledgeImportRow.import_id, KnowledgeImportRow.library_id)
                .join(
                    KnowledgeLibraryRow,
                    KnowledgeLibraryRow.library_id == KnowledgeImportRow.library_id,
                )
                .where(
                    KnowledgeLibraryRow.status == "active",
                    or_(
                        KnowledgeImportRow.status == "pending",
                        (KnowledgeImportRow.status == "processing")
                        & (KnowledgeImportRow.claim_expires_at <= now),
                    ),
                )
                .order_by(KnowledgeImportRow.created_at, KnowledgeImportRow.import_id)
            )
            for import_id, library_id in session.execute(query):
                self._library(session, library_id)
                row = session.get(KnowledgeImportRow, import_id, populate_existing=True)
                if row.status != "pending" and not (
                    row.status == "processing" and (row.claim_expires_at or 0) <= now
                ):
                    continue
                row.status = "processing"
                row.claim_token = f"{worker_id}:{uuid4().hex}"
                row.claim_expires_at = now + max(1, lease_seconds)
                row.revision += 1
                row.updated_at = now
                session.flush()
                return _record(row)
            return None

    def register_parsed_body(
        self, import_id: str, expected_revision: int, claim_token: str,
        source_sha256: str, parsed_body_ref: str, parsed_body_sha256: str,
    ) -> dict:
        from .knowledge_files import KnowledgeFileStore

        with self._write() as session:
            row = session.get(KnowledgeImportRow, import_id)
            if row is None:
                raise KnowledgeNotFound("knowledge_import_unavailable")
            self._library(session, row.library_id)
            session.refresh(row)
            _revision(row, expected_revision)
            if (row.status != "processing" or row.claim_token != claim_token
                    or (row.claim_expires_at or 0) <= time.time()
                    or row.sha256 != source_sha256 or not row.body_ref.endswith(".pdf")):
                raise KnowledgeConflict("knowledge_import_claim_conflict")
            parts = parsed_body_ref.split("/")
            if (len(parts) != 3 or KnowledgeFileStore.build_reference(
                row.library_id, parts[1], parsed_body_sha256,
            ) != parsed_body_ref):
                raise ValueError("invalid_knowledge_parsed_reference")
            if row.parsed_body_ref and row.parsed_body_ref != parsed_body_ref:
                row.cleanup_refs = list(dict.fromkeys([*row.cleanup_refs, row.parsed_body_ref]))
                row.cleanup_pending = True
            row.parsed_body_ref = parsed_body_ref
            row.parsed_body_sha256 = parsed_body_sha256
            row.revision += 1
            row.updated_at = int(time.time())
            session.flush()
            return _record(row)

    def renew_import_claim(
        self, import_id: str, claim_token: str, lease_seconds: int = 120
    ) -> bool:
        with self._write() as session:
            row = session.get(KnowledgeImportRow, import_id)
            if row is None:
                return False
            library = self._library(session, row.library_id, active=False)
            session.refresh(row)
            if (
                library.status != "active"
                or row.status != "processing"
                or row.claim_token != claim_token
                or (row.claim_expires_at or 0) <= time.time()
            ):
                return False
            row.claim_expires_at = int(time.time()) + max(1, lease_seconds)
            # Lease renewal does not mutate the metadata CAS revision held by
            # the processing owner. Claim adoption still advances that revision.
            return True

    def get_document(self, document_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.get(KnowledgeDocumentRow, document_id)
            return _record(row) if row is not None else None

    def list_documents(
        self,
        library_id: str,
        *,
        include_pending: bool = False,
        include_deleted: bool = False,
    ) -> list[dict]:
        query = (
            select(KnowledgeDocumentRow)
            .where(KnowledgeDocumentRow.library_id == library_id)
            .order_by(KnowledgeDocumentRow.document_id)
        )
        if not include_deleted:
            query = query.where(KnowledgeDocumentRow.status == "active")
        if not include_pending:
            query = query.where(KnowledgeDocumentRow.publication_state == "complete")
        with self.session_factory() as session:
            return [_record(row) for row in session.scalars(query)]

    def publish_import(
        self, import_id: str, expected_revision: int | None = None
    ) -> dict:
        with self._write() as session:
            imported = session.get(KnowledgeImportRow, import_id)
            if imported is None:
                raise KnowledgeNotFound("knowledge_import_unavailable")
            library = self._library(session, imported.library_id)
            session.refresh(imported)
            if imported.status in {"publishing", "published"}:
                return _record(session.get(KnowledgeDocumentRow, imported.document_id))
            _revision(imported, expected_revision)
            if imported.status not in {"awaiting_review", "failed"}:
                raise KnowledgeConflict("knowledge_import_not_ready_for_confirmation")
            if (bool(imported.parsed_body_ref) != bool(imported.parsed_body_sha256)
                    or (imported.body_ref.endswith(".pdf") and not imported.parsed_body_ref)):
                raise KnowledgeConflict("knowledge_text_not_ready")
            if not imported.title.strip() or not imported.summary.strip():
                raise ValueError("knowledge_title_summary_required")
            duplicate = self._duplicate(
                session, imported.library_id, imported.sha256, exclude_import=import_id
            )
            if duplicate is not None:
                kind, existing = duplicate
                if not (
                    kind == "document"
                    and existing.document_id == imported.replacement_document_id
                ):
                    raise KnowledgeConflict("knowledge_duplicate_content")
            now = int(time.time())
            if imported.replacement_document_id is not None:
                document = session.get(
                    KnowledgeDocumentRow,
                    imported.replacement_document_id,
                    populate_existing=True,
                )
                if (
                    document is None
                    or document.status != "active"
                    or document.library_id != imported.library_id
                ):
                    raise KnowledgeConflict("knowledge_replacement_target_changed")
                _revision(document, imported.target_revision)
                previous = _body_refs(document)
                document.revision += 1
                obsolete = [ref for ref in previous if ref not in _body_refs(imported)]
                if obsolete:
                    document.cleanup_refs = list(
                        dict.fromkeys([*document.cleanup_refs, *obsolete])
                    )
                    document.cleanup_pending = True
            else:
                document = KnowledgeDocumentRow(
                    document_id=uuid4().hex,
                    library_id=imported.library_id,
                    status="active",
                    revision=1,
                    cleanup_refs=[],
                    cleanup_pending=False,
                    created_at=now,
                )
                session.add(document)
            for key in _METADATA | {"filename", "body_ref", "sha256", "parsed_body_ref", "parsed_body_sha256"}:
                setattr(document, key, getattr(imported, key))
            document.publication_state = "publishing"
            document.updated_at = now
            imported.document_id = document.document_id
            imported.status = "publishing"
            imported.publication_state = "index_pending"
            imported.claim_token = None
            imported.claim_expires_at = None
            imported.revision += 1
            imported.updated_at = now
            self._touch(library)
            session.flush()
            return _record(document)

    def complete_publication(self, import_id: str) -> dict:
        with self._write() as session:
            imported = session.get(KnowledgeImportRow, import_id)
            if imported is None:
                raise KnowledgeNotFound("knowledge_import_unavailable")
            library = self._library(session, imported.library_id)
            session.refresh(imported)
            if imported.status == "published":
                return _record(imported)
            if imported.status != "publishing":
                raise KnowledgeConflict("knowledge_import_not_publishing")
            document = session.get(
                KnowledgeDocumentRow, imported.document_id, populate_existing=True
            )
            if (
                document is None
                or document.status != "active"
                or document.sha256 != imported.sha256
                or document.body_ref != imported.body_ref
                or document.parsed_body_ref != imported.parsed_body_ref
                or document.parsed_body_sha256 != imported.parsed_body_sha256
            ):
                raise KnowledgeConflict("knowledge_publication_changed")
            if (
                library.published_index_revision != library.index_revision
                or document.cleanup_refs
                or imported.cleanup_refs
            ):
                raise KnowledgeConflict("knowledge_publication_incomplete")
            document.publication_state = "complete"
            document.cleanup_pending = False
            imported.status = "published"
            imported.publication_state = "complete"
            imported.cleanup_pending = False
            imported.error = None
            imported.revision += 1
            session.flush()
            return _record(imported)

    def update_document(
        self, document_id: str, expected_revision: int | None = None, **metadata
    ) -> dict:
        if set(metadata) - _METADATA:
            raise ValueError("invalid_knowledge_document_metadata")
        with self._write() as session:
            row = session.get(KnowledgeDocumentRow, document_id)
            if row is None:
                raise KnowledgeNotFound("knowledge_document_unavailable")
            library = self._library(session, row.library_id)
            session.refresh(row)
            if row.status != "active" or row.publication_state != "complete":
                raise KnowledgeConflict("knowledge_document_not_editable")
            _revision(row, expected_revision)
            for key, value in metadata.items():
                setattr(row, key, value)
            if not row.title.strip() or not row.summary.strip():
                raise ValueError("knowledge_title_summary_required")
            row.revision += 1
            row.updated_at = int(time.time())
            self._touch(library)
            session.flush()
            return _record(row)

    @staticmethod
    def _discard(row: KnowledgeImportRow) -> None:
        if row.status != "published":
            row.cleanup_refs = list(dict.fromkeys([*row.cleanup_refs, *_body_refs(row)]))
            row.cleanup_pending = True
        row.status = "discarded"
        row.claim_token = None
        row.claim_expires_at = None
        row.revision += 1
        row.updated_at = int(time.time())

    def discard_import(
        self, import_id: str, expected_revision: int | None = None
    ) -> dict:
        with self._write() as session:
            row = session.get(KnowledgeImportRow, import_id)
            if row is None:
                raise KnowledgeNotFound("knowledge_import_unavailable")
            self._library(session, row.library_id, active=False)
            session.refresh(row)
            if row.status == "discarded":
                return _record(row)
            _revision(row, expected_revision)
            if row.status in {"publishing", "published"}:
                raise KnowledgeConflict("knowledge_import_already_confirmed")
            self._discard(row)
            session.flush()
            return _record(row)

    def delete_document(
        self, document_id: str, expected_revision: int | None = None
    ) -> dict:
        with self._write() as session:
            row = session.get(KnowledgeDocumentRow, document_id)
            if row is None:
                raise KnowledgeNotFound("knowledge_document_unavailable")
            library = self._library(session, row.library_id)
            session.refresh(row)
            if row.status == "deleted":
                return _record(row)
            _revision(row, expected_revision)
            row.status = "deleted"
            row.revision += 1
            row.cleanup_refs = list(dict.fromkeys([*row.cleanup_refs, *_body_refs(row)]))
            row.cleanup_pending = True
            for imported in session.scalars(
                select(KnowledgeImportRow).where(
                    KnowledgeImportRow.library_id == row.library_id,
                    or_(
                        KnowledgeImportRow.replacement_document_id == document_id,
                        KnowledgeImportRow.document_id == document_id,
                    ),
                )
            ):
                self._discard(imported)
            self._touch(library)
            session.flush()
            return _record(row)

    def delete_library(
        self, library_id: str, expected_revision: int | None = None
    ) -> dict:
        with self._write() as session:
            library = self._library(session, library_id, active=False)
            if library.status == "deleted":
                return _record(library)
            _revision(library, expected_revision)
            library.status = "deleted"
            library.cleanup_pending = True
            self._touch(library)
            session.execute(
                delete(LibraryGrantRow).where(LibraryGrantRow.library_id == library_id)
            )
            for document in session.scalars(
                select(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.library_id == library_id
                )
            ):
                document.status = "deleted"
                document.revision += 1
                document.cleanup_refs = list(
                    dict.fromkeys([*document.cleanup_refs, *_body_refs(document)])
                )
                document.cleanup_pending = True
            for imported in session.scalars(
                select(KnowledgeImportRow).where(
                    KnowledgeImportRow.library_id == library_id
                )
            ):
                self._discard(imported)
            session.flush()
            return _record(library)

    def index_snapshot(self, library_id: str) -> dict:
        with self._write() as session:
            library = self._library(session, library_id, active=False)
            documents = list(
                session.scalars(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.library_id == library_id,
                        KnowledgeDocumentRow.status == "active",
                    )
                    .order_by(KnowledgeDocumentRow.document_id)
                )
            )
            return {
                "library": _record(library),
                "documents": [_record(row) for row in documents],
            }

    def set_index_published(self, library_id: str, index_revision: int) -> bool:
        with self._write() as session:
            library = self._library(session, library_id, active=False)
            if library.index_revision != index_revision:
                return False
            library.published_index_revision = index_revision
            library.error = None
            return True

    def _finish_cleanup(
        self, model, key: str, expected_revision: int | None = None
    ) -> dict:
        with self._write() as session:
            row = session.get(model, key)
            if row is None:
                raise KnowledgeNotFound("knowledge_resource_unavailable")
            self._library(session, row.library_id, active=False)
            session.refresh(row)
            _revision(row, expected_revision)
            row.cleanup_refs = []
            row.cleanup_pending = False
            session.flush()
            return _record(row)

    def finish_document_cleanup(
        self, document_id: str, expected_revision: int | None = None
    ) -> dict:
        return self._finish_cleanup(
            KnowledgeDocumentRow, document_id, expected_revision
        )

    def finish_import_cleanup(
        self, import_id: str, expected_revision: int | None = None
    ) -> dict:
        return self._finish_cleanup(KnowledgeImportRow, import_id, expected_revision)

    def finish_library_cleanup(
        self, library_id: str, expected_revision: int | None = None
    ) -> dict:
        with self._write() as session:
            row = self._library(session, library_id, active=False)
            _revision(row, expected_revision)
            if row.status != "deleted":
                raise KnowledgeConflict("knowledge_library_not_deleted")
            row.cleanup_refs = []
            row.cleanup_pending = False
            row.published_index_revision = row.index_revision
            row.error = None
            # A deleted library cannot receive new writes. Its complete directory
            # removal also satisfies every subordinate cleanup obligation.
            for model in (KnowledgeDocumentRow, KnowledgeImportRow):
                for child in session.scalars(
                    select(model).where(model.library_id == library_id)
                ):
                    child.cleanup_refs = []
                    child.cleanup_pending = False
            session.flush()
            return _record(row)

    def set_operation_error(
        self, kind: str, resource_id: str, error: str | None
    ) -> dict:
        models = {"library": KnowledgeLibraryRow, "import": KnowledgeImportRow}
        if kind not in models:
            raise ValueError("invalid_knowledge_operation_kind")
        with self._write() as session:
            row = session.get(models[kind], resource_id)
            if row is None:
                raise KnowledgeNotFound("knowledge_resource_unavailable")
            self._library(session, row.library_id, active=False)
            session.refresh(row)
            row.error = error
            session.flush()
            return _record(row)

    def pending_work(self) -> dict:
        with self.session_factory() as session:
            return {
                "libraries": [
                    _record(row)
                    for row in session.scalars(
                        select(KnowledgeLibraryRow).where(
                            or_(
                                KnowledgeLibraryRow.cleanup_pending.is_(True),
                                KnowledgeLibraryRow.index_revision
                                != KnowledgeLibraryRow.published_index_revision,
                            )
                        )
                    )
                ],
                "documents": [
                    _record(row)
                    for row in session.scalars(
                        select(KnowledgeDocumentRow).where(
                            KnowledgeDocumentRow.cleanup_pending.is_(True)
                        )
                    )
                ],
                "imports": [
                    _record(row)
                    for row in session.scalars(
                        select(KnowledgeImportRow).where(
                            or_(
                                KnowledgeImportRow.status.in_(
                                    ("uploading", "pending", "processing", "publishing")
                                ),
                                KnowledgeImportRow.cleanup_pending.is_(True),
                            )
                        )
                    )
                ],
            }

    @staticmethod
    def _conversation(
        session: Session, actor: str, conversation_id: str, *, create: bool = False
    ) -> ConversationRow | None:
        row = session.execute(
            select(ConversationRow)
            .where(ConversationRow.conversation_id == conversation_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None and create:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            row = ConversationRow(
                conversation_id=conversation_id,
                username=actor,
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
        if row is not None and (row.username != actor or row.status != "active"):
            raise KnowledgeConflict("knowledge_conversation_unavailable")
        return row

    def get_selection(self, actor: str, conversation_id: str) -> dict:
        with self.session_factory() as session:
            self._conversation(session, actor, conversation_id)
            row = session.get(ConversationKnowledgeSelectionRow, conversation_id)
            if row is None:
                return {
                    "conversation_id": conversation_id,
                    "owner_user_id": actor,
                    "mode": "auto",
                    "library_ids": [],
                    "revision": 0,
                }
            if row.owner_user_id != actor:
                raise KnowledgeConflict("knowledge_selection_owner_mismatch")
            return _record(row)

    def save_selection(
        self,
        actor: str,
        conversation_id: str,
        mode: str,
        library_ids: list[str],
        expected_revision: int | None = None,
    ) -> dict:
        ids = list(dict.fromkeys(library_ids))
        if (
            mode not in {"auto", "selected"}
            or (mode == "selected" and not ids)
            or (mode == "auto" and ids)
        ):
            raise ValueError("invalid_knowledge_selection")
        with self._write(lock_key=f"conversation:{conversation_id}") as session:
            self._conversation(session, actor, conversation_id, create=True)
            if any(value not in self._accessible(session, actor) for value in ids):
                raise KnowledgeConflict("knowledge_selection_library_unavailable")
            row = session.get(ConversationKnowledgeSelectionRow, conversation_id)
            if row is None:
                if expected_revision not in {None, 0}:
                    raise KnowledgeConflict("knowledge_revision_conflict")
                row = ConversationKnowledgeSelectionRow(
                    conversation_id=conversation_id,
                    owner_user_id=actor,
                    mode=mode,
                    library_ids=ids,
                    revision=1,
                )
                session.add(row)
            else:
                if row.owner_user_id != actor:
                    raise KnowledgeConflict("knowledge_selection_owner_mismatch")
                _revision(row, expected_revision)
                row.mode, row.library_ids = mode, ids
                row.revision += 1
            session.flush()
            return _record(row)

    def freeze_scope(
        self, actor: str, conversation_id: str, root_message_id: str
    ) -> dict:
        with self._write(lock_key=f"scope:{root_message_id}") as session:
            self._conversation(session, actor, conversation_id)
            row = session.get(KnowledgeQueryScopeRow, root_message_id)
            if row is not None:
                if row.owner_user_id != actor or row.conversation_id != conversation_id:
                    raise KnowledgeConflict("knowledge_scope_identity_mismatch")
                return _record(row)
            selection = session.get(ConversationKnowledgeSelectionRow, conversation_id)
            if selection is not None and selection.owner_user_id != actor:
                raise KnowledgeConflict("knowledge_selection_owner_mismatch")
            mode = selection.mode if selection is not None else "auto"
            selected = list(selection.library_ids) if selection is not None else []
            accessible = self._accessible(session, actor)
            candidates = (
                accessible
                if mode == "auto"
                else [value for value in selected if value in accessible]
            )
            row = KnowledgeQueryScopeRow(
                root_message_id=root_message_id,
                owner_user_id=actor,
                conversation_id=conversation_id,
                mode=mode,
                selected_library_ids=selected,
                candidate_library_ids=candidates,
            )
            session.add(row)
            session.flush()
            return _record(row)

    def get_scope(
        self,
        root_message_id: str,
        actor: str | None = None,
        conversation_id: str | None = None,
    ) -> dict | None:
        with self.session_factory() as session:
            row = session.get(KnowledgeQueryScopeRow, root_message_id)
            if row is None:
                return None
            if (actor is not None and row.owner_user_id != actor) or (
                conversation_id is not None and row.conversation_id != conversation_id
            ):
                raise KnowledgeConflict("knowledge_scope_identity_mismatch")
            return _record(row)

    def delete_conversation_context(self, conversation_id: str) -> None:
        with self._write(lock_key=f"conversation:{conversation_id}") as session:
            session.execute(
                delete(ConversationKnowledgeSelectionRow).where(
                    ConversationKnowledgeSelectionRow.conversation_id == conversation_id
                )
            )
            session.execute(
                delete(KnowledgeQueryScopeRow).where(
                    KnowledgeQueryScopeRow.conversation_id == conversation_id
                )
            )
