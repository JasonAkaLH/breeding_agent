from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import Engine

from src.auth import (
    CaptchaService,
    DuplicateUsernameError,
    PasswordHasher,
    SessionService,
    normalize_username,
    validate_password_policy,
    validate_username,
)
from src.capabilities.main_agent import (
    MAIN_AGENT_CAPABILITY_DESCRIPTORS,
    MAIN_AGENT_PLANNER_PAYLOAD_POLICIES,
    MainAgentExecutor,
    MainAgentRuntimeReplanner,
    MainAgentWorkflowProvider,
    SkillOutputArtifactManager,
    StreamGenerator,
    build_local_main_agent_instance,
)
from src.capabilities.mcp_tool import MCPToolExecutor, build_local_mcp_tool_instance
from src.capabilities.skill_tool import SkillExecutor, build_local_skill_executor_instance
from src.core.enums import EventVisibility, MessageRole, RoutingMode, TaskStatus
from src.core.models import AuthUser, Conversation, EventRecord, InterruptAnswer, Message, Task
from src.integrations.audit_logger import JsonlAuditSink
from src.integrations.codex_skills import (
    SkillCapabilityRegistry,
    SkillCatalog,
    SkillPlatformHandlerRegistry,
    SkillInputTextGenerator,
    SkillServiceRegistry,
    SkillRuntimeRefreshResult,
    SkillRuntimeState,
    SkillScriptRunner,
)
from src.integrations.codex_skills.pyo3_policy import try_load_skill_runtime_pyo3_policy_client
from src.integrations.codex_skills.rust_contract import (
    error_policy as skill_runtime_error_policy,
    load_skill_runtime_contract,
)
from src.integrations.codex_skills.skill_sandbox_client import SkillSandboxGrpcClient
from src.integrations.codex_skills.skill_runtime_gates import validate_skill_runtime_artifact_provenance
from src.integrations.llm_client import DEFAULT_CONFIG_PATH, LLMClient, ReasoningEffort, bootstrap_config_env, load_config
from src.integrations.llm_runtime import SharedLLMRuntime
from src.integrations.mcp import MCPRuntimeBundle, MCPRuntimeConfig, MCPRuntimeRefreshResult, MCPRuntimeState, load_mcp_server_config
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from src.integrations.rust_safety_contract import configure_safety_shadow_sink
from src.lifecycle.cancellation_service import CancellationService
from src.lifecycle.conversation_guard import ConversationSerialGuard
from src.lifecycle.interrupt_service import InterruptService
from src.orchestration.answer_selection import select_final_text_artifact
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.conversation_memory import ConversationMemoryBuilder, ConversationMemoryConfig, ResolutionGenerator
from src.orchestration.llm_workflow_provider import LLMWorkflowProvider, WorkflowPlanningError
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, WorkflowPlan
from src.orchestration.planner_contract import TextGenerator as PlannerTextGenerator
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy
from src.orchestration.composite_executor import CompositeExecutor
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.runtime_replanner import CompositeRuntimeReplanner, RuntimeReplanner
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider
from src.orchestration.workflow_router import WorkflowRouter
from src.storage.rust_contract import error_policy, mode_for_component as runtime_sidecar_mode_for_component
from src.storage.runtime_sidecar_facade import (
    ensure_sidecar_write_allowed,
    validate_runtime_sidecar_artifact_provenance,
    validate_runtime_sidecar_response,
)
from src.storage.runtime_sidecar_grpc_client import RuntimeSidecarGrpcClient
from src.storage.runtime_sidecar_shadow import record_runtime_sidecar_shadow_write_sync
from src.storage.sqlite import SQLiteStorage, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory
from src.storage.artifact_files import LocalArtifactFileStore, parse_file_storage_ref, is_active_skill_output_file

from .conversation_titles import (
    ConversationTitleGenerator,
    build_conversation_title_prompt,
    build_conversation_title_source,
    call_title_generator,
    normalize_generated_conversation_title,
    validate_conversation_title,
)
from .dto import SubmitMessageRequest
from .sse import InMemoryEventBroker, is_frontend_event
from .upload_store import InMemoryUploadStore, UploadedFileRecord, UploadValidationError


UNFINISHED_TASK_STATUSES = {
    TaskStatus.ACCEPTED,
    TaskStatus.PLANNING,
    TaskStatus.RUNNING,
    TaskStatus.CANCELLING,
}


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
        password_hasher: PasswordHasher | None = None,
        captcha_service: CaptchaService | None = None,
        session_service: SessionService | None = None,
        conversation_title_generator: ConversationTitleGenerator | None = None,
        upload_store: InMemoryUploadStore | None = None,
        conversation_memory_builder: ConversationMemoryBuilder | None = None,
        artifact_file_store: LocalArtifactFileStore | None = None,
        audit_sink: JsonlAuditSink | None = None,
        skill_runtime_state: SkillRuntimeState | None = None,
        mcp_runtime_state: MCPRuntimeState | None = None,
        runtime_sidecar_client: Any | None = None,
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
        self.password_hasher = password_hasher or PasswordHasher()
        self.captcha_service = captcha_service
        self.session_service = session_service
        self._conversation_title_generator = conversation_title_generator
        self.upload_store = upload_store or InMemoryUploadStore(now_fn=self._utcnow_naive)
        self._conversation_memory_builder = conversation_memory_builder
        self.artifact_file_store = artifact_file_store or LocalArtifactFileStore(Path("runtime/artifacts"))
        self._audit_sink = audit_sink
        self._skill_runtime_state = skill_runtime_state
        self._mcp_runtime_state = mcp_runtime_state
        self._runtime_sidecar_client = runtime_sidecar_client
        self._runtime_sidecar_shadow_sink = _build_runtime_sidecar_shadow_diff_sink(audit_sink)
        configure_safety_shadow_sink(_build_safety_kernel_shadow_diff_sink(audit_sink))
        self._conversation_guard = ConversationSerialGuard(storage)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._running_title_tasks: set[asyncio.Task[None]] = set()
        self._task_skill_bundle_revisions: dict[str, str] = {}
        self._task_mcp_bundle_revisions: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._skill_refresh_lock = asyncio.Lock()
        self._mcp_refresh_lock = asyncio.Lock()

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
        event = _ensure_event_created_at(event)
        await self.storage.append_event(event)
        await self.event_broker.publish(event)

    async def _iter_event_replay_pages(self, task_id: str):
        after_event_id: str | None = None
        while True:
            page = await self.storage.list_event_page_for_task(task_id, after_event_id=after_event_id)
            if not page:
                return
            for event in page:
                yield event
            after_event_id = page[-1].event_id

    async def create_user(self, username: str, password: str, *, status: str = "active") -> AuthUser:
        username = validate_username(username)
        validate_password_policy(password)
        now = self._utcnow_naive()
        password_hash, password_salt, password_scheme = self.password_hasher.hash_password(password)
        existing = await self.storage.get_auth_user(username)
        user = AuthUser(
            username=username,
            password_hash=password_hash,
            password_salt=password_salt,
            password_scheme=password_scheme,
            status=status,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            last_login_at=existing.last_login_at if existing is not None else None,
        )
        return await self.storage.save_auth_user(user)

    async def register_user(self, username: str, password: str, captcha_id: str, captcha_code: str):
        if self.captcha_service is None or self.session_service is None:
            raise RuntimeError("Authentication services are not configured.")
        username = validate_username(username)
        validate_password_policy(password)
        if await self.storage.get_auth_user(username) is not None:
            raise DuplicateUsernameError(f"Username already exists: {username}")
        if not await self.captcha_service.verify(captcha_id, captcha_code):
            return None
        now = self._utcnow_naive()
        user = await self.create_user(username, password)
        await self.storage.save_auth_user(replace(user, last_login_at=now, updated_at=now))
        return await self.session_service.create_session(username)

    async def create_captcha_challenge(self):
        if self.captcha_service is None:
            raise RuntimeError("Captcha service is not configured.")
        return await self.captcha_service.create_challenge()

    async def login(self, username: str, password: str, captcha_id: str, captcha_code: str):
        if self.captcha_service is None or self.session_service is None:
            raise RuntimeError("Authentication services are not configured.")
        captcha_ok = await self.captcha_service.verify(captcha_id, captcha_code)
        username = normalize_username(username)
        user = await self.storage.get_auth_user(username)
        if user is None or user.status != "active" or not self.password_hasher.verify_password(password, user) or not captcha_ok:
            return None
        now = self._utcnow_naive()
        await self.storage.save_auth_user(replace(user, last_login_at=now, updated_at=now))
        return await self.session_service.create_session(username)

    async def get_session_user(self, session_id: str) -> AuthUser | None:
        if self.session_service is None:
            return None
        return await self.session_service.get_active_user(session_id)

    async def revoke_session(self, session_id: str) -> None:
        if self.session_service is None:
            return
        await self.session_service.revoke_session(session_id)

    async def submit_message(
        self,
        conversation_id: str,
        request: SubmitMessageRequest,
        *,
        authenticated_account_id: str | None = None,
    ) -> tuple[Message, Task]:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if (
            authenticated_account_id is not None
            and existing_conversation is not None
            and existing_conversation.account_id != authenticated_account_id
        ):
            raise PermissionError(f"Conversation does not belong to account: {conversation_id}")
        await self._refresh_skills_for_new_conversation_if_needed(conversation_id, existing_conversation)
        await self._refresh_mcp_for_new_conversation_if_needed(conversation_id, existing_conversation)
        await self._conversation_guard.ensure_conversation_available(conversation_id)
        routing_mode = self._routing_mode(request.routing_mode)
        if routing_mode == RoutingMode.FORCE_CAPABILITY and not request.capability_id:
            raise ValueError("capability_id is required when routing_mode is force_capability")
        requested_capability_id = self._canonical_capability_id(request.capability_id)
        self._ensure_supported_capability(requested_capability_id)

        upload_context = await self.resolve_uploads_for_message(
            conversation_id,
            authenticated_account_id or request.account_id,
            request.metadata.get("upload_ids") or (),
        )
        now = self._utcnow_naive()
        message_id = request.client_message_id or self._make_id("msg")
        task_id = self._make_id("task")
        account_id = authenticated_account_id or request.account_id

        conversation = existing_conversation
        if conversation is None:
            conversation = Conversation(
                conversation_id=conversation_id,
                account_id=account_id,
                current_task_id=task_id,
                created_at=now,
                updated_at=now,
            )
        else:
            if authenticated_account_id is not None and conversation.account_id != authenticated_account_id:
                raise PermissionError(f"Conversation does not belong to account: {conversation_id}")
            conversation = replace(conversation, account_id=account_id, current_task_id=task_id, updated_at=now)
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
        await self._maybe_schedule_conversation_title_generation(conversation_id)

        task = Task(
            task_id=task_id,
            conversation_id=conversation_id,
            root_message_id=message_id,
            status=TaskStatus.ACCEPTED,
            routing_mode=routing_mode,
            requested_capability_id=requested_capability_id,
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

        metadata = dict(request.metadata)
        if request.capability_id != requested_capability_id and request.capability_id is not None:
            metadata["requested_capability_alias"] = request.capability_id
            metadata["canonical_capability_id"] = requested_capability_id
        if self._skill_runtime_state is not None:
            metadata["skill_bundle_revision"] = self._skill_runtime_state.active_revision
        if self._mcp_runtime_state is not None:
            metadata["mcp_bundle_revision"] = self._mcp_runtime_state.active_revision
        if upload_context["uploaded_artifacts"]:
            metadata["uploaded_artifacts"] = [
                *self._metadata_list(metadata.get("uploaded_artifacts")),
                *upload_context["uploaded_artifacts"],
            ]
            metadata["skill_artifacts"] = [
                *self._metadata_list(metadata.get("skill_artifacts")),
                *upload_context["skill_artifacts"],
            ]

        orchestration_request = OrchestrationRequest(
            task_id=task_id,
            conversation_id=conversation_id,
            root_message_id=message_id,
            user_message=request.content,
            requested_capability_id=requested_capability_id,
            metadata=metadata,
        )
        await self._schedule_execution(orchestration_request)
        return message, task

    @staticmethod
    def _routing_mode(value: str | None) -> RoutingMode:
        if not value:
            return RoutingMode.AUTO
        try:
            return RoutingMode(value)
        except ValueError as exc:
            raise ValueError(f"Unsupported routing_mode: {value}") from exc

    @staticmethod
    def _metadata_list(value: Any) -> list[Any]:
        return list(value) if isinstance(value, list | tuple) else []

    async def save_upload(
        self,
        *,
        conversation_id: str,
        account_id: str,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> UploadedFileRecord:
        existing_conversation = await self.ensure_upload_allowed(conversation_id, account_id)
        now = self._utcnow_naive()
        record = self.upload_store.save(
            account_id=account_id,
            conversation_id=conversation_id,
            filename=filename,
            content_type=content_type,
            content=content,
        )
        if existing_conversation is None:
            await self.storage.save_conversation(
                Conversation(
                    conversation_id=conversation_id,
                    account_id=account_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        return record

    async def ensure_upload_allowed(self, conversation_id: str, account_id: str) -> Conversation | None:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.account_id != account_id:
            raise PermissionError(f"Conversation does not belong to account: {conversation_id}")
        return existing_conversation

    async def list_uploads(self, conversation_id: str, account_id: str) -> list[UploadedFileRecord]:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.account_id != account_id:
            raise PermissionError(f"Conversation does not belong to account: {conversation_id}")
        return self.upload_store.list_for_conversation(account_id=account_id, conversation_id=conversation_id)

    async def delete_upload(self, conversation_id: str, account_id: str, upload_id: str) -> bool:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.account_id != account_id:
            raise PermissionError(f"Conversation does not belong to account: {conversation_id}")
        return self.upload_store.delete(upload_id=upload_id, account_id=account_id, conversation_id=conversation_id)

    async def resolve_uploads_for_message(
        self,
        conversation_id: str,
        account_id: str,
        upload_ids,
    ) -> dict[str, Any]:
        if upload_ids is None:
            upload_ids = ()
        if isinstance(upload_ids, str):
            upload_ids = [upload_ids]
        if not isinstance(upload_ids, list | tuple):
            raise UploadValidationError("metadata.upload_ids must be a list")
        uploaded_artifacts: list[dict[str, Any]] = []
        skill_artifacts: list[dict[str, Any]] = []
        missing_upload_ids: list[str] = []
        for upload_id in upload_ids:
            upload_id_text = str(upload_id).strip()
            if not upload_id_text:
                continue
            try:
                record = self.upload_store.get_for_message(
                    upload_id=upload_id_text,
                    account_id=account_id,
                    conversation_id=conversation_id,
                )
            except UploadValidationError:
                missing_upload_ids.append(upload_id_text)
                continue
            uploaded_artifacts.append(record.to_summary())
            skill_artifacts.append(record.to_skill_artifact())
        return {
            "uploaded_artifacts": uploaded_artifacts,
            "skill_artifacts": skill_artifacts,
            "missing_upload_ids": missing_upload_ids,
        }

    async def _maybe_schedule_conversation_title_generation(self, conversation_id: str) -> None:
        if self._conversation_title_generator is None:
            return
        conversation = await self.storage.get_conversation(conversation_id)
        if conversation is None or (conversation.title or "").strip():
            return
        messages = await self.storage.list_messages_for_conversation(conversation_id)
        user_messages = [
            message.content
            for message in messages
            if message.role == MessageRole.USER and message.content.strip()
        ]
        if not user_messages:
            return
        title_source = build_conversation_title_source(user_messages)
        if not title_source:
            return
        expected_user_message_count = len(user_messages)
        task = asyncio.create_task(
            self._generate_and_store_conversation_title(
                conversation_id,
                title_source,
                expected_user_message_count=expected_user_message_count,
            )
        )
        self._running_title_tasks.add(task)
        task.add_done_callback(self._running_title_tasks.discard)

    async def _generate_and_store_conversation_title(
        self,
        conversation_id: str,
        title_source: str,
        *,
        expected_user_message_count: int,
    ) -> None:
        try:
            raw_title = await call_title_generator(self._conversation_title_generator, title_source)
            title = normalize_generated_conversation_title(raw_title)
        except Exception:
            return
        if title is None:
            return
        async with self._lock:
            conversation = await self.storage.get_conversation(conversation_id)
            if conversation is None:
                return
            if (conversation.title or "").strip():
                return
            messages = await self.storage.list_messages_for_conversation(conversation_id)
            current_user_message_count = sum(
                1 for message in messages if message.role == MessageRole.USER and message.content.strip()
            )
            if current_user_message_count != expected_user_message_count:
                return
            await self.storage.save_conversation(
                replace(conversation, title=title, updated_at=self._utcnow_naive())
            )

    async def _schedule_execution(self, request: OrchestrationRequest) -> None:
        self._retain_task_skill_revision(request)
        self._retain_task_mcp_revision(request)
        async with self._lock:
            active_task_count = len(self._running_tasks)
            handle = asyncio.create_task(self._run_execution(request, active_task_count=active_task_count))
            self._running_tasks[request.task_id] = handle

    async def _run_execution(self, request: OrchestrationRequest, *, active_task_count: int) -> None:
        try:
            request = await self._attach_conversation_memory(request)
            plan_result = self.workflow_provider.build_plan(request)
            plan = await plan_result if inspect.isawaitable(plan_result) else plan_result
            await self._record_plan_built(request, plan)
            result = await self.orchestration_service.execute_request(request, plan, active_task_count=active_task_count)
            if result.completion_status == str(TaskStatus.COMPLETED):
                await self._persist_assistant_history_message(request.task_id, request.conversation_id)
        except Exception as exc:
            await self._mark_task_failed(request, exc)
        finally:
            try:
                await self._clear_conversation_current_task(request.conversation_id, request.task_id)
                await self._release_task_skill_revision_if_terminal(request.task_id)
                await self._release_task_mcp_revision_if_terminal(request.task_id)
            finally:
                async with self._lock:
                    self._running_tasks.pop(request.task_id, None)

    async def _attach_conversation_memory(self, request: OrchestrationRequest) -> OrchestrationRequest:
        if self._conversation_memory_builder is None:
            return request
        try:
            conversation = await self.storage.get_conversation(request.conversation_id)
            account_id = conversation.account_id if conversation is not None else None
            context = await self._conversation_memory_builder.build(request, account_id=account_id)
        except PermissionError:
            raise
        except Exception as exc:
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    event_type="conversation.memory_fallback",
                    payload={"fallback_reason": "memory_builder_failed", "error_type": type(exc).__name__},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            return request

        memory_payload = context.to_prompt_payload()
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="conversation.memory_built",
                payload=context.to_audit_payload(),
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        return replace(
            request,
            user_message=context.effective_user_message,
            current_user_message=context.current_user_message,
            resolved_user_message=context.resolved_user_message,
            memory_context=memory_payload,
            metadata={**dict(request.metadata), "conversation_memory": memory_payload},
        )

    async def _mark_task_failed(self, request: OrchestrationRequest, exc: Exception) -> None:
        task = await self.storage.get_task(request.task_id)
        if task is None or task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            return
        failed = replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive())
        await self.storage.save_task(failed)
        payload: dict[str, Any] = {"code": "execution_crash", "message": str(exc)}
        if isinstance(exc, WorkflowPlanningError):
            payload = {
                "code": "planning_failed",
                "message": str(exc),
                "planner_reason": exc.reason,
                "planner_diagnostic": exc.diagnostic[:200],
                "planner_attempts": exc.attempts,
            }
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="task.failed",
                payload=payload,
            )
        )

    async def sync_assistant_history_messages(self, conversation_id: str) -> None:
        tasks = await self.storage.list_tasks_for_conversation(conversation_id, statuses={TaskStatus.COMPLETED})
        for task in tasks:
            await self.sync_assistant_history_message_for_task(task.task_id, task.conversation_id)

    async def sync_assistant_history_message_for_task(self, task_id: str, conversation_id: str) -> None:
        await self._persist_assistant_history_message(task_id, conversation_id)

    async def _persist_assistant_history_message(self, task_id: str, conversation_id: str) -> None:
        message_id = f"{task_id}:assistant"
        if await self.storage.get_message(message_id) is not None:
            return
        artifacts = await self.storage.list_artifacts_for_task(task_id)
        events = await self.storage.list_events_for_task(task_id)
        text_artifact = select_final_text_artifact(artifacts, events=events)
        if text_artifact is None:
            return
        message = Message(
            message_id=message_id,
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=text_artifact.storage_ref,
            task_id=task_id,
            stream_status="complete",
            created_at=self._utcnow_naive(),
        )
        try:
            await self.storage.save_message(message)
        except Exception:
            if await self.storage.get_message(message_id) is not None:
                return
            raise

    async def _record_plan_built(self, request: OrchestrationRequest, plan: WorkflowPlan) -> None:
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="workflow.plan_built",
                payload={
                    "node_count": len(plan.nodes),
                    "metadata": self._plan_audit_metadata(request, plan),
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )

    def _plan_audit_metadata(self, request: OrchestrationRequest, plan: WorkflowPlan) -> dict[str, Any]:
        metadata = dict(plan.metadata)
        revision = request.metadata.get("skill_bundle_revision")
        if revision:
            metadata["skill_bundle_revision"] = revision
        return self._json_safe_mapping(metadata)

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
        existing_task = await self.storage.get_task(task_id)
        if existing_task is not None and self._mcp_runtime_state is not None:
            for envelope in await self._mcp_runtime_state.cancel_platform_task(task_id):
                await self._record_event(
                    self._make_event(
                        task_id=existing_task.task_id,
                        conversation_id=existing_task.conversation_id,
                        event_type=str(envelope.get("event_type") or "mcp.long_task_cancel_requested"),
                        payload=dict(envelope.get("payload") or {}),
                    )
                )
        task = await self.cancellation_service.cancel_task_context(task_id)
        await self._clear_conversation_current_task(task.conversation_id, task.task_id)
        return task

    async def delete_conversation(self, conversation_id: str, *, account_id: str | None = None) -> dict[str, object]:
        conversation = await self.storage.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"Unknown conversation: {conversation_id}")
        if account_id is not None and conversation.account_id != account_id:
            raise PermissionError(f"Conversation does not belong to account: {conversation_id}")

        unfinished_tasks = await self.storage.list_tasks_for_conversation(
            conversation_id,
            statuses=UNFINISHED_TASK_STATUSES,
        )
        cancelled_task_ids: list[str] = []
        for task in unfinished_tasks:
            await self.cancel_task(task.task_id)
            cancelled_task_ids.append(task.task_id)
        for task_id in cancelled_task_ids:
            await self._cancel_existing_execution(task_id)

        await self._delete_conversation_file_artifacts(conversation_id)
        deleted_counts = await self.storage.delete_conversation(conversation_id)
        return {
            "conversation_id": conversation_id,
            "deleted": deleted_counts.get("conversation", 0) > 0,
            "cancelled_task_ids": cancelled_task_ids,
            "deleted_counts": deleted_counts,
        }

    async def _delete_conversation_file_artifacts(self, conversation_id: str) -> None:
        tasks = await self.storage.list_tasks_for_conversation(conversation_id)
        for task in tasks:
            for artifact in await self.storage.list_artifacts_for_task(task.task_id):
                metadata = parse_file_storage_ref(artifact.storage_ref)
                if is_active_skill_output_file(metadata):
                    self.artifact_file_store.delete(str(metadata.get("storage_key")))

    async def rename_conversation(self, conversation_id: str, title: str, *, account_id: str | None = None) -> Conversation:
        normalized_title = validate_conversation_title(title)
        async with self._lock:
            conversation = await self.storage.get_conversation(conversation_id)
            if conversation is None:
                raise ValueError(f"Unknown conversation: {conversation_id}")
            if account_id is not None and conversation.account_id != account_id:
                raise PermissionError(f"Conversation does not belong to account: {conversation_id}")
            updated = replace(conversation, title=normalized_title, updated_at=self._utcnow_naive())
            return await self.storage.save_conversation(updated)

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
                metadata=self._resume_skill_revision_metadata(task.task_id),
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

        yielded_event_ids: set[str] = set()
        terminal_event_types = {"task.completed", "task.failed", "task.cancelled"}
        terminal_event_seen = False
        async for event in self._iter_event_replay_pages(task_id):
            if is_frontend_event(event):
                yielded_event_ids.add(event.event_id)
                terminal_event_seen = terminal_event_seen or event.event_type in terminal_event_types
                yield event

        latest_task = await self.storage.get_task(task_id)
        if latest_task is not None and latest_task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            if not terminal_event_seen:
                deadline = asyncio.get_running_loop().time() + 0.25
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                    async for event in self._iter_event_replay_pages(task_id):
                        if event.event_id in yielded_event_ids:
                            continue
                        if not is_frontend_event(event):
                            continue
                        yielded_event_ids.add(event.event_id)
                        terminal_event_seen = terminal_event_seen or event.event_type in terminal_event_types
                        yield event
                    if terminal_event_seen:
                        return
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
        pending = [*self._running_tasks.values(), *self._running_title_tasks]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2)
            except asyncio.TimeoutError:
                for handle in pending:
                    handle.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._mysql_adapter is not None:
            await self._mysql_adapter.aclose()
        if self._mcp_runtime_state is not None:
            await self._mcp_runtime_state.aclose()
        await asyncio.to_thread(self._engine.dispose)

    @staticmethod
    def _canonical_capability_id(capability_id: str | None) -> str | None:
        if capability_id == "main_agent":
            return "main_agent.respond"
        return capability_id

    def _ensure_supported_capability(self, capability_id: str | None) -> None:
        if capability_id is None:
            return
        descriptor = self.capability_registry.get(capability_id)
        if descriptor is not None and descriptor.public:
            return
        raise ValueError(f"Unsupported capability_id: {capability_id}")

    async def _refresh_skills_for_new_conversation_if_needed(
        self,
        conversation_id: str,
        existing_conversation: Conversation | None,
    ) -> None:
        if self._skill_runtime_state is None:
            return
        if existing_conversation is not None:
            tasks = await self.storage.list_tasks_for_conversation(conversation_id)
            if tasks:
                return
        async with self._skill_refresh_lock:
            if existing_conversation is not None:
                tasks = await self.storage.list_tasks_for_conversation(conversation_id)
                if tasks:
                    return
            self._record_skill_refresh_started("conversation_start")
            previous_revision = self._skill_runtime_state.active_revision
            self._skill_runtime_state.retain_revision(previous_revision)
            try:
                result = self._skill_runtime_state.refresh_if_changed(reason="conversation_start")
                if result.status == "completed":
                    try:
                        self._sync_skill_capability_registry()
                    except Exception:
                        self._skill_runtime_state.activate_revision(previous_revision)
                        raise
                    if self._audit_sink is not None:
                        _record_skill_capability_startup_audit(
                            self._audit_sink,
                            self._skill_runtime_state.active_bundle.skill_capabilities,
                        )
                self._record_skill_refresh_audit(result)
            finally:
                self._skill_runtime_state.release_revision(previous_revision)

    def _sync_skill_capability_registry(self) -> None:
        if self._skill_runtime_state is None:
            return
        _sync_skill_capability_registry(self.capability_registry, self.instance_registry, self._skill_runtime_state)

    async def _refresh_mcp_for_new_conversation_if_needed(
        self,
        conversation_id: str,
        existing_conversation: Conversation | None,
    ) -> None:
        if self._mcp_runtime_state is None or not self._mcp_runtime_state.config.refreshes_on_conversation_start():
            return
        if existing_conversation is not None:
            tasks = await self.storage.list_tasks_for_conversation(conversation_id)
            if tasks:
                return
        async with self._mcp_refresh_lock:
            if existing_conversation is not None:
                tasks = await self.storage.list_tasks_for_conversation(conversation_id)
                if tasks:
                    return
            self._record_mcp_refresh_started("conversation_start")
            pending = await self._mcp_runtime_state.prepare_refresh(reason="conversation_start", force=True)
            result = pending.result
            if result.status == "completed":
                try:
                    self._sync_mcp_capability_registry(pending.bundle)
                except Exception:
                    await self._mcp_runtime_state.discard_activation(pending)
                    raise
                await self._mcp_runtime_state.commit_activation(pending)
            self._record_mcp_refresh_audit(result)

    def _sync_mcp_capability_registry(self, bundle: MCPRuntimeBundle) -> None:
        if self._mcp_runtime_state is None:
            return
        _sync_mcp_capability_registry(self.capability_registry, self.instance_registry, bundle)

    def _record_skill_refresh_audit(self, result: SkillRuntimeRefreshResult) -> None:
        if self._audit_sink is None:
            return
        payload = {
            "reason": result.reason,
            "previous_revision": result.previous_revision,
            "active_revision": result.active_revision,
            "registered_count": result.registered_count,
            "skipped_count": result.skipped_count,
            "duration_ms": result.duration_ms,
            "script_package_snapshot": result.script_package_snapshot,
        }
        if result.status == "completed":
            self._audit_sink.record_sync("skill.bundle_refresh_completed", payload)
        elif result.status == "skipped":
            self._audit_sink.record_sync("skill.bundle_refresh_skipped", payload)
        elif result.status == "failed":
            self._audit_sink.record_sync(
                "skill.bundle_refresh_failed",
                {**payload, "error_type": result.error_type, "fallback_revision": result.active_revision},
            )

    def _record_skill_refresh_started(self, reason: str) -> None:
        if self._audit_sink is None or self._skill_runtime_state is None:
            return
        self._audit_sink.record_sync(
            "skill.bundle_refresh_started",
            {
                "reason": reason,
                "previous_revision": self._skill_runtime_state.active_revision,
            },
        )

    def _record_mcp_refresh_audit(self, result: MCPRuntimeRefreshResult) -> None:
        if self._audit_sink is None:
            return
        payload = {
            "reason": result.reason,
            "previous_revision": result.previous_revision,
            "active_revision": result.active_revision,
            "registered_count": result.registered_count,
            "skipped_count": result.skipped_count,
            "duration_ms": result.duration_ms,
        }
        if result.status == "completed":
            self._audit_sink.record_sync("mcp.server_discovery_completed", payload)
            self._record_mcp_capability_registration_audit()
        elif result.status == "skipped":
            self._audit_sink.record_sync("mcp.server_discovery_skipped", payload)
        elif result.status == "failed":
            self._audit_sink.record_sync(
                "mcp.server_discovery_failed",
                {**payload, "error_type": result.error_type, "fallback_revision": result.active_revision},
            )

    def _record_mcp_refresh_started(self, reason: str) -> None:
        if self._audit_sink is None or self._mcp_runtime_state is None:
            return
        self._audit_sink.record_sync(
            "mcp.server_discovery_started",
            {
                "reason": reason,
                "previous_revision": self._mcp_runtime_state.active_revision,
            },
        )

    def _record_mcp_capability_registration_audit(self) -> None:
        if self._audit_sink is None or self._mcp_runtime_state is None:
            return
        bundle = self._mcp_runtime_state.active_bundle
        for descriptor in bundle.descriptors:
            binding = bundle.bindings.get(descriptor.capability_id)
            self._audit_sink.record_sync(
                "mcp.capability_registered",
                _mcp_capability_audit_payload(
                    descriptor,
                    binding,
                ),
            )
        for diagnostic in bundle.diagnostics:
            self._audit_sink.record_sync(
                "mcp.capability_registration_skipped",
                {
                    "server_id": diagnostic.server_id,
                    "tool_name": diagnostic.tool_name,
                    "capability_id": diagnostic.capability_id,
                    "reason": diagnostic.reason,
                    "message": diagnostic.message,
                    "transport_security": diagnostic.transport_security,
                    "header_names": list(diagnostic.header_names),
                },
            )

    def _retain_task_skill_revision(self, request: OrchestrationRequest) -> None:
        if self._skill_runtime_state is None or request.task_id in self._task_skill_bundle_revisions:
            return
        raw_revision = request.metadata.get("skill_bundle_revision") or self._skill_runtime_state.active_revision
        revision = str(raw_revision).strip()
        if not revision:
            return
        self._pin_bundle_revision_with_sidecar_if_enforced(
            task_id=request.task_id,
            bundle_kind="skill",
            revision=revision,
        )
        self._skill_runtime_state.retain_revision(revision)
        self._task_skill_bundle_revisions[request.task_id] = revision
        self._record_bundle_revision_shadow(
            operation_name="bundle_revision_pin",
            task_id=request.task_id,
            bundle_kind="skill",
            revision=revision,
            legacy_output={"retained": "true", "task_id": request.task_id},
        )

    def _retain_task_mcp_revision(self, request: OrchestrationRequest) -> None:
        if self._mcp_runtime_state is None or request.task_id in self._task_mcp_bundle_revisions:
            return
        raw_revision = request.metadata.get("mcp_bundle_revision") or self._mcp_runtime_state.active_revision
        revision = str(raw_revision).strip()
        if not revision:
            return
        self._pin_bundle_revision_with_sidecar_if_enforced(
            task_id=request.task_id,
            bundle_kind="mcp",
            revision=revision,
        )
        self._mcp_runtime_state.retain_revision(revision)
        self._task_mcp_bundle_revisions[request.task_id] = revision
        self._record_bundle_revision_shadow(
            operation_name="bundle_revision_pin",
            task_id=request.task_id,
            bundle_kind="mcp",
            revision=revision,
            legacy_output={"retained": "true", "task_id": request.task_id},
        )

    async def _release_task_skill_revision_if_terminal(self, task_id: str) -> None:
        if self._skill_runtime_state is None:
            return
        task = await self.storage.get_task(task_id)
        if task is None or task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return
        revision = self._task_skill_bundle_revisions.get(task_id)
        if revision:
            self._release_bundle_revision_with_sidecar_if_enforced(
                task_id=task_id,
                bundle_kind="skill",
                revision=revision,
            )
            self._task_skill_bundle_revisions.pop(task_id, None)
            self._skill_runtime_state.release_revision(revision)
            self._record_bundle_revision_shadow(
                operation_name="bundle_revision_release",
                task_id=task_id,
                bundle_kind="skill",
                revision=revision,
                legacy_output={"released": "true", "task_id": task_id},
            )

    async def _release_task_mcp_revision_if_terminal(self, task_id: str) -> None:
        if self._mcp_runtime_state is None:
            return
        task = await self.storage.get_task(task_id)
        if task is None or task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return
        revision = self._task_mcp_bundle_revisions.get(task_id)
        if revision:
            self._release_bundle_revision_with_sidecar_if_enforced(
                task_id=task_id,
                bundle_kind="mcp",
                revision=revision,
            )
            self._task_mcp_bundle_revisions.pop(task_id, None)
            self._mcp_runtime_state.release_revision(revision)
            self._record_bundle_revision_shadow(
                operation_name="bundle_revision_release",
                task_id=task_id,
                bundle_kind="mcp",
                revision=revision,
                legacy_output={"released": "true", "task_id": task_id},
            )

    def _pin_bundle_revision_with_sidecar_if_enforced(
        self,
        *,
        task_id: str,
        bundle_kind: str,
        revision: str,
    ) -> None:
        sidecar_client = self._task_dispatcher_sidecar_client_for("bundle_revision_pin")
        if sidecar_client is None:
            _ensure_task_dispatcher_write_allowed_by_rust_contract("bundle_revision_pin")
            return
        response = sidecar_client.pin_bundle_revision(
            task_id=task_id,
            bundle_kind=bundle_kind,
            revision=revision,
            idempotency_key=f"{task_id}:{bundle_kind}:{revision}:pin",
        )
        _consume_runtime_sidecar_response("bundle_revision_pin", response)

    def _release_bundle_revision_with_sidecar_if_enforced(
        self,
        *,
        task_id: str,
        bundle_kind: str,
        revision: str,
    ) -> None:
        sidecar_client = self._task_dispatcher_sidecar_client_for("bundle_revision_release")
        if sidecar_client is None:
            _ensure_task_dispatcher_write_allowed_by_rust_contract("bundle_revision_release")
            return
        response = sidecar_client.release_bundle_revision(
            task_id=task_id,
            bundle_kind=bundle_kind,
            revision=revision,
            released_at_ms=int(self._utcnow_naive().timestamp() * 1000),
            idempotency_key=f"{task_id}:{bundle_kind}:{revision}:release",
        )
        _consume_runtime_sidecar_response("bundle_revision_release", response)

    def _record_bundle_revision_shadow(
        self,
        *,
        operation_name: str,
        task_id: str,
        bundle_kind: str,
        revision: str,
        legacy_output: Mapping[str, Any],
    ) -> None:
        released_at_ms = int(self._utcnow_naive().timestamp() * 1000)

        def call_sidecar() -> Any:
            if operation_name == "bundle_revision_pin":
                return self._runtime_sidecar_client.pin_bundle_revision(
                    task_id=task_id,
                    bundle_kind=bundle_kind,
                    revision=revision,
                    idempotency_key=f"{task_id}:{bundle_kind}:{revision}:pin",
                )
            return self._runtime_sidecar_client.release_bundle_revision(
                task_id=task_id,
                bundle_kind=bundle_kind,
                revision=revision,
                released_at_ms=released_at_ms,
                idempotency_key=f"{task_id}:{bundle_kind}:{revision}:release",
            )

        record_runtime_sidecar_shadow_write_sync(
            component="task_dispatcher",
            operation_name=operation_name,
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={
                "bundle_kind": bundle_kind,
                "operation": operation_name,
                "revision": revision,
                "task_id": task_id,
            },
            legacy_output=legacy_output,
            rust_call=call_sidecar,
            rust_output=lambda envelope: {
                "bundle_kind": str(envelope.get("bundle_kind", "")),
                "revision": str(envelope.get("revision", "")),
                "task_id": str(envelope.get("task_id", "")),
            },
        )

    def _task_dispatcher_sidecar_client_for(self, operation_name: str) -> Any | None:
        if runtime_sidecar_mode_for_component("task_dispatcher") != "enforce":
            return None
        if self._runtime_sidecar_client is None:
            _ensure_task_dispatcher_write_allowed_by_rust_contract(operation_name)
            return None
        return self._runtime_sidecar_client

    def _resume_skill_revision_metadata(self, task_id: str) -> dict[str, object]:
        revision = self._task_skill_bundle_revisions.get(task_id)
        if revision:
            return {"skill_bundle_revision": revision}
        if self._skill_runtime_state is not None:
            return {"skill_bundle_revision": self._skill_runtime_state.active_revision}
        return {}

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

    async def _cancel_existing_execution(self, task_id: str) -> None:
        async with self._lock:
            handle = self._running_tasks.get(task_id)
        if handle is None:
            return
        if not handle.done():
            handle.cancel()
        await asyncio.gather(handle, return_exceptions=True)
        async with self._lock:
            if self._running_tasks.get(task_id) is handle:
                self._running_tasks.pop(task_id, None)


def build_api_runtime(
    *,
    database_path: str | Path,
    audit_log_path: str | Path,
    mysql_adapter: MySQLReadonlyAdapter | None = None,
    platform_llm_text_generator=None,
    platform_llm_config: Mapping[str, Any] | None = None,
    platform_llm_config_path: str | Path | None = None,
    platform_llm_client_factory: Callable[..., Any] | None = None,
    enable_platform_llm: bool = True,
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
    skill_input_text_generator: SkillInputTextGenerator | None = None,
    enable_skill_input_llm: bool = True,
    skill_platform_handlers: Mapping[str, Callable[..., Any]] | None = None,
    trusted_skill_handlers: Mapping[str, str] | None = None,
    trusted_skill_services: Mapping[str, tuple[str, ...] | list[str] | set[str]] | None = None,
    skill_services: Mapping[str, Any] | None = None,
    conversation_title_generator: ConversationTitleGenerator | None = None,
    enable_conversation_title_llm: bool = True,
    skill_roots: Iterable[str | Path] | None = None,
    public_skill_roots: Iterable[str | Path] | None = None,
    skill_catalog: SkillCatalog | None = None,
    mcp_config: Mapping[str, Any] | None = None,
    mcp_client_factory: Callable[..., Any] | None = None,
    mcp_sidecar_client: Any | None = None,
    mcp_runtime_state: MCPRuntimeState | None = None,
    runtime_replanner: RuntimeReplanner | None = None,
    auth_captcha_code_generator: Callable[[], str] | None = None,
    auth_captcha_ttl_seconds: int = 300,
    auth_session_ttl_seconds: int = 28_800,
    upload_store: InMemoryUploadStore | None = None,
    conversation_memory_builder: ConversationMemoryBuilder | None = None,
    enable_conversation_memory: bool = True,
    conversation_memory_resolution_generator: ResolutionGenerator | None = None,
    enable_conversation_memory_resolution_llm: bool = True,
    artifact_store_path: str | Path | None = None,
    runtime_sidecar_client: Any | None = None,
    skill_sandbox_client: Any | None = None,
) -> ApiRuntime:
    _bootstrap_runtime_config_env(
        platform_llm_text_generator=platform_llm_text_generator,
        platform_llm_config=platform_llm_config,
        platform_llm_config_path=platform_llm_config_path,
        platform_llm_client_factory=platform_llm_client_factory,
        enable_platform_llm=enable_platform_llm,
        planner_text_generator=planner_text_generator,
        planner_llm_config=planner_llm_config,
        planner_llm_config_path=planner_llm_config_path,
        planner_llm_client_factory=planner_llm_client_factory,
        enable_llm_planner=enable_llm_planner,
        main_agent_stream_generator=main_agent_stream_generator,
        main_agent_llm_config=main_agent_llm_config,
        main_agent_llm_config_path=main_agent_llm_config_path,
        main_agent_llm_client_factory=main_agent_llm_client_factory,
        enable_conversation_title_llm=enable_conversation_title_llm,
        enable_conversation_memory=enable_conversation_memory,
    )

    engine = create_sqlite_engine(database_path)
    bootstrap_sqlite_database(engine)
    resolved_runtime_sidecar_client = runtime_sidecar_client or _resolve_runtime_sidecar_client_from_env()
    audit_sink = JsonlAuditSink(audit_log_path)
    storage = SQLiteStorage(
        create_sqlite_session_factory(engine),
        runtime_sidecar_client=resolved_runtime_sidecar_client,
        runtime_sidecar_shadow_sink=_build_runtime_sidecar_shadow_diff_sink(audit_sink),
    )
    artifact_file_store = LocalArtifactFileStore(artifact_store_path or (Path(database_path).parent / "artifacts"))
    password_hasher = PasswordHasher()
    captcha_service = CaptchaService(
        storage,
        now_fn=ApiRuntime._utcnow_naive,
        code_generator=auth_captcha_code_generator,
        ttl_seconds=auth_captcha_ttl_seconds,
    )
    session_service = SessionService(
        storage,
        now_fn=ApiRuntime._utcnow_naive,
        ttl_seconds=auth_session_ttl_seconds,
    )

    roots = tuple(skill_roots) if skill_roots is not None else _default_skill_roots()
    resolved_public_skill_roots = tuple(public_skill_roots) if public_skill_roots is not None else roots[:1]

    capability_registry = CapabilityRegistry()
    _register_capability_descriptors(
        capability_registry,
        MAIN_AGENT_CAPABILITY_DESCRIPTORS,
        planner_payload_policies=MAIN_AGENT_PLANNER_PAYLOAD_POLICIES,
    )
    skill_runtime_state = SkillRuntimeState(
        skill_roots=roots,
        public_skill_roots=resolved_public_skill_roots,
        reserved_capability_ids=[descriptor.capability_id for descriptor in capability_registry.list()],
        initial_catalog=skill_catalog,
        refresh_enabled=skill_catalog is None,
    )

    instance_registry = InstanceRegistry()
    instance_registry.register(build_local_main_agent_instance())
    _sync_skill_capability_registry(capability_registry, instance_registry, skill_runtime_state)

    _record_skill_capability_startup_audit(audit_sink, skill_runtime_state.active_bundle.skill_capabilities)
    resolved_mcp_runtime_state = mcp_runtime_state or MCPRuntimeState(
        config=_resolve_mcp_runtime_config(mcp_config),
        client_factory=mcp_client_factory,
        sidecar_client=mcp_sidecar_client,
        reserved_capability_ids=[descriptor.capability_id for descriptor in capability_registry.list()],
    )
    if resolved_mcp_runtime_state.config.enabled:
        audit_sink.record_sync(
            "mcp.server_discovery_started",
            {
                "reason": "startup",
                "previous_revision": resolved_mcp_runtime_state.active_revision,
            },
        )
        pending_mcp_activation = resolved_mcp_runtime_state.prepare_refresh_sync(reason="startup", force=True)
        mcp_refresh_result = pending_mcp_activation.result
        if mcp_refresh_result.status == "completed":
            try:
                _sync_mcp_capability_registry(
                    capability_registry,
                    instance_registry,
                    pending_mcp_activation.bundle,
                )
            except Exception:
                resolved_mcp_runtime_state.discard_activation_sync(pending_mcp_activation)
                raise
            resolved_mcp_runtime_state.commit_activation_sync(pending_mcp_activation)
        _record_mcp_startup_audit(
            audit_sink,
            resolved_mcp_runtime_state,
            mcp_refresh_result,
        )
    event_broker = InMemoryEventBroker(audit_sink=audit_sink)

    async def record_live_event(event: EventRecord) -> None:
        event = _ensure_event_created_at(event)
        await storage.append_event(event)
        await event_broker.publish(event)

    main_agent_llm_runtime = _resolve_main_agent_llm_runtime(
        main_agent_llm_config=main_agent_llm_config,
        main_agent_llm_config_path=main_agent_llm_config_path,
        main_agent_llm_client_factory=main_agent_llm_client_factory,
        planner_llm_config=planner_llm_config,
        planner_llm_config_path=planner_llm_config_path,
        planner_llm_client_factory=planner_llm_client_factory,
    )

    resolved_mysql_adapter = mysql_adapter or MySQLReadonlyAdapter()
    resolved_conversation_title_generator = _resolve_conversation_title_generator(
        conversation_title_generator=conversation_title_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        enable_conversation_title_llm=enable_conversation_title_llm,
    )
    resolved_skill_input_text_generator = _resolve_skill_input_text_generator(
        skill_input_text_generator=skill_input_text_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        enable_skill_input_llm=enable_skill_input_llm,
    )
    skill_output_artifact_manager = SkillOutputArtifactManager(
        storage=storage,
        file_store=artifact_file_store,
        now_fn=ApiRuntime._utcnow_naive,
    )
    resolved_skill_sandbox_client = skill_sandbox_client or _resolve_skill_sandbox_client_from_env()
    resolved_skill_policy_client = _resolve_skill_policy_client(resolved_skill_sandbox_client)
    skill_script_runner = SkillScriptRunner(
        output_processor=skill_output_artifact_manager.process_for_runner,
        rust_sandbox_client=resolved_skill_sandbox_client,
        rust_sandbox_mode=os.environ.get("MAF_RUST_SKILL_RUNTIME_MODE", "off"),
        rust_sandbox_root=os.environ.get("MAF_SKILL_SANDBOX_ROOT") or None,
    )
    resolved_main_agent_stream_generator, main_agent_stream_metadata = _resolve_main_agent_stream_binding(
        main_agent_stream_generator=main_agent_stream_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        main_agent_reasoning_effort=main_agent_reasoning_effort,
    )
    resolved_platform_text_generator = _resolve_platform_text_generator(
        platform_llm_text_generator=platform_llm_text_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        platform_llm_config=platform_llm_config,
        platform_llm_client_factory=platform_llm_client_factory,
        enable_platform_llm=enable_platform_llm,
    )
    resolved_skill_service_registry = SkillServiceRegistry(
        {
            "mysql_readonly": resolved_mysql_adapter,
            "llm.non_stream": resolved_platform_text_generator,
            "artifact_writer": skill_output_artifact_manager,
            "progress_events": record_live_event,
            **dict(skill_services or {}),
        }
    )
    resolved_skill_platform_handler_registry = SkillPlatformHandlerRegistry(
        handlers=dict(skill_platform_handlers or {}),
        trusted_skill_handlers=dict(trusted_skill_handlers or {}),
        trusted_skill_services=dict(trusted_skill_services or {}),
        public_skill_roots=tuple(resolved_public_skill_roots),
        rust_policy_client=resolved_skill_policy_client,
        rust_policy_mode=os.environ.get("MAF_RUST_SKILL_RUNTIME_MODE", "off"),
        rust_policy_shadow_diff_sink=_build_skill_policy_shadow_diff_sink(audit_sink),
    )
    resolved_conversation_memory_builder = _resolve_conversation_memory_builder(
        storage=storage,
        conversation_memory_builder=conversation_memory_builder,
        main_agent_llm_runtime=main_agent_llm_runtime,
        enable_conversation_memory=enable_conversation_memory,
        resolution_generator=conversation_memory_resolution_generator,
        enable_resolution_llm=enable_conversation_memory_resolution_llm,
    )

    main_agent_workflow_provider = MainAgentWorkflowProvider()

    def resolve_skill_name(capability_id: str, revision: str | None) -> str | None:
        try:
            return skill_runtime_state.skill_name_for_capability(capability_id, revision)
        except KeyError:
            return None

    def resolve_skill_manifest(capability_id: str, revision: str | None):
        skill_name = resolve_skill_name(capability_id, revision)
        if not skill_name:
            return None
        try:
            return skill_runtime_state.catalog_for_revision(revision).get(skill_name)
        except KeyError:
            return None

    skill_workflow_provider = SkillWorkflowProvider(
        skill_name_resolver=resolve_skill_name,
        skill_manifest_resolver=resolve_skill_manifest,
    )

    def resolve_macro_provider(capability_id: str):
        if capability_id.startswith("skill."):
            return skill_workflow_provider
        return None

    macro_providers = {}

    auto_workflow_provider = AutoWorkflowProvider(
        main_agent_provider=main_agent_workflow_provider,
        macro_providers=macro_providers,
        macro_provider_resolver=resolve_macro_provider,
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
        macro_provider_resolver=resolve_macro_provider,
        text_generator=resolved_planner_text_generator,
        payload_policies=planner_payload_policies,
    )
    default_replanners: list[RuntimeReplanner] = [
        MainAgentRuntimeReplanner(
            capability_registry=capability_registry,
            macro_providers=macro_providers,
            macro_provider_resolver=resolve_macro_provider,
            text_generator=resolved_planner_text_generator,
            payload_policies=planner_payload_policies,
        )
    ]
    resolved_runtime_replanner = runtime_replanner or CompositeRuntimeReplanner(default_replanners)

    cancellation_service = CancellationService(
        storage,
        event_sink=event_broker,
        audit_sink=audit_sink,
        runtime_sidecar_client=resolved_runtime_sidecar_client,
    )
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
                    skill_catalog=skill_runtime_state.active_bundle.catalog,
                    skill_catalog_resolver=skill_runtime_state.catalog_for_revision,
                    script_runner=skill_script_runner,
                    skill_input_text_generator=resolved_skill_input_text_generator,
                    skill_output_artifact_manager=skill_output_artifact_manager,
                    live_event_recorder=record_live_event,
                ),
                SkillExecutor(
                    runtime_state=skill_runtime_state,
                    script_runner=skill_script_runner,
                    skill_input_text_generator=resolved_skill_input_text_generator,
                    platform_handler_registry=resolved_skill_platform_handler_registry,
                    service_registry=resolved_skill_service_registry,
                ),
                MCPToolExecutor(runtime_state=resolved_mcp_runtime_state, live_event_recorder=record_live_event),
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
            skill_provider=skill_workflow_provider,
        ),
        mysql_adapter=resolved_mysql_adapter,
        password_hasher=password_hasher,
        captcha_service=captcha_service,
        session_service=session_service,
        conversation_title_generator=resolved_conversation_title_generator,
        upload_store=upload_store,
        conversation_memory_builder=resolved_conversation_memory_builder,
        artifact_file_store=artifact_file_store,
        audit_sink=audit_sink,
        skill_runtime_state=skill_runtime_state,
        mcp_runtime_state=resolved_mcp_runtime_state,
        runtime_sidecar_client=resolved_runtime_sidecar_client,
    )


def _bootstrap_runtime_config_env(
    *,
    platform_llm_text_generator,
    platform_llm_config: Mapping[str, Any] | None,
    platform_llm_config_path: str | Path | None,
    platform_llm_client_factory: Callable[..., Any] | None,
    enable_platform_llm: bool,
    planner_text_generator: PlannerTextGenerator | None,
    planner_llm_config: Mapping[str, Any] | None,
    planner_llm_config_path: str | Path | None,
    planner_llm_client_factory: Callable[..., Any] | None,
    enable_llm_planner: bool,
    main_agent_stream_generator: StreamGenerator | None,
    main_agent_llm_config: Mapping[str, Any] | None,
    main_agent_llm_config_path: str | Path | None,
    main_agent_llm_client_factory: Callable[..., Any] | None,
    enable_conversation_title_llm: bool,
    enable_conversation_memory: bool,
) -> None:
    explicit_paths: list[str | Path] = []
    should_bootstrap_default = False

    if platform_llm_config is None:
        if platform_llm_config_path is not None:
            explicit_paths.append(platform_llm_config_path)
        elif enable_platform_llm and platform_llm_text_generator is None and platform_llm_client_factory is None:
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

    if enable_conversation_title_llm and main_agent_llm_config is None:
        if main_agent_llm_config_path is not None:
            explicit_paths.append(main_agent_llm_config_path)
        elif main_agent_llm_client_factory is None:
            should_bootstrap_default = True

    if enable_conversation_memory:
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


def _resolve_runtime_sidecar_client_from_env() -> RuntimeSidecarGrpcClient | None:
    endpoint = os.environ.get("MAF_RUNTIME_SIDECAR_ENDPOINT", "").strip()
    if not endpoint:
        return None
    artifact_provenance, allowed_artifact_checksums, allowed_cargo_lock_digests = (
        _resolve_runtime_sidecar_artifact_trust_from_env()
    )
    allowed_hosts = tuple(
        host.strip()
        for host in os.environ.get("MAF_RUNTIME_SIDECAR_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    )
    mtls_enabled = os.environ.get("MAF_RUNTIME_SIDECAR_MTLS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return RuntimeSidecarGrpcClient(
        endpoint,
        config_source="environment_variable",
        allowed_hosts=allowed_hosts,
        mtls_enabled=mtls_enabled,
        tls_ca_path=os.environ.get("MAF_RUNTIME_SIDECAR_TLS_CA_PATH", "").strip() or None,
        tls_cert_path=os.environ.get("MAF_RUNTIME_SIDECAR_TLS_CERT_PATH", "").strip() or None,
        tls_key_path=os.environ.get("MAF_RUNTIME_SIDECAR_TLS_KEY_PATH", "").strip() or None,
        tls_server_name=os.environ.get("MAF_RUNTIME_SIDECAR_TLS_SERVER_NAME", "").strip() or None,
        artifact_provenance=artifact_provenance,
        allowed_artifact_checksums=allowed_artifact_checksums,
        allowed_cargo_lock_digests=allowed_cargo_lock_digests,
    )


def _resolve_runtime_sidecar_artifact_trust_from_env() -> tuple[dict[str, Any] | None, tuple[str, ...], tuple[str, ...]]:
    manifest_path = os.environ.get("MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH", "").strip()
    allowlist_path = os.environ.get("MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH", "").strip()
    if not manifest_path and not allowlist_path:
        if _runtime_sidecar_enforce_enabled():
            _raise_runtime_sidecar_artifact_untrusted(
                "Rust runtime sidecar enforce mode requires an artifact manifest and allowlist"
            )
        return None, (), ()
    if not manifest_path or not allowlist_path:
        _raise_runtime_sidecar_artifact_untrusted(
            "Rust runtime sidecar artifact trust requires both manifest and allowlist paths"
        )

    manifest = _load_artifact_trust_json_file(
        manifest_path,
        "runtime sidecar artifact manifest",
        _raise_runtime_sidecar_artifact_untrusted,
    )
    allowlist = _load_artifact_trust_json_file(
        allowlist_path,
        "runtime sidecar artifact allowlist",
        _raise_runtime_sidecar_artifact_untrusted,
    )
    metadata = _runtime_sidecar_artifact_metadata_from_manifest(manifest)
    allowed_checksums, allowed_cargo_lock_digests = _artifact_allowlist_digests(
        allowlist,
        required_manifest=manifest,
        artifact_label="Rust runtime sidecar",
        raise_untrusted=_raise_runtime_sidecar_artifact_untrusted,
    )
    validate_runtime_sidecar_artifact_provenance(
        metadata,
        allowed_checksums=set(allowed_checksums),
        allowed_cargo_lock_digests=set(allowed_cargo_lock_digests),
    )
    return metadata, allowed_checksums, allowed_cargo_lock_digests


def _runtime_sidecar_enforce_enabled() -> bool:
    return any(
        runtime_sidecar_mode_for_component(component) == "enforce"
        for component in ("runtime_store", "event_log", "task_dispatcher")
    )


def _runtime_sidecar_artifact_metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    contract_hashes = manifest.get("contract_hashes")
    proto_hashes = manifest.get("proto_hashes")
    if not isinstance(contract_hashes, Mapping) or not isinstance(proto_hashes, Mapping):
        _raise_runtime_sidecar_artifact_untrusted(
            "Rust runtime sidecar artifact manifest must include contract and proto hashes"
        )
    return {
        "source": manifest.get("source"),
        "artifact_kind": manifest.get("artifact_kind"),
        "checksum_sha256": manifest.get("artifact_sha256"),
        "sbom_digest": manifest.get("sbom_sha256"),
        "cargo_lock_digest": manifest.get("cargo_lock_sha256"),
        "proto_hash": proto_hashes.get("runtime"),
        "schema_hash": contract_hashes.get("runtime_sidecar"),
        "provenance_attestation": manifest.get("provenance_sha256"),
    }


def _raise_runtime_sidecar_artifact_untrusted(message: str) -> None:
    error_code = error_policy("runtime_store_artifact_untrusted")["code"]
    raise RuntimeError(f"{error_code}: {message}")


def _resolve_skill_sandbox_client_from_env() -> SkillSandboxGrpcClient | None:
    endpoint = os.environ.get("MAF_SKILL_SANDBOX_ENDPOINT", "").strip()
    if not endpoint:
        return None
    artifact_provenance, allowed_artifact_checksums, allowed_cargo_lock_digests = (
        _resolve_skill_sandbox_artifact_trust_from_env()
    )
    return SkillSandboxGrpcClient(
        endpoint,
        artifact_provenance=artifact_provenance,
        allowed_artifact_checksums=allowed_artifact_checksums,
        allowed_cargo_lock_digests=allowed_cargo_lock_digests,
    )


def _resolve_skill_sandbox_artifact_trust_from_env() -> tuple[dict[str, Any] | None, tuple[str, ...], tuple[str, ...]]:
    manifest_path = os.environ.get("MAF_SKILL_SANDBOX_ARTIFACT_MANIFEST_PATH", "").strip()
    allowlist_path = os.environ.get("MAF_SKILL_SANDBOX_ARTIFACT_ALLOWLIST_PATH", "").strip()
    if not manifest_path and not allowlist_path:
        if os.environ.get("MAF_RUST_SKILL_RUNTIME_MODE", "off").strip().lower() == "enforce":
            _raise_skill_runtime_artifact_untrusted(
                "Rust Skill Sandbox enforce mode requires an artifact manifest and allowlist"
            )
        return None, (), ()
    if not manifest_path or not allowlist_path:
        _raise_skill_runtime_artifact_untrusted(
            "Rust Skill Sandbox artifact trust requires both manifest and allowlist paths"
        )

    manifest = _load_artifact_trust_json_file(
        manifest_path,
        "Rust Skill Sandbox artifact manifest",
        _raise_skill_runtime_artifact_untrusted,
    )
    allowlist = _load_artifact_trust_json_file(
        allowlist_path,
        "Rust Skill Sandbox artifact allowlist",
        _raise_skill_runtime_artifact_untrusted,
    )
    metadata = _skill_sandbox_artifact_metadata_from_manifest(manifest)
    allowed_checksums, allowed_cargo_lock_digests = _artifact_allowlist_digests(
        allowlist,
        required_manifest=manifest,
        artifact_label="Rust Skill Sandbox",
        raise_untrusted=_raise_skill_runtime_artifact_untrusted,
    )
    validate_skill_runtime_artifact_provenance(
        metadata,
        allowed_checksums=set(allowed_checksums),
        allowed_cargo_lock_digests=set(allowed_cargo_lock_digests),
    )
    return metadata, allowed_checksums, allowed_cargo_lock_digests


def _skill_sandbox_artifact_metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    contract_hashes = manifest.get("contract_hashes")
    if not isinstance(contract_hashes, Mapping):
        _raise_skill_runtime_artifact_untrusted(
            "Rust Skill Sandbox artifact manifest must include contract hashes"
        )
    contract = load_skill_runtime_contract()
    artifact_kind = manifest.get("artifact_kind")
    artifact_id = manifest.get("artifact_id")
    if artifact_id == "maf_skill_sandbox" and artifact_kind == "sidecar_binary":
        artifact_kind = "skill_sandbox_sidecar_binary"
    return {
        "source": manifest.get("source"),
        "artifact_kind": artifact_kind,
        "checksum_sha256": manifest.get("artifact_sha256"),
        "cargo_lock_digest": manifest.get("cargo_lock_sha256"),
        "contract_version": contract["contract_version"],
        "bundle_revision": manifest.get("git_commit") or manifest.get("artifact_name"),
        "schema_hash": contract_hashes.get("skill_runtime"),
        "sbom_digest": manifest.get("sbom_sha256"),
        "provenance_attestation": manifest.get("provenance_sha256"),
    }


def _load_artifact_trust_json_file(
    path: str,
    label: str,
    raise_untrusted: Callable[[str], None],
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise_untrusted(f"{label} is unavailable or invalid")
        raise AssertionError("unreachable") from exc
    if not isinstance(payload, dict):
        raise_untrusted(f"{label} must be a JSON object")
    return payload


def _artifact_allowlist_digests(
    allowlist: Mapping[str, Any],
    *,
    required_manifest: Mapping[str, Any],
    artifact_label: str,
    raise_untrusted: Callable[[str], None],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    entries = allowlist.get("allowed_artifacts")
    if not isinstance(entries, list):
        raise_untrusted(f"{artifact_label} artifact allowlist must contain allowed_artifacts")
    checksums: list[str] = []
    cargo_lock_digests: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise_untrusted(f"{artifact_label} artifact allowlist entry is invalid")
        checksum = entry.get("artifact_sha256") or entry.get("checksum_sha256")
        cargo_lock = entry.get("cargo_lock_sha256") or entry.get("cargo_lock_digest")
        if isinstance(checksum, str) and checksum:
            checksums.append(checksum)
        if isinstance(cargo_lock, str) and cargo_lock:
            cargo_lock_digests.append(cargo_lock)
    if not any(_artifact_allowlist_entry_matches_manifest(entry, required_manifest) for entry in entries):
        raise_untrusted(f"{artifact_label} artifact manifest is not present in the allowlist")
    if not checksums or not cargo_lock_digests:
        raise_untrusted(f"{artifact_label} artifact allowlist must include checksums and Cargo.lock digests")
    return tuple(sorted(set(checksums))), tuple(sorted(set(cargo_lock_digests)))


def _artifact_allowlist_entry_matches_manifest(entry: object, manifest: Mapping[str, Any]) -> bool:
    if not isinstance(entry, Mapping):
        return False
    exact_fields = (
        "component",
        "artifact_id",
        "artifact_kind",
        "artifact_sha256",
        "cargo_lock_sha256",
        "sbom_sha256",
        "provenance_sha256",
        "source",
        "git_commit",
        "toolchain",
        "target_triple",
        "build_profile",
    )
    if any(entry.get(field) != manifest.get(field) for field in exact_fields):
        return False
    for field in ("cargo_features", "contract_hashes", "proto_hashes"):
        if entry.get(field) != manifest.get(field):
            return False
    return True


def _raise_skill_runtime_artifact_untrusted(message: str) -> None:
    error_code = skill_runtime_error_policy("skill_runtime_artifact_untrusted")["code"]
    raise RuntimeError(f"{error_code}: {message}")


def _resolve_skill_policy_client(fallback_client: Any | None) -> Any | None:
    module_name = os.environ.get("MAF_SKILL_POLICY_PYO3_MODULE", "").strip() or "maf_skill_runtime_pyo3"
    pyo3_client = try_load_skill_runtime_pyo3_policy_client(module_name=module_name)
    return pyo3_client or fallback_client


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


def _resolve_platform_text_generator(
    *,
    platform_llm_text_generator,
    main_agent_llm_runtime: SharedLLMRuntime,
    platform_llm_config: Mapping[str, Any] | None,
    platform_llm_client_factory: Callable[..., Any] | None,
    enable_platform_llm: bool,
):
    if platform_llm_text_generator is not None:
        return platform_llm_text_generator
    if not enable_platform_llm:
        return None
    runtime = main_agent_llm_runtime
    if platform_llm_config is not None or platform_llm_client_factory is not None:
        runtime = SharedLLMRuntime(
            client_factory=platform_llm_client_factory or LLMClient,
            config=platform_llm_config,
            config_source="injected_config" if platform_llm_config is not None else "environment",
        )

    async def generate(prompt: str, **_: Any) -> str:
        return await runtime.generate_text(
            prompt,
            thinking=False,
            reasoning_effort="minimal",
        )

    return generate

def _resolve_conversation_title_generator(
    *,
    conversation_title_generator: ConversationTitleGenerator | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    enable_conversation_title_llm: bool,
) -> ConversationTitleGenerator | None:
    if conversation_title_generator is not None:
        return conversation_title_generator
    if not enable_conversation_title_llm:
        return None

    async def generate(title_source: str) -> str:
        return await main_agent_llm_runtime.generate_text(
            build_conversation_title_prompt(title_source),
            thinking=False,
            reasoning_effort="minimal",
        )

    return generate


def _resolve_skill_input_text_generator(
    *,
    skill_input_text_generator: SkillInputTextGenerator | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    enable_skill_input_llm: bool,
) -> SkillInputTextGenerator | None:
    if skill_input_text_generator is not None:
        return skill_input_text_generator
    if not enable_skill_input_llm:
        return None

    async def generate(prompt: str) -> str:
        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=False,
            reasoning_effort="minimal",
        )

    return generate


def _resolve_conversation_memory_builder(
    *,
    storage: SQLiteStorage,
    conversation_memory_builder: ConversationMemoryBuilder | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    enable_conversation_memory: bool,
    resolution_generator: ResolutionGenerator | None,
    enable_resolution_llm: bool,
) -> ConversationMemoryBuilder | None:
    if conversation_memory_builder is not None:
        return conversation_memory_builder
    if not enable_conversation_memory:
        return None

    async def generate_summary(prompt: str) -> str:
        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=False,
            reasoning_effort="minimal",
        )

    async def generate_resolution(prompt: str) -> str:
        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=False,
            reasoning_effort="minimal",
        )

    return ConversationMemoryBuilder(
        storage=storage,
        config=ConversationMemoryConfig.from_runtime_config(),
        summary_generator=generate_summary,
        resolution_generator=resolution_generator or (generate_resolution if enable_resolution_llm else None),
    )


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


def _sync_skill_capability_registry(
    capability_registry: CapabilityRegistry,
    instance_registry: InstanceRegistry,
    runtime_state: SkillRuntimeState,
) -> None:
    registry = runtime_state.active_bundle.skill_capabilities
    previous_descriptors = [descriptor for descriptor in capability_registry.list() if _is_skill_descriptor(descriptor)]
    previous_policies = {
        descriptor.capability_id: capability_registry.get_planner_payload_policy(descriptor.capability_id)
        for descriptor in previous_descriptors
    }
    try:
        for descriptor in previous_descriptors:
            capability_registry.unregister(descriptor.capability_id)
        _register_capability_descriptors(
            capability_registry,
            registry.descriptors,
            planner_payload_policies=registry.payload_policies,
        )
        instance_registry.register(build_local_skill_executor_instance(runtime_state.known_skill_capability_ids()))
    except Exception:
        for descriptor in list(capability_registry.list()):
            if _is_skill_descriptor(descriptor):
                capability_registry.unregister(descriptor.capability_id)
        for descriptor in previous_descriptors:
            capability_registry.register(
                descriptor,
                planner_payload_policy=previous_policies.get(descriptor.capability_id),
            )
        instance_registry.register(build_local_skill_executor_instance(runtime_state.known_skill_capability_ids()))
        raise


def _sync_mcp_capability_registry(
    capability_registry: CapabilityRegistry,
    instance_registry: InstanceRegistry,
    bundle: MCPRuntimeBundle,
) -> None:
    previous_descriptors = [descriptor for descriptor in capability_registry.list() if _is_mcp_descriptor(descriptor)]
    previous_policies = {
        descriptor.capability_id: capability_registry.get_planner_payload_policy(descriptor.capability_id)
        for descriptor in previous_descriptors
    }
    try:
        for descriptor in previous_descriptors:
            capability_registry.unregister(descriptor.capability_id)
        _register_capability_descriptors(
            capability_registry,
            bundle.descriptors,
            planner_payload_policies=bundle.payload_policies,
        )
        if bundle.bindings:
            instance_registry.register(build_local_mcp_tool_instance(tuple(bundle.bindings)))
    except Exception:
        for descriptor in list(capability_registry.list()):
            if _is_mcp_descriptor(descriptor):
                capability_registry.unregister(descriptor.capability_id)
        for descriptor in previous_descriptors:
            capability_registry.register(
                descriptor,
                planner_payload_policy=previous_policies.get(descriptor.capability_id),
            )
        if previous_descriptors:
            instance_registry.register(
                build_local_mcp_tool_instance(tuple(descriptor.capability_id for descriptor in previous_descriptors))
            )
        raise


def _is_mcp_descriptor(descriptor: CapabilityDescriptor) -> bool:
    return descriptor.kind == "mcp_tool" or descriptor.source == "mcp" or descriptor.capability_id.startswith("mcp.")


def _is_skill_descriptor(descriptor: CapabilityDescriptor) -> bool:
    return descriptor.kind == "skill" or descriptor.source == "skill" or descriptor.capability_id.startswith("skill.")


def _resolve_mcp_runtime_config(config: Mapping[str, Any] | None) -> MCPRuntimeConfig:
    if config is not None:
        return load_mcp_server_config(base_config=MCPRuntimeConfig.from_mapping(config))
    loaded = load_config()
    raw = loaded.get("mcp")
    if isinstance(raw, Mapping):
        return load_mcp_server_config(base_config=MCPRuntimeConfig.from_mapping(raw))
    return load_mcp_server_config(base_config=MCPRuntimeConfig.disabled())


def _record_mcp_startup_audit(
    audit_sink: JsonlAuditSink,
    runtime_state: MCPRuntimeState,
    refresh_result: MCPRuntimeRefreshResult,
) -> None:
    payload = {
        "reason": refresh_result.reason,
        "previous_revision": refresh_result.previous_revision,
        "active_revision": refresh_result.active_revision,
        "registered_count": refresh_result.registered_count,
        "skipped_count": refresh_result.skipped_count,
        "duration_ms": refresh_result.duration_ms,
    }
    payload.update(_mcp_security_audit_summary(runtime_state.active_bundle))
    if refresh_result.status == "completed":
        audit_sink.record_sync("mcp.server_discovery_completed", payload)
        for descriptor in runtime_state.active_bundle.descriptors:
            binding = runtime_state.active_bundle.bindings.get(descriptor.capability_id)
            audit_sink.record_sync(
                "mcp.capability_registered",
                _mcp_capability_audit_payload(descriptor, binding),
            )
        for diagnostic in runtime_state.active_bundle.diagnostics:
            audit_sink.record_sync(
                "mcp.capability_registration_skipped",
                {
                    "server_id": diagnostic.server_id,
                    "tool_name": diagnostic.tool_name,
                    "capability_id": diagnostic.capability_id,
                    "reason": diagnostic.reason,
                    "message": diagnostic.message,
                    "transport_security": diagnostic.transport_security,
                    "header_names": list(diagnostic.header_names),
                },
            )
        return
    if refresh_result.status == "failed":
        audit_sink.record_sync(
            "mcp.server_discovery_failed",
            {**payload, "error_type": refresh_result.error_type, "fallback_revision": refresh_result.active_revision},
        )
        return
    audit_sink.record_sync("mcp.server_discovery_skipped", payload)


def _mcp_capability_audit_payload(descriptor: CapabilityDescriptor, binding: Any | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "capability_id": descriptor.capability_id,
        "name": descriptor.name,
        "source_path": descriptor.source_path,
        "source": descriptor.source,
    }
    if binding is not None:
        payload.update(
            {
                "server_id": getattr(binding, "server_id", ""),
                "tool_name": getattr(binding, "tool_name", ""),
                "transport_security": getattr(binding, "transport_security", ""),
                "header_names": list(getattr(binding, "header_names", ()) or ()),
                "credential_over_plaintext_http": bool(getattr(binding, "credential_over_plaintext_http", False)),
            }
        )
    return payload


def _mcp_security_audit_summary(bundle: MCPRuntimeBundle) -> dict[str, Any]:
    transport_security = {
        value
        for value in (
            *(getattr(binding, "transport_security", "") for binding in bundle.bindings.values()),
            *(diagnostic.transport_security for diagnostic in bundle.diagnostics),
        )
        if value
    }
    header_names = {
        name
        for names in (
            *(getattr(binding, "header_names", ()) or () for binding in bundle.bindings.values()),
            *(diagnostic.header_names for diagnostic in bundle.diagnostics),
        )
        for name in names
        if name
    }
    credential_over_plaintext_http = any(
        bool(getattr(binding, "credential_over_plaintext_http", False))
        for binding in bundle.bindings.values()
    )
    return {
        "transport_security": sorted(transport_security),
        "header_names": sorted(header_names),
        "credential_over_plaintext_http": credential_over_plaintext_http,
    }


def _record_skill_capability_startup_audit(
    audit_sink: JsonlAuditSink,
    skill_capabilities: SkillCapabilityRegistry,
) -> None:
    for descriptor in skill_capabilities.descriptors:
        audit_sink.record_sync(
            "skill.capability_registered",
            {
                "capability_id": descriptor.capability_id,
                "skill_name": skill_capabilities.skill_name_by_capability_id[descriptor.capability_id],
                "source_path": skill_capabilities.source_path_by_capability_id.get(descriptor.capability_id, ""),
                "source": descriptor.source,
            },
        )
    for diagnostic in skill_capabilities.diagnostics:
        audit_sink.record_sync(
            "skill.capability_registration_skipped",
            {
                "skill_name": diagnostic.skill_name,
                "reason": diagnostic.reason,
                "message": diagnostic.message,
                "source_path": diagnostic.source_path_summary,
            },
        )


def _ensure_event_created_at(event: EventRecord) -> EventRecord:
    if event.created_at is not None:
        return event
    return replace(event, created_at=datetime.now(timezone.utc).replace(tzinfo=None))


def _ensure_task_dispatcher_write_allowed_by_rust_contract(operation_name: str) -> None:
    ensure_sidecar_write_allowed(
        component="task_dispatcher",
        operation_name=operation_name,
        unavailable_error_code="dispatcher_unavailable",
    )


def _consume_runtime_sidecar_response(operation_name: str, response: Mapping[str, Any]) -> None:
    envelope = validate_runtime_sidecar_response(operation_name, response)
    error = envelope.get("error")
    if isinstance(error, Mapping):
        raise RuntimeError(f"{error['code']}: {error['message']}")


def _build_skill_policy_shadow_diff_sink(
    audit_sink: JsonlAuditSink | None,
) -> Callable[[Mapping[str, str]], None] | None:
    if audit_sink is None:
        return None

    def record_shadow_diff(payload: Mapping[str, str]) -> None:
        audit_sink.record_sync("skill.runtime_policy_shadow_diff", payload)

    return record_shadow_diff


def _build_runtime_sidecar_shadow_diff_sink(
    audit_sink: JsonlAuditSink | None,
) -> Callable[[Mapping[str, str]], None] | None:
    if audit_sink is None:
        return None

    def record_shadow_diff(payload: Mapping[str, str]) -> None:
        audit_sink.record_sync("runtime.sidecar_shadow_diff", payload)

    return record_shadow_diff


def _build_safety_kernel_shadow_diff_sink(
    audit_sink: JsonlAuditSink | None,
) -> Callable[[Mapping[str, str]], None] | None:
    if audit_sink is None:
        return None

    def record_shadow_diff(payload: Mapping[str, str]) -> None:
        audit_sink.record_sync("safety.kernel_shadow_diff", payload)

    return record_shadow_diff


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
        Path.cwd() / "skill",
        Path.home() / ".codex" / "skills",
    )
