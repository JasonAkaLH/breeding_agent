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
ArtifactType = _rust_enum("ArtifactType")
EventVisibility = _rust_enum("EventVisibility")
MailboxChannel = _rust_enum("MailboxChannel")
AckPolicy = _rust_enum("AckPolicy")
MailboxDeliveryStatus = _rust_enum("MailboxDeliveryStatus")
InterruptStatus = _rust_enum("InterruptStatus")


class UserMCPTransport(StrEnum):
    STREAMABLE_HTTP = "streamable_http"
    LEGACY_HTTP_SSE = "legacy_http_sse"


class UserMCPProtocolPreference(StrEnum):
    AUTO = "auto"
    V2024_11_05 = "2024-11-05"
    V2025_03_26 = "2025-03-26"
    V2025_06_18 = "2025-06-18"
    V2025_11_25 = "2025-11-25"
    V2026_07_28 = "2026-07-28"


class UserMCPAuthType(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    API_KEY_HEADER = "api_key_header"
    STATIC_HEADERS = "static_headers"


class UserMCPHealthStatus(StrEnum):
    UNTESTED = "untested"
    TESTING = "testing"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
