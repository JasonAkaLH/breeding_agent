from __future__ import annotations

from src.core.contracts import StoragePort

from .errors import ConversationBusyError


class ConversationSerialGuard:
    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    async def ensure_conversation_available(self, conversation_id: str) -> None:
        active_task = await self._storage.get_active_task_for_conversation(conversation_id)
        if active_task is not None:
            raise ConversationBusyError(
                f"Conversation {conversation_id} already has active task {active_task.task_id} with status {active_task.status}."
            )
        return None
