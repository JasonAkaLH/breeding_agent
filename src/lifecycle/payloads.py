from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(slots=True, frozen=True)
class NodeAssignmentPayload:
    capability_id: str
    node_id: str
    input_refs: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    retry_policy: Mapping[str, Any] | None = None
    resource_budget: Mapping[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class CancelNoticePayload:
    task_id: str
    reason: str
    cancel_token: str | None = None


@dataclass(slots=True, frozen=True)
class ResumeNoticePayload:
    task_id: str
    node_id: str
    resume_token: str
    checkpoint_ref: str | None = None


@dataclass(slots=True, frozen=True)
class ClarificationRequestPayload:
    interrupt_id: str
    question: str
    required_fields: Mapping[str, Any]


@dataclass(slots=True, frozen=True)
class ClarificationAnswerPayload:
    interrupt_id: str
    answer_payload: Mapping[str, Any]


def payload_as_dict(payload: object) -> dict[str, Any]:
    if hasattr(payload, "__dataclass_fields__"):
        return asdict(payload)
    raise TypeError("payload_as_dict expects a dataclass payload object.")
