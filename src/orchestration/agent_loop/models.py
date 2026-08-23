from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Literal, Mapping


AgentMessageRole = Literal["system", "developer", "user", "assistant", "tool"]
AgentToolChoiceMode = Literal["auto", "required", "none"]
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AgentProtocolErrorCode(StrEnum):
    MISSING_CALL_ID = "missing_call_id"
    DUPLICATE_CALL_ID = "duplicate_call_id"
    INVALID_TOOL_NAME = "invalid_tool_name"
    MALFORMED_ARGUMENTS = "malformed_arguments"
    INCOMPLETE_STREAM = "incomplete_stream"
    REQUIRED_TOOL_MISSING = "required_tool_missing"
    REQUIRED_TOOL_MULTIPLE = "required_tool_multiple"
    REQUIRED_TOOL_MISMATCH = "required_tool_mismatch"
    EMPTY_SAMPLE = "empty_sample"


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_DEPENDENCY = "waiting_for_dependency"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentItemKind(StrEnum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SKILL_ACTIVATION = "skill_activation"
    CONTEXT_SUMMARY = "context_summary"
    CONTINUATION = "continuation"


class AgentItemState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"


class AgentProtocolViolation(Exception):
    def __init__(self, code: AgentProtocolErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class AgentProtocolFailure(Exception):
    def __init__(self, code: AgentProtocolErrorCode, *, attempts: int) -> None:
        super().__init__(f"Agent model protocol failed after {attempts} attempt(s): {code.value}")
        self.code = code
        self.attempts = attempts


class AgentSamplingCancelled(Exception):
    """Raised when Agent sampling is cancelled before a closed sample exists."""


@dataclass(slots=True)
class AgentCancellationToken:
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True, slots=True)
class AgentProtocolRetryPolicy:
    max_retries: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError("agent_protocol_max_retries must be a non-negative integer")

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    @property
    def digest(self) -> str:
        return hashlib.sha256(f"agent_protocol_max_retries={self.max_retries}".encode()).hexdigest()

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> AgentProtocolRetryPolicy:
        value = (config or {}).get("agent_protocol_max_retries", 1)
        if isinstance(value, bool):
            raise ValueError("agent_protocol_max_retries must be a non-negative integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("agent_protocol_max_retries must be a non-negative integer") from exc
        if str(value).strip() != str(parsed) and not isinstance(value, int):
            raise ValueError("agent_protocol_max_retries must be a non-negative integer")
        return cls(max_retries=parsed)


@dataclass(frozen=True, slots=True)
class AgentModelBinding:
    model_edition: str
    reasoning_effort: str = "minimal"
    thinking_enabled: bool = False
    option_digests: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model_edition.strip():
            raise ValueError("model_edition must not be empty")
        safe_digests = {str(key): str(value) for key, value in self.option_digests.items()}
        object.__setattr__(self, "option_digests", MappingProxyType(safe_digests))

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "model_edition": self.model_edition,
            "reasoning_effort": self.reasoning_effort,
            "thinking_enabled": self.thinking_enabled,
            "option_digests": dict(self.option_digests),
        }


@dataclass(frozen=True, slots=True)
class AgentToolCall:
    call_id: str
    provider_safe_name: str
    arguments_json: str
    ordinal: int

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("call_id must not be empty")
        validate_provider_safe_tool_name(self.provider_safe_name)
        if self.ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        try:
            value = json.loads(self.arguments_json)
        except json.JSONDecodeError as exc:
            raise ValueError("arguments_json must contain valid JSON") from exc
        object.__setattr__(self, "arguments_json", canonical_json(value))


@dataclass(frozen=True, slots=True)
class AgentMessage:
    role: AgentMessageRole
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[AgentToolCall, ...] = ()

    def __post_init__(self) -> None:
        if self.role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError(f"Unsupported Agent message role: {self.role}")
        if self.role == "tool" and not (self.tool_call_id or "").strip():
            raise ValueError("tool messages require tool_call_id")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool_calls")


@dataclass(frozen=True, slots=True)
class AgentToolDescriptor:
    provider_safe_name: str
    capability_id: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        validate_provider_safe_tool_name(self.provider_safe_name)
        if not self.capability_id.strip():
            raise ValueError("capability_id must not be empty")
        canonical = json.loads(canonical_json(self.input_schema))
        object.__setattr__(self, "input_schema", MappingProxyType(canonical))

    @classmethod
    def for_capability(
        cls,
        capability_id: str,
        *,
        description: str,
        input_schema: Mapping[str, Any],
    ) -> AgentToolDescriptor:
        return cls(
            provider_safe_name=provider_safe_tool_name(capability_id),
            capability_id=capability_id,
            description=description,
            input_schema=input_schema,
        )


@dataclass(frozen=True, slots=True)
class AgentToolChoice:
    mode: AgentToolChoiceMode = "auto"
    required_name: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"auto", "required", "none"}:
            raise ValueError(f"Unsupported Agent tool choice: {self.mode}")
        if self.mode == "required":
            validate_provider_safe_tool_name(self.required_name or "")
        elif self.required_name is not None:
            raise ValueError("required_name is only valid for required tool choice")


@dataclass(frozen=True, slots=True)
class AgentModelRequest:
    request_id: str
    binding: AgentModelBinding
    messages: tuple[AgentMessage, ...]
    tools: tuple[AgentToolDescriptor, ...] = ()
    tool_choice: AgentToolChoice = field(default_factory=AgentToolChoice)
    cancellation: AgentCancellationToken | None = field(default=None, compare=False, repr=False)
    reasoning_delta_sink: Callable[[str], Awaitable[None]] | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        names = [tool.provider_safe_name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("provider_safe_name values must be unique")
        if self.tool_choice.mode == "required" and self.tool_choice.required_name not in names:
            raise ValueError("required tool must be present in request tools")


@dataclass(frozen=True, slots=True)
class AgentUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    status: Literal["available", "usage_unavailable"] = "usage_unavailable"


@dataclass(frozen=True, slots=True)
class AgentFinishMetadata:
    finish_reason: str
    attempts: int
    protocol_outcome: Literal["closed"] = "closed"
    mixed_text_and_tool_calls: bool = False


@dataclass(frozen=True, slots=True)
class AgentSample:
    sample_id: str
    binding: AgentModelBinding
    visible_text: str
    tool_calls: tuple[AgentToolCall, ...]
    usage: AgentUsage
    finish: AgentFinishMetadata

    @property
    def is_final_candidate(self) -> bool:
        return bool(self.visible_text.strip()) and not self.tool_calls


@dataclass(frozen=True, slots=True)
class AgentRun:
    run_id: str
    task_id: str
    conversation_id: str
    status: AgentRunStatus
    binding: AgentModelBinding
    next_item_sequence: int = 1
    compacted_through_sequence: int = 0
    active_sample_item_id: str | None = None
    waiting_call_item_ids: tuple[str, ...] = ()
    next_batch_call_ordinal: int = 0
    claim_owner: str | None = None
    claim_token: str | None = None
    lease_expires_at: datetime | None = None
    revision: int = 0
    terminal_reason_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    terminal_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.task_id or not self.conversation_id:
            raise ValueError("AgentRun identity fields must not be empty")
        if self.next_item_sequence < 1:
            raise ValueError("next_item_sequence must be positive")
        if self.compacted_through_sequence < 0:
            raise ValueError("compacted_through_sequence must be non-negative")
        if self.compacted_through_sequence >= self.next_item_sequence:
            raise ValueError("compacted_through_sequence must precede next_item_sequence")
        if self.next_batch_call_ordinal < 0 or self.revision < 0:
            raise ValueError("AgentRun ordinals and revision must be non-negative")
        if len(set(self.waiting_call_item_ids)) != len(self.waiting_call_item_ids):
            raise ValueError("waiting_call_item_ids must be unique")


@dataclass(frozen=True, slots=True)
class AgentItem:
    item_id: str
    run_id: str
    task_id: str
    sequence: int
    kind: AgentItemKind
    state: AgentItemState
    payload_json: str
    payload_sha256: str
    parent_item_id: str | None = None
    source_call_item_id: str | None = None
    provider_sample_id: str | None = None
    call_ordinal: int | None = None
    created_at: datetime | None = None
    committed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.item_id or not self.run_id or not self.task_id:
            raise ValueError("AgentItem identity fields must not be empty")
        if self.sequence < 1:
            raise ValueError("AgentItem sequence must be positive")
        if self.call_ordinal is not None and self.call_ordinal < 0:
            raise ValueError("call_ordinal must be non-negative")
        if self.kind is AgentItemKind.TOOL_RESULT and not self.source_call_item_id:
            raise ValueError("tool_result must reference a source call")


@dataclass(frozen=True, slots=True)
class AgentSampleCommit:
    run_id: str
    expected_revision: int
    expected_claim_token: str | None
    sample: AgentSample
    capability_ids_by_tool_name: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class AgentSampleCommitResult:
    run: AgentRun
    assistant_item: AgentItem
    call_items: tuple[AgentItem, ...]
    result_reservations: tuple[AgentItem, ...]
    node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentUserMessageCommit:
    run_id: str
    expected_revision: int
    expected_claim_token: str | None
    text: str

    def __post_init__(self) -> None:
        if not self.run_id or not self.text.strip():
            raise ValueError("Agent user message commit must not be empty")


@dataclass(frozen=True, slots=True)
class AgentUserMessageCommitResult:
    run: AgentRun
    item: AgentItem


@dataclass(frozen=True, slots=True)
class AgentCompactionCommit:
    run_id: str
    expected_revision: int
    expected_claim_token: str | None
    covered_start_sequence: int
    covered_end_sequence: int
    source_digest: str
    summary: str


@dataclass(frozen=True, slots=True)
class AgentCompactionResult:
    run: AgentRun
    summary_item: AgentItem


class AgentCallOutcomeStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_DEPENDENCY = "waiting_for_dependency"


@dataclass(frozen=True, slots=True)
class AgentStagedArtifact:
    artifact_id: str
    artifact_type: str
    storage_ref: str
    summary: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.artifact_type or not self.storage_ref:
            raise ValueError("staged artifact identity and storage_ref must not be empty")


@dataclass(frozen=True, slots=True)
class AgentCallOutcomeCommit:
    run_id: str
    expected_revision: int
    expected_claim_token: str | None
    call_item_id: str
    safe_result_payload: Any
    status: AgentCallOutcomeStatus
    staged_artifacts: tuple[AgentStagedArtifact, ...] = ()
    safe_error_code: str | None = None
    continuation_payload: Any | None = None
    skill_activation_item: AgentItem | None = None


@dataclass(frozen=True, slots=True)
class AgentFinalOutputCommit:
    run_id: str
    expected_revision: int
    expected_claim_token: str | None
    text: str


@dataclass(frozen=True, slots=True)
class AgentFinalOutputResult:
    run: AgentRun
    assistant_item: AgentItem
    node_id: str
    artifact_id: str
    message_id: str
    event_id: str
    receipt_id: str


class AgentStorageConflict(RuntimeError):
    """Closed fail-closed CAS or durable identity conflict."""


@dataclass(frozen=True, slots=True)
class AgentTaskLease:
    run_id: str
    task_id: str
    owner_id: str
    token: str
    revision: int
    expires_at: datetime


class AgentLeaseLost(RuntimeError):
    """The current worker can no longer prove Task lease ownership."""


def validate_provider_safe_tool_name(name: str) -> str:
    if not _TOOL_NAME_RE.fullmatch(name):
        raise ValueError("provider-safe tool name must match [A-Za-z0-9_-]{1,64}")
    return name


def provider_safe_tool_name(capability_id: str) -> str:
    source = capability_id.strip()
    if not source:
        raise ValueError("capability_id must not be empty")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", source).strip("_-") or "tool"
    digest = hashlib.sha256(source.encode()).hexdigest()[:12]
    return f"{slug[:51]}_{digest}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
