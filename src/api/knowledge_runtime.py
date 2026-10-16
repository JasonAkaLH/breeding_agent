from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from src.capabilities.knowledge.access import KnowledgeError
from src.storage.knowledge_repository import KnowledgeConflict, KnowledgeNotFound


class KnowledgeRuntimeMixin:
    """Trusted query scope loading, separate from submission/Task authority."""

    async def _knowledge_repository_call(self, operation, *args, **kwargs):
        try:
            return await asyncio.to_thread(operation, *args, **kwargs)
        except (KnowledgeConflict, KnowledgeNotFound) as exc:
            raise KnowledgeError(exc.code, status_code=exc.status_code) from exc
        except ValueError as exc:
            raise KnowledgeError("invalid_knowledge_selection", status_code=422) from exc

    async def get_knowledge_selection(self, actor: str, conversation_id: str) -> dict[str, Any]:
        repository = getattr(self, "knowledge_repository", None)
        service = getattr(self, "knowledge_service", None)
        if repository is None or service is None:
            raise KnowledgeError("knowledge_unavailable", status_code=503)
        result = await self._knowledge_repository_call(repository.get_selection, actor, conversation_id)
        accessible = {item["library_id"] for item in await service.list_libraries(actor)}
        return {
            **result,
            "unavailable_library_ids": [item for item in result["library_ids"] if item not in accessible],
        }

    async def save_knowledge_selection(
        self, actor: str, conversation_id: str, mode: str, library_ids: list[str],
        *, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        repository = getattr(self, "knowledge_repository", None)
        if repository is None:
            raise KnowledgeError("knowledge_unavailable", status_code=503)
        result = await self._knowledge_repository_call(
            repository.save_selection, actor, conversation_id, mode, library_ids,
            expected_revision=expected_revision,
        )
        return {**result, "unavailable_library_ids": []}

    async def _knowledge_scope_metadata(
        self, *, actor: str, conversation_id: str, root_message_id: str,
    ) -> dict[str, Any]:
        repository = getattr(self, "knowledge_repository", None)
        if repository is None:
            return {}
        scope = await self._knowledge_repository_call(
            repository.get_scope, root_message_id, actor=actor, conversation_id=conversation_id,
        )
        if scope is None:
            # Runs predating this component keep their original tool contract.
            return {}
        return {
            "knowledge_scope": scope,
            "authenticated_owner_scope": self._agent_owner_scope(actor),
        }

    async def _knowledge_scope_for_run(self, run):
        task = await self.storage.get_task(run.task_id)
        if task is None or task.conversation_id != run.conversation_id:
            raise RuntimeError("knowledge_task_identity_mismatch")
        conversation = await self.storage.get_conversation(run.conversation_id)
        if conversation is None:
            return None
        values = await self._knowledge_scope_metadata(
            actor=conversation.username, conversation_id=run.conversation_id,
            root_message_id=task.root_message_id,
        )
        scope = values.get("knowledge_scope")
        if scope is None:
            return None
        return {**scope, "authenticated_owner_scope": values["authenticated_owner_scope"]}

    async def _with_knowledge_scope(self, request):
        if getattr(self, "knowledge_repository", None) is None:
            return request
        conversation = await self.storage.get_conversation(request.conversation_id)
        metadata = dict(request.metadata)
        metadata.pop("knowledge_scope", None)
        metadata.pop("authenticated_owner_scope", None)
        if conversation is not None:
            if self._agent_owner_scope(conversation.username) != request.owner_scope:
                raise RuntimeError("knowledge_request_owner_mismatch")
            metadata.update(await self._knowledge_scope_metadata(
                actor=conversation.username, conversation_id=request.conversation_id,
                root_message_id=request.root_message_id,
            ))
        return replace(request, metadata=metadata)
