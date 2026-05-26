from __future__ import annotations

from dataclasses import dataclass, field

from src.core.models import Conversation, Message, Task

READ_SQL_BY_OPERATION = {
    "get_conversation": "SELECT * FROM conversation WHERE conversation_id = :conversation_id",
    "list_messages_for_conversation": "SELECT * FROM message WHERE conversation_id = :conversation_id ORDER BY created_at, message_id",
    "get_task": "SELECT * FROM task WHERE task_id = :task_id",
}


@dataclass(slots=True)
class InMemoryPostgresReadStore:
    conversations: dict[str, Conversation] = field(default_factory=dict)
    messages: dict[str, list[Message]] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.conversations.get(conversation_id)

    async def list_messages_for_conversation(self, conversation_id: str) -> list[Message]:
        return list(self.messages.get(conversation_id, ()))

    async def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)
