from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING

from src.core.enums import MessageRole
from src.core.models import Interrupt, Message

if TYPE_CHECKING:
    from src.core.contracts import StoragePort


INTERRUPT_VISIBLE_STREAM_STATUS = "interrupt_visible"


def interrupt_visible_message_id(interrupt: Interrupt, content: str | None = None) -> str:
    """Return a stable message id for frontend-visible interrupt text."""

    normalized_content = (content if content is not None else interrupt.question).strip()
    digest = hashlib.sha256(
        f"{interrupt.interrupt_id}\n{normalized_content}".encode("utf-8")
    ).hexdigest()[:24]
    return f"msg-interrupt-{digest}"


async def persist_interrupt_question_message(
    storage: "StoragePort",
    interrupt: Interrupt,
    *,
    created_at: datetime | None = None,
) -> Message | None:
    """Persist the user-visible interrupt question as assistant chat history.

    Interrupt lifecycle state remains in the interrupt table. The message row only
    stores content that was shown to the frontend so history restore and
    conversation memory see the same assistant text as the live UI.
    """

    content = interrupt.question.strip()
    if not content:
        return None

    message_id = interrupt_visible_message_id(interrupt, content)
    existing = await storage.get_message(message_id)
    if existing is not None:
        return existing

    message = Message(
        message_id=message_id,
        conversation_id=interrupt.conversation_id,
        role=MessageRole.ASSISTANT,
        content=content,
        task_id=interrupt.task_id,
        stream_status=INTERRUPT_VISIBLE_STREAM_STATUS,
        created_at=created_at or interrupt.created_at,
    )
    try:
        return await storage.save_message(message)
    except Exception:
        existing_after_race = await storage.get_message(message_id)
        if existing_after_race is not None:
            return existing_after_race
        raise
