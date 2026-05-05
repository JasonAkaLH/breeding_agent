from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from .enums import TaskStatus
from .models import (
    Artifact,
    AuthSession,
    AuthUser,
    Checkpoint,
    Conversation,
    CaptchaChallenge,
    EventRecord,
    Interrupt,
    InterruptAnswer,
    MailboxDelivery,
    MailboxMessage,
    Message,
    Task,
    TaskEdge,
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
    async def save_auth_user(self, user: AuthUser) -> AuthUser: ...

    async def get_auth_user(self, username: str) -> AuthUser | None: ...

    async def save_captcha_challenge(self, challenge: CaptchaChallenge) -> CaptchaChallenge: ...

    async def get_captcha_challenge(self, captcha_id: str) -> CaptchaChallenge | None: ...

    async def save_auth_session(self, session: AuthSession) -> AuthSession: ...

    async def get_auth_session(self, session_id: str) -> AuthSession | None: ...

    async def save_conversation(self, conversation: Conversation) -> Conversation: ...

    async def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    async def list_conversations_for_account(self, account_id: str) -> list[Conversation]: ...

    async def delete_conversation(self, conversation_id: str) -> dict[str, int]: ...

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

    async def append_event(self, event: EventRecord) -> EventRecord: ...

    async def list_events_for_task(self, task_id: str) -> list[EventRecord]: ...

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
