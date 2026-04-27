from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from sqlalchemy import Engine

from src.capabilities.main_agent import (
    MAIN_AGENT_CAPABILITY_DESCRIPTORS,
    MainAgentExecutor,
    MainAgentWorkflowProvider,
    StreamGenerator,
    build_local_main_agent_instance,
)
from src.capabilities.sql_query import (
    SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS,
    SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS,
    SQLQueryExecutor,
    SQLQueryWorkflowProvider,
    build_local_sql_query_instance,
)
from src.core.enums import EventVisibility, MessageRole, TaskStatus
from src.core.models import Conversation, EventRecord, InterruptAnswer, Message, Task
from src.integrations.audit_logger import JsonlAuditSink
from src.integrations.codex_skills import SkillCatalog
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from src.lifecycle.cancellation_service import CancellationService
from src.lifecycle.conversation_guard import ConversationSerialGuard
from src.lifecycle.interrupt_service import InterruptService
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.models import OrchestrationRequest
from src.orchestration.composite_executor import CompositeExecutor
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from src.orchestration.workflow_router import WorkflowRouter
from src.storage.sqlite import SQLiteStorage, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory

from .dto import SubmitMessageRequest
from .sse import InMemoryEventBroker, is_frontend_event


class ApiRuntime:
    def __init__(
        self,
        *,
        engine: Engine,
        storage: SQLiteStorage,
        capability_registry: CapabilityRegistry,
        instance_registry: InstanceRegistry,
        event_broker: InMemoryEventBroker,
        cancellation_service: CancellationService,
        interrupt_service: InterruptService,
        orchestration_service: OrchestrationService,
        workflow_provider: WorkflowRouter,
        mysql_adapter: MySQLReadonlyAdapter | None = None,
    ) -> None:
        self._engine = engine
        self.storage = storage
        self.capability_registry = capability_registry
        self.instance_registry = instance_registry
        self.event_broker = event_broker
        self.cancellation_service = cancellation_service
        self.interrupt_service = interrupt_service
        self.orchestration_service = orchestration_service
        self.workflow_provider = workflow_provider
        self._mysql_adapter = mysql_adapter
        self._conversation_guard = ConversationSerialGuard(storage)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _utcnow_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _make_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:12]}"

    def _make_event(
        self,
        *,
        task_id: str,
        conversation_id: str,
        event_type: str,
        node_id: str | None = None,
        payload: dict | None = None,
        visibility: EventVisibility = EventVisibility.FRONTEND,
        created_at: datetime | None = None,
    ) -> EventRecord:
        return EventRecord(
            event_id=self._make_id("evt"),
            conversation_id=conversation_id,
            task_id=task_id,
            node_id=node_id,
            event_type=event_type,
            payload=payload or {},
            visibility=visibility,
            created_at=created_at or self._utcnow_naive(),
        )

    async def _record_event(self, event: EventRecord) -> None:
        await self.storage.append_event(event)
        await self.event_broker.publish(event)

    async def submit_message(self, conversation_id: str, request: SubmitMessageRequest) -> tuple[Message, Task]:
        await self._conversation_guard.ensure_conversation_available(conversation_id)
        self._ensure_supported_capability(request.capability_id)

        now = self._utcnow_naive()
        message_id = request.client_message_id or self._make_id("msg")
        task_id = self._make_id("task")

        conversation = await self.storage.get_conversation(conversation_id)
        if conversation is None:
            conversation = Conversation(
                conversation_id=conversation_id,
                account_id=request.account_id,
                current_task_id=task_id,
                created_at=now,
                updated_at=now,
            )
        else:
            conversation = replace(conversation, account_id=request.account_id, current_task_id=task_id, updated_at=now)
        await self.storage.save_conversation(conversation)

        message = Message(
            message_id=message_id,
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=request.content,
            task_id=task_id,
            created_at=now,
        )
        await self.storage.save_message(message)

        task = Task(
            task_id=task_id,
            conversation_id=conversation_id,
            root_message_id=message_id,
            status=TaskStatus.ACCEPTED,
            requested_capability_id=request.capability_id,
            summary=request.content,
            created_at=now,
            updated_at=now,
        )
        await self.storage.save_task(task)
        await self._record_event(
            self._make_event(
                task_id=task_id,
                conversation_id=conversation_id,
                event_type="task.accepted",
                payload={"message_id": message_id, "status": str(task.status)},
                created_at=now,
            )
        )

        orchestration_request = OrchestrationRequest(
            task_id=task_id,
            conversation_id=conversation_id,
            root_message_id=message_id,
            user_message=request.content,
            requested_capability_id=request.capability_id,
            metadata=dict(request.metadata),
        )
        await self._schedule_execution(orchestration_request)
        return message, task

    async def _schedule_execution(self, request: OrchestrationRequest) -> None:
        async with self._lock:
            active_task_count = len(self._running_tasks)
            handle = asyncio.create_task(self._run_execution(request, active_task_count=active_task_count))
            self._running_tasks[request.task_id] = handle

    async def _run_execution(self, request: OrchestrationRequest, *, active_task_count: int) -> None:
        try:
            plan = self.workflow_provider.build_plan(request)
            await self.orchestration_service.execute_request(request, plan, active_task_count=active_task_count)
        except Exception as exc:
            await self._mark_task_failed(request, exc)
        finally:
            async with self._lock:
                self._running_tasks.pop(request.task_id, None)
            await self._clear_conversation_current_task(request.conversation_id, request.task_id)

    async def _mark_task_failed(self, request: OrchestrationRequest, exc: Exception) -> None:
        task = await self.storage.get_task(request.task_id)
        if task is None or task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            return
        failed = replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive())
        await self.storage.save_task(failed)
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="task.failed",
                payload={"code": "execution_crash", "message": str(exc)},
            )
        )

    async def _clear_conversation_current_task(self, conversation_id: str, task_id: str) -> None:
        conversation = await self.storage.get_conversation(conversation_id)
        task = await self.storage.get_task(task_id)
        if conversation is None or task is None:
            return
        if conversation.current_task_id != task_id:
            return
        if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return
        await self.storage.save_conversation(
            replace(conversation, current_task_id=None, updated_at=self._utcnow_naive())
        )

    async def cancel_task(self, task_id: str) -> Task:
        task = await self.cancellation_service.cancel_task_context(task_id)
        await self._clear_conversation_current_task(task.conversation_id, task.task_id)
        return task

    async def list_interrupts(self, task_id: str) -> list[dict[str, object]]:
        interrupts = await self.storage.list_interrupts_for_task(task_id)
        return [
            {
                "interrupt_id": interrupt.interrupt_id,
                "conversation_id": interrupt.conversation_id,
                "task_id": interrupt.task_id,
                "node_id": interrupt.node_id,
                "question": interrupt.question,
                "reason_code": interrupt.reason_code,
                "required_fields": dict(interrupt.required_fields),
                "status": str(interrupt.status),
            }
            for interrupt in interrupts
        ]

    async def answer_interrupt(self, task_id: str, interrupt_id: str, answer_payload: dict[str, object]) -> dict[str, object]:
        task = await self.storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        interrupt = await self.storage.get_interrupt(interrupt_id)
        if interrupt is None or interrupt.task_id != task_id:
            raise ValueError(f"Unknown interrupt: {interrupt_id}")

        answer = InterruptAnswer(
            interrupt_answer_id=self._make_id("interrupt-answer"),
            interrupt_id=interrupt_id,
            answer_payload=dict(answer_payload),
            source_message_id=self._make_id("msg"),
            created_at=self._utcnow_naive(),
        )
        saved_interrupt = await self.interrupt_service.record_answer(answer)

        answer_message = Message(
            message_id=answer.source_message_id or self._make_id("msg"),
            conversation_id=task.conversation_id,
            role=MessageRole.USER,
            content=self._format_answer_message(answer_payload),
            task_id=task.task_id,
            created_at=self._utcnow_naive(),
        )
        await self.storage.save_message(answer_message)
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=interrupt.node_id,
                event_type="task.interrupt_answered",
                payload={"interrupt_id": interrupt_id, "answer_payload": dict(answer_payload)},
            )
        )

        root_message = await self.storage.get_message(task.root_message_id)
        combined_message = self._combine_resume_message(root_message.content if root_message is not None else task.summary or "", answer_payload)
        await self._await_existing_execution(task.task_id)
        await self._schedule_execution(
            OrchestrationRequest(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                user_message=combined_message,
                requested_capability_id=task.requested_capability_id,
            )
        )
        return {
            "interrupt_id": saved_interrupt.interrupt_id,
            "status": str(saved_interrupt.status),
            "node_id": saved_interrupt.node_id,
            "answer_payload": dict(answer_payload),
        }

    async def iter_frontend_events(self, task_id: str):
        task = await self.storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")

        history = await self.storage.list_events_for_task(task_id)
        for event in history:
            if is_frontend_event(event):
                yield event

        latest_task = await self.storage.get_task(task_id)
        if latest_task is not None and latest_task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return

        subscription = self.event_broker.subscribe(task_id)
        try:
            while True:
                event = await subscription.get()
                if is_frontend_event(event):
                    yield event
                if event.event_type in {"task.completed", "task.failed", "task.cancelled"}:
                    return
        finally:
            subscription.close()

    async def shutdown(self) -> None:
        pending = list(self._running_tasks.values())
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2)
            except asyncio.TimeoutError:
                for handle in pending:
                    handle.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._mysql_adapter is not None:
            await self._mysql_adapter.aclose()
        await asyncio.to_thread(self._engine.dispose)

    def _ensure_supported_capability(self, capability_id: str | None) -> None:
        if capability_id is None:
            return
        if capability_id in {"main_agent", "sql_query"}:
            return
        descriptor = self.capability_registry.get(capability_id)
        if descriptor is not None and descriptor.public:
            return
        raise ValueError(f"Unsupported capability_id: {capability_id}")

    @staticmethod
    def _format_answer_message(answer_payload: dict[str, object]) -> str:
        return "；".join(f"{key}={value}" for key, value in answer_payload.items())

    @classmethod
    def _combine_resume_message(cls, root_content: str, answer_payload: dict[str, object]) -> str:
        answer_text = cls._format_answer_message(answer_payload)
        if not answer_text:
            return root_content
        return f"{root_content}\n补充信息：{answer_text}"

    async def _await_existing_execution(self, task_id: str) -> None:
        async with self._lock:
            handle = self._running_tasks.get(task_id)
        if handle is None:
            return
        await asyncio.gather(handle, return_exceptions=True)
        async with self._lock:
            if self._running_tasks.get(task_id) is handle:
                self._running_tasks.pop(task_id, None)


def build_api_runtime(
    *,
    database_path: str | Path,
    audit_log_path: str | Path,
    mysql_adapter: MySQLReadonlyAdapter | None = None,
    sql_generator=None,
    summarizer=None,
    llm_text_generator=None,
    main_agent_stream_generator: StreamGenerator | None = None,
    skill_roots: Iterable[str | Path] | None = None,
    skill_catalog: SkillCatalog | None = None,
) -> ApiRuntime:
    engine = create_sqlite_engine(database_path)
    bootstrap_sqlite_database(engine)
    storage = SQLiteStorage(create_sqlite_session_factory(engine))

    capability_registry = CapabilityRegistry()
    for descriptor in MAIN_AGENT_CAPABILITY_DESCRIPTORS:
        capability_registry.register(descriptor)
    for descriptor in SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS:
        capability_registry.register(descriptor)
    for descriptor in SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS:
        capability_registry.register(descriptor)

    instance_registry = InstanceRegistry()
    instance_registry.register(build_local_main_agent_instance())
    instance_registry.register(build_local_sql_query_instance())

    audit_sink = JsonlAuditSink(audit_log_path)
    event_broker = InMemoryEventBroker(audit_sink=audit_sink)

    async def record_live_event(event: EventRecord) -> None:
        await storage.append_event(event)
        await event_broker.publish(event)

    resolved_skill_catalog = skill_catalog
    if resolved_skill_catalog is None:
        roots = tuple(skill_roots) if skill_roots is not None else _default_skill_roots()
        resolved_skill_catalog = SkillCatalog.from_roots(roots)

    resolved_mysql_adapter = mysql_adapter or MySQLReadonlyAdapter()

    cancellation_service = CancellationService(storage, event_sink=event_broker, audit_sink=audit_sink)
    interrupt_service = InterruptService(storage, event_sink=event_broker, audit_sink=audit_sink)
    orchestration_service = OrchestrationService(
        storage=storage,
        capability_registry=capability_registry,
        instance_registry=instance_registry,
        scheduler=Scheduler(instance_registry),
        executor=CompositeExecutor(
            [
                MainAgentExecutor(
                    stream_generator=main_agent_stream_generator,
                    skill_catalog=resolved_skill_catalog,
                    live_event_recorder=record_live_event,
                ),
                SQLQueryExecutor(
                    mysql_adapter=resolved_mysql_adapter,
                    sql_generator=sql_generator,
                    summarizer=summarizer,
                    llm_text_generator=llm_text_generator,
                ),
            ]
        ),
        completion_policy=CompletionPolicy(),
        backpressure=BackpressureGuard(max_active_tasks=10),
        event_sink=event_broker,
    )
    return ApiRuntime(
        engine=engine,
        storage=storage,
        capability_registry=capability_registry,
        instance_registry=instance_registry,
        event_broker=event_broker,
        cancellation_service=cancellation_service,
        interrupt_service=interrupt_service,
        orchestration_service=orchestration_service,
        workflow_provider=WorkflowRouter(
            default_provider=MainAgentWorkflowProvider(),
            main_agent_provider=MainAgentWorkflowProvider(),
            sql_query_provider=SQLQueryWorkflowProvider(),
        ),
        mysql_adapter=resolved_mysql_adapter,
    )


def _default_skill_roots() -> tuple[Path, ...]:
    return (
        Path.cwd() / ".codex" / "skills",
        Path.home() / ".codex" / "skills",
    )
