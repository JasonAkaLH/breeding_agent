from __future__ import annotations

from enum import Enum

from .rust_contract import core_enum_members


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


def _rust_enum(enum_name: str) -> type[StrEnum]:
    return StrEnum(enum_name, core_enum_members(enum_name), module=__name__)


ConversationStatus = _rust_enum("ConversationStatus")
MessageRole = _rust_enum("MessageRole")
TaskStatus = _rust_enum("TaskStatus")
RoutingMode = _rust_enum("RoutingMode")
NodeStatus = _rust_enum("NodeStatus")
NodeCriticality = _rust_enum("NodeCriticality")
DependencyType = _rust_enum("DependencyType")
EdgeType = _rust_enum("EdgeType")
ArtifactType = _rust_enum("ArtifactType")
EventVisibility = _rust_enum("EventVisibility")
MailboxChannel = _rust_enum("MailboxChannel")
AckPolicy = _rust_enum("AckPolicy")
MailboxDeliveryStatus = _rust_enum("MailboxDeliveryStatus")
InterruptStatus = _rust_enum("InterruptStatus")
