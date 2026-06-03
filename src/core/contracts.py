from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from .enums import EventVisibility, TaskStatus
from .models import (
    Artifact,
    AuthUserToken,
    Checkpoint,
    Conversation,
    ConversationMemorySummary,
    EventRecord,
    Interrupt,
    InterruptAnswer,
    MailboxDelivery,
    MailboxMessage,
    Message,
    PendingSkillContext,
    Task,
    TaskEdge,
    TaskInputAttachment,
    TaskNode,
)


Payload = Mapping[str, Any]


@dataclass(slots=True, frozen=True)
class CapabilityExecutionRequest:
    capability_id: str
    conversation_id: str
    task_id: str
    node_id: str
    input_payload: Payload = field(default_factory=dict)
    context_refs: tuple[str, ...] = ()
    dependency_outputs: Mapping[str, Payload] = field(default_factory=dict)
    metadata: Payload = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CapabilityExecutionError:
    code: str
    message: str
    retriable: bool = False
    metadata: Payload = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CapabilityExecutionResult:
    capability_id: str
    task_id: str
    node_id: str
    output_payload: Payload = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    events: tuple[EventRecord, ...] = ()
    interrupt: Interrupt | None = None
    error: CapabilityExecutionError | None = None
    metadata: Payload = field(default_factory=dict)


@runtime_checkable
class StoragePort(Protocol):
    async def save_auth_user_token(self, token: AuthUserToken, *, auth_generation_reason: str | None = None) -> AuthUserToken: ...

    async def get_auth_user_token(self, username: str) -> AuthUserToken | None: ...

    async def get_auth_user_token_by_hash(self, api_token_hash: str) -> AuthUserToken | None: ...

    async def get_auth_user_generation(self, username: str) -> AuthUserToken | None: ...

    async def list_auth_user_generations(self) -> list[AuthUserToken]: ...

    async def touch_auth_user_token_last_used(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None: ...

    async def clear_auth_user_token(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None: ...

    async def rotate_auth_user_token(
        self,
        username: str,
        *,
        old_api_token_hash: str,
        new_api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None: ...

    async def save_conversation(self, conversation: Conversation) -> Conversation: ...

    async def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    async def list_conversations_for_username(self, username: str) -> list[Conversation]: ...

    async def list_deleting_conversations(self) -> list[Conversation]: ...

    async def mark_conversation_deleting(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None: ...

    async def update_conversation_delete_phase(
        self,
        conversation_id: str,
        *,
        phase: str,
        updated_at: datetime,
        runner_id: str | None = None,
    ) -> Conversation | None: ...

    async def mark_conversation_delete_failed(
        self,
        conversation_id: str,
        *,
        failed_at: datetime,
        phase: str,
        error_code: str,
        error_summary: str,
        runner_id: str | None = None,
    ) -> Conversation | None: ...

    async def retry_failed_conversation_delete(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None: ...

    async def delete_conversation(self, conversation_id: str) -> dict[str, int]: ...

    async def delete_conversation_physical(self, conversation_id: str) -> dict[str, int]: ...

    async def save_conversation_memory_summary(self, summary: ConversationMemorySummary) -> ConversationMemorySummary: ...

    async def get_conversation_memory_summary(self, summary_id: str) -> ConversationMemorySummary | None: ...

    async def get_latest_conversation_memory_summary(
        self,
        conversation_id: str,
        username: str | None = None,
    ) -> ConversationMemorySummary | None: ...

    async def list_conversation_memory_summaries(self, conversation_id: str) -> list[ConversationMemorySummary]: ...

    async def delete_conversation_memory_summaries_for_conversation(self, conversation_id: str) -> int: ...

    async def save_pending_skill_context(self, context: PendingSkillContext) -> PendingSkillContext: ...

    async def get_pending_skill_context(self, context_id: str) -> PendingSkillContext | None: ...

    async def get_active_pending_skill_context(self, conversation_id: str) -> PendingSkillContext | None: ...

    async def mark_pending_skill_context_consumed(self, context_id: str) -> PendingSkillContext | None: ...

    async def mark_pending_skill_context_cancelled(self, context_id: str) -> PendingSkillContext | None: ...

    async def mark_pending_skill_context_superseded(self, conversation_id: str) -> int: ...

    async def save_message(self, message: Message) -> Message: ...

    async def get_message(self, message_id: str) -> Message | None: ...

    async def list_messages_for_conversation(self, conversation_id: str) -> list[Message]: ...

    async def save_task(self, task: Task) -> Task: ...

    async def get_task(self, task_id: str) -> Task | None: ...

    async def get_active_task_for_conversation(self, conversation_id: str) -> Task | None: ...

    async def list_tasks_for_conversation(self, conversation_id: str, statuses: Iterable[TaskStatus] | None = None) -> list[Task]: ...

    async def save_task_node(self, node: TaskNode) -> TaskNode: ...

    async def get_task_node(self, node_id: str) -> TaskNode | None: ...

    async def list_task_nodes_for_task(self, task_id: str) -> list[TaskNode]: ...

    async def save_task_edge(self, task_id: str, edge: TaskEdge) -> TaskEdge: ...

    async def list_task_edges(self, task_id: str) -> list[TaskEdge]: ...

    async def save_artifact(self, artifact: Artifact) -> Artifact: ...

    async def get_artifact(self, artifact_id: str) -> Artifact | None: ...

    async def list_artifacts_for_task(self, task_id: str) -> list[Artifact]: ...

    async def list_artifacts_for_conversation(self, conversation_id: str) -> list[Artifact]: ...

    async def save_task_input_attachment(self, attachment: TaskInputAttachment) -> TaskInputAttachment: ...

    async def list_task_input_attachments_for_task(self, task_id: str) -> list[TaskInputAttachment]: ...

    async def append_event(self, event: EventRecord) -> EventRecord: ...

    async def list_events_for_task(self, task_id: str) -> list[EventRecord]: ...

    async def list_events_for_task_filtered(
        self,
        task_id: str,
        *,
        event_types: Iterable[str] | None = None,
        node_id: str | None = None,
        visibility: EventVisibility | str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]: ...

    async def list_event_page_for_task(
        self,
        task_id: str,
        *,
        after_event_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]: ...

    async def save_mailbox_message(self, message: MailboxMessage) -> MailboxMessage: ...

    async def get_mailbox_message(self, message_id: str) -> MailboxMessage | None: ...

    async def save_mailbox_delivery(self, delivery: MailboxDelivery) -> MailboxDelivery: ...

    async def get_mailbox_delivery(self, delivery_id: str) -> MailboxDelivery | None: ...

    async def list_mailbox_messages_for_task(self, task_id: str) -> list[MailboxMessage]: ...

    async def list_mailbox_deliveries_for_message(self, message_id: str) -> list[MailboxDelivery]: ...

    async def save_interrupt(self, interrupt: Interrupt) -> Interrupt: ...

    async def get_interrupt(self, interrupt_id: str) -> Interrupt | None: ...

    async def get_interrupt_for_node(self, task_id: str, node_id: str) -> Interrupt | None: ...

    async def list_interrupts_for_task(self, task_id: str) -> list[Interrupt]: ...

    async def save_interrupt_answer(self, interrupt_answer: InterruptAnswer) -> InterruptAnswer: ...

    async def get_interrupt_answer(self, interrupt_answer_id: str) -> InterruptAnswer | None: ...

    async def list_interrupt_answers(self, interrupt_id: str) -> list[InterruptAnswer]: ...

    async def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint: ...

    async def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None: ...

    async def get_checkpoint_by_resume_token(self, resume_token: str) -> Checkpoint | None: ...

    async def list_checkpoints_for_task(self, task_id: str) -> list[Checkpoint]: ...


@runtime_checkable
class CapabilityContract(Protocol):
    capability_id: str
    version: str
    description: str

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult: ...


@runtime_checkable
class ExecutorPort(Protocol):
    def supports(self, capability_id: str) -> bool: ...

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult: ...


@runtime_checkable
class EventSink(Protocol):
    async def publish(self, event: EventRecord) -> None: ...


@runtime_checkable
class AuditSink(Protocol):
    async def record(
        self,
        event_type: str,
        payload: Payload,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> None: ...
