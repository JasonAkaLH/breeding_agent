"""State Platform contracts and PostgreSQL-backed queue foundation helpers."""

from .commands import build_command, command_partition_key, payload_fingerprint
from .contracts import (
    CommandStatus,
    StateCommand,
    StateCommandRecord,
    StateCommandResult,
    StateHealthSnapshot,
    StateReadStore,
    StateService,
    StateWriteQueue,
)
from .errors import StatePlatformError, classify_state_error

__all__ = [
    "CommandStatus",
    "StateCommand",
    "StateCommandRecord",
    "StateCommandResult",
    "StateHealthSnapshot",
    "StatePlatformError",
    "StateReadStore",
    "StateService",
    "StateWriteQueue",
    "build_command",
    "classify_state_error",
    "command_partition_key",
    "payload_fingerprint",
]
