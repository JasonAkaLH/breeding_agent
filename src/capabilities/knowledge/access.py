from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.auth.services import normalize_username


class KnowledgeError(Exception):
    def __init__(self, code: str, message: str | None = None, *, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message or code)


class AccessSubjectResolver:
    """The seam for trusted membership resolution; current identities are users only."""

    def resolve_subjects(self, actor: str) -> set[tuple[str, str]]:
        return {("user", normalize_username(actor))}


class LibraryAccessService:
    def __init__(self, repository: Any, subject_resolver: AccessSubjectResolver | None = None) -> None:
        self.repository = repository
        self.subject_resolver = subject_resolver or AccessSubjectResolver()

    def _role(self, actor: str, library: Mapping[str, Any]) -> str | None:
        actor = normalize_username(actor)
        if library.get("owner_user_id") == actor:
            return "owner"
        subjects = self.subject_resolver.resolve_subjects(actor)
        for grant in self.repository.list_grants(str(library["library_id"])):
            if (grant["subject_type"], grant["subject_id"]) in subjects and grant["role"] == "reader":
                return "reader"
        return None

    def require_permission(self, actor: str, library_id: str, action: str = "read") -> dict[str, Any]:
        library = self.repository.get_library(library_id)
        if not library or library.get("status") != "active":
            raise KnowledgeError("library_unavailable", status_code=404)
        role = self._role(actor, library)
        if role is None or (action != "read" and role != "owner"):
            raise KnowledgeError("knowledge_forbidden", status_code=403)
        return {**library, "role": role}

    def list_accessible_libraries(self, actor: str) -> list[dict[str, Any]]:
        results = []
        for library in self.repository.list_libraries():
            if library.get("status") == "active":
                role = self._role(actor, library)
                if role:
                    results.append({**library, "role": role})
        return results
