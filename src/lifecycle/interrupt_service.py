from __future__ import annotations

from datetime import datetime, timezone

from src.core.contracts import AuditSink, EventSink, StoragePort
from src.core.models import Interrupt, InterruptAnswer, TaskNode

from . import task_state_machine


class InterruptService:
    def __init__(
        self,
        storage: StoragePort,
        *,
        event_sink: EventSink | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._storage = storage
        self._event_sink = event_sink
        self._audit_sink = audit_sink

    @staticmethod
    def _utcnow_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    async def open_interrupt(self, interrupt: Interrupt, *, now: datetime | None = None) -> Interrupt:
        node = await self._storage.get_task_node(interrupt.node_id)
        if node is None:
            raise ValueError(f"Unknown node for interrupt: {interrupt.node_id}")
        updated_interrupt, updated_node = task_state_machine.open_interrupt(interrupt, node, now=now or self._utcnow_naive())
        await self._storage.save_task_node(updated_node)
        saved_interrupt = await self._storage.save_interrupt(updated_interrupt)
        if self._audit_sink is not None:
            await self._audit_sink.record(
                "lifecycle.interrupt_opened",
                {"interrupt_id": saved_interrupt.interrupt_id, "node_id": updated_node.node_id},
                conversation_id=saved_interrupt.conversation_id,
                task_id=saved_interrupt.task_id,
                node_id=saved_interrupt.node_id,
            )
        return saved_interrupt

    async def record_answer(self, answer: InterruptAnswer, *, now: datetime | None = None) -> Interrupt:
        interrupt = await self._storage.get_interrupt(answer.interrupt_id)
        if interrupt is None:
            raise ValueError(f"Unknown interrupt: {answer.interrupt_id}")
        node = await self._storage.get_task_node(interrupt.node_id)
        if node is None:
            raise ValueError(f"Unknown node for interrupt: {interrupt.node_id}")
        updated_interrupt, accepted_answer, updated_node = task_state_machine.answer_interrupt(
            interrupt,
            answer,
            node,
            now=now or self._utcnow_naive(),
        )
        await self._storage.save_interrupt_answer(accepted_answer)
        await self._storage.save_task_node(updated_node)
        saved_interrupt = await self._storage.save_interrupt(updated_interrupt)
        return saved_interrupt

    async def begin_resume(self, resume_token: str) -> TaskNode:
        checkpoint = await self._storage.get_checkpoint_by_resume_token(resume_token)
        if checkpoint is None:
            raise ValueError(f"Unknown resume token: {resume_token}")
        node = await self._storage.get_task_node(checkpoint.node_id)
        if node is None:
            raise ValueError(f"Unknown node for checkpoint: {checkpoint.node_id}")
        resumed = task_state_machine.begin_resume(node)
        return await self._storage.save_task_node(resumed)
