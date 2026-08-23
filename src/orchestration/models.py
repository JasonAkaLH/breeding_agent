from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.core.enums import UserMCPTransport


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class InstanceState(StrEnum):
    ONLINE = "online"
    BUSY = "busy"
    DRAINING = "draining"
    OFFLINE = "offline"


@dataclass(slots=True, frozen=True)
class CapabilityDescriptor:
    capability_id: str
    name: str
    description: str
    display_name: str = ""
    version: str = "1"
    enabled: bool = True
    public: bool = True
    kind: str = "capability"
    source: str = "builtin"
    source_path: str = ""


@dataclass(slots=True, frozen=True)
class UserMCPServerProfile:
    """Model-safe description of one available user-scoped MCP server."""

    server_id: str
    display_name: str
    routing_description: str
    transport: str

    def __post_init__(self) -> None:
        for field_name in ("server_id", "display_name", "transport"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        try:
            UserMCPTransport(self.transport)
        except ValueError as exc:
            raise ValueError(f"Unsupported MCP transport: {self.transport}") from exc
        object.__setattr__(self, "routing_description", str(self.routing_description or "").strip())


@dataclass(slots=True, frozen=True)
class ExecutionInstance:
    instance_id: str
    supported_capabilities: tuple[str, ...]
    state: InstanceState = InstanceState.ONLINE
    load_score: int = 0
    endpoint: str | None = None
