from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    LOCKED = "locked"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class TaskStatus(StrEnum):
    ACCEPTED = "accepted"
    PLANNING = "planning"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class RoutingMode(StrEnum):
    AUTO = "auto"
    HINT = "hint"
    FORCE_CAPABILITY = "force_capability"


class NodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_DEPENDENCY = "waiting_for_dependency"
    WAITING_FOR_INPUT = "waiting_for_input"
    READY_TO_RESUME = "ready_to_resume"
    RESUMING = "resuming"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_BY_CANCELLATION = "blocked_by_cancellation"
    ORPHANED = "orphaned"


class NodeCriticality(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    FALLBACK = "fallback"


class DependencyType(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class EdgeType(StrEnum):
    DATA = "data"
    CONTROL = "control"
    FALLBACK = "fallback"


class ArtifactType(StrEnum):
    TEXT = "text"
    JSON = "json"
    FILE = "file"
    DATASET = "dataset"
    SUMMARY = "summary"


class EventVisibility(StrEnum):
    FRONTEND = "frontend"
    INTERNAL = "internal"
    AUDIT_ONLY = "audit_only"


class MailboxChannel(StrEnum):
    ORCHESTRATOR_CONTROL = "orchestrator_control"
    PEER_COLLABORATION = "peer_collaboration"
    INTERRUPT_RESUME = "interrupt_resume"


class AckPolicy(StrEnum):
    STRONG = "strong"
    LIGHT = "light"


class MailboxDeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class InterruptStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
