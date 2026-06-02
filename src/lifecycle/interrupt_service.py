from __future__ import annotations

from datetime import datetime, timezone

from src.core.contracts import AuditSink, EventSink, StoragePort
from src.core.enums import EventVisibility
from src.core.models import EventRecord, Interrupt, InterruptAnswer, TaskNode

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

    async def _record_event(self, event: EventRecord) -> None:
        await self._storage.append_event(event)
        if self._event_sink is not None:
            await self._event_sink.publish(event)

    def _make_event(
        self,
        *,
        task_id: str,
        conversation_id: str,
        event_type: str,
        node_id: str | None = None,
        payload: dict | None = None,
        visibility: EventVisibility = EventVisibility.FRONTEND,
        now: datetime | None = None,
    ) -> EventRecord:
        event_time = now or self._utcnow_naive()
        suffix = int(event_time.timestamp() * 1_000_000)
        node_part = node_id or "task"
        return EventRecord(
            event_id=f"evt-{task_id}-{event_type}-{node_part}-{suffix}",
            conversation_id=conversation_id,
            task_id=task_id,
            node_id=node_id,
            event_type=event_type,
            payload=payload or {},
            visibility=visibility,
            created_at=event_time,
        )

    @staticmethod
    def _skill_name_from_agent(agent_id: str | None) -> str | None:
        if not isinstance(agent_id, str) or not agent_id.startswith("skill."):
            return None
        skill_name = agent_id.removeprefix("skill.").strip()
        return skill_name or None

    @classmethod
    def _skill_payload_from_agent(cls, agent_id: str | None) -> dict[str, str]:
        skill_name = cls._skill_name_from_agent(agent_id)
        return {"skill_name": skill_name} if skill_name is not None else {}

    async def open_interrupt(self, interrupt: Interrupt, *, now: datetime | None = None) -> Interrupt:
        node = await self._storage.get_task_node(interrupt.node_id)
        if node is None:
            raise ValueError(f"Unknown node for interrupt: {interrupt.node_id}")
        current_time = now or self._utcnow_naive()
        updated_interrupt, updated_node = task_state_machine.open_interrupt(interrupt, node, now=current_time)
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
        current_time = now or self._utcnow_naive()
        updated_interrupt, accepted_answer, updated_node = task_state_machine.answer_interrupt(
            interrupt,
            answer,
            node,
            now=current_time,
        )
        await self._storage.save_interrupt_answer(accepted_answer)
        await self._storage.save_task_node(updated_node)
        saved_interrupt = await self._storage.save_interrupt(updated_interrupt)
        await self._record_event(
            self._make_event(
                task_id=saved_interrupt.task_id,
                conversation_id=saved_interrupt.conversation_id,
                node_id=saved_interrupt.node_id,
                event_type="node.ready_to_resume",
                payload={
                    "interrupt_id": saved_interrupt.interrupt_id,
                    "status": str(updated_node.status),
                    "capability_id": updated_node.capability_id,
                    **self._skill_payload_from_agent(saved_interrupt.source_agent),
                },
                now=current_time,
            )
        )
        if self._audit_sink is not None:
            await self._audit_sink.record(
                "lifecycle.interrupt_answered",
                {"interrupt_id": saved_interrupt.interrupt_id, "node_id": updated_node.node_id},
                conversation_id=saved_interrupt.conversation_id,
                task_id=saved_interrupt.task_id,
                node_id=saved_interrupt.node_id,
            )
        return saved_interrupt

    async def begin_resume(self, resume_token: str) -> TaskNode:
        checkpoint = await self._storage.get_checkpoint_by_resume_token(resume_token)
        if checkpoint is None:
            raise ValueError(f"Unknown resume token: {resume_token}")
        node = await self._storage.get_task_node(checkpoint.node_id)
        if node is None:
            raise ValueError(f"Unknown node for checkpoint: {checkpoint.node_id}")
        task = await self._storage.get_task(node.task_id)
        if task is None:
            raise ValueError(f"Unknown task for node: {node.task_id}")
        current_time = self._utcnow_naive()
        resumed = task_state_machine.begin_resume(node)
        saved = await self._storage.save_task_node(resumed)
        await self._record_event(
            self._make_event(
                task_id=saved.task_id,
                conversation_id=task.conversation_id,
                node_id=saved.node_id,
                event_type="node.resuming",
                payload={
                    "status": str(saved.status),
                    "capability_id": saved.capability_id,
                    **self._skill_payload_from_agent(checkpoint.agent_id),
                },
                now=current_time,
            )
        )
        return saved
