from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import Engine

from src.capabilities.main_agent import (
    MAIN_AGENT_CAPABILITY_DESCRIPTORS,
    MAIN_AGENT_PLANNER_PAYLOAD_POLICIES,
    MainAgentExecutor,
    MainAgentRuntimeReplanner,
    MainAgentWorkflowProvider,
    StreamGenerator,
    build_local_main_agent_instance,
)
from src.capabilities.sql_query import (
    SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS,
    SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS,
    SQL_QUERY_PUBLIC_PLANNER_PAYLOAD_POLICIES,
    SQLQueryExecutor,
    SQLQueryRuntimeReplanner,
    SQLQueryWorkflowProvider,
    build_local_sql_query_instance,
)
from src.core.enums import EventVisibility, MessageRole, TaskStatus
from src.core.models import Conversation, EventRecord, InterruptAnswer, Message, Task
from src.integrations.audit_logger import JsonlAuditSink
from src.integrations.codex_skills import SkillCatalog
from src.integrations.llm_client import DEFAULT_CONFIG_PATH, LLMClient, ReasoningEffort, bootstrap_config_env, load_config
from src.integrations.llm_runtime import SharedLLMRuntime
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from src.lifecycle.cancellation_service import CancellationService
from src.lifecycle.conversation_guard import ConversationSerialGuard
from src.lifecycle.interrupt_service import InterruptService
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.llm_workflow_provider import LLMWorkflowProvider
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, WorkflowPlan
from src.orchestration.planner_contract import TextGenerator as PlannerTextGenerator
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy
from src.orchestration.composite_executor import CompositeExecutor
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.runtime_replanner import CompositeRuntimeReplanner, RuntimeReplanner
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
            plan_result = self.workflow_provider.build_plan(request)
            plan = await plan_result if inspect.isawaitable(plan_result) else plan_result
            await self._record_plan_built(request, plan)
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

    async def _record_plan_built(self, request: OrchestrationRequest, plan: WorkflowPlan) -> None:
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="workflow.plan_built",
                payload={
                    "node_count": len(plan.nodes),
                    "metadata": self._json_safe_mapping(plan.metadata),
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )

    @staticmethod
    def _json_safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))

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
    llm_text_generator=None,
    sql_query_llm_config: Mapping[str, Any] | None = None,
    sql_query_llm_config_path: str | Path | None = None,
    sql_query_llm_client_factory: Callable[..., Any] | None = None,
    sql_query_reasoning_effort: ReasoningEffort = "minimal",
    sql_query_trim_max_tokens: int | None = None,
    enable_sql_query_llm: bool = True,
    planner_text_generator: PlannerTextGenerator | None = None,
    planner_llm_config: Mapping[str, Any] | None = None,
    planner_llm_config_path: str | Path | None = None,
    planner_llm_client_factory: Callable[..., Any] | None = None,
    planner_reasoning_effort: ReasoningEffort = "minimal",
    enable_llm_planner: bool = True,
    planner_payload_policies: Mapping[str, CapabilityPayloadPolicy] | None = None,
    main_agent_stream_generator: StreamGenerator | None = None,
    main_agent_llm_config: Mapping[str, Any] | None = None,
    main_agent_llm_config_path: str | Path | None = None,
    main_agent_llm_client_factory: Callable[..., Any] | None = None,
    main_agent_reasoning_effort: ReasoningEffort = "minimal",
    skill_roots: Iterable[str | Path] | None = None,
    skill_catalog: SkillCatalog | None = None,
    runtime_replanner: RuntimeReplanner | None = None,
) -> ApiRuntime:
    _bootstrap_runtime_config_env(
        llm_text_generator=llm_text_generator,
        sql_query_llm_config=sql_query_llm_config,
        sql_query_llm_config_path=sql_query_llm_config_path,
        sql_query_llm_client_factory=sql_query_llm_client_factory,
        enable_sql_query_llm=enable_sql_query_llm,
        planner_text_generator=planner_text_generator,
        planner_llm_config=planner_llm_config,
        planner_llm_config_path=planner_llm_config_path,
        planner_llm_client_factory=planner_llm_client_factory,
        enable_llm_planner=enable_llm_planner,
        main_agent_stream_generator=main_agent_stream_generator,
        main_agent_llm_config=main_agent_llm_config,
        main_agent_llm_config_path=main_agent_llm_config_path,
        main_agent_llm_client_factory=main_agent_llm_client_factory,
    )

    engine = create_sqlite_engine(database_path)
    bootstrap_sqlite_database(engine)
    storage = SQLiteStorage(create_sqlite_session_factory(engine))

    capability_registry = CapabilityRegistry()
    _register_capability_descriptors(
        capability_registry,
        MAIN_AGENT_CAPABILITY_DESCRIPTORS,
        planner_payload_policies=MAIN_AGENT_PLANNER_PAYLOAD_POLICIES,
    )
    _register_capability_descriptors(
        capability_registry,
        SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS,
        planner_payload_policies=SQL_QUERY_PUBLIC_PLANNER_PAYLOAD_POLICIES,
    )
    _register_capability_descriptors(capability_registry, SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS)

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

    main_agent_llm_runtime = _resolve_main_agent_llm_runtime(
        main_agent_llm_config=main_agent_llm_config,
        main_agent_llm_config_path=main_agent_llm_config_path,
        main_agent_llm_client_factory=main_agent_llm_client_factory,
        planner_llm_config=planner_llm_config,
        planner_llm_config_path=planner_llm_config_path,
        planner_llm_client_factory=planner_llm_client_factory,
    )

    resolved_mysql_adapter = mysql_adapter or MySQLReadonlyAdapter()
    resolved_main_agent_stream_generator, main_agent_stream_metadata = _resolve_main_agent_stream_binding(
        main_agent_stream_generator=main_agent_stream_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        main_agent_reasoning_effort=main_agent_reasoning_effort,
    )
    resolved_sql_query_text_generator = _resolve_sql_query_text_generator(
        llm_text_generator=llm_text_generator,
        sql_query_llm_config=sql_query_llm_config,
        sql_query_llm_client_factory=sql_query_llm_client_factory,
        sql_query_reasoning_effort=sql_query_reasoning_effort,
        enable_sql_query_llm=enable_sql_query_llm,
    )
    resolved_sql_query_trim_max_tokens = _resolve_sql_query_trim_max_tokens(
        sql_query_trim_max_tokens=sql_query_trim_max_tokens,
        sql_query_llm_config=sql_query_llm_config,
    )

    main_agent_workflow_provider = MainAgentWorkflowProvider()
    sql_query_workflow_provider = SQLQueryWorkflowProvider()
    macro_providers = {"sql_query.query": sql_query_workflow_provider}

    auto_workflow_provider = AutoWorkflowProvider(
        main_agent_provider=main_agent_workflow_provider,
        macro_providers=macro_providers,
    )
    resolved_planner_text_generator = _resolve_planner_text_generator(
        planner_text_generator=planner_text_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        event_recorder=record_live_event,
        planner_reasoning_effort=planner_reasoning_effort,
        enable_llm_planner=enable_llm_planner,
    )
    default_workflow_provider = LLMWorkflowProvider(
        capability_registry=capability_registry,
        fallback_provider=auto_workflow_provider,
        macro_providers=macro_providers,
        text_generator=resolved_planner_text_generator,
        payload_policies=planner_payload_policies,
    )
    resolved_runtime_replanner = runtime_replanner or CompositeRuntimeReplanner(
        [
            MainAgentRuntimeReplanner(
                capability_registry=capability_registry,
                macro_providers=macro_providers,
                text_generator=resolved_planner_text_generator,
                payload_policies=planner_payload_policies,
            ),
            SQLQueryRuntimeReplanner(macro_providers=macro_providers),
        ]
    )

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
                    stream_generator=resolved_main_agent_stream_generator,
                    stream_metadata=main_agent_stream_metadata,
                    default_reasoning_effort=main_agent_reasoning_effort,
                    skill_catalog=resolved_skill_catalog,
                    live_event_recorder=record_live_event,
                ),
                SQLQueryExecutor(
                    mysql_adapter=resolved_mysql_adapter,
                    sql_generator=sql_generator,
                    llm_text_generator=resolved_sql_query_text_generator,
                    trim_max_tokens=resolved_sql_query_trim_max_tokens,
                ),
            ]
        ),
        completion_policy=CompletionPolicy(),
        backpressure=BackpressureGuard(max_active_tasks=10),
        event_sink=event_broker,
        runtime_replanner=resolved_runtime_replanner,
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
            default_provider=default_workflow_provider,
            main_agent_provider=main_agent_workflow_provider,
            sql_query_provider=sql_query_workflow_provider,
        ),
        mysql_adapter=resolved_mysql_adapter,
    )


def _bootstrap_runtime_config_env(
    *,
    llm_text_generator,
    sql_query_llm_config: Mapping[str, Any] | None,
    sql_query_llm_config_path: str | Path | None,
    sql_query_llm_client_factory: Callable[..., Any] | None,
    enable_sql_query_llm: bool,
    planner_text_generator: PlannerTextGenerator | None,
    planner_llm_config: Mapping[str, Any] | None,
    planner_llm_config_path: str | Path | None,
    planner_llm_client_factory: Callable[..., Any] | None,
    enable_llm_planner: bool,
    main_agent_stream_generator: StreamGenerator | None,
    main_agent_llm_config: Mapping[str, Any] | None,
    main_agent_llm_config_path: str | Path | None,
    main_agent_llm_client_factory: Callable[..., Any] | None,
) -> None:
    explicit_paths: list[str | Path] = []
    should_bootstrap_default = False

    if sql_query_llm_config is None:
        if sql_query_llm_config_path is not None:
            explicit_paths.append(sql_query_llm_config_path)
        elif enable_sql_query_llm and llm_text_generator is None and sql_query_llm_client_factory is None:
            should_bootstrap_default = True

    if planner_llm_config is None:
        if planner_llm_config_path is not None:
            explicit_paths.append(planner_llm_config_path)
        elif enable_llm_planner and planner_text_generator is None and planner_llm_client_factory is None:
            should_bootstrap_default = True

    if main_agent_stream_generator is None and main_agent_llm_config is None:
        if main_agent_llm_config_path is not None:
            explicit_paths.append(main_agent_llm_config_path)
        elif main_agent_llm_client_factory is None:
            should_bootstrap_default = True

    seen_paths: set[Path] = set()
    for config_path in explicit_paths:
        resolved_path = Path(config_path).resolve()
        if resolved_path in seen_paths:
            continue
        seen_paths.add(resolved_path)

    if len(seen_paths) > 1:
        raise ValueError(
            "LLM config path values must resolve to the same startup config file; "
            "use explicit config dictionaries or client factories for component-specific provider settings."
        )

    for resolved_path in seen_paths:
        bootstrap_config_env(resolved_path, override=True)

    if explicit_paths or not should_bootstrap_default:
        return
    bootstrap_config_env(DEFAULT_CONFIG_PATH, strict=False)


def _resolve_main_agent_llm_runtime(
    *,
    main_agent_llm_config: Mapping[str, Any] | None,
    main_agent_llm_config_path: str | Path | None,
    main_agent_llm_client_factory: Callable[..., Any] | None,
    planner_llm_config: Mapping[str, Any] | None,
    planner_llm_config_path: str | Path | None,
    planner_llm_client_factory: Callable[..., Any] | None,
) -> SharedLLMRuntime:
    config = main_agent_llm_config or planner_llm_config
    factory = main_agent_llm_client_factory or planner_llm_client_factory or LLMClient
    if main_agent_llm_config is not None:
        config_source = "injected_config"
    elif planner_llm_config is not None:
        config_source = "injected_config"
    elif main_agent_llm_config_path is not None or planner_llm_config_path is not None:
        config_source = "environment"
    elif main_agent_llm_client_factory is not None:
        config_source = "main_agent_factory_default"
    elif planner_llm_client_factory is not None:
        config_source = "planner_factory_default"
    else:
        config_source = "environment"
    return SharedLLMRuntime(client_factory=factory, config=config, config_source=config_source)


def _resolve_sql_query_text_generator(
    *,
    llm_text_generator,
    sql_query_llm_config: Mapping[str, Any] | None,
    sql_query_llm_client_factory: Callable[..., Any] | None,
    sql_query_reasoning_effort: ReasoningEffort,
    enable_sql_query_llm: bool,
):
    if llm_text_generator is not None:
        return llm_text_generator
    if not enable_sql_query_llm:
        return None

    config_source = (
        "sql_query_explicit_override"
        if sql_query_llm_config is not None or sql_query_llm_client_factory is not None
        else "environment"
    )
    sql_query_runtime = SharedLLMRuntime(
        client_factory=sql_query_llm_client_factory or LLMClient,
        config=sql_query_llm_config,
        config_source=config_source,
    )

    async def generate(prompt: str) -> str:
        return await sql_query_runtime.generate_text(
            prompt,
            thinking=False,
            reasoning_effort=sql_query_reasoning_effort,
        )

    return generate


def _resolve_sql_query_trim_max_tokens(
    *,
    sql_query_trim_max_tokens: int | None,
    sql_query_llm_config: Mapping[str, Any] | None,
) -> int | None:
    if sql_query_trim_max_tokens is not None:
        return _coerce_nonnegative_int(sql_query_trim_max_tokens)

    raw_value: Any = None
    if sql_query_llm_config is not None:
        raw_value = sql_query_llm_config.get("trim_max_tokens")
    else:
        raw_value = load_config().get("trim_max_tokens")
    return _coerce_nonnegative_int(raw_value)


def _coerce_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _register_capability_descriptors(
    capability_registry: CapabilityRegistry,
    descriptors: Iterable[CapabilityDescriptor],
    *,
    planner_payload_policies: Mapping[str, CapabilityPayloadPolicy] | None = None,
) -> None:
    policies = dict(planner_payload_policies or {})
    for descriptor in descriptors:
        capability_registry.register(
            descriptor,
            planner_payload_policy=policies.get(descriptor.capability_id),
        )


def _resolve_planner_text_generator(
    *,
    planner_text_generator: PlannerTextGenerator | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    event_recorder: Callable[[EventRecord], Any],
    planner_reasoning_effort: ReasoningEffort,
    enable_llm_planner: bool,
) -> PlannerTextGenerator | None:
    if planner_text_generator is not None:
        return planner_text_generator
    if not enable_llm_planner:
        return None

    call_ordinal = 0

    async def generate(prompt: str, *, request: OrchestrationRequest | None = None, stage: str = "orchestration_plan") -> str:
        nonlocal call_ordinal
        call_ordinal += 1
        call_id = call_ordinal
        reasoning_ordinal = 0

        async def record_reasoning(delta: str) -> None:
            nonlocal reasoning_ordinal
            if request is None:
                return
            reasoning_ordinal += 1
            maybe_result = event_recorder(
                EventRecord(
                    event_id=f"{request.task_id}:main_agent.{stage}.reasoning:{call_id}:{reasoning_ordinal}",
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id="main_agent.orchestrator",
                    event_type="main_agent.reasoning_delta",
                    payload={
                        "delta": delta,
                        "ordinal": reasoning_ordinal,
                        "call_id": call_id,
                        "stage": stage,
                        "llm_runtime_id": main_agent_llm_runtime.runtime_id,
                    },
                    visibility=EventVisibility.FRONTEND,
                )
            )
            if inspect.isawaitable(maybe_result):
                await maybe_result

        metadata = dict(request.metadata) if request is not None else {}
        thinking = _resolve_request_thinking_enabled(metadata)
        reasoning_effort = _resolve_request_reasoning_effort(metadata, fallback=planner_reasoning_effort)
        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            on_reasoning_delta=record_reasoning,
        )

    return generate


def _resolve_request_reasoning_effort(metadata: Mapping[str, Any], *, fallback: ReasoningEffort) -> ReasoningEffort:
    explicit = metadata.get("main_agent_reasoning_effort")
    if isinstance(explicit, str) and explicit in {"minimal", "low", "medium", "high"}:
        return explicit  # type: ignore[return-value]
    return fallback


def _resolve_request_thinking_enabled(metadata: Mapping[str, Any]) -> bool:
    if "main_agent_thinking_enabled" in metadata:
        return _is_truthy(metadata.get("main_agent_thinking_enabled"))
    return _is_truthy(metadata.get("deep_thinking"))


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _resolve_main_agent_stream_binding(
    *,
    main_agent_stream_generator: StreamGenerator | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    main_agent_reasoning_effort: ReasoningEffort,
) -> tuple[StreamGenerator | None, dict[str, Any]]:
    if main_agent_stream_generator is not None:
        return main_agent_stream_generator, {
            "provider": "injected_stream",
            "config_source": "injected_stream",
            "reasoning_effort": main_agent_reasoning_effort,
        }

    return main_agent_llm_runtime.stream_events, main_agent_llm_runtime.static_metadata(
        reasoning_effort=main_agent_reasoning_effort
    )


def _default_skill_roots() -> tuple[Path, ...]:
    return (
        Path.cwd() / ".codex" / "skills",
        Path.home() / ".codex" / "skills",
    )
